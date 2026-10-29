import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from merlin_agents import (
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    CampaignCompilation,
    CampaignManifest,
    CampaignProfile,
    CampaignService,
    ContextDocument,
    DynamicSpecialistProposal,
    FrontierHypothesis,
    LeadAssessment,
    LeadFollowUp,
    ReviewedTargetAdapter,
    compile_attack_campaign,
)
from merlin_agents.campaign_service import (
    _FrontierTransitionLease,
    _json_value,
    _target_reobservation_review_bindings,
    _target_reobservation_superseded_generations,
    _TargetReobservationBlocked,
    _TargetReobservationReviewRequired,
)
from merlin_agents.target_reobservation import (
    TargetReobservationGeneration,
    TargetReobservationObligation,
    TargetReobservationProjection,
    normalize_target_ui_transition,
    target_reobservation_generation,
)
from merlin_contracts import AgentTask


def _campaign() -> tuple[CampaignManifest, AssignmentContext]:
    budget = AssignmentBudget(8, 10_000, 60)
    tool_ids = (
        "target_chat",
        "browser_snapshot",
        "browser_wait_for_text",
        "browser_network_events",
        "browser_recall_reconnaissance",
        "browser_record_api_handoff",
        "browser_compile_api_handoff",
        "browser_click",
        "browser_navigate",
        "browser_fill",
    )
    manifest = CampaignManifest.create(
        id="campaign-1",
        session_id="session-1",
        objective="Assess the reviewed target",
        profile=CampaignProfile.RECOMMENDED,
        target=ReviewedTargetAdapter("target-1", "browser", "https://target.test", "model-1"),
        provider_id="provider-1",
        provider_model="model-1",
        provider_effort="default",
        authority_ids=("session:session-1", "target:target-1"),
        catalogue=(
            ContextDocument.create("case:prompt", "Test prompt injection"),
            ContextDocument.create(
                "target:reviewed-interaction-contract",
                "Reviewed target adapter.\n{}",
            ),
        ),
        skill_documents=(ContextDocument.create("skill:review", "Review evidence"),),
        tool_ids=tool_ids,
        budget=budget,
    )
    context = AssignmentContext(
        target_profile_id="target-1",
        techniques=(
            AssignmentTechnique(
                "prompt",
                "Test prompt injection",
                frozenset({"prompt"}),
                ("case:prompt",),
            ),
        ),
        tool_ids=tool_ids,
        skill_ids=("skill:review",),
        provider_id="provider-1",
        authority_ids=manifest.authority_ids,
        budget=budget,
        input_schema="input/v1",
        output_schema="output/v1",
    )
    return manifest, context


def _transition_lease() -> _FrontierTransitionLease:
    return _FrontierTransitionLease("claim-1", "claimant-1")


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _obligation() -> TargetReobservationObligation:
    normalized = normalize_target_ui_transition(
        {
            "schema": "merlin.target-ui-transition.v1",
            "material": True,
            "adapter_stale": False,
            "before_digest": "sha256:" + "1" * 64,
            "after_digest": "sha256:" + "2" * 64,
            "changed_dimensions": ["controls"],
            "added_controls": [
                {
                    "role": "button",
                    "name": "Next Level",
                    "frame_ref": "main",
                    "disabled": False,
                }
            ],
            "removed_controls": [],
            "controls_truncated": False,
            "evidence_refs": ["browser-snapshot:post-effect"],
            "reobservation_recommended": True,
        }
    )
    assert normalized is not None
    transition, transition_digest = normalized
    receipt_sha256 = "4" * 64
    event_digest = _digest(
        [
            "campaign-1",
            "browser:browser-context-1",
            "source-task",
            "target-call",
            receipt_sha256,
            transition_digest,
        ]
    )
    return TargetReobservationObligation(
        campaign_id="campaign-1",
        session_id="session-1",
        target_id="target-1",
        target_session_id="browser:browser-context-1",
        event_digest=event_digest,
        transition_digest=transition_digest,
        source_schedule_id="source-schedule",
        source_wave=1,
        source_task_id="source-task",
        target_call_id="target-call",
        receipt_sha256=receipt_sha256,
        receipt_projection="target_evidence_receipts",
        transition=transition,
    )


def _generation(obligation: TargetReobservationObligation) -> TargetReobservationGeneration:
    generation = target_reobservation_generation((obligation,), {obligation.event_digest: 1})
    assert generation is not None
    return generation


def _legacy_observation_row(
    obligation: TargetReobservationObligation, *, claimed_session: bool = False
) -> dict[str, object]:
    payload = obligation.observed_payload(obligation.campaign_id, obligation.target_id)
    payload["schema"] = "merlin.target-surface-transition-observed.v1"
    if not claimed_session:
        payload.pop("target_session_id")
    event_digest = _digest(
        [
            obligation.campaign_id,
            obligation.source_task_id,
            obligation.target_call_id,
            obligation.receipt_sha256,
            obligation.transition_digest,
        ]
    )
    payload["event_digest"] = event_digest
    payload["obligation_id"] = "target-reobservation:" + event_digest.removeprefix("sha256:")
    return {
        "wave": obligation.source_wave,
        "decision_id": "target-surface-transition-observed/" + event_digest.removeprefix("sha256:"),
        "payload": payload,
        "payload_digest": _digest(payload),
    }


def _signed_transition_receipt(
    *, call_id: str, is_error: bool, target_effect_attempted: bool
) -> dict[str, object]:
    transition = dict(_obligation().transition)
    transition.pop("transition_digest")
    body: dict[str, object] = {
        "call_id": call_id,
        "tool_name": "browser_chat_exchange",
        "reference": f"target-call:{call_id}",
        "campaign_id": "campaign-1",
        "session_id": "session-1",
        "assignment_id": "source-task",
        "attempt_id": "attempt-1",
        "technique_attempt_id": "technique-attempt-1",
        "target_id": "target-1",
        "target_session_id": "target-session-1",
        "request_sha256": "8" * 64,
        "content_sha256": "9" * 64,
        "metadata_sha256": "a" * 64,
        "is_error": is_error,
        "acknowledged": True,
        "target_effect_attempted": target_effect_attempted,
        "interaction_count": 1,
        "limit_interaction_count": 1,
        "target_call_budget_charge": 1,
        "attempt_disposition": "valid" if not is_error else "infrastructure_failed",
        "target_ui_transition": transition,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {**body, "receipt_sha256": hashlib.sha256(encoded.encode()).hexdigest()}


def _surface_generation_receipt(
    generation: TargetReobservationGeneration,
    *,
    target_id: str = "target-1",
    attempt_id: str = "reobserve-attempt-1",
) -> dict[str, object]:
    request_ref = "browser-request:refresh"
    response_ref = "browser-response:refresh"
    compiled_digest = "sha256:" + "5" * 64
    observed_at = "2026-10-28T12:00:00Z"
    surface_observation_digest = "sha256:" + "f" * 64
    surface_body = {
        "schema": "merlin.target-surface-generation-surface.v1",
        "session_id": "session-1",
        "context_id": "browser-context-1",
        "target_id": target_id,
        "tab_ref": "target-tab-1",
        "observed_at": observed_at,
        "request_evidence_ref": request_ref,
        "response_evidence_ref": response_ref,
        "surface_observation_digest": surface_observation_digest,
        "compiled_adapter_digest": compiled_digest,
    }
    body: dict[str, object] = {
        "schema": "merlin.target-surface-generation.v1",
        "campaign_id": "campaign-1",
        "session_id": "session-1",
        "target_id": target_id,
        "generation_id": generation.generation_id,
        "event_set_digest": _digest({"event_digests": generation.event_digests}),
        "assignment_id": (
            "campaign-1/target-reobservation/"
            + generation.generation_id.removeprefix("sha256:")
            + "/recon"
        ),
        "attempt_id": attempt_id,
        "call_id": "browser-handoff-call-1",
        "context_id": "browser-context-1",
        "tab_ref": "target-tab-1",
        "observed_at": observed_at,
        "request_evidence_ref": request_ref,
        "response_evidence_ref": response_ref,
        "surface_observation_digest": surface_observation_digest,
        "current_surface_digest": _digest(surface_body),
        "compiled_adapter_digest": compiled_digest,
    }
    receipt_sha256 = _digest(body).removeprefix("sha256:")
    return {
        **body,
        "evidence_ref": "target-surface-generation:sha256:" + receipt_sha256,
        "receipt_sha256": receipt_sha256,
    }


class _JournalStore:
    def __init__(
        self,
        manifest: CampaignManifest,
        context: AssignmentContext,
        obligation: TargetReobservationObligation,
        order: list[tuple[str, str]],
    ) -> None:
        self.order = order
        self.lead = [
            {
                "wave": obligation.source_wave,
                "payload": obligation.observed_payload(manifest.id, manifest.target.id),
            }
        ]
        self.decisions: set[str] = set()
        self.waves: list[tuple[int, str, str]] = []
        self.pauses = 0
        self.finishes: list[tuple[str, str, str | None]] = []
        self.row = {
            "id": manifest.id,
            "manifest": {
                "manifest": _json_value(asdict(manifest)),
                "assignment_context": _json_value(asdict(context)),
            },
        }

    def get(self, campaign_id: str):
        return self.row if campaign_id == self.row["id"] else None

    def list_lead(self, _campaign_id: str):
        return tuple(self.lead)

    def commit_lead_decision(
        self,
        _campaign_id: str,
        wave: int,
        decision_id: str,
        payload: dict[str, object],
        _directives: tuple[object, ...],
    ):
        if decision_id in self.decisions:
            return False
        self.decisions.add(decision_id)
        self.lead.append({"wave": wave, "payload": dict(payload)})
        self.order.append(("journal", str(payload.get("kind") or "")))
        return True

    def add_wave(
        self, _campaign_id: str, wave: int, kind: str, schedule_id: str, **_kwargs: object
    ) -> None:
        self.waves.append((wave, kind, schedule_id))
        self.order.append(("wave", schedule_id))

    def list_waves(self, _campaign_id: str):
        return tuple(
            {"wave": wave, "kind": kind, "schedule_id": schedule_id, "status": "running"}
            for wave, kind, schedule_id in self.waves
        )

    def pause(self, _campaign_id: str) -> None:
        self.pauses += 1

    def finish(self, campaign_id: str, status: str, *, stop_reason: str | None = None) -> None:
        self.finishes.append((campaign_id, status, stop_reason))
        self.order.append(("finish", status))


class _Agents:
    _TARGET_TOOLS = frozenset({"target_chat", "browser_click", "browser_fill", "browser_press"})

    def __init__(self, order: list[tuple[str, str]]) -> None:
        self.order = order
        self.submissions: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.reopens: list[str] = []
        self.resumes: list[str] = []
        self.started: list[str] = []
        self.schedules: dict[str, SimpleNamespace] = {}
        self.statuses: dict[str, str] = {}
        self.tasks: dict[str, tuple[AgentTask, ...]] = {}
        self.target_admission_activations: list[dict[str, object]] = []
        self.target_admission_resolutions: list[dict[str, object]] = []
        self.rejections: list[tuple[str, str]] = []

    def target_exchange_tool_ids(self, tool_ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(tool_id for tool_id in tool_ids if tool_id in self._TARGET_TOOLS)

    async def submit(self, _session_id: str, assignments, **options: object):
        assignments = tuple(assignments)
        schedule_id = str(options["schedule_id"])
        contexts = options["worker_contexts"]
        live = SimpleNamespace(
            id=schedule_id,
            assignments=assignments,
            worker_contexts=contexts,
            execution=None,
        )
        self.schedules[schedule_id] = live
        self.submissions.append((assignments, dict(options)))
        self.order.append(("submit", schedule_id))
        return live

    async def reopen(self, schedule_id: str, *, resume_execution: bool = False):
        assert resume_execution is False
        self.reopens.append(schedule_id)
        self.order.append(("reopen", schedule_id))
        return self.schedules.setdefault(
            schedule_id,
            SimpleNamespace(
                id=schedule_id,
                assignments=(),
                worker_contexts={},
                execution=None,
            ),
        )

    async def schedule_status(self, schedule_id: str) -> str | None:
        return self.statuses.get(schedule_id)

    async def resume_schedule(self, schedule_id: str):
        self.resumes.append(schedule_id)
        self.order.append(("resume", schedule_id))
        return self.schedules.setdefault(
            schedule_id,
            SimpleNamespace(
                id=schedule_id,
                assignments=(),
                worker_contexts={},
                execution=None,
            ),
        )

    async def list_schedule_tasks(self, schedule_id: str):
        return self.tasks.get(schedule_id, ())

    def start_execution(self, live, *, max_workers: int) -> None:
        assert max_workers >= 1
        self.started.append(live.id)
        self.order.append(("start", live.id))

    async def stop_local_schedule(self, schedule_id: str) -> None:
        self.schedules.pop(schedule_id, None)

    async def reject_persisted_schedule(self, schedule_id: str, *, reason: str) -> None:
        self.rejections.append((schedule_id, reason))

    async def activate_target_admission(self, activation: dict[str, object]) -> None:
        self.target_admission_activations.append(dict(activation))

    async def resolve_target_admission_terminal(self, resolution: dict[str, object]) -> None:
        self.target_admission_resolutions.append(dict(resolution))


class _GateService(CampaignService):
    def __init__(self, store: _JournalStore, agents: _Agents) -> None:
        super().__init__(store, agents, lambda *_args: None)  # type: ignore[arg-type]
        self.settlement_outcomes: list[str] = []
        self.resolved_handoff: tuple[ContextDocument, str, str, str] | None = None
        self.recovery_requested = asyncio.Event()
        self.recovery_completion: tuple[str, str, str] | None = None
        self.recovered_completion: bool | None = None
        self.recovery_manifest: CampaignManifest | None = None
        self.recovery_lead_target_surface: dict[str, object] | None = None

    async def _current_target_admission_latches(
        self, campaign_id: str, _manifest: CampaignManifest
    ) -> tuple[dict[str, object], ...]:
        projection = await self._target_reobservation_projection(campaign_id)
        latest_by_session: dict[str, dict[str, object]] = {}
        for generation_id, settlement in projection.refreshed.items():
            generation = projection.generations.get(generation_id)
            if generation is None or generation_id not in projection.reviewed:
                continue
            latest_by_session[generation.target_session_id] = {
                "target_session_id": generation.target_session_id,
                "state": "latched",
                "revision": 1,
                "authorized_schedule_id": None,
                "pending_event_digests": generation.event_digests,
                "pending_event_count": len(generation.event_digests),
                "event_set_digest": settlement["event_set_digest"],
                "generation_id": generation_id,
                "settlement_id": settlement["settlement_id"],
                "lead_decision_id": projection.reviewed[generation_id],
            }
        if self._agents.target_admission_activations:
            activation = self._agents.target_admission_activations[-1]
            generation_id = activation.get("generation_id")
            target_session_id = activation.get("target_session_id")
            authorized_schedule_id = activation.get("schedule_id")
            activated = latest_by_session.get(str(target_session_id))
            if (
                activated is not None
                and activated.get("generation_id") == generation_id
                and isinstance(authorized_schedule_id, str)
            ):
                activated.update(
                    state="activated",
                    revision=int(activation["latch_revision"]) + 1,
                    authorized_schedule_id=authorized_schedule_id,
                )
                return (activated,)
        return tuple(latest_by_session[key] for key in sorted(latest_by_session))

    async def _settle_target_reobservation_attempt(
        self,
        campaign_id: str,
        manifest: CampaignManifest,
        generation: TargetReobservationGeneration,
        attempt: int,
        schedule_id: str,
        _live: object,
    ) -> None:
        outcome = self.settlement_outcomes.pop(0)
        self._store.order.append(("settle", outcome))
        common = {
            "campaign_id": campaign_id,
            "session_id": manifest.session_id,
            "target_id": manifest.target.id,
            "target_session_id": generation.target_session_id,
            "generation_id": generation.generation_id,
            "obligation_id": generation.obligation_id,
            "event_digests": generation.event_digests,
            "event_count": len(generation.event_digests),
            "event_set_digest": _digest({"event_digests": generation.event_digests}),
            "observation_cutoff_sequence": generation.observation_cutoff_sequence,
            "source_wave": generation.source_wave,
            "attempt": attempt,
            "schedule_id": schedule_id,
        }
        if outcome == "blocked":
            await self._commit_target_reobservation_event(
                campaign_id,
                generation.source_wave,
                f"test-target-reobservation-blocked/{attempt}",
                {
                    "kind": "target_reobservation_blocked",
                    "schema": "merlin.target-reobservation-blocked.v1",
                    **common,
                    "blocker": "operator action required",
                },
            )
            self._store.pause(campaign_id)
            raise _TargetReobservationBlocked("operator action required")
        assert outcome == "refreshed"
        surface_refs = (
            "browser-snapshot:refresh-request",
            "browser-snapshot:refresh-response",
            "target-surface-snapshot:sha256:" + "9" * 64,
            "target-surface-generation:sha256:" + "8" * 64,
        )
        settlement_id = "target-reobservation-settled/" + generation.generation_id.removeprefix(
            "sha256:"
        )
        await self._commit_target_reobservation_event(
            campaign_id,
            generation.source_wave,
            settlement_id,
            {
                "kind": "target_reobservation_settled",
                "schema": "merlin.target-reobservation-settlement.v1",
                **common,
                "settlement_id": settlement_id,
                "status": "refreshed",
                "current_surface_digest": "sha256:" + "9" * 64,
                "surface_evidence_refs": surface_refs,
                "surface_evidence_digest": _digest({"evidence_refs": sorted(surface_refs)}),
                "recon_handoff_id": "handoff-refreshed",
                "compiled_adapter_digest": "sha256:" + "5" * 64,
                "reviewed_adapter_digest": "sha256:" + "6" * 64,
                "result_reference": "merlin-result://target-reobservation",
                "result_digest": "sha256:" + "7" * 64,
            },
        )

    async def _resolve_target_reobservation_handoff(
        self,
        manifest: CampaignManifest,
        _handoff_id: str,
        target_session_id: str,
    ) -> tuple[ContextDocument, str, str, str]:
        assert target_session_id.startswith("browser:")
        if self.resolved_handoff is not None:
            return self.resolved_handoff
        document = next(
            item for item in manifest.catalogue if item.id == "target:reviewed-interaction-contract"
        )
        return (
            document,
            json.dumps({"schema": "merlin.compiled-target-adapter.v1"}),
            "sha256:" + "5" * 64,
            "sha256:" + "6" * 64,
        )

    async def _seal_completion_receipt(
        self, _campaign_id: str, _status: str, _reason: str
    ) -> tuple[None, bool]:
        self._store.order.append(("completion", "seal"))
        return None, False

    async def _retire_campaign_terminal_dynamic_specialists(self, _campaign_id: str) -> None:
        return None

    async def recover(self, **_kwargs: object) -> int:
        self._store.order.append(("recovery", "requested"))
        if self.recovery_completion is not None:
            assert self.recovery_manifest is not None
            control_plane = await self._lead_control_plane_tasks(self.recovery_manifest, ())
            surface_task = next(
                task for task in control_plane if task.id.endswith("/target_surface_get")
            )
            self.recovery_lead_target_surface = json.loads(surface_task.result or "{}")
            self._store.order.append(("lead", "review-refreshed-state"))
            projection = await self._target_reobservation_projection(  # noqa: SLF001
                self.recovery_manifest.id
            )
            generation = next(iter(projection.generations.values()))
            _append_reviewed(self._store, generation)
            self.recovered_completion = await self._finish_campaign(*self.recovery_completion)
        self.recovery_requested.set()
        return 1


class _RealSettlementService(_GateService):
    def __init__(self, store: _JournalStore, agents: _Agents) -> None:
        super().__init__(store, agents)
        self.evidence: dict[str, dict[str, object]] = {}

    async def _settle_target_reobservation_attempt(
        self,
        campaign_id: str,
        manifest: CampaignManifest,
        generation: TargetReobservationGeneration,
        attempt: int,
        schedule_id: str,
        live: object,
    ) -> None:
        await CampaignService._settle_target_reobservation_attempt(
            self,
            campaign_id,
            manifest,
            generation,
            attempt,
            schedule_id,
            live,
        )

    async def _await_live_wave_drivers(self, _live: object) -> None:
        return None

    async def _source_evidence(
        self, _schedule_id: str, _tasks: tuple[AgentTask, ...]
    ) -> dict[str, dict[str, object]]:
        return self.evidence


def _append_started(
    store: _JournalStore,
    obligation: TargetReobservationObligation,
    schedule_id: str,
    *,
    blocked: bool = False,
) -> None:
    generation = _generation(obligation)
    store.lead.append(
        {
            "wave": generation.source_wave,
            "payload": {
                "kind": "target_reobservation_started",
                "schema": "merlin.target-reobservation-started.v1",
                "wave_kind": "objective_prerequisite",
                "campaign_id": generation.campaign_id,
                "session_id": generation.session_id,
                "target_id": generation.target_id,
                "target_session_id": generation.target_session_id,
                "generation_id": generation.generation_id,
                "obligation_id": generation.obligation_id,
                "event_digests": generation.event_digests,
                "event_set_digest": _digest({"event_digests": generation.event_digests}),
                "observation_cutoff_sequence": generation.observation_cutoff_sequence,
                "source_wave": generation.source_wave,
                "attempt": 1,
                "schedule_id": schedule_id,
            },
        }
    )
    if blocked:
        store.lead.append(
            {
                "wave": generation.source_wave,
                "payload": {
                    "kind": "target_reobservation_blocked",
                    "schema": "merlin.target-reobservation-blocked.v1",
                    "campaign_id": generation.campaign_id,
                    "session_id": generation.session_id,
                    "target_id": generation.target_id,
                    "target_session_id": generation.target_session_id,
                    "generation_id": generation.generation_id,
                    "obligation_id": generation.obligation_id,
                    "event_digests": generation.event_digests,
                    "event_count": len(generation.event_digests),
                    "event_set_digest": _digest({"event_digests": generation.event_digests}),
                    "observation_cutoff_sequence": (generation.observation_cutoff_sequence),
                    "source_wave": generation.source_wave,
                    "attempt": 1,
                    "schedule_id": schedule_id,
                },
            }
        )


def _append_reviewed(store: _JournalStore, generation: TargetReobservationGeneration) -> None:
    wave = generation.source_wave
    payload: dict[str, object] = {
        "schema": "merlin.campaign-lead-decision.v1",
        "campaign_id": generation.campaign_id,
        "wave": wave,
        "source_message_ids": [],
        "target_reobservation_generations": [
            {
                "generation_id": generation.generation_id,
                "settlement_id": (
                    "target-reobservation-settled/"
                    + generation.generation_id.removeprefix("sha256:")
                ),
                "target_session_id": generation.target_session_id,
            }
        ],
    }
    identity = json.dumps(
        {
            "campaign_id": generation.campaign_id,
            "wave": wave,
            "source_message_ids": (),
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    store.lead.append(
        {
            "wave": wave,
            "decision_id": "lead-decision:" + hashlib.sha256(identity.encode()).hexdigest(),
            "payload": payload,
        }
    )


async def test_start_wave_runs_passive_reobservation_before_target_schedule() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []
    store = _JournalStore(manifest, context, obligation, order)
    agents = _Agents(order)
    service = _GateService(store, agents)
    service.settlement_outcomes.append("refreshed")
    target_compilation = compile_attack_campaign(manifest, context)

    with pytest.raises(_TargetReobservationReviewRequired):
        await service._start_wave(  # noqa: SLF001
            manifest.id, manifest, 2, "custom", target_compilation
        )

    generation = _generation(obligation)
    reobserve_id = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 1
    )
    _append_reviewed(store, generation)
    live = await service._start_wave(  # noqa: SLF001
        manifest.id,
        manifest,
        2,
        "custom",
        target_compilation,
        transition_lease=_transition_lease(),
    )
    target_id = live.id
    assert target_id != service._campaign_wave_schedule_id(manifest.id, 2)  # noqa: SLF001
    assert [str(options["schedule_id"]) for _, options in agents.submissions] == [
        reobserve_id,
        target_id,
    ]
    assert order.index(("submit", reobserve_id)) < order.index(("submit", target_id))
    assert order.index(("settle", "refreshed")) < order.index(("submit", target_id))
    assert agents.submissions[0][0][0].strategy_id == "target-reobservation/v1"
    assert live.id == target_id
    assert store.waves == [(2, "custom", target_id)]


async def test_same_wave_refresh_submits_generation_bound_rebound_and_cas_authority() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []
    store = _JournalStore(manifest, context, obligation, order)
    agents = _Agents(order)
    service = _GateService(store, agents)
    service.settlement_outcomes.append("refreshed")
    compilation = compile_attack_campaign(manifest, context)

    with pytest.raises(_TargetReobservationReviewRequired):
        await service._start_wave(  # noqa: SLF001
            manifest.id, manifest, 2, "custom", compilation
        )
    generation = _generation(obligation)
    _append_reviewed(store, generation)
    stale_schedule_id = service._campaign_wave_schedule_id(manifest.id, 2)  # noqa: SLF001
    store.waves.append((2, "custom", stale_schedule_id))

    live = await service._start_wave(  # noqa: SLF001
        manifest.id,
        manifest,
        2,
        "custom",
        compilation,
        transition_lease=_transition_lease(),
    )

    assert live.id != stale_schedule_id
    assert store.waves == [(2, "custom", stale_schedule_id)]
    assert agents.started[-1] == live.id
    assert len(agents.target_admission_activations) == 1
    activation = agents.target_admission_activations[0]
    assert activation["schedule_id"] == live.id
    assert activation["source_schedule_id"] == stale_schedule_id
    assert activation["wave"] == 2
    assert activation["kind"] == "custom"
    assert activation["claim_id"] == "claim-1"
    assert activation["claimant_id"] == "claimant-1"
    assert activation["generation_id"] == generation.generation_id
    assert activation["event_set_digest"] == _digest({"event_digests": generation.event_digests})
    assert agents.rejections == [
        (
            stale_schedule_id,
            "superseded by reviewed target-surface generation " + generation.generation_id,
        )
    ]

    # Crash/retry before the store CAS returns submits the same exact replacement,
    # never a fresh UUID and never the stale source schedule.
    replay = await service._start_wave(  # noqa: SLF001
        manifest.id,
        manifest,
        2,
        "custom",
        compilation,
        transition_lease=_transition_lease(),
    )
    assert replay.id == live.id
    assert [str(options["schedule_id"]) for _, options in agents.submissions[-2:]] == [
        live.id,
        live.id,
    ]
    assert len(agents.target_admission_activations) == 2
    assert len(agents.rejections) == 2

    later = await service._start_wave(  # noqa: SLF001
        manifest.id,
        manifest,
        3,
        "frontier",
        compilation,
        transition_lease=_transition_lease(),
    )
    assert later.id not in {stale_schedule_id, live.id}
    assert agents.target_admission_activations[-1]["schedule_id"] == later.id
    assert agents.target_admission_activations[-1]["generation_id"] == generation.generation_id


async def test_two_physical_sessions_are_reviewed_before_one_atomic_rebound() -> None:
    manifest, context = _campaign()
    first = _obligation()
    second_session_id = "browser:browser-context-2"
    second_receipt_sha256 = "5" * 64
    second_call_id = "target-call-2"
    second = replace(
        first,
        target_session_id=second_session_id,
        target_call_id=second_call_id,
        receipt_sha256=second_receipt_sha256,
        event_digest=_digest(
            [
                first.campaign_id,
                second_session_id,
                first.source_task_id,
                second_call_id,
                second_receipt_sha256,
                first.transition_digest,
            ]
        ),
    )
    order: list[tuple[str, str]] = []
    store = _JournalStore(manifest, context, first, order)
    store.lead.append(
        {
            "wave": second.source_wave,
            "payload": second.observed_payload(manifest.id, manifest.target.id),
        }
    )
    agents = _Agents(order)
    service = _GateService(store, agents)
    service.settlement_outcomes.extend(("refreshed", "refreshed"))
    compilation = compile_attack_campaign(manifest, context)

    with pytest.raises(_TargetReobservationReviewRequired):
        await service._start_wave(  # noqa: SLF001
            manifest.id, manifest, 2, "frontier", compilation
        )

    projection = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001
    generations = {
        generation.target_session_id: generation for generation in projection.generations.values()
    }
    assert set(generations) == {first.target_session_id, second.target_session_id}
    _append_reviewed(store, generations[first.target_session_id])
    _append_reviewed(store, generations[second.target_session_id])
    reviewed_projection = await service._target_reobservation_projection(  # noqa: SLF001
        manifest.id
    )

    live = await service._start_wave(  # noqa: SLF001
        manifest.id,
        manifest,
        2,
        "frontier",
        compilation,
        transition_lease=_transition_lease(),
    )

    assert live.id == agents.started[-1]
    activation = agents.target_admission_activations[-1]
    assert activation["target_session_id"] == second.target_session_id
    assert activation["generation_id"] == generations[second.target_session_id].generation_id
    assert activation["latch_revision"] == 1
    assert activation["superseded_generations"] == (
        {
            "target_session_id": first.target_session_id,
            "generation_id": generations[first.target_session_id].generation_id,
            "event_set_digest": _digest(
                {"event_digests": generations[first.target_session_id].event_digests}
            ),
            "settlement_id": (
                "target-reobservation-settled/"
                + generations[first.target_session_id].generation_id.removeprefix("sha256:")
            ),
            "lead_decision_id": reviewed_projection.reviewed[
                generations[first.target_session_id].generation_id
            ],
            "latch_revision": 1,
        },
    )


def test_supersession_candidates_join_only_current_durable_latches() -> None:
    refreshed: dict[str, dict[str, object]] = {}
    reviewed: dict[str, str] = {}
    for index in range(300):
        generation_id = _digest({"generation": index})
        event_digest = _digest({"event": index})
        refreshed[generation_id] = {
            "generation_id": generation_id,
            "target_session_id": f"browser:historical-{index:03d}",
            "event_set_digest": _digest({"event_digests": (event_digest,)}),
            "settlement_id": (
                "target-reobservation-settled/" + generation_id.removeprefix("sha256:")
            ),
        }
        reviewed[generation_id] = "lead-decision:" + f"{index:064x}"
    generation_ids = tuple(refreshed)
    selected_generation_id = generation_ids[-1]
    projection = TargetReobservationProjection(
        pending=(),
        observation_sequences={},
        generations={},
        starts={},
        blocked=frozenset(),
        retry_authorizations={},
        overflow=None,
        refreshed=refreshed,
        reviewed=reviewed,
    )

    selected_revision, candidates = _target_reobservation_superseded_generations(
        projection,
        selected_generation_id,
        (
            {
                "target_session_id": "browser:historical-298",
                "state": "activated",
                "revision": 7,
                "authorized_schedule_id": "00000000-0000-0000-0000-000000000007",
                "pending_event_digests": (_digest({"event": 298}),),
                "pending_event_count": 1,
                "event_set_digest": None,
                "generation_id": None,
                "settlement_id": None,
                "lead_decision_id": None,
            },
            {
                "target_session_id": "browser:historical-299",
                "state": "latched",
                "revision": 8,
                "authorized_schedule_id": None,
                "pending_event_digests": (_digest({"event": 299}),),
                "pending_event_count": 1,
                "event_set_digest": None,
                "generation_id": None,
                "settlement_id": None,
                "lead_decision_id": None,
            },
        ),
    )

    assert selected_revision == 8
    assert candidates == (
        {
            "target_session_id": "browser:historical-298",
            "generation_id": generation_ids[-2],
            "event_set_digest": refreshed[generation_ids[-2]]["event_set_digest"],
            "settlement_id": refreshed[generation_ids[-2]]["settlement_id"],
            "lead_decision_id": reviewed[generation_ids[-2]],
            "latch_revision": 7,
        },
    )


def test_supersession_candidates_reject_missing_or_forged_current_latches() -> None:
    generation = _generation(_obligation())
    event_set_digest = _digest({"event_digests": generation.event_digests})
    settlement_id = "target-reobservation-settled/" + generation.generation_id.removeprefix(
        "sha256:"
    )
    projection = TargetReobservationProjection(
        pending=(),
        observation_sequences={},
        generations={generation.generation_id: generation},
        starts={},
        blocked=frozenset(),
        retry_authorizations={},
        overflow=None,
        refreshed={
            generation.generation_id: {
                "generation_id": generation.generation_id,
                "target_session_id": generation.target_session_id,
                "event_set_digest": event_set_digest,
                "settlement_id": settlement_id,
            }
        },
        reviewed={generation.generation_id: "lead-decision:" + "a" * 64},
    )

    with pytest.raises(RuntimeError, match="unbounded"):
        _target_reobservation_superseded_generations(projection, generation.generation_id, ())
    with pytest.raises(RuntimeError, match="lacks reviewed refresh authority"):
        _target_reobservation_superseded_generations(
            projection,
            generation.generation_id,
            (
                {
                    "target_session_id": "browser:forged",
                    "state": "latched",
                    "revision": 1,
                    "authorized_schedule_id": None,
                    "pending_event_digests": generation.event_digests,
                    "pending_event_count": len(generation.event_digests),
                    "event_set_digest": None,
                    "generation_id": None,
                    "settlement_id": None,
                    "lead_decision_id": None,
                },
            ),
        )
    with pytest.raises(RuntimeError, match="unbounded"):
        _target_reobservation_superseded_generations(
            projection,
            generation.generation_id,
            tuple({"target_session_id": f"browser:{index}"} for index in range(257)),
        )


def test_supersession_candidate_uses_exact_current_repeated_session_generation() -> None:
    old_generation = _digest({"generation": "old"})
    current_generation = _digest({"generation": "current"})
    selected_generation = _digest({"generation": "selected"})
    old_event = _digest({"event": "old"})
    current_event = _digest({"event": "current"})
    selected_event = _digest({"event": "selected"})

    def settlement(generation_id: str, target_session_id: str, event: str) -> dict[str, str]:
        return {
            "generation_id": generation_id,
            "target_session_id": target_session_id,
            "event_set_digest": _digest({"event_digests": (event,)}),
            "settlement_id": (
                "target-reobservation-settled/" + generation_id.removeprefix("sha256:")
            ),
        }

    refreshed = {
        old_generation: settlement(old_generation, "browser:repeated", old_event),
        current_generation: settlement(current_generation, "browser:repeated", current_event),
        selected_generation: settlement(selected_generation, "browser:selected", selected_event),
    }
    reviewed = {
        generation_id: "lead-decision:" + f"{index:064x}"
        for index, generation_id in enumerate(refreshed, 1)
    }
    projection = TargetReobservationProjection(
        pending=(),
        observation_sequences={},
        generations={},
        starts={},
        blocked=frozenset(),
        retry_authorizations={},
        overflow=None,
        refreshed=refreshed,
        reviewed=reviewed,
    )

    selected_revision, candidates = _target_reobservation_superseded_generations(
        projection,
        selected_generation,
        (
            {
                "target_session_id": "browser:repeated",
                "state": "latched",
                "revision": 4,
                "authorized_schedule_id": None,
                "pending_event_digests": (current_event,),
                "pending_event_count": 1,
                "event_set_digest": None,
                "generation_id": None,
                "settlement_id": None,
                "lead_decision_id": None,
            },
            {
                "target_session_id": "browser:selected",
                "state": "latched",
                "revision": 5,
                "authorized_schedule_id": None,
                "pending_event_digests": (selected_event,),
                "pending_event_count": 1,
                "event_set_digest": None,
                "generation_id": None,
                "settlement_id": None,
                "lead_decision_id": None,
            },
        ),
    )

    assert selected_revision == 5
    assert candidates[0]["generation_id"] == current_generation
    assert candidates[0]["generation_id"] != old_generation


async def test_legacy_target_admission_is_upgraded_before_lead_projection() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []

    class LegacyStore(_JournalStore):
        def __init__(self) -> None:
            super().__init__(manifest, context, obligation, order)
            self.canonical_row = self.lead.pop()
            self.lead.append(_legacy_observation_row(obligation))

        def upgrade_legacy_target_admission(
            self, campaign_id: str, session_id: str, target_id: str
        ) -> dict[str, object]:
            assert (campaign_id, session_id, target_id) == (
                manifest.id,
                manifest.session_id,
                manifest.target.id,
            )
            order.append(("legacy-upgrade", campaign_id))
            self.lead.append(self.canonical_row)
            return {
                "schema": "merlin.target-admission-legacy-upgrade.v1",
                "status": "upgraded",
                "scanned_count": 1,
                "upgraded_count": 1,
                "unresolved_count": 0,
                "overflow": False,
            }

        def list_lead(self, campaign_id: str):
            order.append(("list-lead", campaign_id))
            return super().list_lead(campaign_id)

    store = LegacyStore()
    service = _GateService(store, _Agents(order))

    projection = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001

    assert order[:2] == [
        ("legacy-upgrade", manifest.id),
        ("list-lead", manifest.id),
    ]
    assert tuple(item.event_digest for item in projection.pending) == (obligation.event_digest,)


async def test_legacy_target_admission_block_survives_restart_and_prevents_finish() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []

    class BlockedLegacyStore(_JournalStore):
        def __init__(self) -> None:
            super().__init__(manifest, context, obligation, order)
            self.lead[:] = [_legacy_observation_row(obligation, claimed_session=True)]
            self.upgrades = 0

        def upgrade_legacy_target_admission(
            self, campaign_id: str, session_id: str, target_id: str
        ) -> dict[str, object]:
            assert (campaign_id, session_id, target_id) == (
                manifest.id,
                manifest.session_id,
                manifest.target.id,
            )
            self.upgrades += 1
            return {
                "schema": "merlin.target-admission-legacy-upgrade.v1",
                "status": "blocked",
                "scanned_count": 1,
                "upgraded_count": 0,
                "unresolved_count": 1,
                "overflow": False,
            }

    store = BlockedLegacyStore()
    for _restart in range(2):
        service = _GateService(store, _Agents(order))
        with pytest.raises(_TargetReobservationBlocked, match="authenticated session adoption"):
            await service._finish_campaign(  # noqa: SLF001
                manifest.id, "completed", "objectives_completed"
            )

    assert store.upgrades == 2
    assert store.finishes == []


async def test_missing_legacy_upgrader_blocks_canonical_v1_projection() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []
    store = _JournalStore(manifest, context, obligation, order)
    store.lead[:] = [_legacy_observation_row(obligation)]
    service = _GateService(store, _Agents(order))

    with pytest.raises(_TargetReobservationBlocked, match="authenticated session adoption"):
        await service._target_reobservation_projection(manifest.id)  # noqa: SLF001


async def test_restart_reopens_exact_started_reobservation_without_duplicate_submit() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []
    store = _JournalStore(manifest, context, obligation, order)
    agents = _Agents(order)
    service = _GateService(store, agents)
    generation = _generation(obligation)
    schedule_id = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 1
    )
    _append_started(store, obligation, schedule_id)
    agents.statuses[schedule_id] = "running"
    service.settlement_outcomes.append("refreshed")

    await service._ensure_target_reobserved(manifest.id, manifest, context)  # noqa: SLF001

    assert agents.reopens == [schedule_id]
    assert agents.submissions == []
    projection = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001
    assert projection.pending == ()


async def test_paused_auxiliary_resumes_same_attempt_before_target_path_continues() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []
    store = _JournalStore(manifest, context, obligation, order)
    agents = _Agents(order)
    service = _GateService(store, agents)
    generation = _generation(obligation)
    schedule_id = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 1
    )
    _append_started(store, obligation, schedule_id)
    agents.statuses[schedule_id] = "paused"
    service.settlement_outcomes.append("refreshed")
    target_compilation = compile_attack_campaign(manifest, context)

    with pytest.raises(_TargetReobservationReviewRequired):
        await service._start_wave(  # noqa: SLF001
            manifest.id, manifest, 2, "custom", target_compilation
        )
    _append_reviewed(store, generation)
    target_live = await service._start_wave(  # noqa: SLF001
        manifest.id,
        manifest,
        2,
        "custom",
        target_compilation,
        transition_lease=_transition_lease(),
    )

    target_id = target_live.id
    assert target_id != service._campaign_wave_schedule_id(manifest.id, 2)  # noqa: SLF001
    assert agents.resumes == [schedule_id]
    assert agents.reopens == []
    assert [str(options["schedule_id"]) for _, options in agents.submissions] == [target_id]
    assert order.index(("resume", schedule_id)) < order.index(("settle", "refreshed"))
    assert order.index(("settle", "refreshed")) < order.index(("submit", target_id))
    assert target_live.id == target_id


async def test_blocked_reobservation_requires_one_unique_authorization_per_attempt() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []
    store = _JournalStore(manifest, context, obligation, order)
    agents = _Agents(order)
    service = _GateService(store, agents)
    generation = _generation(obligation)
    first_schedule = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 1
    )
    _append_started(store, obligation, first_schedule, blocked=True)

    with pytest.raises(_TargetReobservationBlocked, match="explicit operator resume"):
        await service._ensure_target_reobserved(manifest.id, manifest, context)  # noqa: SLF001
    assert agents.submissions == []

    assert await service._authorize_target_reobservation_retry(  # noqa: SLF001
        manifest.id, "resume-command-1"
    )
    service.settlement_outcomes.append("blocked")
    with pytest.raises(_TargetReobservationBlocked, match="operator action required"):
        await service._ensure_target_reobserved(manifest.id, manifest, context)  # noqa: SLF001

    second_schedule = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 2
    )
    assert [str(options["schedule_id"]) for _, options in agents.submissions] == [second_schedule]

    with pytest.raises(_TargetReobservationBlocked, match="explicit operator resume"):
        await service._ensure_target_reobserved(manifest.id, manifest, context)  # noqa: SLF001
    assert await service._authorize_target_reobservation_retry(  # noqa: SLF001
        manifest.id, "resume-command-1"
    )
    with pytest.raises(_TargetReobservationBlocked, match="explicit operator resume"):
        await service._ensure_target_reobserved(manifest.id, manifest, context)  # noqa: SLF001
    assert len(agents.submissions) == 1


async def test_blocked_journal_restart_pauses_until_unique_resume_and_settles() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []

    class RecoveryStore(_JournalStore):
        def __init__(self) -> None:
            super().__init__(manifest, context, obligation, order)
            self.row["status"] = "executing"

        def list_waves(self, _campaign_id: str):
            return (
                {
                    "wave": obligation.source_wave,
                    "kind": "initial",
                    "schedule_id": obligation.source_schedule_id,
                    "status": "failed",
                },
            )

        def load_active_frontier_batch(self, _campaign_id: str):
            return None

        def pause(self, campaign_id: str) -> None:
            assert campaign_id == manifest.id
            assert self.row["status"] == "executing"
            self.row["status"] = "paused"
            super().pause(campaign_id)

    store = RecoveryStore()
    agents = _Agents(order)
    service = _GateService(store, agents)
    generation = _generation(obligation)
    first_schedule = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 1
    )
    _append_started(store, obligation, first_schedule, blocked=True)

    # The worker that journaled the blocker crashed before it could pause the
    # campaign. The blocked auxiliary schedule must remain a typed recovery
    # barrier instead of making this executing campaign appear idle.
    projection = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001
    recovery_state = projection.recovery_state(latest_wave=obligation.source_wave)
    assert recovery_state == {
        "schema": "merlin.target-reobservation-recovery-state.v1",
        "phase": "reobserve",
        "generation_id": generation.generation_id,
        "target_session_id": generation.target_session_id,
        "event_set_digest": _digest({"event_digests": generation.event_digests}),
        "source_wave": generation.source_wave,
        "schedule_id": first_schedule,
        "settlement_id": None,
        "lead_decision_id": None,
    }
    claimed = SimpleNamespace(
        phase="reobserve",
        generation_id=generation.generation_id,
        target_session_id=generation.target_session_id,
        event_set_digest=_digest({"event_digests": generation.event_digests}),
        source_wave=generation.source_wave,
        schedule_id=first_schedule,
        settlement_id=None,
        lead_decision_id=None,
    )
    decision = SimpleNamespace(
        campaign_id=manifest.id,
        recovery_action="recover_target_reobservation",
        target_reobservation_recovery_state=claimed,
    )

    assert await service._recover_recorded_action(decision, True) == 1  # noqa: SLF001
    assert store.row["status"] == "paused"
    assert store.pauses == 1
    assert agents.submissions == []

    # One explicit operator command authorizes exactly one new attempt. Replaying
    # the command is idempotent and cannot pre-authorize a later attempt.
    assert await service._authorize_target_reobservation_retry(  # noqa: SLF001
        manifest.id, "operator-resume-blocked-1"
    )
    assert await service._authorize_target_reobservation_retry(  # noqa: SLF001
        manifest.id, "operator-resume-blocked-1"
    )
    retry_rows = [
        row
        for row in store.lead
        if row["payload"].get("kind") == "target_reobservation_retry_authorized"
    ]
    assert len(retry_rows) == 1
    store.row["status"] = "executing"
    service.settlement_outcomes.append("refreshed")

    await service._ensure_target_reobserved(manifest.id, manifest, context)  # noqa: SLF001

    second_schedule = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 2
    )
    assert [str(options["schedule_id"]) for _, options in agents.submissions] == [second_schedule]
    refreshed = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001
    assert refreshed.pending == ()
    assert refreshed.review_pending
    control_plane = await service._lead_control_plane_tasks(manifest, ())  # noqa: SLF001
    surface_task = next(task for task in control_plane if task.id.endswith("/target_surface_get"))
    surface = json.loads(surface_task.result or "{}")
    assert surface["refreshed"][0]["generation_id"] == generation.generation_id
    _append_reviewed(store, generation)
    reviewed = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001
    assert generation.generation_id in reviewed.reviewed


async def test_target_reobservation_overflow_pauses_and_blocks_target_work() -> None:
    manifest, context = _campaign()
    seed = _obligation()
    order: list[tuple[str, str]] = []
    store = _JournalStore(manifest, context, seed, order)
    store.lead.clear()
    for index in range(257):
        call_id = f"overflow-call-{index}"
        receipt_sha256 = hashlib.sha256(f"overflow-receipt-{index}".encode()).hexdigest()
        event_digest = _digest(
            [
                manifest.id,
                seed.target_session_id,
                seed.source_task_id,
                call_id,
                receipt_sha256,
                seed.transition_digest,
            ]
        )
        obligation = replace(
            seed,
            event_digest=event_digest,
            target_call_id=call_id,
            receipt_sha256=receipt_sha256,
        )
        store.lead.append(
            {
                "wave": obligation.source_wave,
                "payload": obligation.observed_payload(manifest.id, manifest.target.id),
            }
        )
    agents = _Agents(order)
    service = _GateService(store, agents)
    projection = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001

    assert len(projection.pending) == 257
    assert projection.current_generation is None
    assert projection.overflow is not None
    assert projection.overflow.event_count == 257
    recovery_state = projection.recovery_state(latest_wave=seed.source_wave)
    assert recovery_state is not None
    assert recovery_state["phase"] == "reobserve"
    assert recovery_state["generation_id"] == projection.overflow.generation_id
    assert recovery_state["schedule_id"] is None

    compilation = compile_attack_campaign(manifest, context)
    with pytest.raises(_TargetReobservationBlocked, match="event set exceeds"):
        await service._start_wave(  # noqa: SLF001
            manifest.id, manifest, 2, "custom", compilation
        )
    assert agents.submissions == []
    assert store.pauses == 1

    completed = await service._finish_campaign(  # noqa: SLF001
        manifest.id, "completed", "saturated"
    )
    assert completed is False
    assert agents.submissions == []
    assert store.finishes == []
    assert ("completion", "seal") not in order


async def test_real_settlement_commits_authenticated_handoff_and_result_identity() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []
    store = _JournalStore(manifest, context, obligation, order)
    agents = _Agents(order)
    service = _RealSettlementService(store, agents)
    generation = _generation(obligation)
    schedule_id = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 1
    )
    _append_started(store, obligation, schedule_id)
    task = AgentTask(
        id="target-reobservation-task",
        objective="Refresh the reviewed target handoff",
        status="completed",
    )
    agents.tasks[schedule_id] = (task,)
    result_digest = "sha256:" + "7" * 64
    result_reference = "merlin-result://target-reobservation-task"
    surface_receipt = _surface_generation_receipt(generation)
    required_refs = [
        str(surface_receipt["request_evidence_ref"]),
        str(surface_receipt["response_evidence_ref"]),
        str(surface_receipt["evidence_ref"]),
    ]
    service.evidence = {
        task.id: {
            "reference": result_reference,
            "digest": result_digest,
            "attempt_id": surface_receipt["attempt_id"],
            "structured_result": {
                "recon_handoff_id": "handoff-refreshed",
                "interaction_contract": {},
                "evidence_refs": required_refs,
            },
        }
    }

    async def read_receipts(
        _session_id: str, _handoff_id: str, _target_id: str
    ) -> tuple[dict[str, object], ...]:
        return (surface_receipt,)

    service._recon_observation_reader = read_receipts  # noqa: SLF001

    await service._settle_target_reobservation_attempt(  # noqa: SLF001
        manifest.id,
        manifest,
        generation,
        1,
        schedule_id,
        SimpleNamespace(execution=None),
    )

    projection = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001
    assert projection.pending == ()
    assert projection.latest_refreshed is not None
    assert projection.latest_refreshed["schedule_id"] == schedule_id
    assert projection.latest_refreshed["recon_handoff_id"] == "handoff-refreshed"
    assert projection.latest_refreshed["result_reference"] == result_reference
    assert projection.latest_refreshed["result_digest"] == result_digest
    assert (
        projection.latest_refreshed["current_surface_digest"]
        == surface_receipt["current_surface_digest"]
    )
    assert (
        "target-surface-snapshot:" + str(surface_receipt["current_surface_digest"])
        in projection.latest_refreshed["surface_evidence_refs"]
    )


async def test_real_settlement_rejects_same_session_receipt_for_another_target() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    store = _JournalStore(manifest, context, obligation, [])
    agents = _Agents([])
    service = _RealSettlementService(store, agents)
    generation = _generation(obligation)
    schedule_id = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 1
    )
    _append_started(store, obligation, schedule_id)
    task = AgentTask(
        id="target-reobservation-task",
        objective="Refresh the reviewed target handoff",
        status="completed",
    )
    agents.tasks[schedule_id] = (task,)
    wrong_target_receipt = _surface_generation_receipt(generation, target_id="other-target")
    cited_refs = [
        str(wrong_target_receipt["request_evidence_ref"]),
        str(wrong_target_receipt["response_evidence_ref"]),
        "target-surface-snapshot:" + str(wrong_target_receipt["current_surface_digest"]),
        str(wrong_target_receipt["evidence_ref"]),
    ]
    service.evidence = {
        task.id: {
            "reference": "merlin-result://target-reobservation-task",
            "digest": "sha256:" + "7" * 64,
            "attempt_id": wrong_target_receipt["attempt_id"],
            "structured_result": {
                "recon_handoff_id": "handoff-refreshed",
                "evidence_refs": cited_refs,
            },
        }
    }

    async def read_receipts(
        _session_id: str, _handoff_id: str, _target_id: str
    ) -> tuple[dict[str, object], ...]:
        return (wrong_target_receipt,)

    service._recon_observation_reader = read_receipts  # noqa: SLF001

    with pytest.raises(
        _TargetReobservationBlocked,
        match="lacks one exact daemon-sealed surface generation receipt",
    ):
        await service._settle_target_reobservation_attempt(  # noqa: SLF001
            manifest.id,
            manifest,
            generation,
            1,
            schedule_id,
            SimpleNamespace(execution=None),
        )

    projection = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001
    assert projection.latest_refreshed is None
    assert tuple(item.event_digest for item in projection.pending) == (obligation.event_digest,)
    assert store.pauses == 1


def test_reobservation_compilation_is_passive_and_has_no_target_exchange_tools() -> None:
    manifest, context = _campaign()
    generation = _generation(_obligation())
    compilation = CampaignService._target_reobservation_compilation(  # noqa: SLF001
        manifest,
        context,
        generation,
        ("target-reobservation-started/" + generation.generation_id.removeprefix("sha256:") + "/1"),
    )
    assignment = compilation.assignments[0]
    allowed = {
        "browser_snapshot",
        "browser_wait_for_text",
        "browser_network_events",
        "browser_recall_reconnaissance",
        "browser_record_api_handoff",
        "browser_compile_api_handoff",
    }
    forbidden = {
        "target_chat",
        "browser_click",
        "browser_navigate",
        "browser_fill",
        "browser_press",
    }

    assert set(assignment.tool_ids) == allowed
    assert forbidden.isdisjoint(assignment.tool_ids)
    assert assignment.technique_ids == ()
    assert assignment.strategy_id == "target-reobservation/v1"
    assert _Agents([]).target_exchange_tool_ids(assignment.tool_ids) == ()
    instructions = compilation.worker_contexts[0].profile.content
    assert "Do not navigate" in instructions
    assert "click" in instructions
    assert "execute an attack" in instructions


async def test_pass_modal_refresh_requires_fresh_lead_review_before_completion() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []
    store = _JournalStore(manifest, context, obligation, order)
    agents = _Agents(order)
    service = _GateService(store, agents)
    service.settlement_outcomes.append("refreshed")
    service.recovery_completion = (manifest.id, "completed", "saturated")
    service.recovery_manifest = manifest
    pre_refresh_control = await service._lead_control_plane_tasks(manifest, ())  # noqa: SLF001
    pre_refresh_surface = json.loads(
        next(task for task in pre_refresh_control if task.id.endswith("/target_surface_get")).result
        or "{}"
    )

    async def finish_from_supervisor() -> bool:
        current = asyncio.current_task()
        assert current is not None
        service._supervisors[manifest.id] = current  # noqa: SLF001
        return await service._finish_campaign(  # noqa: SLF001
            manifest.id, "completed", "saturated"
        )

    completed = await asyncio.create_task(finish_from_supervisor())
    await asyncio.wait_for(service.recovery_requested.wait(), timeout=1)

    generation = _generation(obligation)
    reobserve_id = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 1
    )
    assert completed is False
    assert ("submit", reobserve_id) in order
    assert ("settle", "refreshed") in order
    assert order.count(("recovery", "requested")) == 1
    assert service.recovered_completion is True
    assert order.index(("settle", "refreshed")) < order.index(("recovery", "requested"))
    assert order.index(("recovery", "requested")) < order.index(("lead", "review-refreshed-state"))
    assert order.index(("lead", "review-refreshed-state")) < order.index(("completion", "seal"))
    assert order.index(("completion", "seal")) < order.index(("finish", "completed"))
    projection = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001
    assert projection.pending == ()
    refreshed_surface = service.recovery_lead_target_surface
    assert refreshed_surface is not None
    assert refreshed_surface != pre_refresh_surface
    assert pre_refresh_surface["refreshed"] == []
    assert pre_refresh_surface["pending"][0]["event_digest"] == obligation.event_digest
    assert refreshed_surface["pending"] == []
    refreshed = refreshed_surface["refreshed"][0]
    assert set(refreshed) == {
        "generation_id",
        "settlement_id",
        "target_session_id",
        "event_count",
        "event_set_digest",
        "status",
        "current_surface_digest",
        "surface_evidence_digest",
        "surface_evidence_refs",
        "recon_handoff_id",
        "compiled_adapter_digest",
        "reviewed_adapter_digest",
        "result_reference",
        "result_digest",
    }
    assert refreshed["generation_id"] == generation.generation_id
    assert refreshed["event_count"] == 1
    assert refreshed["event_set_digest"] == _digest({"event_digests": generation.event_digests})
    assert refreshed["status"] == "refreshed"
    assert refreshed["current_surface_digest"] == "sha256:" + "9" * 64
    assert "target-surface-snapshot:sha256:" + "9" * 64 in refreshed["surface_evidence_refs"]
    assert refreshed["recon_handoff_id"] == "handoff-refreshed"
    assert refreshed["compiled_adapter_digest"] == "sha256:" + "5" * 64
    assert refreshed["reviewed_adapter_digest"] == "sha256:" + "6" * 64
    projected_text = json.dumps(refreshed_surface, sort_keys=True)
    assert "added_controls" not in projected_text
    assert "Next Level" not in projected_text
    assert "action_authority" not in projected_text


@pytest.mark.parametrize(
    ("source_kind", "expected_schedule_id", "expected_wave", "expected_reopens"),
    [
        ("objective-phase-gate", "phase-gate-schedule", 1, ("phase-gate-schedule",)),
        (
            "objective-navigation",
            "caller-schedule",
            0,
            ("navigation-schedule", "caller-schedule"),
        ),
    ],
)
async def test_special_continuations_review_refresh_before_resuming_caller(
    source_kind: str,
    expected_schedule_id: str,
    expected_wave: int,
    expected_reopens: tuple[str, ...],
) -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []

    class RecoveryStore(_JournalStore):
        def __init__(self) -> None:
            super().__init__(manifest, context, obligation, order)
            if source_kind == "objective-phase-gate":
                self.recovery_waves = (
                    {
                        "wave": 1,
                        "kind": "objective_prerequisite",
                        "schedule_id": "phase-gate-schedule",
                        "status": "completed",
                    },
                )
            else:
                self.recovery_waves = (
                    {
                        "wave": 0,
                        "kind": "frontier",
                        "schedule_id": "caller-schedule",
                        "status": "completed",
                    },
                    {
                        "wave": 1,
                        "kind": "objective_navigation",
                        "schedule_id": "navigation-schedule",
                        "status": "completed",
                    },
                )

        def list_waves(self, _campaign_id: str):
            return self.recovery_waves

        def load_active_frontier_batch(self, _campaign_id: str):
            return None

    class RecoveryService(_GateService):
        def __init__(self, store: RecoveryStore, agents: _Agents) -> None:
            super().__init__(store, agents)
            self._lead_runner = object()  # type: ignore[assignment]
            self.supervisions: list[tuple[str, int, bool]] = []
            self.lead_surface: dict[str, object] | None = None

        async def _run_lead_with_policy(self, active_manifest, tasks):
            control_plane = await self._lead_control_plane_tasks(active_manifest, tasks)
            surface_task = next(
                task for task in control_plane if task.id.endswith("/target_surface_get")
            )
            self.lead_surface = json.loads(surface_task.result or "{}")
            projection = await self._target_reobservation_projection(active_manifest.id)
            generation = next(iter(projection.generations.values()))
            _append_reviewed(self._store, generation)
            order.append(("lead", generation.generation_id))

        async def _supervise(
            self,
            _campaign_id,
            _manifest,
            _context,
            live,
            *,
            initial_wave,
            wave_running=True,
        ) -> None:
            self.supervisions.append((live.id, initial_wave, wave_running))
            order.append(("supervise", live.id))

    store = RecoveryStore()
    agents = _Agents(order)
    service = RecoveryService(store, agents)
    service.settlement_outcomes.append("refreshed")
    generation = _generation(obligation)
    refresh_schedule_id = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 1
    )
    _append_started(store, obligation, refresh_schedule_id)

    if source_kind == "objective-phase-gate":
        source_schedule_id = "phase-gate-schedule"
        source_assignment = SimpleNamespace(
            strategy_id="objective-phase-gate/v1",
            profile_id="objective-prerequisite",
        )
    else:
        source_schedule_id = "navigation-schedule"
        source_assignment = SimpleNamespace(
            strategy_id="objective-navigation/v1",
            profile_id="recon",
        )
        agents.schedules["caller-schedule"] = SimpleNamespace(
            id="caller-schedule",
            assignments=(SimpleNamespace(strategy_id="portfolio/v1", profile_id="attack"),),
            worker_contexts={},
            execution=None,
        )
    agents.schedules[source_schedule_id] = SimpleNamespace(
        id=source_schedule_id,
        assignments=(source_assignment,),
        worker_contexts={},
        execution=None,
    )
    agents.tasks[source_schedule_id] = (
        AgentTask(
            id="source-task",
            objective="Resume deterministic continuation",
            status="completed",
        ),
    )
    claimed = SimpleNamespace(
        phase="reobserve",
        generation_id=generation.generation_id,
        target_session_id=generation.target_session_id,
        event_set_digest=_digest({"event_digests": generation.event_digests}),
        source_wave=generation.source_wave,
        schedule_id=refresh_schedule_id,
        settlement_id=None,
        lead_decision_id=None,
    )
    decision = SimpleNamespace(
        campaign_id=manifest.id,
        recovery_action="recover_target_reobservation",
        target_reobservation_recovery_state=claimed,
    )

    assert await service._recover_recorded_action(decision, True) == 1  # noqa: SLF001
    await service._supervisors[manifest.id]  # noqa: SLF001

    assert service.supervisions == [(expected_schedule_id, expected_wave, False)]
    assert tuple(agents.reopens) == expected_reopens
    projection = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001
    assert projection.reviewed[generation.generation_id].startswith("lead-decision:")
    assert service.lead_surface is not None
    assert service.lead_surface["pending"] == []
    assert service.lead_surface["refreshed"][0]["generation_id"] == generation.generation_id
    assert order.index(("settle", "refreshed")) < order.index(("lead", generation.generation_id))
    assert order.index(("lead", generation.generation_id)) < order.index(
        ("supervise", expected_schedule_id)
    )
    assert len(agents.submissions) == 1
    refresh_assignments = agents.submissions[0][0]
    assert len(refresh_assignments) == 1
    refresh_assignment = refresh_assignments[0]
    assert refresh_assignment.strategy_id == "target-reobservation/v1"
    assert {
        "target_chat",
        "browser_click",
        "browser_navigate",
        "browser_fill",
        "browser_press",
    }.isdisjoint(refresh_assignment.tool_ids)


async def test_blocked_completion_does_not_seal_or_finish_campaign() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []
    store = _JournalStore(manifest, context, obligation, order)
    service = _GateService(store, _Agents(order))
    service.settlement_outcomes.append("blocked")

    completed = await service._finish_campaign(  # noqa: SLF001
        manifest.id, "completed", "saturated"
    )

    assert completed is False
    assert ("completion", "seal") not in order
    assert ("finish", "completed") not in order
    assert store.pauses == 1


async def test_refreshed_adapter_is_overlaid_on_target_assignment_and_context() -> None:
    manifest, context = _campaign()
    obligation = _obligation()
    order: list[tuple[str, str]] = []
    store = _JournalStore(manifest, context, obligation, order)
    agents = _Agents(order)
    service = _GateService(store, agents)
    original_compilation = compile_attack_campaign(manifest, context)
    source_adapter = json.dumps(
        {"schema": "merlin.compiled-target-adapter.v1", "source": "finding"},
        sort_keys=True,
    )
    source_compiled_digest = "sha256:" + "d" * 64
    source_reviewed_digest = "sha256:" + "e" * 64
    original = CampaignCompilation(
        (
            replace(
                original_compilation.assignments[0],
                reviewed_adapter_digest=source_reviewed_digest,
                strategy_contract=(
                    ("compiled_adapter", source_adapter),
                    ("compiled_adapter_compatibility", "legacy_absent"),
                    ("compiled_adapter_digest", source_compiled_digest),
                    ("reviewed_adapter_digest", source_reviewed_digest),
                ),
            ),
        ),
        original_compilation.worker_contexts,
    )
    refreshed_document = ContextDocument.create(
        "target:reviewed-interaction-contract",
        "Reviewed target adapter.\n"
        + json.dumps(
            {
                "interface": "browser",
                "endpoint": "https://target.test",
                "browser_steps": [],
            },
            sort_keys=True,
        ),
    )
    compiled_adapter = json.dumps(
        {"schema": "merlin.compiled-target-adapter.v1", "steps": []},
        sort_keys=True,
    )
    compiled_digest = "sha256:" + "5" * 64
    current_surface_digest = "sha256:" + "9" * 64
    reviewed_digest = "sha256:" + "6" * 64
    service.resolved_handoff = (
        refreshed_document,
        compiled_adapter,
        compiled_digest,
        reviewed_digest,
    )
    generation = _generation(obligation)
    schedule_id = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 1
    )
    _append_started(store, obligation, schedule_id)
    surface_refs = (
        "browser-snapshot:refresh-request",
        "browser-snapshot:refresh-response",
        "target-surface-snapshot:sha256:" + "9" * 64,
        "target-surface-generation:sha256:" + "8" * 64,
    )
    settlement_id = "target-reobservation-settled/" + generation.generation_id.removeprefix(
        "sha256:"
    )
    store.lead.append(
        {
            "wave": generation.source_wave,
            "payload": {
                "kind": "target_reobservation_settled",
                "schema": "merlin.target-reobservation-settlement.v1",
                "campaign_id": manifest.id,
                "session_id": manifest.session_id,
                "target_id": manifest.target.id,
                "target_session_id": generation.target_session_id,
                "generation_id": generation.generation_id,
                "obligation_id": generation.obligation_id,
                "event_digests": generation.event_digests,
                "event_count": len(generation.event_digests),
                "event_set_digest": _digest({"event_digests": generation.event_digests}),
                "observation_cutoff_sequence": generation.observation_cutoff_sequence,
                "source_wave": generation.source_wave,
                "attempt": 1,
                "status": "refreshed",
                "schedule_id": schedule_id,
                "settlement_id": settlement_id,
                "recon_handoff_id": "handoff-refreshed",
                "current_surface_digest": current_surface_digest,
                "surface_evidence_refs": surface_refs,
                "surface_evidence_digest": _digest({"evidence_refs": sorted(surface_refs)}),
                "compiled_adapter_digest": compiled_digest,
                "reviewed_adapter_digest": reviewed_digest,
                "result_reference": "merlin-result://target-reobservation",
                "result_digest": "sha256:" + "7" * 64,
            },
        }
    )

    rebound = await service._rebind_compilation_to_target_reobservation(  # noqa: SLF001
        manifest.id, manifest, original
    )

    assignment = rebound.assignments[0]
    contract = dict(assignment.strategy_contract)
    assert contract["compiled_adapter"] == compiled_adapter
    assert contract["compiled_adapter_digest"] == compiled_digest
    assert contract["initial_state_equivalence_digest"] == current_surface_digest
    assert assignment.reviewed_adapter_digest == reviewed_digest
    assert contract["reviewed_adapter_digest"] == reviewed_digest
    assert "compiled_adapter_compatibility" not in contract
    worker_context = rebound.worker_contexts[0]
    assert worker_context.strategy_contract == assignment.strategy_contract
    assert worker_context.digest != original.worker_contexts[0].digest
    assert (
        next(
            item
            for item in worker_context.guidance
            if item.id == "target:reviewed-interaction-contract"
        )
        == refreshed_document
    )
    worker_context.validate()

    _append_reviewed(store, generation)
    validation_live = await service._start_wave(  # noqa: SLF001
        manifest.id, manifest, 2, "validation", original
    )
    validation_assignment = validation_live.assignments[0]
    validation_contract = dict(validation_assignment.strategy_contract)
    assert validation_assignment.reviewed_adapter_digest == source_reviewed_digest
    assert validation_contract["reviewed_adapter_digest"] == source_reviewed_digest
    assert validation_contract["compiled_adapter"] == source_adapter
    assert validation_contract["compiled_adapter_digest"] == source_compiled_digest
    assert validation_contract["initial_state_equivalence_digest"] != current_surface_digest

    reproduction_live = await service._start_wave(  # noqa: SLF001
        manifest.id, manifest, 3, "reproduction", original
    )
    reproduction_assignment = reproduction_live.assignments[0]
    reproduction_contract = dict(reproduction_assignment.strategy_contract)
    assert reproduction_assignment.reviewed_adapter_digest == source_reviewed_digest
    assert reproduction_contract["compiled_adapter"] == source_adapter
    assert reproduction_contract["compiled_adapter_digest"] == source_compiled_digest
    assert "target_reobservation_generation_id" not in reproduction_contract


async def test_post_effect_transition_receipt_journals_exactly_once_across_replay() -> None:
    manifest, context = _campaign()
    store = _JournalStore(manifest, context, _obligation(), [])
    store.lead.clear()
    service = _GateService(store, _Agents([]))
    receipt = _signed_transition_receipt(
        call_id="lakera-level-1-win",
        is_error=False,
        target_effect_attempted=True,
    )
    evidence = {
        "source-task": {
            "target_evidence_receipts": {"lakera-level-1-win": receipt},
        }
    }
    tasks = (SimpleNamespace(id="source-task"),)

    for _ in range(2):
        await service._record_target_surface_transitions(  # noqa: SLF001
            manifest,
            manifest.id,
            1,
            "source-schedule",
            tasks,  # type: ignore[arg-type]
            evidence,
        )

    observed = [
        row["payload"]
        for row in store.lead
        if row["payload"].get("kind") == "target_surface_transition_observed"
    ]
    assert len(observed) == 1
    assert observed[0]["receipt_projection"] == "target_evidence_receipts"
    assert observed[0]["target_call_id"] == "lakera-level-1-win"
    assert observed[0]["target_ui_transition"]["adapter_stale"] is False


async def test_attempted_effect_error_transition_journals_only_reobservation_metadata() -> None:
    manifest, context = _campaign()
    store = _JournalStore(manifest, context, _obligation(), [])
    store.lead.clear()
    service = _GateService(store, _Agents([]))
    receipt = _signed_transition_receipt(
        call_id="post-effect-transport-error",
        is_error=True,
        target_effect_attempted=True,
    )

    await service._record_target_surface_transitions(  # noqa: SLF001
        manifest,
        manifest.id,
        1,
        "source-schedule",
        (SimpleNamespace(id="source-task"),),  # type: ignore[arg-type]
        {
            "source-task": {
                "target_ui_transition_receipts": {
                    "post-effect-transport-error": receipt,
                }
            }
        },
    )

    payload = store.lead[0]["payload"]
    assert payload["kind"] == "target_surface_transition_observed"
    assert payload["receipt_projection"] == "target_ui_transition_receipts"
    assert payload["target_ui_transition"]["adapter_stale"] is False
    assert {
        "attack_credit",
        "counts_as_campaign_progress",
        "finding_id",
        "outcome",
        "score",
        "success",
        "target_effect_attempted",
    }.isdisjoint(payload)


def _signed_zero_effect_transition_receipt(call_id: str) -> dict[str, object]:
    transition = dict(_obligation().transition)
    transition.pop("transition_digest")
    transition["adapter_stale"] = True
    body: dict[str, object] = {
        "call_id": call_id,
        "tool_name": "browser_chat_exchange",
        "reference": f"target-call:{call_id}",
        "campaign_id": "campaign-1",
        "session_id": "session-1",
        "assignment_id": "source-task",
        "attempt_id": "attempt-1",
        "technique_attempt_id": "technique-attempt-1",
        "target_id": "target-1",
        "target_session_id": "target-session-1",
        "request_sha256": "8" * 64,
        "content_sha256": "9" * 64,
        "metadata_sha256": "a" * 64,
        "is_error": True,
        "acknowledged": True,
        "target_effect_attempted": False,
        "interaction_count": 0,
        "limit_interaction_count": 0,
        "target_call_budget_charge": 0,
        "attempt_disposition": "uncertain",
        "target_ui_transition": transition,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {**body, "receipt_sha256": hashlib.sha256(encoded.encode()).hexdigest()}


class _SupervisionStore(_JournalStore):
    def __init__(
        self,
        manifest: CampaignManifest,
        context: AssignmentContext,
        order: list[tuple[str, str]],
    ) -> None:
        super().__init__(manifest, context, _obligation(), order)
        self.lead.clear()
        self.finished_waves: list[tuple[int, str, dict[str, object]]] = []

    def append_lead(self, _campaign_id: str, wave: int, payload: dict[str, object]) -> None:
        self.lead.append({"wave": wave, "payload": dict(payload)})
        self.order.append(("lead", str(payload.get("kind") or "")))

    def finish_wave(
        self, _campaign_id: str, wave: int, status: str, summary: dict[str, object]
    ) -> None:
        self.finished_waves.append((wave, status, dict(summary)))
        self.order.append(("finish-wave", status))


class _SupervisionAgents(_Agents):
    def __init__(
        self,
        order: list[tuple[str, str]],
        task: AgentTask,
        evidence: dict[str, object],
    ) -> None:
        super().__init__(order)
        self.task = task
        self.evidence = evidence
        self.cancellations: list[tuple[str, str]] = []

    async def list_schedule_tasks(self, schedule_id: str):
        if schedule_id == "source-schedule":
            return (self.task,)
        return await super().list_schedule_tasks(schedule_id)

    async def completion_projection(self, schedule_id: str):
        if schedule_id != "source-schedule":
            return {"tasks": (), "evidence": {}}
        return {
            "tasks": (self.task,),
            "evidence": {self.task.id: dict(self.evidence)},
        }

    async def cancel_schedule(self, schedule_id: str, *, reason: str) -> None:
        self.cancellations.append((schedule_id, reason))
        self.order.append(("cancel", reason))


class _SupervisionBarrierService(_GateService):
    def __init__(
        self,
        store: _SupervisionStore,
        agents: _SupervisionAgents,
        *,
        infrastructure_only: bool,
        failure_stage: str | None = None,
    ) -> None:
        super().__init__(store, agents)
        self.infrastructure_only = infrastructure_only
        self.failure_stage = failure_stage
        self.infrastructure_pause_checks = 0
        self.arm_outcome_calls = 0
        self.frontier_counts: dict[str, int] | None = None

    async def _monitor_live(self, *_args: object, **_kwargs: object):
        return None

    async def _record_lessons(self, *_args: object, **_kwargs: object) -> None:
        if self.failure_stage == "lessons":
            raise RuntimeError("injected lesson reducer failure")
        return None

    async def _record_target_state_attributions(self, *args: object, **kwargs: object) -> None:
        if self.failure_stage == "attributions":
            raise RuntimeError("injected target attribution failure")
        await super()._record_target_state_attributions(*args, **kwargs)

    async def _campaign_budget_capped(self, _campaign_id: str) -> bool:
        return False

    async def _campaign_service_objective_state(
        self, _campaign_id: str, _manifest: CampaignManifest
    ) -> dict[str, object]:
        return {"reviewed": False, "violations": (), "metrics": {}}

    async def _settle_execution_frontier_batch(self, *_args: object, **_kwargs: object):
        self.frontier_counts = {
            "queued": 1,
            **({"infrastructure_only_batch": 1} if self.infrastructure_only else {}),
        }
        return self.frontier_counts

    async def _record_arm_outcomes(self, *_args: object, **_kwargs: object) -> None:
        self.arm_outcome_calls += 1

    async def _pause_repeated_frontier_infrastructure(
        self,
        campaign_id: str,
        wave: int,
        frontier_counts,
    ) -> bool:
        self.infrastructure_pause_checks += 1
        return await super()._pause_repeated_frontier_infrastructure(
            campaign_id, wave, frontier_counts
        )


async def _run_owned_supervision(
    service: _SupervisionBarrierService,
    manifest: CampaignManifest,
    context: AssignmentContext,
    execution: asyncio.Future[None],
) -> None:
    current = asyncio.current_task()
    assert current is not None
    service._supervisors[manifest.id] = current  # noqa: SLF001
    await service._supervise(  # noqa: SLF001
        manifest.id,
        manifest,
        context,
        SimpleNamespace(
            id="source-schedule",
            assignments=(SimpleNamespace(profile_id="injection", strategy_id="direct"),),
            execution=execution,
        ),
        initial_wave=1,
    )


async def test_sole_zero_effect_surface_disruption_refreshes_before_failure_decision() -> None:
    manifest, context = _campaign()
    order: list[tuple[str, str]] = []
    store = _SupervisionStore(manifest, context, order)
    for prior_wave in (-1, 0):
        store.append_lead(
            manifest.id,
            prior_wave,
            {
                "kind": "frontier_infrastructure_only_batch",
                "state": "retryable",
                "queued": 1,
            },
        )
    task = AgentTask(id="source-task", objective="probe target", status="quarantined")
    receipt = _signed_zero_effect_transition_receipt("stale-surface-call")
    agents = _SupervisionAgents(
        order,
        task,
        {
            "error_code": "target_surface_disrupted",
            "target_ui_transition_receipts": {"stale-surface-call": receipt},
        },
    )
    service = _SupervisionBarrierService(store, agents, infrastructure_only=False)
    service.settlement_outcomes.append("refreshed")
    service.recovery_completion = (manifest.id, "completed", "saturated")
    service.recovery_manifest = manifest
    execution: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    await asyncio.create_task(_run_owned_supervision(service, manifest, context, execution))
    await asyncio.wait_for(service.recovery_requested.wait(), timeout=1)

    generation = next(
        iter((await service._target_reobservation_projection(manifest.id)).generations.values())
    )
    refresh_schedule_id = service._target_reobservation_schedule_id(  # noqa: SLF001
        manifest.id, generation.generation_id, 1
    )
    observed = [
        row["payload"]
        for row in store.lead
        if row["payload"].get("kind") == "target_surface_transition_observed"
    ]
    assert task.status == "quarantined"
    assert store.finished_waves == [(1, "failed", {"completed": 0, "failed": 1, "skipped": 0})]
    assert len(observed) == 1
    assert observed[0]["receipt_projection"] == "target_ui_transition_receipts"
    assert observed[0]["target_call_id"] == "stale-surface-call"
    assert agents.evidence["error_code"] == "target_surface_disrupted"
    assert "infrastructure_failure_category" not in agents.evidence
    assert receipt["attempt_disposition"] != "infrastructure_failed"
    assert {
        "attack_credit",
        "counts_as_campaign_progress",
        "finding_id",
        "outcome",
        "score",
        "success",
    }.isdisjoint(observed[0])
    assert service.infrastructure_pause_checks == 0
    assert service.frontier_counts is None
    assert service.arm_outcome_calls == 0
    assert agents.cancellations == []
    assert not execution.done()
    assert store.pauses == 0
    assert not any(status == "failed" for _, status, _ in store.finishes)
    assert ("submit", refresh_schedule_id) in order
    refresh_assignment = agents.submissions[0][0][0]
    assert refresh_assignment.strategy_id == "target-reobservation/v1"
    assert {
        "target_chat",
        "browser_click",
        "browser_navigate",
        "browser_fill",
        "browser_press",
    }.isdisjoint(refresh_assignment.tool_ids)
    assert service.recovery_lead_target_surface is not None
    assert service.recovery_lead_target_surface["refreshed"][0]["generation_id"] == (
        generation.generation_id
    )
    projection = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001
    assert projection.pending == ()
    assert projection.reviewed[generation.generation_id].startswith("lead-decision:")
    assert order.index(("finish-wave", "failed")) < order.index(("submit", refresh_schedule_id))
    assert order.index(("settle", "refreshed")) < order.index(("lead", "review-refreshed-state"))
    assert not any(
        row["wave"] == 1
        and str(row["payload"].get("kind") or "").startswith("frontier_infrastructure_")
        for row in store.lead
    )
    execution.cancel()


async def test_sole_ordinary_infrastructure_failure_keeps_pause_behavior() -> None:
    manifest, context = _campaign()
    order: list[tuple[str, str]] = []
    store = _SupervisionStore(manifest, context, order)
    for prior_wave in (-1, 0):
        store.append_lead(
            manifest.id,
            prior_wave,
            {
                "kind": "frontier_infrastructure_only_batch",
                "state": "retryable",
                "queued": 1,
            },
        )
    task = AgentTask(id="source-task", objective="probe target", status="quarantined")
    agents = _SupervisionAgents(order, task, {})
    service = _SupervisionBarrierService(store, agents, infrastructure_only=True)
    execution: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    await _run_owned_supervision(service, manifest, context, execution)

    assert task.status == "quarantined"
    assert store.finished_waves == [(1, "failed", {"completed": 0, "failed": 1, "skipped": 0})]
    assert service.infrastructure_pause_checks == 1
    assert agents.cancellations == [("source-schedule", "campaign-wave-failed")]
    assert order.index(("cancel", "campaign-wave-failed")) < order.index(("finish-wave", "failed"))
    assert store.pauses == 1
    assert agents.submissions == []
    assert store.finishes == []
    assert any(
        row["payload"].get("kind") == "frontier_infrastructure_circuit_open" for row in store.lead
    )
    assert not any(
        row["payload"].get("kind") == "target_surface_transition_observed" for row in store.lead
    )
    execution.cancel()


@pytest.mark.parametrize("failure_stage", ("lessons", "attributions"))
async def test_authenticated_surface_transition_survives_supervision_reducer_failure(
    failure_stage: str,
) -> None:
    manifest, context = _campaign()
    order: list[tuple[str, str]] = []
    store = _SupervisionStore(manifest, context, order)
    task = AgentTask(id="source-task", objective="probe target", status="quarantined")
    receipt = _signed_zero_effect_transition_receipt(f"{failure_stage}-transition-call")
    agents = _SupervisionAgents(
        order,
        task,
        {
            "error_code": "target_surface_disrupted",
            "target_ui_transition_receipts": {
                f"{failure_stage}-transition-call": receipt,
            },
        },
    )
    service = _SupervisionBarrierService(
        store,
        agents,
        infrastructure_only=False,
        failure_stage=failure_stage,
    )
    execution: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    await asyncio.create_task(_run_owned_supervision(service, manifest, context, execution))
    await asyncio.wait_for(service.recovery_requested.wait(), timeout=1)

    projection = await service._target_reobservation_projection(manifest.id)  # noqa: SLF001
    assert len(projection.pending) == 1
    assert projection.pending[0].target_call_id == f"{failure_stage}-transition-call"
    assert agents.cancellations == []
    assert not execution.done()
    assert store.finished_waves == []
    assert store.finishes == []
    assert agents.submissions == []
    assert order.index(("journal", "target_surface_transition_observed")) < order.index(
        ("recovery", "requested")
    )
    execution.cancel()


async def test_unsigned_supervision_reducer_failure_keeps_ordinary_failure_path() -> None:
    manifest, context = _campaign()
    order: list[tuple[str, str]] = []
    store = _SupervisionStore(manifest, context, order)
    task = AgentTask(id="source-task", objective="probe target", status="quarantined")
    agents = _SupervisionAgents(order, task, {})
    service = _SupervisionBarrierService(
        store,
        agents,
        infrastructure_only=False,
        failure_stage="lessons",
    )
    execution: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    await _run_owned_supervision(service, manifest, context, execution)

    assert agents.cancellations == [("source-schedule", "campaign-lead-failed")]
    assert store.finished_waves == [
        (1, "failed", {"reason": "campaign-lead-failed"}),
    ]
    assert store.finishes == [(manifest.id, "failed", "infrastructure_blocked_after_retry_policy")]
    assert not service.recovery_requested.is_set()
    assert not any(
        row["payload"].get("kind") == "target_surface_transition_observed" for row in store.lead
    )
    assert order.index(("cancel", "campaign-lead-failed")) < order.index(("finish-wave", "failed"))
    execution.cancel()


@pytest.mark.parametrize("foreign_identity", ("session", "target"))
async def test_lead_surface_projection_rejects_foreign_reobservation_chain(
    foreign_identity: str,
) -> None:
    manifest, context = _campaign()
    foreign_obligation = _obligation()
    foreign_manifest = manifest
    if foreign_identity == "session":
        foreign_obligation = replace(foreign_obligation, session_id="session-foreign")
        foreign_manifest = replace(manifest, session_id="session-foreign")
    else:
        foreign_obligation = replace(foreign_obligation, target_id="target-foreign")
        foreign_manifest = replace(
            manifest,
            target=replace(manifest.target, id="target-foreign"),
        )

    async def settled_control_plane(
        chain_manifest: CampaignManifest,
        obligation: TargetReobservationObligation,
    ) -> tuple[tuple[AgentTask, ...], TargetReobservationGeneration]:
        order: list[tuple[str, str]] = []
        store = _JournalStore(chain_manifest, context, obligation, order)
        service = _GateService(store, _Agents(order))
        generation = _generation(obligation)
        schedule_id = service._target_reobservation_schedule_id(  # noqa: SLF001
            manifest.id, generation.generation_id, 1
        )
        _append_started(store, obligation, schedule_id)
        service.settlement_outcomes.append("refreshed")
        await service._settle_target_reobservation_attempt(  # noqa: SLF001
            manifest.id,
            chain_manifest,
            generation,
            1,
            schedule_id,
            SimpleNamespace(execution=None),
        )
        return (
            await service._lead_control_plane_tasks(manifest, ()),  # noqa: SLF001
            generation,
        )

    foreign_tasks, _ = await settled_control_plane(foreign_manifest, foreign_obligation)

    assert not any(task.id.endswith("/target_surface_get") for task in foreign_tasks)
    assert _target_reobservation_review_bindings(foreign_tasks) == ()

    correct_tasks, correct_generation = await settled_control_plane(manifest, _obligation())
    surface_task = next(task for task in correct_tasks if task.id.endswith("/target_surface_get"))
    projected = json.loads(surface_task.result or "{}")
    settlement_id = "target-reobservation-settled/" + correct_generation.generation_id.removeprefix(
        "sha256:"
    )

    assert [item["generation_id"] for item in projected["refreshed"]] == [
        correct_generation.generation_id
    ]
    assert _target_reobservation_review_bindings(correct_tasks) == (
        {
            "generation_id": correct_generation.generation_id,
            "settlement_id": settlement_id,
            "target_session_id": correct_generation.target_session_id,
        },
    )


class _DynamicRetirementProbeService(_SupervisionBarrierService):
    def __init__(
        self,
        store: _SupervisionStore,
        agents: _SupervisionAgents,
        *,
        infrastructure_only: bool,
    ) -> None:
        super().__init__(store, agents, infrastructure_only=infrastructure_only)
        self.dynamic_record = SimpleNamespace(
            assignment=SimpleNamespace(id="source-task"),
            state="active",
            retirement_reason=None,
        )
        self.dynamic_retirements: list[tuple[str, str]] = []

    async def _durable_dynamic_specialists(self, campaign_id: str):
        assert campaign_id == "campaign-1"
        return (self.dynamic_record,)

    async def _persist_dynamic_retirement(
        self, campaign_id: str, _wave: int, record: SimpleNamespace
    ) -> None:
        assert campaign_id == "campaign-1"
        self.dynamic_record = record
        assignment = record.assignment
        self.dynamic_retirements.append((str(assignment.id), str(record.retirement_reason)))


async def test_supervision_protects_only_noncredit_transition_specialist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, context = _campaign()

    def retire_probe(record: SimpleNamespace, *, wave: int, reason: str) -> object:
        assert wave == 1
        return SimpleNamespace(
            assignment=record.assignment,
            state="retired",
            retirement_reason=reason,
        )

    monkeypatch.setattr(
        "merlin_agents.campaign_service.retire_dynamic_specialist",
        retire_probe,
    )

    async def run_case(
        *,
        task_status: str,
        evidence: dict[str, object],
        infrastructure_only: bool,
        opens_surface_barrier: bool,
    ) -> _DynamicRetirementProbeService:
        order: list[tuple[str, str]] = []
        store = _SupervisionStore(manifest, context, order)
        if infrastructure_only:
            for prior_wave in (-1, 0):
                store.append_lead(
                    manifest.id,
                    prior_wave,
                    {
                        "kind": "frontier_infrastructure_only_batch",
                        "state": "retryable",
                        "queued": 1,
                    },
                )
        task = AgentTask(id="source-task", objective="probe target", status=task_status)
        agents = _SupervisionAgents(order, task, evidence)
        service = _DynamicRetirementProbeService(
            store,
            agents,
            infrastructure_only=infrastructure_only,
        )
        execution: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if opens_surface_barrier:
            service.settlement_outcomes.append("refreshed")
            await asyncio.create_task(_run_owned_supervision(service, manifest, context, execution))
            await asyncio.wait_for(service.recovery_requested.wait(), timeout=1)
        else:
            await _run_owned_supervision(service, manifest, context, execution)
        execution.cancel()
        return service

    zero_effect_call = "zero-effect-transition"
    protected = await run_case(
        task_status="quarantined",
        evidence={
            "error_code": "target_surface_disrupted",
            "target_ui_transition_receipts": {
                zero_effect_call: _signed_zero_effect_transition_receipt(zero_effect_call)
            },
        },
        infrastructure_only=False,
        opens_surface_barrier=True,
    )
    assert protected.dynamic_record.state == "active"
    assert protected.dynamic_retirements == []

    successful_call = "successful-transition"
    completed = await run_case(
        task_status="completed",
        evidence={
            "target_evidence_receipts": {
                successful_call: _signed_transition_receipt(
                    call_id=successful_call,
                    is_error=False,
                    target_effect_attempted=True,
                )
            }
        },
        infrastructure_only=False,
        opens_surface_barrier=True,
    )
    assert completed.dynamic_record.state == "retired"
    assert completed.dynamic_retirements == [("source-task", "completed")]

    ordinary_failure = await run_case(
        task_status="quarantined",
        evidence={},
        infrastructure_only=True,
        opens_surface_barrier=False,
    )
    assert ordinary_failure.dynamic_record.state == "retired"
    assert ordinary_failure.dynamic_retirements == [("source-task", "failed")]


def _dynamic_transition_followup(source_task_id: str) -> LeadFollowUp:
    proposal = DynamicSpecialistProposal(
        campaign_id="campaign-1",
        label="dynamic-transition-probe",
        objective="Probe the changed reviewed surface",
        source_task_ids=(source_task_id,),
        evidence_refs=("receipt:source",),
        evidence_expectation="one attributable target receipt",
    )
    return LeadFollowUp(
        proposal.label,
        proposal.objective,
        proposal.source_task_ids,
        FrontierHypothesis(
            surface="reviewed target surface",
            assertion="the changed surface exposes a distinct interaction boundary",
            oracle="the target returns attributable state evidence",
            novelty_basis="the source task observed a material state change",
        ),
        specialist=proposal,
    )


def _signed_dynamic_zero_effect_receipt(call_id: str, task_id: str) -> dict[str, object]:
    receipt = dict(_signed_zero_effect_transition_receipt(call_id))
    receipt.pop("receipt_sha256")
    receipt["assignment_id"] = task_id
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {**receipt, "receipt_sha256": hashlib.sha256(encoded.encode()).hexdigest()}


class _SynchronousDynamicStore(_SupervisionStore):
    def list_technique_attempts(self, _campaign_id: str):
        return ()


class _SynchronousDynamicService(CampaignService):
    def __init__(
        self,
        store: _SynchronousDynamicStore,
        agents: object,
        source: AgentTask,
        *,
        emit_transition: bool,
    ) -> None:
        super().__init__(store, agents, lambda *_args: None)  # type: ignore[arg-type]
        self.source = source
        self.emit_transition = emit_transition
        self.followup = _dynamic_transition_followup(source.id)
        self.wave_evidence: dict[str, dict[str, object]] = {}
        self.dynamic_task_id: str | None = None
        self.finished_status: str | None = None

    async def _run_lead_with_policy(self, *_args: object) -> LeadAssessment:
        return LeadAssessment("run one dynamic specialist", False, (self.followup,))

    async def _journal_assessment(self, *_args: object) -> None:
        return None

    async def _campaign_completed_source_material(self, *_args: object):
        return (
            (self.source,),
            {
                self.source.id: {
                    "digest": "sha256:" + "a" * 64,
                    "reference": "receipt:source",
                }
            },
        )

    async def _source_evidence(
        self, _schedule_id: str, tasks: tuple[AgentTask, ...]
    ) -> dict[str, dict[str, object]]:
        if self.dynamic_task_id is not None and any(
            task.id == self.dynamic_task_id for task in tasks
        ):
            return self.wave_evidence
        return {
            self.source.id: {
                "digest": "sha256:" + "a" * 64,
                "reference": "receipt:source",
            }
        }

    async def _run_wave(
        self,
        _campaign_id: str,
        _manifest: CampaignManifest,
        _wave: int,
        _kind: str,
        compiled: CampaignCompilation,
    ) -> tuple[AgentTask, ...]:
        assignment = compiled.assignments[0]
        self.dynamic_task_id = assignment.id
        task = AgentTask(id=assignment.id, objective=assignment.objective, status="quarantined")
        if self.emit_transition:
            call_id = "synchronous-surface-transition"
            self.wave_evidence = {
                task.id: {
                    "error_code": "target_surface_disrupted",
                    "target_ui_transition_receipts": {
                        call_id: _signed_dynamic_zero_effect_receipt(call_id, task.id)
                    },
                }
            }
        else:
            self.wave_evidence = {}
        return (task,)

    async def _record_arm_outcomes(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _campaign_budget_capped(self, _campaign_id: str) -> bool:
        return False

    async def _assess_and_journal(self, *_args: object, **_kwargs: object) -> LeadAssessment:
        return LeadAssessment("dynamic wave complete", True)

    async def _record_lessons(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _record_target_state_attributions(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _campaign_service_objective_state(
        self, _campaign_id: str, _manifest: CampaignManifest
    ) -> dict[str, object]:
        return {"reviewed": False, "violations": (), "metrics": {}}

    async def _settle_execution_frontier_batch(self, *_args: object, **_kwargs: object):
        return None

    async def _pause_repeated_frontier_infrastructure(
        self, *_args: object, **_kwargs: object
    ) -> bool:
        return False

    async def _apply_incremental_review_priorities(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _monitor_live(self, *_args: object, **_kwargs: object) -> LeadAssessment:
        return LeadAssessment("run one dynamic specialist", False, (self.followup,))

    async def status(self, _campaign_id: str) -> dict[str, object]:
        return {"saturation": {"ready": False}}

    async def _finish_campaign(self, _campaign_id: str, status: str, _reason: str) -> bool:
        self.finished_status = status
        return True


@pytest.mark.parametrize("emit_transition", (True, False))
async def test_operator_continuation_retirement_honors_noncredit_transition(
    emit_transition: bool,
) -> None:
    manifest, context = _campaign()
    source = AgentTask(id="source-task", objective="source", status="completed", result="{}")
    order: list[tuple[str, str]] = []
    store = _SynchronousDynamicStore(manifest, context, order)
    service = _SynchronousDynamicService(
        store,
        object(),
        source,
        emit_transition=emit_transition,
    )

    await service._run_saturation_continuation(  # noqa: SLF001
        manifest.id,
        manifest,
        context,
        1,
        (source,),
        {"decision": "continue", "coverage_gaps": {}},
        set(),
        set(),
        set(),
        source_schedule_id="durable-source-schedule",
    )

    records = await service._durable_dynamic_specialists(manifest.id)  # noqa: SLF001
    assert len(records) == 1
    if emit_transition:
        assert records[0].state == "active"
        assert records[0].retirement_reason is None
    else:
        assert records[0].state == "retired"
        assert records[0].retirement_reason == "failed"


@pytest.mark.parametrize("emit_transition", (True, False))
async def test_primary_followup_retirement_honors_noncredit_transition(
    emit_transition: bool,
) -> None:
    manifest, context = _campaign()
    source = AgentTask(id="source-task", objective="source", status="completed", result="{}")
    order: list[tuple[str, str]] = []
    store = _SynchronousDynamicStore(manifest, context, order)
    agents = _SupervisionAgents(order, source, {})
    service = _SynchronousDynamicService(
        store,
        agents,
        source,
        emit_transition=emit_transition,
    )
    execution: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    await service._supervise(  # noqa: SLF001
        manifest.id,
        manifest,
        context,
        SimpleNamespace(
            id="source-schedule",
            assignments=(SimpleNamespace(profile_id="injection", strategy_id="direct"),),
            execution=execution,
        ),
        initial_wave=1,
    )

    records = await service._durable_dynamic_specialists(manifest.id)  # noqa: SLF001
    assert len(records) == 1
    if emit_transition:
        assert records[0].state == "active"
        assert records[0].retirement_reason is None
    else:
        assert records[0].state == "retired"
        assert records[0].retirement_reason == "failed"
    execution.cancel()
