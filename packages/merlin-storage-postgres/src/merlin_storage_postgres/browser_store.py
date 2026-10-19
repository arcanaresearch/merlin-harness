from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from merlin_browser import (
    ApiReconHandoff,
    BrowserAction,
    BrowserMetadata,
    BrowserObservation,
)
from psycopg.types.json import Jsonb

_LOGGER = logging.getLogger(__name__)


class BrowserRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def save_metadata(self, metadata: BrowserMetadata) -> None:
        document = metadata.model_dump(mode="json")
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO merlin_storage.browser_contexts
                       (context_id, session_id, target_id, mode, worker_id, technique_id,
                        state, allowed_origins, metadata, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (context_id) DO UPDATE SET
                       state = EXCLUDED.state, allowed_origins = EXCLUDED.allowed_origins,
                       metadata = EXCLUDED.metadata, updated_at = EXCLUDED.updated_at""",
                (
                    metadata.context_id,
                    metadata.owner.session_id,
                    metadata.target_id,
                    metadata.mode,
                    metadata.owner.worker_id,
                    metadata.owner.technique_id,
                    metadata.state,
                    Jsonb(sorted(metadata.allowed_origins)),
                    Jsonb(document),
                    metadata.created_at,
                    metadata.updated_at,
                ),
            )
            self.connection.execute(
                "DELETE FROM merlin_storage.browser_tabs WHERE context_id = %s",
                (metadata.context_id,),
            )
            for tab in metadata.tabs:
                self.connection.execute(
                    """INSERT INTO merlin_storage.browser_tabs
                           (context_id, tab_ref, url, opener_ref, state)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (
                        metadata.context_id,
                        tab.tab_ref,
                        tab.url,
                        tab.opener_ref,
                        tab.state,
                    ),
                )

    def load_metadata(self, context_id: str) -> BrowserMetadata | None:
        row = self.connection.execute(
            "SELECT metadata FROM merlin_storage.browser_contexts WHERE context_id = %s",
            (context_id,),
        ).fetchone()
        return BrowserMetadata.model_validate(row["metadata"]) if row else None

    def list_metadata(self, session_id: str) -> Sequence[BrowserMetadata]:
        rows = self.connection.execute(
            """SELECT metadata FROM merlin_storage.browser_contexts
               WHERE session_id = %s ORDER BY updated_at DESC, context_id""",
            (session_id,),
        ).fetchall()
        return tuple(BrowserMetadata.model_validate(row["metadata"]) for row in rows)

    def append_action(self, action: BrowserAction) -> str:
        if (
            action.kind == "fill"
            and action.redaction_status == "redacted"
            and action.value != "[redacted]"
        ):
            raise ValueError("browser fill values must be redacted before persistence")
        action_id = str(uuid4())
        self.connection.execute(
            """INSERT INTO merlin_storage.browser_actions
                   (action_id, context_id, tab_ref, step, kind, document, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                action_id,
                action.context_id,
                action.tab_ref,
                action.step,
                action.kind,
                Jsonb(action.model_dump(mode="json")),
                action.created_at,
            ),
        )
        self.connection.commit()
        return action_id

    def append_observation(self, observation: BrowserObservation) -> str:
        observation_id = str(uuid4())
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO merlin_storage.browser_observations
                       (observation_id, context_id, tab_ref, frame_ref, step, kind,
                        document, observed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    observation_id,
                    observation.context_id,
                    observation.tab_ref,
                    observation.frame_ref,
                    observation.step,
                    observation.kind,
                    Jsonb(observation.model_dump(mode="json")),
                    observation.observed_at,
                ),
            )
            for ordinal, reference in enumerate(observation.evidence_refs):
                self.connection.execute(
                    """INSERT INTO merlin_storage.browser_observation_evidence
                           (observation_id, evidence_ref, ordinal) VALUES (%s, %s, %s)""",
                    (observation_id, reference, ordinal),
                )
        return observation_id

    def record_evidence(
        self, context_id: str, evidence_ref: str, kind: str, payload: Mapping[str, Any]
    ) -> None:
        if _contains_secret_key(payload):
            raise ValueError("browser evidence contains credential-like fields")
        self.connection.execute(
            """INSERT INTO merlin_storage.browser_evidence
                   (evidence_ref, context_id, kind, payload)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (evidence_ref) DO NOTHING""",
            (evidence_ref, context_id, kind, Jsonb(dict(payload))),
        )
        self.connection.commit()

    def record_handoff(self, handoff: ApiReconHandoff) -> None:
        existing = self.load_handoff(handoff.id)
        if existing is not None:
            if existing.model_dump(exclude={"created_at"}) == handoff.model_dump(
                exclude={"created_at"}
            ):
                return
            raise ValueError("API handoff identity collides with different durable evidence")
        context = self.connection.execute(
            """SELECT target_id, allowed_origins FROM merlin_storage.browser_contexts
               WHERE context_id = %s""",
            (handoff.context_id,),
        ).fetchone()
        parsed = urlsplit(handoff.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if (
            context is None
            or context["target_id"] != handoff.target_id
            or origin not in context["allowed_origins"]
        ):
            raise ValueError("API handoff is outside its persisted target bounds")
        recon_evidence_refs = {
            str(reference)
            for observation in handoff.recon_observations
            for reference in observation.get("evidence_refs", ())
        }
        required_evidence_refs = {
            handoff.request_evidence_ref,
            handoff.response_evidence_ref,
            *recon_evidence_refs,
        }
        rows = self.connection.execute(
            """SELECT evidence_ref FROM merlin_storage.browser_evidence
               WHERE context_id = %s AND evidence_ref = ANY(%s)""",
            (
                handoff.context_id,
                list(required_evidence_refs),
            ),
        ).fetchall()
        if {row["evidence_ref"] for row in rows} != required_evidence_refs:
            raise ValueError("API handoff evidence is missing or belongs to another context")
        self.connection.execute(
            """INSERT INTO merlin_storage.browser_recon_handoffs
                   (handoff_id, context_id, target_id, method, url,
                    request_evidence_ref, response_evidence_ref, document, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                handoff.id,
                handoff.context_id,
                handoff.target_id,
                handoff.method,
                handoff.url,
                handoff.request_evidence_ref,
                handoff.response_evidence_ref,
                Jsonb(handoff.model_dump(mode="json")),
                handoff.created_at,
            ),
        )
        self.connection.commit()

    def list_observations(self, context_id: str) -> Sequence[BrowserObservation]:
        rows = self.connection.execute(
            """SELECT document FROM merlin_storage.browser_observations
               WHERE context_id = %s ORDER BY step, observed_at, observation_id""",
            (context_id,),
        ).fetchall()
        return tuple(BrowserObservation.model_validate(row["document"]) for row in rows)

    def list_actions(self, context_id: str) -> Sequence[BrowserAction]:
        rows = self.connection.execute(
            """SELECT document FROM merlin_storage.browser_actions
               WHERE context_id = %s ORDER BY step, created_at, action_id""",
            (context_id,),
        ).fetchall()
        return tuple(BrowserAction.model_validate(row["document"]) for row in rows)

    def load_handoff(self, handoff_id: str) -> ApiReconHandoff | None:
        row = self.connection.execute(
            """SELECT document FROM merlin_storage.browser_recon_handoffs
               WHERE handoff_id = %s""",
            (handoff_id,),
        ).fetchone()
        return ApiReconHandoff.model_validate(row["document"]) if row else None

    def list_handoffs(self, target_id: str, limit: int = 20) -> tuple[ApiReconHandoff, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("handoff limit must be between 1 and 100")
        rows = self.connection.execute(
            """SELECT handoff.document
                 FROM merlin_storage.browser_recon_handoffs AS handoff
                 JOIN merlin_storage.browser_contexts AS context
                   ON context.context_id = handoff.context_id
                  AND context.target_id = handoff.target_id
                 JOIN merlin_storage.browser_evidence AS request_evidence
                   ON request_evidence.context_id = handoff.context_id
                  AND request_evidence.evidence_ref = handoff.request_evidence_ref
                 JOIN merlin_storage.browser_evidence AS response_evidence
                   ON response_evidence.context_id = handoff.context_id
                  AND response_evidence.evidence_ref = handoff.response_evidence_ref
                WHERE handoff.target_id = %s
                ORDER BY handoff.created_at DESC, handoff.handoff_id
                LIMIT %s""",
            (target_id, limit),
        ).fetchall()
        handoffs: list[ApiReconHandoff] = []
        for row in rows:
            try:
                handoffs.append(ApiReconHandoff.model_validate(row["document"]))
            except ValueError as error:
                # Historical handoffs can outlive stricter schema revisions. A
                # malformed cache entry must not prevent fresh reconnaissance or
                # hide other valid handoffs for the same target.
                document = row.get("document") if isinstance(row, Mapping) else None
                handoff_id = document.get("id") if isinstance(document, Mapping) else None
                _LOGGER.warning(
                    "ignoring invalid reconnaissance handoff %s for target %s: %s",
                    handoff_id or "<unknown>",
                    target_id,
                    error,
                )
        return tuple(handoffs)

    def verify_handoff_evidence(
        self, handoff_id: str, target_id: str, evidence_refs: Sequence[str]
    ) -> bool:
        requested = frozenset(reference.strip() for reference in evidence_refs)
        if not requested or len(requested) != len(evidence_refs) or "" in requested:
            return False
        row = self.connection.execute(
            """SELECT handoff.target_id, handoff.context_id,
                      handoff.request_evidence_ref, handoff.response_evidence_ref
               FROM merlin_storage.browser_recon_handoffs AS handoff
               JOIN merlin_storage.browser_contexts AS context
                 ON context.context_id = handoff.context_id
                AND context.target_id = handoff.target_id
               WHERE handoff.handoff_id = %s AND handoff.target_id = %s""",
            (handoff_id, target_id),
        ).fetchone()
        if row is None:
            return False
        handoff_refs = frozenset((row["request_evidence_ref"], row["response_evidence_ref"]))
        # The handoff's request/response references are the mandatory anchors.
        # A reviewed adapter may also cite snapshots or additional network
        # observations captured in the same sealed browser context.
        if not handoff_refs <= requested:
            return False
        evidence = self.connection.execute(
            """SELECT evidence_ref FROM merlin_storage.browser_evidence
               WHERE context_id = %s AND evidence_ref = ANY(%s)""",
            (row["context_id"], list(requested)),
        ).fetchall()
        return {item["evidence_ref"] for item in evidence} == requested


class PostgresBrowserStore:
    def __init__(self, connection: Any) -> None:
        self._repository = BrowserRepository(connection)
        self._lock = asyncio.Lock()

    async def _call(self, function: Any, /, *args: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(function, *args)

    async def save_metadata(self, metadata: BrowserMetadata) -> None:
        await self._call(self._repository.save_metadata, metadata)

    async def load_metadata(self, context_id: str) -> BrowserMetadata | None:
        return await self._call(self._repository.load_metadata, context_id)

    async def list_metadata(self, session_id: str) -> Sequence[BrowserMetadata]:
        return await self._call(self._repository.list_metadata, session_id)

    async def append_action(self, action: BrowserAction) -> None:
        await self._call(self._repository.append_action, action)

    async def append_observation(self, observation: BrowserObservation) -> None:
        await self._call(self._repository.append_observation, observation)

    async def record_evidence(
        self, context_id: str, evidence_ref: str, kind: str, payload: Mapping[str, Any]
    ) -> None:
        await self._call(self._repository.record_evidence, context_id, evidence_ref, kind, payload)

    async def record_handoff(self, handoff: ApiReconHandoff) -> None:
        await self._call(self._repository.record_handoff, handoff)

    async def list_observations(self, context_id: str) -> Sequence[BrowserObservation]:
        return await self._call(self._repository.list_observations, context_id)

    async def list_actions(self, context_id: str) -> Sequence[BrowserAction]:
        return await self._call(self._repository.list_actions, context_id)

    async def load_handoff(self, handoff_id: str) -> ApiReconHandoff | None:
        return await self._call(self._repository.load_handoff, handoff_id)

    async def list_handoffs(self, target_id: str, limit: int = 20) -> tuple[ApiReconHandoff, ...]:
        return await self._call(self._repository.list_handoffs, target_id, limit)

    async def verify_handoff_evidence(
        self, handoff_id: str, target_id: str, evidence_refs: Sequence[str]
    ) -> bool:
        return await self._call(
            self._repository.verify_handoff_evidence,
            handoff_id,
            target_id,
            evidence_refs,
        )


def _contains_secret_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(
                token in normalized
                for token in ("authorization", "cookie", "password", "secret", "token", "api_key")
            ):
                return True
            if _contains_secret_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_secret_key(item) for item in value)
    return False
