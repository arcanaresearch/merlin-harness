import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from merlin_contracts import (
    EvidenceWitness,
    ModelJsonError,
    ToolEffectRecord,
    ToolEffectState,
    parse_model_json,
)
from merlin_tui.bootstrap import (
    _browser_exchange_rows,
    _browser_origin,
    _register_browser_tools,
    _resolve_mcp_credential,
    _terminalize_validator_supported_attempt,
    _validated_browser_authentication,
    _validator_exchange_witnesses,
    _verified_session_concurrency,
    _verify_validator_evidence,
)


def test_validator_terminalizes_only_exact_attributable_candidate_attempt() -> None:
    source_digest = "sha256:" + "1" * 64
    validator_digest = "sha256:" + "2" * 64
    identity = {
        "campaign_id": "campaign/one",
        "target_id": "target/one",
        "target_generation": "generation/one",
        "scenario_fingerprint": "sha256:" + "3" * 64,
        "technique_id": "technique/one",
        "controller_id": "controller/one",
        "payload_family": "family/one",
        "requested_language": "lzh",
        "mutation_id": "mutation/one",
    }
    attempt = SimpleNamespace(
        id="technique-attempt/one",
        campaign_id="campaign/one",
        task_id="task/one",
        technique_id="technique/one",
        state="observed",
        strategy_state={"candidate_identity": identity},
        evidence_refs=(source_digest,),
    )

    class AttemptStore:
        terminalized: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def get_technique_attempt(self, attempt_id: str):
            return attempt if attempt_id == attempt.id else None

        def terminalize_technique_attempt(self, *args, **kwargs):
            self.terminalized.append((args, kwargs))

    store = AttemptStore()
    finding = SimpleNamespace(
        creator="task/one",
        technique_id="technique/one",
        target_id="target/one",
        evidence_references=(
            "merlin-result://schedule/schedule-not-campaign/attempt/result-attempt"
            f"?digest={source_digest}&technique_attempt_id={attempt.id}",
        ),
    )
    witness = EvidenceWitness(
        reference="merlin-exchange://validator?call_id=target-call-1",
        digest=validator_digest,
        source="validator:test",
    )

    assert _terminalize_validator_supported_attempt(
        store,
        campaign_id="campaign/one",
        finding=finding,
        desired="validated",
        witnesses=(witness,),
    )
    assert len(store.terminalized) == 1
    args, kwargs = store.terminalized[0]
    assert args == (attempt.id, "confirmed")
    assert kwargs["evidence_refs"] == (source_digest, validator_digest)
    assert kwargs["strategy_state"] == {
        "candidate_identity": identity,
        "outcome_authority": {
            "kind": "validator",
            "outcome": "confirmed",
            "evidence_refs": [source_digest, validator_digest],
            "source_evidence_refs": [source_digest],
            "validator_evidence_refs": [validator_digest],
            "validator_witnesses": [
                {
                    "reference": witness.reference,
                    "digest": validator_digest,
                    "source": "validator:test",
                }
            ],
            "target_call_ids": ["target-call-1"],
        },
    }


def test_validator_does_not_resolve_a_different_candidate_variant() -> None:
    source_digest = "sha256:" + "1" * 64
    attempt = SimpleNamespace(
        id="technique-attempt/one",
        campaign_id="campaign/one",
        task_id="task/one",
        technique_id="technique/one",
        state="observed",
        strategy_state={
            "candidate_identity": {
                "campaign_id": "campaign/one",
                "target_id": "target/other",
                "target_generation": "generation/one",
                "scenario_fingerprint": "sha256:" + "3" * 64,
                "technique_id": "technique/one",
                "controller_id": "controller/one",
                "payload_family": "family/one",
                "requested_language": "lzh",
                "mutation_id": "mutation/one",
            }
        },
        evidence_refs=(source_digest,),
    )

    class AttemptStore:
        terminalized = False

        def get_technique_attempt(self, _attempt_id: str):
            return attempt

        def terminalize_technique_attempt(self, *_args, **_kwargs):
            self.terminalized = True

    store = AttemptStore()
    finding = SimpleNamespace(
        creator="task/one",
        technique_id="technique/one",
        target_id="target/one",
        evidence_references=(
            "merlin-result://schedule/schedule-1/attempt/result-attempt"
            f"?digest={source_digest}&technique_attempt_id={attempt.id}",
        ),
    )
    witness = EvidenceWitness(
        reference="merlin-exchange://validator?call_id=target-call-1",
        digest="sha256:" + "2" * 64,
        source="validator:test",
    )

    assert not _terminalize_validator_supported_attempt(
        store,
        campaign_id="campaign/one",
        finding=finding,
        desired="validated",
        witnesses=(witness,),
    )
    assert not store.terminalized


class Handle:
    value = "remote-prod"


class Store:
    def load(self):
        return {"remote-prod": "top-secret"}


def test_bootstrap_resolves_opaque_mcp_handle_from_credential_store() -> None:
    assert _resolve_mcp_credential(Store(), Handle()) == "top-secret"


def test_bootstrap_missing_mcp_credential_has_sanitized_error() -> None:
    class MissingStore:
        def load(self):
            return {}

    with pytest.raises(ValueError, match="credential is unavailable") as captured:
        _resolve_mcp_credential(MissingStore(), Handle())
    assert "remote-prod" not in str(captured.value)


def test_browser_origin_is_exact_and_rejects_non_http_targets() -> None:
    assert _browser_origin("https://target.test:8443/v1/chat") == "https://target.test:8443"
    with pytest.raises(ValueError, match="browser origin"):
        _browser_origin("file:///tmp/target")


def test_only_verified_session_cardinality_becomes_a_target_limit() -> None:
    assert _verified_session_concurrency(None) is None
    assert (
        _verified_session_concurrency(
            {"status": "unverified", "maximum_concurrency": None, "evidence_refs": []}
        )
        is None
    )
    assert (
        _verified_session_concurrency(
            {"status": "unverified", "maximum_concurrency": 1, "evidence_refs": []}
        )
        is None
    )
    assert (
        _verified_session_concurrency(
            {"status": "verified", "maximum_concurrency": True, "evidence_refs": ["call:1"]}
        )
        is None
    )
    assert (
        _verified_session_concurrency(
            {
                "status": "verified",
                "maximum_concurrency": 1,
                "source": "observed_isolation_probe",
                "evidence_refs": ["call:1", "call:2"],
            },
            ["call:1", "call:2"],
        )
        == 1
    )
    assert (
        _verified_session_concurrency(
            {
                "status": "verified",
                "maximum_concurrency": 3,
                "source": "observed_isolation_probe",
                "evidence_refs": ["invented:1", "invented:2"],
            },
            ["evidence:real-1", "evidence:real-2"],
        )
        is None
    )
    assert (
        _verified_session_concurrency(
            {
                "status": "verified",
                "maximum_concurrency": 3,
                "source": "model_claim",
                "evidence_refs": ["call:1", "call:2"],
            },
            ["call:1", "call:2"],
        )
        is None
    )


def test_parse_model_json_accepts_bare_and_fenced_replies() -> None:
    assert parse_model_json('{"summary": "ok"}') == {"summary": "ok"}
    assert parse_model_json('```json\n{\n  "summary": "ok"\n}\n```') == {"summary": "ok"}
    assert parse_model_json('```\n{"summary": "ok"}\n```\n') == {"summary": "ok"}
    # Surrounding prose is tolerated: the JSON object is extracted.
    assert parse_model_json('Here is the decision: {"summary": "ok"} thanks') == {"summary": "ok"}


def test_parse_model_json_quotes_reply_head_on_junk() -> None:
    with pytest.raises(ModelJsonError, match="reply head") as captured:
        parse_model_json("I cannot help with that request at all.")
    assert "I cannot help" in str(captured.value)
    with pytest.raises(ModelJsonError):
        parse_model_json("")


def _fake_capability_registry(window: int | None, assumed: bool = False) -> object:
    field = SimpleNamespace(
        value=window,
        provenance=SimpleNamespace(assumed=assumed),
    )
    return SimpleNamespace(
        resolve_model=lambda *args, **kwargs: SimpleNamespace(context_window_tokens=field)
    )


def test_lead_result_char_budget_derives_from_resolved_window() -> None:
    from merlin_tui.bootstrap import _lead_result_char_budget

    provider = SimpleNamespace(route_identity=None)
    # The 64 KiB aggregate ceiling is shared across all 10 rows.
    budget = _lead_result_char_budget(_fake_capability_registry(1_000_000), provider, "acme-1", 10)
    assert budget == 6_553
    # A 131K window is also aggregate-capped; 100 rows receive 655 chars each.
    budget = _lead_result_char_budget(_fake_capability_registry(131_072), provider, "acme-1", 100)
    assert budget == 655
    # A single-row wave may use the full aggregate ceiling.
    budget = _lead_result_char_budget(_fake_capability_registry(131_072), provider, "acme-1", 5)
    assert budget == 13_107


def test_lead_result_char_budget_distrusts_assumed_window() -> None:
    from merlin_tui.bootstrap import _lead_result_char_budget

    provider = SimpleNamespace(route_identity=None)
    # Assumed (tier-7) windows must not inflate the budget: fallback 32K window.
    budget = _lead_result_char_budget(
        _fake_capability_registry(1_000_000, assumed=True), provider, "acme-1", 4
    )
    assert budget == (32_768 // 5) * 4 // 4
    # Registry without resolve_model: same fallback.
    budget = _lead_result_char_budget(object(), provider, "acme-1", 4)
    assert budget == (32_768 // 5) * 4 // 4


def test_lead_task_snapshot_preserves_structured_control_plane_for_93_techniques() -> None:
    from merlin_tui.bootstrap import _lead_task_rows

    technique_ids = tuple(
        f"merlin-injection-taxonomy/cat/multi-turn/technique-{index:03d}-long-identity"
        for index in range(93)
    )
    worker_tasks = tuple(
        SimpleNamespace(
            id=(
                "campaign/campaign-1/wave-1/dynamic-adversarial-specialist/"
                f"agenda-a1b2c3d4e5f6/worker-{index:03d}"
            ),
            status="completed",
            objective="Execute the reviewed technique with attributable evidence. " * 12,
            technique_ids=(technique_id,),
            result='Observed result with JSON-hostile text: "\\n". ' * 300,
        )
        for index, technique_id in enumerate(technique_ids)
    )
    coverage = [
        {
            "id": task.id,
            "status": task.status,
            "technique_ids": task.technique_ids,
            "has_committed_evidence": index < 12,
        }
        for index, task in enumerate(worker_tasks)
    ]
    evidence = [
        {
            "task_id": worker_tasks[index].id,
            "digest": f"sha256:{index:064x}",
            "reference": f"receipt:campaign-1/target-call-{index:03d}",
            "target_call_ids": [
                f"target-call:{index:03d}:request",
                f"target-call:{index:03d}:reply",
            ],
        }
        for index in range(12)
    ]
    histories = [
        {
            "id": f"history-{index:03d}",
            "task_id": worker_tasks[index % len(worker_tasks)].id,
            "state": "observed",
            "rationale": "low-priority historical prose " * 20,
        }
        for index in range(100)
    ]
    control_values = {
        "campaign_manifest_get": {
            "campaign_id": "campaign-1",
            "objective": "long reviewed objective " * 100,
            "profile": "recommended",
        },
        "coverage_ledger_get": coverage,
        "worker_status_get": {
            "counts": {"completed": 93},
            "active_task_ids": [task.id for task in worker_tasks],
        },
        "evidence_get": evidence,
        "finding_get": [
            {
                "id": "finding:001",
                "technique_id": technique_ids[0],
                "evidence_references": [evidence[0]["reference"]],
            }
        ],
        "attack_graph_get": {
            "prior_plan_updates": histories,
            "omitted_prior_plan_update_count": 7,
            "automation_requests": histories,
            "omitted_automation_request_count": 9,
            "technique_attempts": histories,
            "omitted_technique_attempt_count": 11,
        },
        "campaign_state_get": {
            "status": "executing",
            "waves": histories,
            "omitted_wave_count": 3,
            "directives": histories,
            "omitted_directive_count": 5,
        },
        "specialist_composition_get": {
            "sealed_technique_ids": technique_ids,
            "composition_pending": True,
            "initial_composition_request": "verbose composition instructions " * 200,
        },
    }
    control_tasks = tuple(
        SimpleNamespace(
            id=f"campaign-1/lead-control/{name}",
            status="completed",
            objective="Read-only Campaign Lead control-plane projection",
            technique_ids=(),
            result=json.dumps(value),
        )
        for name, value in control_values.items()
    )
    tasks = (*worker_tasks, *control_tasks)

    snapshot = _lead_task_rows(
        tasks,
        campaign_id="campaign-1",
        profile="recommended",
        attempt=0,
        per_result_chars=704,
    )
    assert "terminal_task_manifest" not in snapshot
    rows = snapshot["tasks"]
    payload = json.dumps(
        {
            "campaign_id": "campaign-1",
            "profile": "recommended",
            "attempt": 0,
            "tasks": rows,
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    assert len(payload.encode("utf-8")) <= 65_536
    assert tuple(row["id"] for row in rows[:93]) == tuple(task.id for task in worker_tasks)
    assert tuple(row["technique_ids"][0] for row in rows[:93]) == technique_ids
    assert all(row["objective"] for row in rows)
    projected = {row["id"].rsplit("/", 1)[-1]: row["result"] for row in rows[93:]}
    composition = projected["specialist_composition_get"]
    assert isinstance(composition, dict)
    assert tuple(composition["sealed_technique_ids"]) == technique_ids
    assert composition["initial_composition_required"] is True
    projected_evidence = projected["evidence_get"]
    assert projected_evidence == evidence
    graph = projected["attack_graph_get"]
    assert graph["prior_plan_updates"] == []
    assert graph["omitted_prior_plan_update_count"] == 107
    assert graph["omitted_automation_request_count"] == 109
    assert graph["omitted_technique_attempt_count"] == 111


def test_lead_task_snapshot_demotes_oldest_terminal_tasks_to_digest_manifest() -> None:
    from merlin_tui.bootstrap import _lead_task_rows

    campaign_id = "campaign/09ff0c4b-a48a-47fb-9f6e-2ba280a0d09d"
    worker_tasks = tuple(
        SimpleNamespace(
            id=(
                f"{campaign_id}/wave-{1 + index // 100}/"
                f"adversarial-explorer-{index % 40}/goat-{index:016x}"
            ),
            status="completed" if index % 5 else "failed",
            objective="Execute the reviewed technique with attributable evidence. " * 6,
            technique_ids=(
                f"merlin-injection-taxonomy/cat/multi-turn/technique-{index:03d}-long-identity",
            ),
            result="Observed target exchange with attributable receipts. " * 40,
        )
        for index in range(640)
    )
    active_task = SimpleNamespace(
        id=f"{campaign_id}/wave-7/adversarial-explorer-7/goat-ffffffffffffffff",
        status="running",
        objective="Currently executing specialist.",
        technique_ids=("merlin-injection-taxonomy/cat/multi-turn/technique-active",),
        result="",
    )
    control_task = SimpleNamespace(
        id=f"{campaign_id}/lead-control/coverage_ledger_get",
        status="completed",
        objective="Read-only Campaign Lead control-plane projection",
        technique_ids=(),
        result=json.dumps(
            [
                {
                    "id": task.id,
                    "status": task.status,
                    "technique_ids": task.technique_ids,
                    "has_committed_evidence": False,
                }
                for task in worker_tasks
            ]
        ),
    )
    tasks = (*worker_tasks, active_task, control_task)

    snapshot = _lead_task_rows(
        tasks,
        campaign_id=campaign_id,
        profile="exhaustive",
        attempt=2,
        per_result_chars=102,
    )
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))

    assert len(payload.encode("utf-8")) <= 65_536
    manifest = snapshot["terminal_task_manifest"]
    assert manifest["omitted_terminal_task_count"] > 0
    assert manifest["id_digest"].startswith("sha256:")
    assert set(manifest["status_counts"]) <= {"completed", "failed"}
    assert sum(manifest["status_counts"].values()) == manifest["omitted_terminal_task_count"]
    rows = snapshot["tasks"]
    projected_ids = {row["id"] for row in rows}
    # Active and control-plane rows are never demoted, and the newest terminal
    # rows survive in full with their exact identities.
    assert active_task.id in projected_ids
    assert control_task.id in projected_ids
    assert worker_tasks[-1].id in projected_ids
    assert worker_tasks[0].id not in projected_ids
    assert len(projected_ids) + manifest["omitted_terminal_task_count"] == len(tasks)


def test_lead_task_snapshot_compacts_oversized_specialist_ownership_projection() -> None:
    from merlin_tui.bootstrap import _lead_task_rows

    technique_ids = tuple(
        f"case:family-{index % 69:02d}@lang=language-{index:05d}@variant=mixed"
        for index in range(12_765)
    )
    control_task = SimpleNamespace(
        id="campaign-1/lead-control/specialist_composition_get",
        status="completed",
        objective="Read-only Campaign Lead control-plane projection",
        technique_ids=(),
        result=json.dumps(
            {
                "specialist_composition": "autonomous",
                "composition_pending": True,
                "sealed_technique_ids": technique_ids,
                "orphaned_technique_ids": technique_ids,
                "double_owned_technique_ids": technique_ids,
                "initial_composition_request": "cover every sealed id exactly once",
            }
        ),
    )

    snapshot = _lead_task_rows(
        (control_task,),
        campaign_id="campaign-1",
        profile="recommended",
        attempt=0,
        per_result_chars=65_536,
    )

    assert len(json.dumps(snapshot, separators=(",", ":")).encode("utf-8")) <= 65_536
    composition = snapshot["tasks"][0]["result"]
    assert "sealed_technique_ids" not in composition
    assert "orphaned_technique_ids" not in composition
    assert "double_owned_technique_ids" not in composition
    assert composition["sealed_technique_manifest"]["count"] == 12_765
    assert composition["sealed_technique_manifest"]["family_count"] == 69
    assert len(composition["sealed_technique_manifest"]["sample_ids"]) == 8
    assert composition["sealed_technique_manifest"]["digest"].startswith("sha256:")
    assert composition["initial_composition_required"] is False
    assert composition["initial_composition_deferred"] is True


def test_authenticated_browser_profile_treats_inferred_anonymous_label_as_advisory() -> None:
    assert (
        _validated_browser_authentication(
            "Anonymous device-token auth because no credential handle was supplied",
            "shared_persistent_authenticated",
        )
        == "authenticated browser profile"
    )

    assert (
        _validated_browser_authentication(
            "Signed-in LU browser session with an auto-issued device token",
            "shared_persistent_authenticated",
        )
        == "Signed-in LU browser session with an auto-issued device token"
    )
    assert _validated_browser_authentication("Anonymous public session", None) == (
        "Anonymous public session"
    )


def test_browser_exchange_projection_pairs_redacted_submit_with_response_witness() -> None:
    now = datetime.now(UTC)
    context = SimpleNamespace(context_id="context-1")
    actions = (
        SimpleNamespace(
            kind="fill",
            step=3,
            created_at=now,
            evidence_refs=("input-ref",),
            value="Check this policy; token=SECRET",
        ),
        SimpleNamespace(
            kind="press", step=4, created_at=now, evidence_refs=("submit-ref",), value="Enter"
        ),
    )
    observations = (
        SimpleNamespace(
            kind="snapshot",
            step=4,
            observed_at=now,
            evidence_refs=("reply-ref",),
            text="FareCare response completed",
        ),
    )

    rows = _browser_exchange_rows(context, actions, observations)

    assert len(rows) == 1
    assert rows[0]["id"].startswith("browser:context-1:sha256:")
    assert rows[0] == {
        "id": rows[0]["id"],
        "request": {
            "content": "Check this policy; token=SECRET",
            "evidence_refs": ("input-ref", "submit-ref"),
            "submit_action": "press",
        },
        "response": {
            "text": "FareCare response completed",
            "evidence_refs": ("reply-ref",),
        },
        "response_kind": "browser_observation",
        "outcome": "observed",
        "request_at": now.isoformat(),
        "response_at": now.isoformat(),
    }


def test_browser_exchange_projection_ids_are_unique_when_worker_steps_restart() -> None:
    first = datetime(2026, 9, 1, 1, tzinfo=UTC)
    second = datetime(2026, 9, 1, 2, tzinfo=UTC)
    context = SimpleNamespace(context_id="shared-context")
    actions = tuple(
        item
        for timestamp, suffix in ((first, "one"), (second, "two"))
        for item in (
            SimpleNamespace(
                kind="fill",
                step=3,
                created_at=timestamp,
                evidence_refs=(f"input-{suffix}",),
                value="[redacted]",
            ),
            SimpleNamespace(
                kind="press",
                step=4,
                created_at=timestamp,
                evidence_refs=(f"submit-{suffix}",),
                value="Enter",
            ),
        )
    )

    rows = _browser_exchange_rows(context, actions, ())

    assert len(rows) == 2
    assert len({row["id"] for row in rows}) == 2


def _validator_payload(*call_ids: str) -> dict[str, object]:
    return {
        "target_session_id": "api:campaign/one",
        "worker_checkpoint": {"messages": [{"tool_call_id": call_id} for call_id in call_ids]},
    }


def _effect(call_id: str, *, attempt_id: str = "attempt-1", target_id: str = "target-1"):
    return ToolEffectRecord(
        idempotency_key=f"effect-{call_id}",
        session_id="session-1",
        call_id=call_id,
        agent_id="campaign/one/validator",
        attempt_id=attempt_id,
        campaign_id="campaign/one",
        target_id=target_id,
        tool_name="target_request",
        effect="target_network",
        arguments_digest="a" * 64,
        state=ToolEffectState.ACKNOWLEDGED,
        result_content="response",
        result_is_error=False,
        result_metadata={
            "target_profile_id": target_id,
            "target_session_id": "api:campaign/one",
            "target_exchanges": [
                {
                    "id": "turn-1",
                    "interaction": True,
                    "cleanup": False,
                    "request": "probe",
                    "response": "observed",
                }
            ],
        },
    )


def test_validator_exchange_witness_requires_exact_attempt_campaign_target_and_call() -> None:
    row = {
        "attempt_id": "attempt-1",
        "node_id": "worker:campaign/one/validator",
        "payload_digest": "b" * 64,
    }
    effect = _effect("call-1")
    witnesses = _validator_exchange_witnesses(
        schedule_id="schedule-1",
        result_row=row,
        result_payload=_validator_payload("call-1"),
        finding_target_id="target-1",
        campaign_id="campaign/one",
        session_id="session-1",
        effects=(effect,),
        source="independent-validator:schedule-1:attempt-1",
    )
    assert len(witnesses) == 1
    assert "effect_id=effect-call-1" in witnesses[0].reference
    for rejected in (
        _effect("call-1", attempt_id="attempt-2"),
        _effect("call-1", target_id="target-2"),
        effect.model_copy(update={"campaign_id": "campaign/two"}),
    ):
        assert not _validator_exchange_witnesses(
            schedule_id="schedule-1",
            result_row=row,
            result_payload=_validator_payload("call-1"),
            finding_target_id="target-1",
            campaign_id="campaign/one",
            session_id="session-1",
            effects=(rejected,),
            source="independent-validator:schedule-1:attempt-1",
        )
    assert not _validator_exchange_witnesses(
        schedule_id="schedule-1",
        result_row=row,
        result_payload=_validator_payload("another-call"),
        finding_target_id="target-1",
        campaign_id="campaign/one",
        session_id="session-1",
        effects=(effect,),
        source="independent-validator:schedule-1:attempt-1",
    )


def test_browser_validator_requires_fill_submit_and_response_receipts() -> None:
    row = {
        "attempt_id": "attempt-1",
        "node_id": "worker:campaign/one/validator",
        "payload_digest": "b" * 64,
    }
    effects = tuple(
        _effect(call_id).model_copy(
            update={
                "tool_name": tool_name,
                "result_content": json.dumps(
                    {
                        "context_id": "browser-context-1",
                        "tab_ref": "tab-1",
                        "step": step,
                        "target_id": "target-1",
                        "kind": "snapshot",
                        "text": "reply",
                    }
                ),
                "result_metadata": {
                    "target": "target-1",
                    "target_session_id": "api:campaign/one",
                    "browser_action": {
                        "kind": action_kind,
                        "value": action_value,
                    },
                },
            }
        )
        for call_id, tool_name, step, action_kind, action_value in (
            ("fill", "browser_fill", 1, "fill", "[redacted]"),
            ("submit", "browser_press", 2, "press", "Enter"),
            ("response", "browser_snapshot", 3, "snapshot", None),
        )
    )
    witnesses = _validator_exchange_witnesses(
        schedule_id="schedule-1",
        result_row=row,
        result_payload=_validator_payload("fill", "submit", "response"),
        finding_target_id="target-1",
        campaign_id="campaign/one",
        session_id="session-1",
        effects=effects,
        source="independent-validator:schedule-1:attempt-1",
    )
    assert len(witnesses) == 1
    assert "request_effect_id=effect-fill" in witnesses[0].reference
    assert "target_action_effect_id=effect-submit" in witnesses[0].reference
    click_effects = (
        effects[0],
        effects[1].model_copy(
            update={
                "tool_name": "browser_click",
                "result_metadata": {
                    **effects[1].result_metadata,
                    "browser_action": {"kind": "click", "value": "button:send"},
                },
            }
        ),
        effects[2],
    )
    click_witnesses = _validator_exchange_witnesses(
        schedule_id="schedule-1",
        result_row=row,
        result_payload=_validator_payload("fill", "submit", "response"),
        finding_target_id="target-1",
        campaign_id="campaign/one",
        session_id="session-1",
        effects=click_effects,
        source="independent-validator:schedule-1:attempt-1",
    )
    assert len(click_witnesses) == 1
    assert "target_action_effect_id=effect-submit" in click_witnesses[0].reference
    assert not _validator_exchange_witnesses(
        schedule_id="schedule-1",
        result_row=row,
        result_payload=_validator_payload("response"),
        finding_target_id="target-1",
        campaign_id="campaign/one",
        session_id="session-1",
        effects=(effects[-1],),
        source="independent-validator:schedule-1:attempt-1",
    )


@pytest.mark.parametrize(
    "reference",
    (
        "merlin-result://validator",
        "merlin-exchange://validator/not-empty",
        "merlin-exchange://validator?schedule_id=not-a-uuid",
        "merlin-exchange://other",
    ),
)
def test_validator_evidence_verifier_fails_closed_for_malformed_references(
    reference: str,
) -> None:
    witness = EvidenceWitness(
        reference=reference,
        digest="sha256:" + "a" * 64,
        source="independent-validator:forged:forged:forged",
    )
    unavailable = SimpleNamespace()

    assert not _verify_validator_evidence(
        witness,
        "session-1",
        "target-1",
        scheduler_store=unavailable,
        effect_repository=unavailable,
    )


def test_validator_evidence_verifier_rejects_cross_session_schedule() -> None:
    schedule_id, attempt_id = str(uuid4()), str(uuid4())
    digest = "sha256:" + "a" * 64
    query = urlencode(
        {
            "schedule_id": schedule_id,
            "node_id": "worker:validator",
            "attempt_id": attempt_id,
            "result_digest": digest,
            "effect_id": "effect-1",
            "call_id": "call-1",
            "exchange_id": "exchange-1",
            "exchange_digest": digest,
            "target_id": "target-1",
            "campaign_id": "campaign-1",
            "target_session_id": "target-session-1",
            "digest": digest,
        }
    )
    witness = EvidenceWitness(
        reference=f"merlin-exchange://validator?{query}",
        digest=digest,
        source=f"independent-validator:{schedule_id}:{attempt_id}:{'a' * 64}",
    )
    scheduler = SimpleNamespace(load_schedule=lambda _schedule_id: {"session_id": "other-session"})

    assert not _verify_validator_evidence(
        witness,
        "session-1",
        "target-1",
        scheduler_store=scheduler,
        effect_repository=SimpleNamespace(),
    )


def test_bootstrap_registers_browser_tools_with_read_and_mutating_effects() -> None:
    class Broker:
        def __init__(self):
            self.tools = []

        def register(self, tool):
            self.tools.append(tool)

    class Service:
        async def for_tool(self, session_id):
            raise AssertionError(session_id)

    broker = Broker()
    names = _register_browser_tools(broker, Service(), object())
    assert names == (
        "browser_open_tab",
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_fill",
        "browser_press",
        "browser_select",
        "browser_check",
        "browser_uncheck",
        "browser_hover",
        "browser_wait_for_text",
        "browser_network_events",
        "browser_upload",
        "browser_close_tab",
        "browser_release",
        "browser_end_owned_session",
        "browser_recall_reconnaissance",
        "browser_record_api_handoff",
        "browser_compile_api_handoff",
    )
    assert {tool.effect for tool in broker.tools} == {
        "target_read",
        "target_network",
        "workspace_read",
        "harness_state",
    }
    assert broker.tools[-1].effect == "harness_state"


def test_browser_exchange_projection_uses_submission_time_not_worker_step():
    from datetime import timedelta

    now = datetime.now(UTC)

    def action(kind, seconds, value, step=1):
        return SimpleNamespace(
            kind=kind,
            created_at=now + timedelta(seconds=seconds),
            value=value,
            step=step,
            evidence_refs=(),
        )

    def observation(kind, seconds, text, step=100):
        return SimpleNamespace(
            kind=kind,
            observed_at=now + timedelta(seconds=seconds),
            text=text,
            step=step,
            evidence_refs=(),
        )

    rows = _browser_exchange_rows(
        SimpleNamespace(context_id="ctx"),
        (
            action("fill", 10, "first"),
            action("press", 11, "Enter"),
            action("fill", 20, "second"),
            action("press", 21, "Enter"),
        ),
        (
            observation("navigation", 0, "https://target/robots.txt"),
            observation("snapshot", 5, "old snapshot"),
            observation("navigation", 12, "https://target/chat"),
            observation("snapshot", 22, "second response", step=0),
        ),
    )
    assert rows[0]["outcome"] == "response_not_observed"
    assert rows[0]["response"]["text"] == ""
    assert rows[1]["response"]["text"] == "second response"
    assert rows[1]["response_at"] == (now + timedelta(seconds=22)).isoformat()
