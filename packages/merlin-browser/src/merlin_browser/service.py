from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .core import (
    BrowserBoundaryError,
    BrowserSession,
    launch_isolated,
    launch_persistent,
    restart_isolated,
    restart_persistent,
)
from .models import BrowserBounds, BrowserMetadata, BrowserOwner


class BrowserMetadataStore(Protocol):
    async def save_metadata(self, metadata: BrowserMetadata) -> None: ...
    async def load_metadata(self, context_id: str) -> BrowserMetadata | None: ...
    async def list_metadata(self, session_id: str) -> Sequence[BrowserMetadata]: ...


class BrowserTargetResolver(Protocol):
    async def __call__(self, session_id: str) -> BrowserBounds: ...


class BrowserService:
    """Owns live browser contexts and durable restart/ownership metadata."""

    def __init__(
        self,
        store: BrowserMetadataStore,
        targets: BrowserTargetResolver,
        *,
        persistent_profile: Path | None = None,
        persistent_headless: bool = False,
        actor_wait_seconds: float = 30.0,
    ) -> None:
        self._store, self._targets = store, targets
        self._persistent_profile = persistent_profile
        self._persistent_headless = persistent_headless
        self._actor_wait_seconds = actor_wait_seconds
        self._live: dict[str, BrowserSession] = {}
        self._session_context: dict[str, str] = {}
        self._shared_target_context: dict[str, str] = {}
        self._shared_origin_context: dict[frozenset[str], str] = {}
        self._restarted_context_at: dict[str, datetime] = {}
        self._closed = False

    def handoff_context_is_current(self, context_id: str, sealed_at: datetime) -> bool:
        """Return whether a handoff still belongs to this live browser incarnation.

        Restarting an isolated Playwright context restores its tabs but intentionally
        does not persist cookies or other browser credentials.  A handoff sealed before
        that restart must therefore be re-established after authentication.  Persistent
        profiles retain their own browser state and remain reusable.
        """
        session = self._live.get(context_id)
        if session is None:
            return False
        if session.mode == "persistent":
            return True
        restarted_at = self._restarted_context_at.get(context_id)
        return restarted_at is None or sealed_at >= restarted_at

    def existing_for_tool(self, session_id: str) -> BrowserSession:
        """Return an already-owned live context without launch, resume, or transfer."""

        if self._closed:
            raise BrowserBoundaryError("browser service is shutting down")
        context_id = self._session_context.get(session_id)
        session = self._live.get(context_id) if context_id is not None else None
        if session is None:
            raise BrowserBoundaryError(
                "reviewed browser context is not live; passive reconnaissance must establish it"
            )
        if session.owner.worker_id == "operator":
            raise BrowserBoundaryError("browser context is under operator control")
        return session

    async def for_tool(self, session_id: str) -> BrowserSession:
        if self._closed:
            raise BrowserBoundaryError("browser service is shutting down")
        bounds = await self._targets(session_id)
        context_id = self._session_context.get(session_id)
        if context_id is not None and context_id in self._live:
            session = self._live[context_id]
            if session.owner.worker_id == "operator":
                raise BrowserBoundaryError("browser context is under operator control")
            if bounds.requires_persistent_profile and session.mode != "persistent":
                raise BrowserBoundaryError(
                    "target browser session policy requires a persistent context; "
                    "the current isolated context cannot preserve durable authentication"
                )
            return session
        restartable = [
            item
            for item in await self._store.list_metadata(session_id)
            if item.state == "restart_required"
            and item.target_id == bounds.target_id
            and item.allowed_origins == bounds.allowed_origins
        ]
        shared_id = self._shared_target_context.get(bounds.target_id)
        if shared_id is None and bounds.requires_persistent_profile:
            shared_id = self._shared_origin_context.get(bounds.allowed_origins)
        if shared_id is not None and shared_id in self._live:
            for restored in restartable:
                await self._store.save_metadata(restored.model_copy(update={"state": "cleaned"}))
            return await self._transfer_session_ownership(session_id, self._live[shared_id])
        if len(restartable) > 1:
            raise BrowserBoundaryError(
                "multiple durable browser contexts match this target; explicit recovery is required"
            )
        if restartable:
            restored = restartable[0]
            if restored.owner.worker_id == "operator":
                raise BrowserBoundaryError(
                    "browser context is under operator control; resume it explicitly"
                )
            await self.resume(session_id, restored.context_id)
            return self._live[restored.context_id]
        if bounds.requires_persistent_profile and self._persistent_profile is None:
            raise BrowserBoundaryError(
                "this target requires its authenticated shared browser profile; configure "
                "[browser] profile_directory in ~/.merlin/config.toml and restart Merlin"
            )
        owner = BrowserOwner(
            session_id=session_id,
            worker_id="agent:browser",
            technique_id="api-first-reconnaissance",
        )
        if not bounds.requires_persistent_profile:
            session = await self._launch_isolated(bounds, owner)
        else:
            assert self._persistent_profile is not None
            session = await self._launch_persistent(bounds, owner)
            self._shared_target_context[bounds.target_id] = session.context_id
            self._shared_origin_context[bounds.allowed_origins] = session.context_id
        self._live[session.context_id] = session
        self._session_context[session_id] = session.context_id
        await self._store.save_metadata(session.metadata(restart=True))
        return session

    async def _transfer_session_ownership(
        self, session_id: str, session: BrowserSession
    ) -> BrowserSession:
        """Transfer one shared browser context to the requesting conversation.

        A persistent browser process may outlive a conversation. Its target and
        authenticated profile are reusable, but its authority is not: every new
        conversation must seal subsequent actions and reconnaissance handoffs
        under its own session id.
        """
        if session.owner.worker_id == "operator":
            raise BrowserBoundaryError("browser context is under operator control")
        context_id = session.context_id
        for prior_session, owned_context in tuple(self._session_context.items()):
            if owned_context == context_id and prior_session != session_id:
                self._session_context.pop(prior_session, None)
        session.owner = session.owner.model_copy(
            update={
                "session_id": session_id,
                "worker_id": "agent:browser",
                "technique_id": "api-first-reconnaissance",
            }
        )
        self._session_context[session_id] = context_id
        await self._store.save_metadata(session.metadata(restart=True))
        return session

    async def for_reviewed_persistent(
        self, session_id: str, bounds: BrowserBounds
    ) -> BrowserSession:
        """Resolve one explicitly reviewed persistent origin without exposing its profile path."""
        if self._closed:
            raise BrowserBoundaryError("browser service is shutting down")
        if not bounds.requires_persistent_profile:
            raise BrowserBoundaryError("reviewed persistent browser bounds are required")
        if self._persistent_profile is None:
            raise BrowserBoundaryError("the reviewed persistent browser profile is unavailable")
        context_id = self._shared_origin_context.get(bounds.allowed_origins)
        if context_id is not None and context_id in self._live:
            session = self._live[context_id]
            if session.owner.worker_id == "operator":
                raise BrowserBoundaryError("browser context is under operator control")
            if session.bounds.max_tabs > bounds.max_tabs:
                raise BrowserBoundaryError(
                    "live persistent context exceeds the reviewed browser tab bound"
                )
            self._session_context[session_id] = context_id
            return session
        owner = BrowserOwner(
            session_id=session_id,
            worker_id="promotion:browser",
            technique_id="held-out-browser-certification",
        )
        session = await self._launch_persistent(bounds, owner)
        self._live[session.context_id] = session
        self._session_context[session_id] = session.context_id
        self._shared_target_context[bounds.target_id] = session.context_id
        self._shared_origin_context[bounds.allowed_origins] = session.context_id
        await self._store.save_metadata(session.metadata(restart=True))
        return session

    async def _launch_isolated(self, bounds: BrowserBounds, owner: BrowserOwner) -> BrowserSession:
        try:
            return await launch_isolated(bounds, owner, headless=self._persistent_headless)
        except Exception as error:
            if self._persistent_headless:
                raise
            session = await launch_isolated(bounds, owner, headless=True)
            session.launch_fallback_reason = (
                f"headful launch failed ({type(error).__name__}); retried headless"
            )
            return session

    async def _launch_persistent(
        self, bounds: BrowserBounds, owner: BrowserOwner
    ) -> BrowserSession:
        assert self._persistent_profile is not None
        options = {
            "actor_wait_seconds": self._actor_wait_seconds,
        }
        try:
            return await launch_persistent(
                self._persistent_profile,
                bounds,
                owner,
                headless=self._persistent_headless,
                **options,
            )
        except Exception as error:
            if self._persistent_headless:
                raise
            session = await launch_persistent(
                self._persistent_profile,
                bounds,
                owner,
                headless=True,
                **options,
            )
            session.launch_fallback_reason = (
                f"headful launch failed ({type(error).__name__}); retried headless"
            )
            return session

    async def release_actor(self, actor_id: str) -> None:
        """Release a worker's shared-browser turn after every terminal worker outcome."""
        for session in tuple(self._live.values()):
            await session.release_actor(actor_id)
            await self._store.save_metadata(session.metadata())

    async def list_contexts(self, session_id: str) -> Sequence[Mapping[str, object]]:
        rows = []
        list_actions = getattr(self._store, "list_actions", None)
        list_observations = getattr(self._store, "list_observations", None)
        for metadata in await self._store.list_metadata(session_id):
            actions = await list_actions(metadata.context_id) if callable(list_actions) else ()
            observations = (
                await list_observations(metadata.context_id) if callable(list_observations) else ()
            )
            latest_action = actions[-1] if actions else None
            latest_observation = next(
                (item for item in reversed(observations) if item.text or item.evidence_refs),
                None,
            )
            evidence = tuple(
                dict.fromkeys(
                    reference
                    for item in (*actions, *observations)
                    for reference in item.evidence_refs
                )
            )
            rows.append(
                self._context_row(
                    metadata,
                    latest_action=(latest_action.kind if latest_action is not None else ""),
                    latest_observation=(
                        latest_observation.text if latest_observation is not None else ""
                    ),
                    evidence=evidence,
                )
            )
        return rows

    async def list_tabs(self, session_id: str, context_id: str) -> Sequence[Mapping[str, object]]:
        metadata = await self._owned_metadata(session_id, context_id)
        return [tab.model_dump(mode="json") for tab in metadata.tabs]

    async def takeover(self, session_id: str, context_id: str) -> Mapping[str, object]:
        session = self._live.get(context_id)
        if session is None or session.owner.session_id != session_id:
            raise BrowserBoundaryError("only a live owned context can be taken over")
        session.owner = session.owner.model_copy(update={"worker_id": "operator"})
        await self._store.save_metadata(session.metadata(restart=True))
        return self._context_row(session.metadata(restart=True))

    async def cleanup(self, session_id: str, context_id: str) -> Mapping[str, object]:
        await self._owned_metadata(session_id, context_id)
        session = self._live.pop(context_id, None)
        if session is not None:
            await session.close()
            metadata = session.metadata()
            await self._store.save_metadata(metadata)
        else:
            metadata = await self._store.load_metadata(context_id)
            assert metadata is not None
            metadata = metadata.model_copy(update={"state": "cleaned"})
            await self._store.save_metadata(metadata)
        if self._session_context.get(session_id) == context_id:
            self._session_context.pop(session_id, None)
        if self._shared_target_context.get(metadata.target_id) == context_id:
            self._shared_target_context.pop(metadata.target_id, None)
        if self._shared_origin_context.get(metadata.allowed_origins) == context_id:
            self._shared_origin_context.pop(metadata.allowed_origins, None)
        return self._context_row(metadata)

    async def resume(self, session_id: str, context_id: str) -> Mapping[str, object]:
        metadata = await self._owned_metadata(session_id, context_id)
        if metadata.state != "restart_required":
            raise BrowserBoundaryError("browser context is not awaiting restart")
        existing = self._live.get(context_id)
        if existing is None:
            bounds = await self._targets(session_id)
            if metadata.mode == "isolated":
                session = await restart_isolated(metadata, bounds, metadata.owner)
                self._restarted_context_at[context_id] = datetime.now(UTC)
            elif metadata.mode == "persistent":
                if self._persistent_profile is None:
                    raise BrowserBoundaryError(
                        "persistent context recovery requires the configured authenticated profile"
                    )
                session = await restart_persistent(
                    metadata,
                    bounds,
                    metadata.owner,
                    self._persistent_profile,
                    headless=self._persistent_headless,
                    actor_wait_seconds=self._actor_wait_seconds,
                )
                self._shared_target_context[bounds.target_id] = context_id
                self._shared_origin_context[bounds.allowed_origins] = context_id
            else:
                raise BrowserBoundaryError(
                    "attached context requires explicit operator reattachment"
                )
            self._live[context_id] = session
        else:
            session = existing
        if session.owner.worker_id == "operator":
            for page in session.context.pages:
                if not page.is_closed():
                    session._require_url(page.url, allow_blank=True)
            session.owner = session.owner.model_copy(update={"worker_id": "agent:browser"})
        self._session_context[session_id] = context_id
        await self._store.save_metadata(session.metadata(restart=True))
        return self._context_row(session.metadata(restart=True))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors = []
        for context_id, session in tuple(self._live.items()):
            try:
                restart_metadata = session.metadata(restart=True)
                await session.close()
                await self._store.save_metadata(restart_metadata)
            except BaseException as error:
                errors.append(error)
            finally:
                self._live.pop(context_id, None)
        self._session_context.clear()
        self._shared_target_context.clear()
        self._shared_origin_context.clear()
        if errors:
            raise ExceptionGroup("browser cleanup failed", errors)

    async def _owned_metadata(self, session_id: str, context_id: str) -> BrowserMetadata:
        metadata = await self._store.load_metadata(context_id)
        if metadata is None or metadata.owner.session_id != session_id:
            raise BrowserBoundaryError("browser context is unavailable for this session")
        return metadata

    @staticmethod
    def _context_row(
        metadata: BrowserMetadata,
        *,
        latest_action: str = "",
        latest_observation: str = "",
        evidence: tuple[str, ...] = (),
    ) -> Mapping[str, object]:
        return {
            "context_id": metadata.context_id,
            "mode": metadata.mode,
            "target_id": metadata.target_id,
            "worker_id": metadata.owner.worker_id,
            "technique_id": metadata.owner.technique_id,
            "state": metadata.state,
            "cleanup_state": metadata.state,
            "launch_mode": metadata.launch_mode,
            "launch_fallback_reason": metadata.launch_fallback_reason,
            "allowed_origins": sorted(metadata.allowed_origins),
            "latest_action": latest_action,
            "latest_observation": latest_observation,
            "evidence": evidence,
            "updated_at": metadata.updated_at.isoformat(),
        }
