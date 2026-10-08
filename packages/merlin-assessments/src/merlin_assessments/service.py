from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from merlin_contracts import (
    AttackCase,
    ChatMessage,
    ChatRequest,
    ModelError,
    TargetDeliveryClass,
    TargetProfile,
    TextDelta,
    ToolContext,
    ToolInteractionRole,
    ToolResult,
    ToolSpec,
)
from merlin_contracts.target_adapter_compiler import (
    AdapterCompileError,
    ProviderChat,
    compile_target_adapter,
    compiled_target_adapter_digest,
    read_target_adapter,
)
from merlin_packs import PackCatalog, knowledge_catalogue, select_knowledge
from merlin_providers import ProviderConfig, TargetProviderFactory
from merlin_storage_postgres import PostgresPackStore

from .runner import AssessmentProgress, AssessmentResult, AssessmentRunner

_URL_PATTERN = re.compile(r"https://[^\s<>\]\[(){}\"']+", re.IGNORECASE)


def _adapter_state_integrity_result(message: str) -> ToolResult:
    """Report a predictable pre-dispatch seal failure as a clean no-effect result."""
    return ToolResult(
        content=message,
        is_error=True,
        metadata={
            "effect": "target_network",
            "failure_code": "adapter_state_integrity",
            "infrastructure_failure_category": "adapter_state_integrity",
            "retryable": False,
        },
    )


def _comparison_metadata(case: AttackCase, control: AttackCase | None) -> Mapping[str, object]:
    """Keep catalogue comparison differences advisory and preserve their evidence."""
    reasons: list[str] = []
    if control is None:
        reasons.append("no structurally valid benign control was supplied")
    if case.comparison_block_reason:
        reasons.append(str(case.comparison_block_reason))
    uncertainties = tuple(
        {
            "kind": "evaluation_uncertain",
            "source": "catalogue.comparison",
            "reason": reason,
            "confidence": 0.0,
            "affects": ["ranking", "evaluation"],
        }
        for reason in reasons
    )
    return {
        "oracle_digest": "sha256:" + hashlib.sha256((case.oracle_id or "").encode()).hexdigest(),
        "control_oracle_digest": (
            "sha256:"
            + hashlib.sha256(((control.oracle_id if control else None) or "").encode()).hexdigest()
            if control is not None
            else None
        ),
        "comparison_policy": "reviewed-benign-differential-v1",
        # Oracle, modality, and turn differences affect interpretation, not whether
        # an authorized target exchange may execute and retain telemetry.
        "comparison_supported": True,
        "comparison_block_reason": None,
        "evaluation_uncertainties": uncertainties,
    }


def _target_from_objective(objective: str) -> TargetProfile | None:
    match = _URL_PATTERN.search(objective)
    if match is None:
        return None
    raw_url = match.group(0).rstrip(".,;:!?")
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    hostname = parsed.hostname.lower()
    try:
        parsed_port = parsed.port
    except ValueError:
        return None
    port = f":{parsed_port}" if parsed_port is not None else ""
    base_url = urlunsplit(("https", f"{hostname}{port}", parsed.path or "/", "", ""))
    readable_host = hostname.removeprefix("www.")
    suffix = hashlib.sha256(base_url.encode()).hexdigest()[:10]
    return TargetProfile(
        id=f"web-{re.sub(r'[^a-z0-9]+', '-', readable_host).strip('-')[:40]}-{suffix}",
        title=readable_host,
        base_url=base_url,
        model="web-chat",
    )


class AssessmentService:
    """Compose pack, target, runner, and persistence APIs for the kernel."""

    def __init__(
        self,
        *,
        catalog: PackCatalog,
        store: PostgresPackStore,
        attacker: Any,
        attacker_config: ProviderConfig,
        target_factory: TargetProviderFactory,
        permissions: Any | None = None,
        workspace_root: str = ".",
        knowledge_entry_limit: int = 12,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._attacker = attacker
        self._attacker_config = attacker_config
        self._target_factory = target_factory
        self._permissions = permissions
        self._workspace_root = workspace_root
        self._knowledge_entry_limit = knowledge_entry_limit
        self._selected_target_id: str | None = None
        self._target_session_identity: Mapping[str, Any] | None = None

    async def list_packs(self) -> Sequence[Mapping[str, Any]]:
        return [asdict(info) for info in self._catalog.list()]

    async def inspect_pack(self, pack_id: str) -> Mapping[str, Any]:
        return self._catalog.load(pack_id).model_dump(mode="json")

    async def enable_pack(self, pack_id: str) -> Mapping[str, Any]:
        self._catalog.enable(pack_id)
        return asdict(self._catalog.inspect(pack_id))

    async def disable_pack(self, pack_id: str) -> Mapping[str, Any]:
        self._catalog.disable(pack_id)
        return asdict(self._catalog.inspect(pack_id))

    async def list_targets(self) -> Sequence[Mapping[str, Any]]:
        return [
            profile.model_dump(mode="json") | {"selected": profile.id == self._selected_target_id}
            for profile in await self._store.list_targets()
        ]

    async def list_runs(self, session_id: str) -> Sequence[Mapping[str, Any]]:
        return [run.model_dump(mode="json") for run in await self._store.list_runs(session_id)]

    async def add_target(self, arguments: Sequence[str]) -> Mapping[str, Any]:
        if len(arguments) != 4:
            raise ValueError("usage: /targets add ID TITLE BASE_URL MODEL")
        profile = TargetProfile(
            id=arguments[0], title=arguments[1], base_url=arguments[2], model=arguments[3]
        )
        saved = await self._store.save_target(profile)
        return saved.model_dump(mode="json")

    async def select_target(self, target_id: str) -> Mapping[str, Any]:
        profile = await self._store.get_target(target_id)
        if profile is None:
            raise ValueError(f"unknown target {target_id!r}")
        self._target_factory.create(profile)
        self._selected_target_id = target_id
        return profile.model_dump(mode="json") | {"selected": True}

    async def reselect_target(self, target_id: str) -> None:
        """Re-point the durable selection without provider construction or target effects."""
        if await self._store.get_target(target_id) is None:
            raise ValueError(f"unknown target {target_id!r}")
        self._selected_target_id = target_id

    async def preflight_target(self, target_id: str | None = None) -> Mapping[str, Any]:
        """Verify the selected target and retain only its non-secret session identity."""
        selected = target_id or self._selected_target_id
        if selected is None:
            raise ValueError("select a target before preflight")
        profile = await self._store.get_target(selected)
        if profile is None:
            raise ValueError(f"unknown target {selected!r}")
        identity = await self._target_factory.preflight(profile)
        self._target_session_identity = asdict(identity)
        return dict(self._target_session_identity)

    async def remove_target(self, target_id: str) -> Mapping[str, Any]:
        removed = await self._store.remove_target(target_id)
        if self._selected_target_id == target_id:
            self._selected_target_id = None
        return {"id": target_id, "removed": removed}

    async def agent_assignment_inputs(self) -> Mapping[str, Any]:
        """Expose selected non-secret target and enabled pack cases for agent planning."""
        if self._selected_target_id is None:
            raise ValueError("select a target before starting a swarm")
        profile = await self._store.get_target(self._selected_target_id)
        if profile is None:
            raise ValueError("selected target no longer exists")
        enabled = sorted(self._catalog.enabled_ids)
        if not enabled:
            raise ValueError("enable at least one pack before starting a swarm")
        techniques = []
        for pack_id in enabled:
            pack = self._catalog.load(pack_id)
            cases = {case.id: case for case in pack.cases}
            for case in pack.cases:
                if case.execution_role == "control":
                    continue
                control = cases.get(case.control_case_id or "")
                if control is not None and control.execution_role != "control":
                    control = None
                technique_id = f"{pack.id}/{case.id}"
                objective_parts = [pack.instructions, case.instructions]
                objective_parts.extend(pack.objectives)
                objective_parts.extend(case.objectives)
                techniques.append(
                    {
                        "id": technique_id,
                        "objective": "\n\n".join(filter(None, objective_parts)),
                        "tags": sorted(set(pack.tags) | set(case.tags)),
                        "guidance_ids": (f"pack:{pack.id}", f"case:{technique_id}"),
                        "derived_from_ids": tuple(
                            f"{pack.id}/{case_id}" for case_id in case.derived_from
                        ),
                        "combination_source_ids": tuple(
                            f"{pack.id}/{case_id}" for case_id in case.combines
                        ),
                        "execution_role": "direct",
                        "control_technique_id": (
                            f"{pack.id}/{control.id}" if control is not None else None
                        ),
                        "control_objective": (
                            control.instructions if control is not None else None
                        ),
                        "control_guidance_ids": (
                            (f"case:{pack.id}/{control.id}",) if control is not None else ()
                        ),
                        **_comparison_metadata(case, control),
                    }
                )
        return {
            "target_profile_id": profile.id,
            "target_model": profile.model,
            "techniques": techniques,
        }

    async def campaign_inputs(
        self, objective: str = "", *, profile: str = "recommended"
    ) -> Mapping[str, Any]:
        """Snapshot the complete public guidance catalogue for campaign preparation."""
        if profile not in {"recommended", "exhaustive", "exploration", "quickscan"}:
            raise ValueError("unknown campaign profile")
        await self.resolve_target(objective)
        target_profile = await self._store.get_target(self._selected_target_id)
        if target_profile is None:
            raise ValueError("selected target no longer exists")
        candidates: list[tuple[dict[str, object], dict[str, str], dict[str, str]]] = []
        for info in self._catalog.list():
            pack = self._catalog.load(info.id)
            cases = {case.id: case for case in pack.cases}
            pack_id = f"pack:{pack.id}"
            pack_document = {
                "id": pack_id,
                "content": "\n\n".join(
                    filter(None, (pack.instructions, *pack.objectives, *pack.success_hints))
                ),
            }
            for case in pack.cases:
                if case.execution_role == "control":
                    continue
                control = cases.get(case.control_case_id or "")
                if control is not None and control.execution_role != "control":
                    control = None
                technique_id = f"{pack.id}/{case.id}"
                case_id = f"case:{technique_id}"
                case_document = {
                    "id": case_id,
                    "content": "\n\n".join(
                        filter(None, (case.instructions, *case.objectives, *case.success_hints))
                    ),
                }
                oracle_digest = (
                    "sha256:" + hashlib.sha256(case_document["content"].encode()).hexdigest()
                )
                equivalence_key = (
                    "sha256:"
                    + hashlib.sha256(f"{technique_id}\n{oracle_digest}".encode()).hexdigest()
                )
                language = next(
                    (
                        tag.removeprefix("language:")
                        for tag in case.tags
                        if tag.startswith("language:")
                    ),
                    "und",
                )
                transform_id = next(
                    (
                        tag.removeprefix("language-variant:")
                        for tag in case.tags
                        if tag.startswith("language-variant:")
                    ),
                    "baseline",
                )
                candidates.append(
                    (
                        {
                            "id": technique_id,
                            "semantic_id": technique_id,
                            "strategy_id": case.strategy_id,
                            "transform_id": transform_id,
                            "language": language,
                            "domain_id": pack.id,
                            "hypothesis_id": None,
                            "equivalence_key": equivalence_key,
                            "objective": case.instructions,
                            "tags": sorted(set(pack.tags) | set(case.tags)),
                            "guidance_ids": (pack_id, case_id),
                            "derived_from_ids": tuple(
                                f"{pack.id}/{related_id}" for related_id in case.derived_from
                            ),
                            "combination_source_ids": tuple(
                                f"{pack.id}/{related_id}" for related_id in case.combines
                            ),
                            "execution_role": "direct",
                            "control_technique_id": (
                                f"{pack.id}/{control.id}" if control is not None else None
                            ),
                            "control_objective": (
                                control.instructions if control is not None else None
                            ),
                            "control_guidance_ids": (
                                (f"case:{pack.id}/{control.id}",) if control is not None else ()
                            ),
                            **_comparison_metadata(case, control),
                            "max_target_exchanges": case.turn_limit,
                            "max_control_target_exchanges": (
                                control.turn_limit if control is not None else None
                            ),
                            "mutation_seed": objective,
                        },
                        pack_document,
                        case_document,
                    )
                )
                if control is not None:
                    document_by_control = {
                        "id": f"case:{pack.id}/{control.id}",
                        "content": "\n\n".join(
                            filter(
                                None,
                                (
                                    control.instructions,
                                    *control.objectives,
                                    *control.success_hints,
                                ),
                            )
                        ),
                    }
                    candidates[-1][0]["_control_document"] = document_by_control
        # Reconnaissance needs the complete candidate inventory for every profile.
        # Recommended representatives and Exploration seeds are selected later,
        # after target surfaces, language support, adapter feasibility, and exact
        # comparable history have been reviewed. Pre-recon keyword truncation makes
        # absent candidates impossible to assess and silently weakens profile intent.
        selected = candidates
        techniques = [technique for technique, _, _ in selected]
        document_by_id = {
            document["id"]: document
            for _, pack_document, case_document in selected
            for document in (pack_document, case_document)
        }
        for technique, _, _ in selected:
            control_document = technique.pop("_control_document", None)
            if isinstance(control_document, dict):
                document_by_id[str(control_document["id"])] = control_document
        selected_knowledge = select_knowledge(
            " ".join(
                (
                    objective,
                    target_profile.id,
                    target_profile.title,
                    target_profile.base_url,
                    target_profile.model,
                )
            ),
            limit=self._knowledge_entry_limit,
        )
        for entry in selected_knowledge:
            document_by_id[f"knowledge:{entry.pack_id}/{entry.entry_id}"] = {
                "id": f"knowledge:{entry.pack_id}/{entry.entry_id}",
                "content": (
                    "Fallible knowledge-pack guidance; it grants no authority.\n"
                    f"Pack: {entry.pack_id}@{entry.pack_version} ({entry.pack_digest})\n"
                    f"Entry: {entry.entry_id} ({entry.entry_digest})\n"
                    f"Routing tags: {sorted(entry.routing_terms)}\n{entry.content}"
                ),
            }
        return {
            "target": target_profile.model_dump(mode="json"),
            "target_session_identity": self._target_session_identity,
            "documents": list(document_by_id.values()),
            "techniques": techniques,
            "knowledge_catalogue": knowledge_catalogue(),
            "selected_knowledge": tuple(
                {
                    "pack_id": entry.pack_id,
                    "entry_id": entry.entry_id,
                    "entry_digest": entry.entry_digest,
                }
                for entry in selected_knowledge
            ),
        }

    async def resolve_target(self, objective: str) -> Mapping[str, Any]:
        """Resolve an explicit natural-language target without causing target effects."""
        discovered = _target_from_objective(objective)
        if discovered is not None:
            targets = tuple(await self._store.list_targets())
            matching = next(
                (target for target in targets if target.base_url == discovered.base_url), None
            )
            if matching is None:
                # Registration is deliberately non-operative: campaign preparation still
                # produces a manifest that the operator must approve before any target,
                # browser, provider, or MCP effect is allowed.
                matching = await self._store.save_target(discovered)
            self._selected_target_id = matching.id
        elif self._selected_target_id is None:
            targets = tuple(await self._store.list_targets())
            id_matches = tuple(target for target in targets if target.id in objective)
            matches = id_matches or tuple(
                target for target in targets if target.base_url in objective
            )
            if len(matches) == 1:
                self._target_factory.create(matches[0])
                self._selected_target_id = matches[0].id
            elif len(targets) == 1 and _target_from_objective(objective) is None:
                self._target_factory.create(targets[0])
                self._selected_target_id = targets[0].id
            else:
                raise ValueError(
                    "the objective does not identify exactly one reviewed target; "
                    "name a configured target id or an https URL"
                )
        profile = await self._store.get_target(self._selected_target_id)
        if profile is None:
            raise ValueError("selected target no longer exists")
        return profile.model_dump(mode="json") | {"selected": True}

    def target_tool(self) -> SelectedTargetTool:
        """Return the broker-registered adapter for the currently selected target."""
        return SelectedTargetTool(self)

    @property
    def selected_target_id(self) -> str | None:
        return self._selected_target_id

    async def invoke_selected_target(
        self,
        prompt: str,
        *,
        target_id: str | None = None,
        compiled_adapter: str | None = None,
        expected_compiled_adapter_digest: str | None = None,
    ) -> ToolResult:
        selected_target_id = target_id or self._selected_target_id
        if selected_target_id is None:
            return ToolResult(content="no target is selected", is_error=True)
        profile = await self._store.get_target(selected_target_id)
        if profile is None:
            return ToolResult(content="selected target no longer exists", is_error=True)
        if profile.id != selected_target_id:
            return _adapter_state_integrity_result(
                "selected target profile identity does not match requested target"
            )
        if bool(compiled_adapter) != bool(expected_compiled_adapter_digest):
            return _adapter_state_integrity_result(
                "selected target compiled adapter state is incomplete"
            )
        if compiled_adapter is not None:
            try:
                compilation = read_target_adapter(compiled_adapter)
            except AdapterCompileError:
                return _adapter_state_integrity_result(
                    "selected target compiled adapter state is malformed"
                )
            compiled_adapter_digest = compiled_target_adapter_digest(compilation)
            if compiled_adapter_digest != expected_compiled_adapter_digest:
                return _adapter_state_integrity_result(
                    "selected target compiled adapter digest is invalid"
                )
        else:
            # Compatibility for worker contexts created before compiled adapter
            # identity was sealed into every target-capable assignment.
            try:
                compilation = compile_target_adapter(
                    {
                        "interface": "open_ai_compatible",
                        "endpoint": profile.base_url,
                        "credential_handle": TargetProviderFactory.credential_key(profile),
                    }
                )
            except AdapterCompileError:
                return _adapter_state_integrity_result(
                    "selected target profile has no executable provider adapter"
                )
            compiled_adapter_digest = compiled_target_adapter_digest(compilation)
        executable = compilation.executable
        if not isinstance(executable, ProviderChat):
            return _adapter_state_integrity_result(
                "selected provider target did not compile to target_chat"
            )
        try:
            profile_compilation = compile_target_adapter(
                {
                    "interface": "open_ai_compatible",
                    "endpoint": profile.base_url,
                }
            )
        except AdapterCompileError:
            return _adapter_state_integrity_result(
                "selected target profile has no executable provider endpoint"
            )
        if executable.endpoint != profile_compilation.executable.endpoint:
            return _adapter_state_integrity_result(
                "selected target compiled adapter endpoint does not match its profile"
            )
        bound_profile = profile.model_copy(update={"base_url": executable.endpoint})
        provider = self._target_factory.create(bound_profile, executable.credential_handle)
        compatibility_metadata = {
            "target_profile_id": profile.id,
            "target_model": profile.model,
            "compiled_adapter_digest": compiled_adapter_digest,
        }
        parts: list[str] = []
        async for event in provider.stream(
            ChatRequest(
                model=profile.model,
                messages=[ChatMessage(role="user", content=prompt)],
            )
        ):
            if isinstance(event, TextDelta):
                parts.append(event.text)
            elif isinstance(event, ModelError):
                return ToolResult(
                    content=event.message,
                    is_error=True,
                    metadata=compatibility_metadata,
                )
        return ToolResult(
            content="".join(parts),
            metadata=compatibility_metadata,
        )

    async def run_pack(
        self, pack_id: str | None, session_id: str
    ) -> AsyncIterator[Mapping[str, Any]]:
        selected_pack_id = pack_id or self._only_enabled_pack()
        if not self._catalog.is_enabled(selected_pack_id):
            raise ValueError(f"pack {selected_pack_id!r} is disabled")
        if self._selected_target_id is None:
            raise ValueError("select a target before running a pack")
        profile = await self._store.get_target(self._selected_target_id)
        if profile is None:
            raise ValueError("selected target no longer exists")
        if self._permissions is not None:
            if not await self._permissions.authorize_provider(session_id, self._workspace_root):
                raise PermissionError("provider network permission denied")
            if not await self._permissions.authorize_target(
                session_id, self._workspace_root, profile.id
            ):
                raise PermissionError("target network permission denied")
        pack = self._catalog.load(selected_pack_id)
        case = pack.cases[0]
        queue: asyncio.Queue[AssessmentProgress] = asyncio.Queue()

        async def progress(event: AssessmentProgress) -> None:
            await queue.put(event)

        runner = AssessmentRunner(
            attacker=self._attacker,
            target=self._target_factory.create(profile),
            attacker_model=self._attacker_config.model,
            target_model=profile.model,
            on_progress=progress,
        )
        task = asyncio.create_task(
            runner.run(
                pack,
                case,
                session_id=session_id,
                target_profile_id=profile.id,
            )
        )
        try:
            while not task.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.05)
                except TimeoutError:
                    continue
                yield {
                    "state": event.kind,
                    "run_id": event.run_id,
                    "role": event.role,
                    "sequence": event.sequence,
                    "text": event.text,
                }
            result: AssessmentResult = await task
            await self._store.create_run(result.run)
            for turn in result.turns:
                await self._store.append_turn(turn)
            yield {
                "state": result.run.status,
                "run_id": result.run.id,
                "text": result.outcome.summary,
            }
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    def _only_enabled_pack(self) -> str:
        enabled = sorted(self._catalog.enabled_ids)
        if len(enabled) != 1:
            raise ValueError("specify a pack id or enable exactly one pack")
        return enabled[0]


class SelectedTargetTool:
    """Brokered network boundary for the exact target selected by the operator."""

    effect = "target_network"

    def __init__(self, service: AssessmentService) -> None:
        self._service = service

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="target_chat",
            description="Send one message to the operator-selected AI target.",
            input_schema={
                "type": "object",
                "properties": {"prompt": {"type": "string", "minLength": 1}},
                "required": ["prompt"],
                "additionalProperties": False,
            },
            interaction_role=ToolInteractionRole.TARGET_EXCHANGE,
            target_delivery=TargetDeliveryClass.DIRECT_USER_CHANNEL,
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return ToolResult(content="prompt must be a non-empty string", is_error=True)
        return await self._service.invoke_selected_target(
            prompt,
            target_id=context.target_id,
            compiled_adapter=context.compiled_adapter,
            expected_compiled_adapter_digest=context.compiled_adapter_digest,
        )
