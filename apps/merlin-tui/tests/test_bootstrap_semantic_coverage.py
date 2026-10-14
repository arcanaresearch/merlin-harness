import hashlib
import json
from types import SimpleNamespace

import pytest
from merlin_contracts import TechniqueLanguageEvidence
from merlin_findings import DurableReportService
from merlin_tui.bootstrap import (
    _apply_pair_verdict,
    _canonical_report_candidate_projections,
    _current_terminal_report_authority,
    _fold_attempted_technique_outcome,
    _language_evidence_disagreement,
    _language_observation_for_technique,
    _model_transport,
    _require_terminal_report_authority,
    _sealed_report_technique_ids,
    _semantic_coverage,
    _semantic_unsearched_frontier,
    _transform_payload_receipt_chain,
)


class _ReportRepository:
    def __init__(self) -> None:
        self.values = {}

    def next_revision(self, _campaign_id):
        return len(self.values) + 1

    def save(self, projection):
        self.values[projection.revision_id] = projection

    def get(self, revision_id):
        return self.values.get(revision_id)

    def list(self, campaign_id):
        return tuple(item for item in self.values.values() if item.campaign_id == campaign_id)


@pytest.mark.asyncio
async def test_terminal_report_authority_uses_normalized_frontier_snapshot() -> None:
    calls = []

    class Campaigns:
        async def status(
            self, campaign_id: str, *, seal_frontier_snapshot: bool = False
        ) -> dict[str, object]:
            calls.append(seal_frontier_snapshot)
            return {
                "campaign_id": campaign_id,
                "completion_document": {
                    "schema": (
                        "merlin.campaign-completion.v2"
                        if seal_frontier_snapshot
                        else "merlin.campaign-completion.live.v2"
                    )
                },
            }

    authority = await _current_terminal_report_authority(Campaigns(), "campaign/example")

    assert calls == [True]
    assert authority["completion_document"] == {"schema": "merlin.campaign-completion.v2"}


@pytest.mark.asyncio
async def test_large_frontier_report_authority_is_idempotent_and_rejects_late_evidence() -> None:
    campaign_id = "campaign/large-frontier"
    snapshot_id = "frontier-final/large"
    rows = tuple(
        {
            "position": position,
            "candidate_id": f"candidate/{position}",
            "state": "queued",
            "outcome": "unattempted",
            "attempt_id": None,
            "task_id": None,
            "evidence_refs": [],
            "disposition_reason": None,
        }
        for position in range(12_765)
    )
    counts = {
        "states": {"queued": 12_765, "claimed": 0, "scheduled": 0, "terminal": 0},
        "outcomes": {
            "confirmed": 0,
            "refuted": 0,
            "partial": 0,
            "unclear": 0,
            "blocked": 0,
            "retryable": 0,
            "unattempted": 12_765,
        },
    }
    row_digest = hashlib.sha256(
        json.dumps(
            {"schema_version": 1, "rows": rows},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    snapshot = {
        "id": snapshot_id,
        "kind": "final",
        "wave": None,
        "row_digest": row_digest,
        "total_count": len(rows),
        "counts": counts,
        "reference": f"merlin-frontier-snapshot:{campaign_id}:{snapshot_id}",
        "sample": rows[:2],
    }
    document = {
        "schema": "merlin.campaign-completion.v2",
        "campaign_id": campaign_id,
        "manifest_digest": "sha256:" + "a" * 64,
        "target_id": "target/large",
        "profile": "quickscan",
        "frontier_snapshot": snapshot,
        "waves": (),
        "receipts": (),
        "findings": (),
        "cleanup": {
            "expected": (),
            "states": (),
            "authority": {"status": "acknowledged"},
        },
        "saturation": {"ready": True},
    }

    def digest(value) -> str:
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )

    completion_digest = digest(document)
    receipt = {
        "kind": "campaign_completion",
        "schema": "merlin.campaign-completion.v2",
        "completion_digest": completion_digest,
        "terminal_status": "completed",
        "stop_reason": "frontier_exhausted",
        "document": document,
    }
    receipt_digest = digest(receipt)

    class Snapshots:
        def get_frontier_completion_snapshot(self, requested_campaign_id, requested_snapshot_id):
            assert (requested_campaign_id, requested_snapshot_id) == (campaign_id, snapshot_id)
            return snapshot

        def frontier_completion_counts(self, requested_campaign_id, requested_snapshot_id):
            return self.get_frontier_completion_snapshot(
                requested_campaign_id, requested_snapshot_id
            )

        def list_frontier_completion_page(
            self,
            requested_campaign_id,
            requested_snapshot_id,
            *,
            limit=100,
            after_position=-1,
        ):
            assert (requested_campaign_id, requested_snapshot_id) == (campaign_id, snapshot_id)
            return rows[after_position + 1 : after_position + 1 + limit]

    class Repository:
        def __init__(self) -> None:
            self.values = {}

        def next_revision(self, _campaign_id):
            return len(self.values) + 1

        def save(self, projection):
            self.values[projection.revision_id] = projection

        def get(self, revision_id):
            return self.values.get(revision_id)

        def list(self, requested_campaign_id):
            return tuple(
                item for item in self.values.values() if item.campaign_id == requested_campaign_id
            )

        def find_by_completion_digest(self, requested_campaign_id, requested_digest):
            return next(
                (
                    item
                    for item in self.list(requested_campaign_id)
                    if item.completion_digest == requested_digest
                ),
                None,
            )

    class Campaigns:
        late_evidence = False

        async def status(self, requested_campaign_id: str, *, seal_frontier_snapshot: bool = False):
            assert requested_campaign_id == campaign_id
            assert seal_frontier_snapshot is True
            current = document
            if self.late_evidence:
                current = {**document, "receipts": ({"kind": "late-evidence"},)}
            return {
                "campaign_id": campaign_id,
                "status": "completed",
                "stop_reason": "frontier_exhausted",
                "completion_digest": digest(current),
                "completion_document": current,
            }

    campaigns = Campaigns()
    repository = Repository()
    reports = DurableReportService(repository, frontier_snapshot_provider=Snapshots())

    for _ in range(2):
        authority = await _current_terminal_report_authority(campaigns, campaign_id)
        _require_terminal_report_authority(
            campaign_id,
            "frontier_exhausted",
            authority,
            completion_digest=completion_digest,
            cleanup_status="completed",
            report_state="complete",
        )
        projection = reports.create(
            campaign_id,
            stop_reason="frontier_exhausted",
            cleanup_status="completed",
            report_state="complete",
            findings=(),
            tasks=(),
            evidence=(),
            usage={},
            metadata={
                "manifest_digest": document["manifest_digest"],
                "profile": document["profile"],
            },
            completion_document=receipt,
            completion_digest=receipt_digest,
        )

    assert projection.revision == 1
    assert len(repository.list(campaign_id)) == 1

    campaigns.late_evidence = True
    changed = await _current_terminal_report_authority(campaigns, campaign_id)
    with pytest.raises(RuntimeError, match="campaign evidence changed"):
        _require_terminal_report_authority(
            campaign_id,
            "frontier_exhausted",
            changed,
            completion_digest=completion_digest,
            cleanup_status="completed",
            report_state="complete",
        )


def test_campaign_report_projects_unique_candidates_and_repeated_attempts() -> None:
    projections = _canonical_report_candidate_projections(
        (
            SimpleNamespace(
                arm_id="arm/one",
                evidence_refs=("target-call:1",),
                attempt_disposition="valid",
                outcome_authority="target",
                outcome="authoritative_success",
            ),
            SimpleNamespace(
                arm_id="arm/one",
                evidence_refs=("target-call:2",),
                attempt_disposition="valid",
                outcome_authority="target",
                outcome="authoritative_success",
            ),
            SimpleNamespace(
                arm_id="arm/two",
                evidence_refs=("target-call:3",),
                attempt_disposition="valid",
                outcome_authority="target",
                outcome="clean_negative",
            ),
        )
    )
    service = DurableReportService(_ReportRepository())

    report = service.create(
        "campaign/report-counts",
        stop_reason="operator_stopped",
        cleanup_status="completed",
        findings=(),
        tasks=(),
        evidence=(),
        usage={},
        metadata={"candidate_outcome_projections": projections},
    )

    assert report.metadata["attempt_count"] == 3
    assert report.metadata["unique_candidate_count"] == 2
    assert report.metadata["repeat_attempt_count"] == 1
    assert report.metadata["terminal_unique_candidate_count"] == 2
    with pytest.raises(ValueError, match="attempt_count conflicts"):
        service.create(
            "campaign/report-mismatch",
            stop_reason="operator_stopped",
            cleanup_status="completed",
            findings=(),
            tasks=(),
            evidence=(),
            usage={},
            metadata={
                "candidate_outcome_projections": projections,
                "attempt_count": 99,
            },
        )


def test_campaign_report_preserves_persisted_canonical_settlement() -> None:
    from merlin_contracts import candidate_id_for_identity, project_candidate_outcome

    identity = {
        "campaign_id": "campaign/one",
        "target_id": "target/one",
        "target_generation": "generation/one",
        "scenario_fingerprint": "sha256:scenario",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "payload_family": "baseline",
        "requested_language": "en",
        "mutation_id": "baseline",
    }
    projection = project_candidate_outcome(
        candidate_id=candidate_id_for_identity(identity),
        attempt_disposition="valid",
        outcome_authority="target",
        outcome="confirmed",
        evidence_digest="a" * 64,
    )
    outcome = SimpleNamespace(
        arm_id="arm/presentation-only",
        evidence_refs=("target-call:1",),
        attempt_disposition="valid",
        outcome_authority="target",
        outcome="authoritative_success",
        usage={
            "candidate_outcome_projection": projection,
            "canonical_candidate_identity": identity,
        },
    )

    assert _canonical_report_candidate_projections((outcome,)) == (
        {**projection, "candidate_identity": identity},
    )
    with pytest.raises(ValueError, match="canonical attempt ledger"):
        _canonical_report_candidate_projections(
            (SimpleNamespace(**{**vars(outcome), "outcome": "clean_negative"}),)
        )


def test_report_language_evidence_treats_source_variation_as_provenance() -> None:
    durable = TechniqueLanguageEvidence(
        campaign_id="campaign/one",
        technique_attempt_id="attempt/one",
        requested_language="hi",
        actual_language="hi",
        language_evidence_status="observed",
        language_evidence_source="committed-target-delivery.v1",
        evidence_refs=("effect:delivery",),
    )
    report_observation = durable.model_copy(
        update={
            "language_evidence_source": "committed-target-request-detector.v1",
            "evidence_refs": ("effect:report",),
        }
    )

    assert _language_evidence_disagreement(durable, report_observation) is None


def test_report_language_evidence_records_detector_disagreement_as_uncertain() -> None:
    durable = TechniqueLanguageEvidence(
        campaign_id="campaign/one",
        technique_attempt_id="attempt/one",
        requested_language="la",
        actual_language="fr",
        language_evidence_status="observed",
        language_evidence_source="committed-target-delivery.v1",
        evidence_refs=("effect:delivery",),
    )
    report_observation = durable.model_copy(
        update={
            "actual_language": "la",
            "language_evidence_source": "committed-target-request-detector.v1",
            "evidence_refs": ("effect:report",),
        }
    )

    uncertainty = _language_evidence_disagreement(durable, report_observation)

    assert uncertainty is not None
    assert uncertainty["kind"] == "evaluation_uncertain"
    assert uncertainty["source"] == "report.language-observation"
    assert uncertainty["disagreements"] == {
        "actual_language": {"durable": "fr", "report_observation": "la"}
    }


def test_report_publication_requires_current_terminal_campaign_authority() -> None:
    completion_document = {"cleanup": {"authority": {"status": "acknowledged"}}}
    completion_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(completion_document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    authority = {
        "id": "campaign/example",
        "status": "completed",
        "stop_reason": "frontier_exhausted",
        "completion_digest": completion_digest,
        "completion_document": completion_document,
    }
    _require_terminal_report_authority(
        "campaign/example",
        "frontier_exhausted",
        authority,
        completion_digest=completion_digest,
        cleanup_status="completed",
        report_state="complete",
    )

    with pytest.raises(RuntimeError, match="not terminal"):
        _require_terminal_report_authority(
            "campaign/example",
            "frontier_exhausted",
            {**authority, "status": "executing", "stop_reason": ""},
            completion_digest=completion_digest,
        )
    with pytest.raises(RuntimeError, match="stop reason changed"):
        _require_terminal_report_authority(
            "campaign/example",
            "frontier_exhausted",
            {
                **authority,
                "stop_reason": "infrastructure_blocked_after_retry_policy",
            },
            completion_digest=completion_digest,
        )
    with pytest.raises(RuntimeError, match="completed cleanup"):
        _require_terminal_report_authority(
            "campaign/example",
            "frontier_exhausted",
            authority,
            completion_digest=completion_digest,
            cleanup_status="pending",
            report_state="complete",
        )


def test_report_publication_rejects_missing_live_completion_digest() -> None:
    completion_document = {"cleanup": {"authority": {"status": "acknowledged"}}}
    completion_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(completion_document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    authority = {
        "id": "campaign/example",
        "status": "completed",
        "stop_reason": "frontier_exhausted",
        "completion_document": completion_document,
    }

    with pytest.raises(RuntimeError, match="campaign evidence changed"):
        _require_terminal_report_authority(
            "campaign/example",
            "frontier_exhausted",
            authority,
            completion_digest=completion_digest,
            cleanup_status="completed",
            report_state="complete",
        )


def test_report_publication_rejects_current_unresolved_cleanup() -> None:
    completion_document = {"cleanup": {"authority": {"status": "unresolved"}}}
    completion_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(completion_document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    authority = {
        "id": "campaign/example",
        "status": "completed",
        "stop_reason": "frontier_exhausted",
        "completion_digest": completion_digest,
        "completion_document": completion_document,
    }

    with pytest.raises(RuntimeError, match="cleanup is unresolved"):
        _require_terminal_report_authority(
            "campaign/example",
            "frontier_exhausted",
            authority,
            completion_digest=completion_digest,
            cleanup_status="completed",
            report_state="complete",
        )


def test_report_scope_uses_only_recon_sealed_applicable_techniques() -> None:
    lead = (
        {
            "payload": {
                "kind": "recon_applicability_sealed",
                "applicable_technique_ids": ("applicable",),
                "inapplicable_technique_ids": ("excluded",),
                "partition_digest": "sha256:partition",
            }
        },
    )

    assert _sealed_report_technique_ids(lead) == frozenset({"applicable"})


def test_non_default_model_without_override_uses_openai_compatible_transport() -> None:
    assert _model_transport("deepseek-v4-pro", {}) == "open_ai_compatible"
    assert _model_transport("lead-model", {"lead-model": "codex_app_server"}) == "codex_app_server"


@pytest.mark.parametrize(
    ("verdict", "expected"),
    (
        ("positive", "confirmed"),
        ("negative", "negative"),
        ("ambiguous", "unclear"),
        ("blocked", "blocked"),
    ),
)
def test_pair_receipt_dominates_contradictory_worker_outcome(verdict: str, expected: str) -> None:
    technique = {"id": "pair", "semantic_id": "pair", "outcome": "confirmed"}
    _apply_pair_verdict(technique, {"verdict": verdict})

    assert technique["outcome"] == expected
    coverage = _semantic_coverage([technique])
    expected_bucket = "negative" if expected == "negative" else expected
    assert coverage[expected_bucket] == 1


def test_attempted_outcome_does_not_contaminate_sibling_cell_on_same_worker() -> None:
    rows = [
        {"id": "cell-a", "task_id": "worker:shared", "outcome": "attempted"},
        {"id": "cell-b", "task_id": "worker:shared", "outcome": "attempted"},
    ]

    _fold_attempted_technique_outcome(
        rows,
        technique_id="cell-a",
        node_id="worker:shared",
        outcome="confirmed",
    )

    assert rows[0]["outcome"] == "confirmed"
    assert rows[1]["outcome"] == "attempted"


def test_semantic_coverage_groups_variants_but_keeps_conservative_outcome() -> None:
    rows = [
        {
            "id": "policy@lang=en@variant=native",
            "semantic_id": "policy",
            "outcome": "negative",
        },
        {
            "id": "policy@lang=es@variant=translated",
            "semantic_id": "policy",
            "outcome": "unclear",
        },
        {"id": "leak", "semantic_id": "leak", "outcome": "confirmed"},
    ]

    assert _semantic_coverage(rows) == {
        "planned": 2,
        "attempted": 2,
        "confirmed": 1,
        "negative": 0,
        "unclear": 1,
        "blocked": 0,
        "untested": 0,
    }


def test_semantic_coverage_does_not_hide_conflict_or_incomplete_language_cell() -> None:
    assert (
        _semantic_coverage(
            [
                {"id": "a", "semantic_id": "policy", "outcome": "confirmed"},
                {"id": "b", "semantic_id": "policy", "outcome": "negative"},
            ]
        )["unclear"]
        == 1
    )


def test_cancelled_campaign_counts_attempted_without_result_as_unclear() -> None:
    rows = [
        {"id": "attempted", "semantic_id": "attempted", "outcome": "attempted"},
        *(
            {"id": f"blocked-{index}", "semantic_id": f"blocked-{index}", "outcome": "blocked"}
            for index in range(15)
        ),
    ]

    assert _semantic_coverage(rows) == {
        "planned": 16,
        "attempted": 1,
        "confirmed": 0,
        "negative": 0,
        "unclear": 1,
        "blocked": 15,
        "untested": 0,
    }


def test_transform_payload_receipt_rejects_navigation_and_accepts_exact_api_interaction() -> None:
    binding = ["policy@lang=es@variant=translated", "sha256:spec", "sha256:envelope"]

    def effect(tool: str, metadata: dict[str, object]) -> SimpleNamespace:
        return SimpleNamespace(
            agent_id="campaign/one/direct",
            campaign_id="campaign/one",
            target_id="target-1",
            state="acknowledged",
            result_is_error=False,
            result_metadata=metadata,
            tool_name=tool,
            attempt_id="attempt-1",
            call_id="payload-call",
            result_content="{}",
            arguments_digest="payload",
            idempotency_key=f"effect:{tool}",
            created_at=None,
        )

    navigation = effect("browser_open_tab", {})
    interaction = effect(
        "target_request",
        {
            "transform_bindings": [binding],
            "concrete_payload_arguments_digest": "sha256:payload",
            "target_session_id": "target-session-1",
            "target": "target-1",
            "target_exchanges": [{"id": "1", "interaction": True, "cleanup": False}],
        },
    )
    values = {
        "task_ids": ("campaign/one/direct",),
        "technique_id": binding[0],
        "spec_digest": binding[1],
        "envelope_prompt_digest": binding[2],
        "campaign_id": "campaign/one",
        "target_id": "target-1",
        "committed_receipts": {
            "campaign/one/direct": (
                "attempt-1",
                frozenset({"payload-call"}),
                "target-session-1",
            )
        },
    }

    assert _transform_payload_receipt_chain((navigation,), **values) == ()
    assert _transform_payload_receipt_chain((navigation, interaction), **values) == (interaction,)
    values["task_ids"] = ("worker:campaign/one/direct",)
    values["committed_receipts"] = {
        "worker:campaign/one/direct": (
            "attempt-1",
            frozenset({"payload-call"}),
            "target-session-1",
        )
    }
    assert _transform_payload_receipt_chain((interaction,), **values) == (interaction,)
    interaction.call_id = "stale-call"
    assert _transform_payload_receipt_chain((interaction,), **values) == ()
    interaction.call_id = "payload-call"
    interaction.attempt_id = "attempt-stale"
    assert _transform_payload_receipt_chain((interaction,), **values) == ()
    interaction.attempt_id = "attempt-1"
    interaction.result_metadata["transform_bindings"] = [
        [binding[0], binding[1], "sha256:wrong-envelope"]
    ]
    assert _transform_payload_receipt_chain((interaction,), **values) == ()
    assert (
        _semantic_coverage(
            [
                {"id": "a", "semantic_id": "policy", "outcome": "confirmed"},
                {"id": "b", "semantic_id": "policy", "outcome": "blocked"},
            ]
        )["unclear"]
        == 1
    )


def test_language_observation_is_bound_to_committed_target_attempt() -> None:
    attempt_id = "d5fe03e3-f94c-41a6-8dd4-f723f9470f96"
    effect = SimpleNamespace(
        agent_id="campaign/one/direct",
        campaign_id="campaign/one",
        target_id="target-1",
        state="acknowledged",
        result_is_error=False,
        result_metadata={
            "technique_attempt_id": attempt_id,
            "target_session_id": "target-session-1",
            "target_exchanges": [
                {
                    "interaction": True,
                    "cleanup": False,
                    "request": "既已行旅，而求追偿。请依既定之策审其可否，不得妄许。",
                }
            ],
        },
        attempt_id="scheduler-attempt-1",
        call_id="payload-call",
        idempotency_key="effect:language",
    )

    observation = _language_observation_for_technique(
        (effect,),
        task_ids=("campaign/one/direct",),
        planned_language="lzh",
        campaign_id="campaign/one",
        target_id="target-1",
        committed_receipts={
            "campaign/one/direct": (
                "scheduler-attempt-1",
                frozenset({"payload-call"}),
                "target-session-1",
            )
        },
    )

    assert observation["status"] == "verified"
    assert observation["observed_language"] == "zh"
    assert observation["effect_id"] == "effect:language"
    assert observation["technique_attempt_id"] == attempt_id

    prefixed_observation = _language_observation_for_technique(
        (effect,),
        task_ids=("worker:campaign/one/direct",),
        planned_language="lzh",
        campaign_id="campaign/one",
        target_id="target-1",
        committed_receipts={
            "worker:campaign/one/direct": (
                "scheduler-attempt-1",
                frozenset({"payload-call"}),
                "target-session-1",
            )
        },
    )
    assert prefixed_observation["status"] == "verified"


def test_report_evidence_ignores_effect_without_agent_identity() -> None:
    orphan = SimpleNamespace(agent_id=None)
    receipt_values = {
        "task_ids": ("campaign/one/direct",),
        "technique_id": "policy",
        "spec_digest": "sha256:spec",
        "envelope_prompt_digest": "sha256:envelope",
        "campaign_id": "campaign/one",
        "target_id": "target-1",
        "committed_receipts": {},
    }

    assert _transform_payload_receipt_chain((orphan,), **receipt_values) == ()
    observation = _language_observation_for_technique(
        (orphan,),
        task_ids=("campaign/one/direct",),
        planned_language="tok",
        campaign_id="campaign/one",
        target_id="target-1",
        committed_receipts={},
    )
    assert observation == {
        "status": "unverified",
        "planned_language": "tok",
        "observed_language": None,
        "confidence": None,
    }


def test_unsearched_frontier_groups_semantic_variants_and_keeps_hypotheses_distinct() -> None:
    rows = [
        {
            "id": "policy@lang=en@variant=native",
            "semantic_id": "policy",
            "language": "en",
            "outcome": "untested",
        },
        {
            "id": "policy@lang=es@variant=translated",
            "semantic_id": "policy",
            "language": "es",
            "outcome": "untested",
        },
        {
            "id": "policy@lang=es@variant=mixed",
            "semantic_id": "policy",
            "language": "es",
            "outcome": "untested",
        },
        {
            "id": "leak@lang=en@variant=native",
            "semantic_id": "leak",
            "language": "en",
            "outcome": "attempted",
        },
        {
            "id": "hypothesis:one",
            "semantic_id": "policy",
            "hypothesis_id": "hypothesis:one",
            "language": "und",
            "outcome": "untested",
        },
        {
            "id": "hypothesis:two",
            "semantic_id": "policy",
            "hypothesis_id": "hypothesis:two",
            "language": "und",
            "outcome": "untested",
        },
    ]

    frontier = _semantic_unsearched_frontier(rows)
    assert [row["id"] for row in frontier] == [
        "policy@lang=en",
        "policy@lang=es",
        "hypothesis:one",
        "hypothesis:two",
    ]
    assert frontier[0]["variant_ids"] == ("policy@lang=en@variant=native",)
    assert frontier[0]["languages"] == ("en",)
    assert frontier[1]["variant_ids"] == (
        "policy@lang=es@variant=translated",
        "policy@lang=es@variant=mixed",
    )
    assert frontier[2]["kind"] == "hypothesis"
