"""Same-target technique skip: history lookup at prepare, recording at finish."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

import pytest
from merlin_agents import (
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    CampaignManifest,
    CampaignProfile,
    CampaignService,
    ContextDocument,
    ReviewedTargetAdapter,
)
from merlin_agents.campaign import scenario_fingerprint_for
from merlin_agents.campaign_service import (
    CampaignPreparation,
    _deferred_coverage_rows,
    _json_value,
)
from merlin_agents.technique_history import (
    entries_for_completion,
    plan_skip,
    technique_cell_parts,
)
from merlin_contracts import AgentTask


def _technique(technique_id: str, **overrides: object) -> AssignmentTechnique:
    values: dict[str, object] = {
        "id": technique_id,
        "objective": f"Exercise {technique_id}",
        "tags": frozenset({"catalog"}),
        "guidance_ids": (),
    }
    values.update(overrides)
    return AssignmentTechnique(**values)  # type: ignore[arg-type]


def test_plan_skip_skips_terminal_cells_and_cites_source() -> None:
    techniques = (_technique("cell/a"), _technique("cell/b"), _technique("cell/c"))
    history = {
        technique_cell_parts(techniques[0]): {
            "outcome": "refuted",
            "source_campaign_id": "campaign-0",
        },
        technique_cell_parts(techniques[1]): {
            "outcome": "confirmed",
            "source_campaign_id": "campaign-1",
        },
    }

    runnable, skipped = plan_skip(techniques, history)

    assert [item.id for item in runnable] == ["cell/c"]
    assert skipped == (
        {
            "technique_id": "cell/a",
            "controller_id": "direct",
            "payload_family": "baseline",
            "language_id": "und",
            "mutation_id": "none",
            "outcome": "refuted",
            "source_campaign_id": "campaign-0",
        },
        {
            "technique_id": "cell/b",
            "controller_id": "direct",
            "payload_family": "baseline",
            "language_id": "und",
            "mutation_id": "none",
            "outcome": "confirmed",
            "source_campaign_id": "campaign-1",
        },
    )


def test_plan_skip_reruns_unclear_and_missing_cells() -> None:
    techniques = (_technique("cell/a"), _technique("cell/b"))
    history = {
        technique_cell_parts(techniques[0]): {
            "outcome": "unclear",
            "source_campaign_id": "campaign-0",
        }
    }

    runnable, skipped = plan_skip(techniques, history)

    assert [item.id for item in runnable] == ["cell/a", "cell/b"]
    assert skipped == ()


def test_plan_skip_deprioritizes_partial_behind_untried_and_unclear() -> None:
    """Ordering: untried > unclear > partial; confirmed/refuted stay skipped."""
    partial = _technique("cell/partial")
    confirmed = _technique("cell/confirmed")
    untried = _technique("cell/untried")
    unclear = _technique("cell/unclear")
    history = {
        technique_cell_parts(partial): {
            "outcome": "partial",
            "source_campaign_id": "campaign-0",
            "partial_signal": {
                "progress": "target engaged across 2 observed attempts",
                "engaged_attempts": 2,
                "graded_by": "campaign_technique_attempts.state:observed",
            },
        },
        technique_cell_parts(confirmed): {
            "outcome": "confirmed",
            "source_campaign_id": "campaign-0",
        },
        technique_cell_parts(unclear): {
            "outcome": "unclear",
            "source_campaign_id": "campaign-0",
        },
    }

    runnable, skipped = plan_skip((partial, confirmed, untried, unclear), history)

    # Partial is a ranking input, never a suppression: it re-runs, last.
    assert [item.id for item in runnable] == ["cell/untried", "cell/unclear", "cell/partial"]
    assert [item["technique_id"] for item in skipped] == ["cell/confirmed"]


def test_entries_for_completion_records_partial_with_signal_payload() -> None:
    techniques = (_technique("cell/a"), _technique("cell/b"), _technique("cell/c"))
    cells = (
        (
            "task-1",
            "cell/a",
            "partial",
            {
                "progress": "target engaged across 2 observed attempts",
                "engaged_attempts": 2,
                "evidence_refs": ["ev-2", "ev-1"],
                "graded_by": "campaign_technique_attempts.state:observed",
            },
        ),
        ("task-2", "cell/a", "unclear"),
        ("task-3", "cell/b", "partial"),
        ("task-4", "cell/c", "confirmed"),
        ("task-5", "cell/c", "partial"),
    )

    entries = entries_for_completion(
        "target-1", "gen-1", techniques, cells, source_campaign_id="campaign-9"
    )

    by_id = {str(entry["technique_id"]): entry for entry in entries}
    # Graded engagement outranks a bare inconclusive sibling attempt.
    assert by_id["cell/a"]["outcome"] == "partial"
    assert by_id["cell/a"]["partial_signal"] == {
        "progress": (
            "target engaged with the technique objective and the attempt made "
            "recorded progress, but no confirmed or refuted verdict was reached"
        ),
        "engaged_attempts": 2,
        "evidence_refs": ("ev-1", "ev-2"),
        "graded_by": "campaign_technique_attempts.state:observed",
    }
    # A partial cell without a signal mapping still records an explicit payload.
    assert by_id["cell/b"]["outcome"] == "partial"
    assert isinstance(by_id["cell/b"]["partial_signal"], dict)
    # A terminal verdict is never displaced by partial engagement.
    assert by_id["cell/c"]["outcome"] == "confirmed"
    assert "partial_signal" not in by_id["cell/c"]


@pytest.mark.parametrize("outcome", ("confirmed", "refuted"))
def test_plan_skip_retries_legacy_a2a_terminal_without_peer_delivery(outcome: str) -> None:
    technique = _technique("merlin-injection-taxonomy/cat/agent-to-agent/agent-message-override")
    history = {
        technique_cell_parts(technique): {
            "outcome": outcome,
            "source_campaign_id": "campaign-legacy",
        }
    }

    runnable, skipped = plan_skip((technique,), history)

    assert runnable == (technique,)
    assert skipped == ()


def test_plan_skip_reuses_a2a_terminal_with_authenticated_peer_only_delivery() -> None:
    technique = _technique("merlin-injection-taxonomy/cat/agent-to-agent/agent-message-override")
    history = {
        technique_cell_parts(technique): {
            "outcome": "refuted",
            "source_campaign_id": "campaign-peer",
            "peer_delivery_authenticated": True,
        }
    }

    runnable, skipped = plan_skip((technique,), history)

    assert runnable == ()
    assert [item["technique_id"] for item in skipped] == [technique.id]


def test_plan_skip_retries_a2a_terminal_with_mixed_delivery() -> None:
    technique = _technique("merlin-injection-taxonomy/cat/agent-to-agent/agent-message-override")
    history = {
        technique_cell_parts(technique): {
            "outcome": "confirmed",
            "source_campaign_id": "campaign-mixed",
            "peer_delivery_authenticated": False,
        }
    }

    runnable, skipped = plan_skip((technique,), history)

    assert runnable == (technique,)
    assert skipped == ()


def test_plan_skip_distinguishes_language_and_mutation_variants() -> None:
    conclusive = _technique(
        "cell/a",
        language="lzh",
        transform_id="translated",
        mutation_seed="archaic-authority",
    )
    different_language = replace(conclusive, language="sw")
    different_mutation = replace(conclusive, mutation_seed="encoded-authority")
    history = {
        technique_cell_parts(conclusive): {
            "outcome": "refuted",
            "source_campaign_id": "campaign-0",
        }
    }

    # A clean refusal is not language signal: the sibling language and mutation
    # variants are deferred until lzh/sw show engagement evidence.
    runnable, skipped = plan_skip(
        (conclusive, different_language, different_mutation),
        history,
        signal_languages=("lzh", "sw"),
    )

    assert runnable == (different_language, different_mutation)
    assert len(skipped) == 1
    assert skipped[0]["language_id"] == "lzh"
    assert str(skipped[0]["mutation_id"]).startswith("sha256:")


def test_plan_skip_defers_exotic_language_cells_without_signal() -> None:
    baseline = _technique("cell/a@lang=en@variant=native", language="en")
    exotic = _technique("cell/a@lang=ig@variant=native", language="ig")

    runnable, skipped = plan_skip((baseline, exotic), {})

    assert runnable == (baseline,)
    assert len(skipped) == 1
    deferral = skipped[0]
    assert deferral["technique_id"] == exotic.id
    assert deferral["language_id"] == "ig"
    assert deferral["outcome"] == "deferred"
    assert "no recorded evidence" in str(deferral["reason"])


def test_plan_skip_admits_language_cell_with_attempt_signal() -> None:
    exotic = _technique("cell/a@lang=ig@variant=native", language="ig")
    other_cell = _technique("cell/b@lang=ig@variant=native", language="ig")
    history = {
        # A prior attempt in Igbo ended inconclusively: engagement other than a
        # clean refusal or an infrastructure failure admits the language.
        technique_cell_parts(other_cell): {
            "outcome": "unclear",
            "source_campaign_id": "campaign-0",
        }
    }

    runnable, skipped = plan_skip((exotic, other_cell), history)

    assert runnable == (exotic, other_cell)
    assert skipped == ()


def test_plan_skip_admits_language_cell_with_probe_signal() -> None:
    exotic = _technique("cell/a@lang=tok@variant=native", language="tok")

    runnable, skipped = plan_skip((exotic,), {}, signal_languages=("tok",))

    assert runnable == (exotic,)
    assert skipped == ()


def test_plan_skip_admits_top_ten_languages_without_signal() -> None:
    # LLM-backed targets engage in the ten most-spoken languages with
    # near-certainty; the profile cell cap bounds the spend, so these cells
    # are never deferred for missing engagement evidence.
    cells = tuple(
        _technique(f"cell/a@lang={language}@variant=native", language=language)
        for language in ("zh", "ru", "fr", "bn", "pt", "ur")
    )

    runnable, skipped = plan_skip(cells, {})

    assert runnable == cells
    assert skipped == ()


def test_plan_skip_still_defers_long_tail_languages_without_signal() -> None:
    javanese = _technique("cell/a@lang=jv@variant=native", language="jv")

    runnable, skipped = plan_skip((javanese,), {})

    assert runnable == ()
    assert len(skipped) == 1
    assert skipped[0]["language_id"] == "jv"
    assert skipped[0]["outcome"] == "deferred"


def test_plan_skip_refusal_and_infra_failure_are_not_language_signal() -> None:
    exotic = _technique("cell/a@lang=yo@variant=native", language="yo")
    refused = _technique("cell/b@lang=yo@variant=native", language="yo")
    infra_failed = _technique("cell/c@lang=yo@variant=native", language="yo")
    history = {
        technique_cell_parts(refused): {
            "outcome": "refuted",
            "source_campaign_id": "campaign-0",
        },
        technique_cell_parts(infra_failed): {
            "outcome": "blocked",
            "source_campaign_id": "campaign-0",
        },
    }

    runnable, skipped = plan_skip((exotic, refused, infra_failed), history)

    assert runnable == ()
    outcomes = {item["technique_id"]: item["outcome"] for item in skipped}
    assert outcomes == {refused.id: "refuted", infra_failed.id: "deferred", exotic.id: "deferred"}


def test_completion_does_not_guess_between_legacy_duplicate_variant_ids() -> None:
    variants = (
        _technique("cell/a", language="lzh", mutation_seed="first"),
        _technique("cell/a", language="sw", mutation_seed="second"),
    )

    entries = entries_for_completion(
        "target-1",
        "gen-1",
        variants,
        (("legacy-task", "cell/a", "confirmed"),),
        source_campaign_id="campaign-1",
    )

    assert len(entries) == 2
    assert {entry["language_id"] for entry in entries} == {"lzh", "sw"}
    assert {entry["outcome"] for entry in entries} == {"unclear"}


def test_plan_skip_keeps_direct_control_pairs_coherent() -> None:
    control = _technique(
        "control/x",
        execution_role="control",
        oracle_digest="sha256:" + "1" * 64,
        comparison_policy="reviewed-benign-differential-v1",
    )
    direct = _technique(
        "cell/x",
        execution_role="direct",
        control_technique_id="control/x",
        control_objective="benign baseline",
        control_oracle_digest="sha256:" + "0" * 64,
        oracle_digest="sha256:" + "1" * 64,
        comparison_policy="reviewed-benign-differential-v1",
    )
    # A conclusive direct result is independent from an unexecuted control.
    # Selective validation must be explicitly planned instead of replaying the
    # pair on every later campaign.
    history = {
        technique_cell_parts(direct): {"outcome": "refuted", "source_campaign_id": "campaign-0"}
    }

    runnable, skipped = plan_skip((control, direct), history)

    assert [item.id for item in runnable] == ["control/x"]
    assert [item["technique_id"] for item in skipped] == ["cell/x"]


def test_plan_skip_fires_for_direct_cell_whose_embedded_control_has_no_history() -> None:
    """run12 regression: benign controls execute embedded in the direct cell's
    pair assignment and never appear as planned catalog cells or history rows,
    so requiring the control half to be skipped revoked every skip forever."""
    direct = _technique(
        "cell/x",
        execution_role="direct",
        control_technique_id="control/x",
        control_objective="benign baseline",
        control_oracle_digest="sha256:" + "0" * 64,
        oracle_digest="sha256:" + "1" * 64,
        comparison_policy="reviewed-benign-differential-v1",
    )
    history = {
        technique_cell_parts(direct): {"outcome": "refuted", "source_campaign_id": "campaign-0"}
    }

    runnable, skipped = plan_skip((direct,), history)

    assert runnable == ()
    assert [item["technique_id"] for item in skipped] == ["cell/x"]


def test_entries_for_completion_reduces_outcomes_and_ignores_hypotheses() -> None:
    techniques = (_technique("cell/a"), _technique("cell/b"), _technique("cell/c"))
    cells = (
        ("task-1", "cell/a", "refuted"),
        ("task-2", "cell/a", "refuted"),
        ("task-3", "cell/b", "confirmed"),
        ("task-4", "cell/b", "unclear"),
        ("task-5", "hypothesis:abc", "confirmed"),
        ("task-6", "cell/unknown", "confirmed"),
        ("task-7", "cell/c", "confirmed"),
        ("task-8", "cell/c", "refuted"),
    )

    entries = entries_for_completion(
        "target-1", "gen-1", techniques, cells, source_campaign_id="campaign-9"
    )

    assert entries == (
        {
            "target_id": "target-1",
            "adapter_generation": "gen-1",
            "technique_id": "cell/a",
            "controller_id": "direct",
            "payload_family": "baseline",
            "language_id": "und",
            "mutation_id": "none",
            "outcome": "refuted",
            "source_campaign_id": "campaign-9",
        },
        {
            "target_id": "target-1",
            "adapter_generation": "gen-1",
            "technique_id": "cell/b",
            "controller_id": "direct",
            "payload_family": "baseline",
            "language_id": "und",
            "mutation_id": "none",
            # An inconclusive sibling attempt is not a disagreement: the cell
            # keeps its terminal outcome.
            "outcome": "confirmed",
            "source_campaign_id": "campaign-9",
        },
        {
            "target_id": "target-1",
            "adapter_generation": "gen-1",
            "technique_id": "cell/c",
            "controller_id": "direct",
            "payload_family": "baseline",
            "language_id": "und",
            "mutation_id": "none",
            # Conflicting terminal outcomes never manufacture certainty.
            "outcome": "unclear",
            "source_campaign_id": "campaign-9",
        },
    )


def test_entries_for_completion_retries_blocked_but_keeps_conclusive_outcomes() -> None:
    """Infrastructure/capability blocks remain retryable across campaigns."""
    techniques = (_technique("cell/a"), _technique("cell/b"))
    cells = (
        ("campaign-9/adversarial-explorer/agenda-1/direct", "cell/a", "refuted"),
        ("campaign-9/wave-2/cell-a-retry", "cell/a", "unclear"),
        ("campaign-9/adversarial-explorer/agenda-2/direct", "cell/b", "blocked"),
        ("campaign-9/wave-2/cell-b-retry", "cell/b", "unclear"),
    )

    entries = entries_for_completion(
        "target-1", "gen-1", techniques, cells, source_campaign_id="campaign-9"
    )

    outcomes = {str(entry["technique_id"]): str(entry["outcome"]) for entry in entries}
    assert outcomes == {"cell/a": "refuted", "cell/b": "unclear"}

    # Only the conclusive cell skips; the blocked cell returns to the frontier.
    history = {
        technique_cell_parts(technique): {
            "outcome": outcomes[str(technique.id)],
            "source_campaign_id": "campaign-9",
        }
        for technique in techniques
    }
    runnable, skipped = plan_skip(techniques, history)
    assert [item.id for item in runnable] == ["cell/b"]
    assert [item["technique_id"] for item in skipped] == ["cell/a"]


def test_plan_skip_only_reuses_explicit_permanent_blocks() -> None:
    retryable = _technique("cell/retryable")
    permanent = _technique("cell/permanent")
    history = {
        technique_cell_parts(retryable): {
            "outcome": "blocked",
            "source_campaign_id": "campaign-0",
        },
        technique_cell_parts(permanent): {
            "outcome": "blocked_terminal",
            "source_campaign_id": "campaign-0",
        },
    }

    runnable, skipped = plan_skip((retryable, permanent), history)

    assert [item.id for item in runnable] == ["cell/retryable", "cell/permanent"]
    assert skipped == ()


def _preparation(
    techniques: tuple[AssignmentTechnique, ...],
    *,
    catalogue: tuple[ContextDocument, ...] | None = None,
    campaign_id: str = "campaign-1",
) -> CampaignPreparation:
    budget = AssignmentBudget(256, 1_048_576, 14_400)
    manifest = CampaignManifest.create(
        id=campaign_id,
        session_id="session-1",
        objective="Assess the reviewed target",
        profile=CampaignProfile.RECOMMENDED,
        target=ReviewedTargetAdapter("target-1", "api", "https://target.test", "model"),
        provider_id="provider-1",
        provider_model="model-1",
        provider_effort="default",
        authority_ids=("session:session-1", "target:target-1"),
        catalogue=catalogue if catalogue is not None else (),
        skill_documents=(),
        tool_ids=("target_chat",),
        budget=budget,
    )
    context = AssignmentContext(
        target_profile_id="target-1",
        techniques=techniques,
        tool_ids=("target_chat",),
        skill_ids=(),
        provider_id="provider-1",
        authority_ids=manifest.authority_ids,
        budget=budget,
        input_schema="input/v1",
        output_schema="output/v1",
    )
    return CampaignPreparation(manifest, context)


class _HistoryStore:
    """In-memory campaign store with same-target technique history."""

    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = {}
        self.history = list(rows or [])
        self.journal = []
        self.lookups = []

    def prepare(self, campaign_id, session_id, manifest, digest):
        row = {
            "id": campaign_id,
            "session_id": session_id,
            "manifest": manifest,
            "manifest_digest": digest,
            "status": "prepared",
        }
        self.rows[campaign_id] = row
        return row

    def get(self, campaign_id):
        return self.rows.get(campaign_id)

    def commit_lead_decision(self, campaign_id, wave, decision_id, payload, _mailbox):
        entry = {
            "campaign_id": campaign_id,
            "wave": wave,
            "decision_id": decision_id,
            "payload": json.loads(json.dumps(payload)),
        }
        self.journal.append(entry)
        return entry

    def lookup_technique_history(self, target_id, adapter_generation, cells):
        self.lookups.append((target_id, adapter_generation, tuple(cells)))
        wanted = {tuple(str(part) for part in cell) for cell in cells}
        return {
            (
                str(row["technique_id"]),
                str(row["controller_id"]),
                str(row["payload_family"]),
                str(row.get("language_id", "und")),
                str(row.get("mutation_id", "none")),
            ): {
                "outcome": str(row["outcome"]),
                "source_campaign_id": str(row["source_campaign_id"]),
                **(
                    {"partial_signal": dict(row["partial_signal"])}
                    if isinstance(row.get("partial_signal"), dict)
                    else {}
                ),
            }
            for row in self.history
            if row["target_id"] == target_id
            and row["adapter_generation"] == adapter_generation
            and (
                str(row["technique_id"]),
                str(row["controller_id"]),
                str(row["payload_family"]),
                str(row.get("language_id", "und")),
                str(row.get("mutation_id", "none")),
            )
            in wanted
        }

    def record_technique_history(self, entries):
        self.history.extend(dict(entry) for entry in entries)

    def technique_history_generations(self, target_id):
        counts = {}
        for row in self.history:
            if row["target_id"] == target_id:
                generation = str(row["adapter_generation"])
                counts[generation] = counts.get(generation, 0) + 1
        return [
            {"adapter_generation": generation, "rows": counts[generation]}
            for generation in sorted(counts)
        ]


async def test_partial_repeat_guidance_survives_review_resume_and_worker_compilation():
    from merlin_agents.campaign import compile_attack_campaign, compile_baseline_agenda
    from merlin_agents.campaign_service import _assignment_context, _manifest
    from merlin_agents.repeat_planning import PARTIAL_REPEAT_GUIDANCE_PREFIX

    attempted = _technique("cell/partial", tags=frozenset(), execution_role="direct")
    untried = _technique("cell/new", tags=frozenset(), execution_role="direct")
    resolved = _technique("cell/resolved", tags=frozenset(), execution_role="direct")
    prepared = _preparation((attempted, untried, resolved))
    generation = scenario_fingerprint_for(prepared.manifest.target, prepared.manifest.catalogue)
    store = _HistoryStore(
        [
            {
                "target_id": "target-1",
                "adapter_generation": generation,
                "technique_id": technique.id,
                "controller_id": "direct",
                "payload_family": "baseline",
                "outcome": outcome,
                "source_campaign_id": "campaign-prior",
                "partial_signal": {"evidence_refs": ["receipt/prior"], "engaged_attempts": 1},
            }
            for technique, outcome in ((attempted, "partial"), (resolved, "refuted"))
        ]
    )

    async def factory(*args, **kwargs):
        return prepared

    review = await CampaignService(store, object(), factory).prepare(
        "session-1",
        "Assess",
        CampaignProfile.RECOMMENDED,
    )
    envelope = json.loads(json.dumps(store.rows["campaign-1"]["manifest"]))
    manifest = _manifest(envelope["manifest"])
    context = _assignment_context(envelope["assignment_context"])
    manifest.validate()
    assert scenario_fingerprint_for(manifest.target, manifest.catalogue) == generation
    assert [technique.id for technique in context.techniques] == ["cell/new", "cell/partial"]
    assert len(review["review"]["technique_skips"]) == 1
    guidance = next(
        document
        for document in manifest.catalogue
        if document.id.startswith(PARTIAL_REPEAT_GUIDANCE_PREFIX)
    )
    prior = json.loads(guidance.content)
    assert prior["source_campaign_id"] == "campaign-prior"
    assert prior["partial_signal"]["evidence_refs"] == ["receipt/prior"]
    assert "strategy_plan.thought" in prior["policy"]
    assert context.techniques[0].guidance_ids == ()
    assert guidance.id in context.techniques[1].guidance_ids
    agenda = compile_baseline_agenda(manifest, context, compile_attack_campaign(manifest, context))
    assert len(agenda.assignments) == 2
    assert [assignment.technique_ids for assignment in agenda.assignments] == [
        ("cell/new",),
        ("cell/partial",),
    ]
    assert guidance in agenda.worker_contexts[1].guidance
    assert guidance not in agenda.worker_contexts[0].guidance


async def test_semantic_triage_cannot_retire_partial_repeat(monkeypatch):
    class NegativeTriage:
        input_tokens = 0
        output_tokens = 0

        async def noul(self, *args, **kwargs):
            raise AssertionError("Partial repeats must remain eligible, regardless of triage.")

    technique = _technique("cell/partial")
    prepared = _preparation((technique,))
    service = CampaignService(None, None, object())
    monkeypatch.setattr(service, "_jev_escalation_client", lambda: NegativeTriage())
    runnable, skipped = await service._jev_cell_triage(  # noqa: SLF001
        prepared,
        {technique_cell_parts(technique): {"outcome": "partial"}},
        (technique,),
    )
    assert runnable == (technique,)
    assert skipped == ()


def test_partial_repeat_context_keeps_variant_histories_separate():
    from merlin_agents.repeat_planning import partial_repeat_documents

    first = _technique("same-family", language="en", mutation_seed="first")
    second = replace(first, language="fr", mutation_seed="second")
    prepared = _preparation((first, second))
    history = {
        technique_cell_parts(technique): {
            "outcome": "partial",
            "source_campaign_id": source,
        }
        for technique, source in ((first, "campaign-en"), (second, "campaign-fr"))
    }
    documents = partial_repeat_documents(prepared.assignment_context, history)
    assert len(documents) == 2
    assert (
        json.loads(documents[technique_cell_parts(first)].content)["source_campaign_id"]
        == "campaign-en"
    )
    assert (
        json.loads(documents[technique_cell_parts(second)].content)["source_campaign_id"]
        == "campaign-fr"
    )


async def test_second_campaign_skips_executed_cells_and_cites_source() -> None:
    techniques = (_technique("cell/a"), _technique("cell/b"))
    prepared = _preparation(techniques)
    generation = scenario_fingerprint_for(prepared.manifest.target, prepared.manifest.catalogue)
    store = _HistoryStore(
        [
            {
                "target_id": "target-1",
                "adapter_generation": generation,
                "technique_id": "cell/a",
                "controller_id": "direct",
                "payload_family": "baseline",
                "outcome": "refuted",
                "source_campaign_id": "campaign-0",
            }
        ]
    )

    async def factory(*_args, **_kwargs):
        return prepared

    service = CampaignService(store, object(), factory)  # type: ignore[arg-type]
    review = await service.prepare(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )

    envelope = store.rows["campaign-1"]["manifest"]
    assert envelope["adapter_generation"] == generation
    assert envelope["technique_skips"] == [
        {
            "technique_id": "cell/a",
            "controller_id": "direct",
            "payload_family": "baseline",
            "language_id": "und",
            "mutation_id": "none",
            "outcome": "refuted",
            "source_campaign_id": "campaign-0",
        }
    ]
    remaining = envelope["assignment_context"]["techniques"]
    assert [item["id"] for item in remaining] == ["cell/b"]
    assert review["review"]["technique_skips"][0]["source_campaign_id"] == "campaign-0"
    skip_journal = [item for item in store.journal if item["payload"]["kind"] == "technique_skip"]
    assert len(skip_journal) == 1
    assert skip_journal[0]["payload"]["skips"][0]["technique_id"] == "cell/a"
    assert skip_journal[0]["payload"]["skips"][0]["source_campaign_id"] == "campaign-0"


async def test_adapter_generation_bump_reruns_everything() -> None:
    techniques = (_technique("cell/a"),)
    prepared = _preparation(techniques)
    prior_generation = scenario_fingerprint_for(
        prepared.manifest.target,
        (ContextDocument.create("target:reviewed-interaction-contract", "old adapter"),),
    )
    store = _HistoryStore(
        [
            {
                "target_id": "target-1",
                "adapter_generation": prior_generation,
                "technique_id": "cell/a",
                "controller_id": "direct",
                "payload_family": "baseline",
                "outcome": "refuted",
                "source_campaign_id": "campaign-0",
            }
        ]
    )

    async def factory(*_args, **_kwargs):
        return prepared

    service = CampaignService(store, object(), factory)  # type: ignore[arg-type]
    review = await service.prepare(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )

    # The recorded history lives under a different adapter generation, so the
    # current generation has no hits and the cell runs again.
    assert store.lookups[0][1] != prior_generation
    assert review["review"]["technique_skips"] == ()
    envelope = store.rows["campaign-1"]["manifest"]
    assert [item["id"] for item in envelope["assignment_context"]["techniques"]] == ["cell/a"]


async def test_unclear_cells_rerun_on_next_campaign() -> None:
    techniques = (_technique("cell/a"),)
    prepared = _preparation(techniques)
    generation = scenario_fingerprint_for(prepared.manifest.target, prepared.manifest.catalogue)
    store = _HistoryStore(
        [
            {
                "target_id": "target-1",
                "adapter_generation": generation,
                "technique_id": "cell/a",
                "controller_id": "direct",
                "payload_family": "baseline",
                "outcome": "unclear",
                "source_campaign_id": "campaign-0",
            }
        ]
    )

    async def factory(*_args, **_kwargs):
        return prepared

    service = CampaignService(store, object(), factory)  # type: ignore[arg-type]
    review = await service.prepare(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )

    assert review["review"]["technique_skips"] == ()
    persisted = store.rows["campaign-1"]["manifest"]["assignment_context"]["techniques"]
    assert [item["id"] for item in persisted] == ["cell/a"]


async def test_partial_cells_rerun_deprioritized_and_reported_at_prepare() -> None:
    """A partial prior re-runs (never skipped), ranks behind untried cells, and
    its count and payload surface in the review cross_campaign_learning block."""
    techniques = (_technique("cell/partial"), _technique("cell/untried"))
    prepared = _preparation(techniques)
    generation = scenario_fingerprint_for(prepared.manifest.target, prepared.manifest.catalogue)
    store = _HistoryStore(
        [
            {
                "target_id": "target-1",
                "adapter_generation": generation,
                "technique_id": "cell/partial",
                "controller_id": "direct",
                "payload_family": "baseline",
                "outcome": "partial",
                "source_campaign_id": "campaign-0",
                "partial_signal": {
                    "progress": "target engaged across 1 observed attempt",
                    "engaged_attempts": 1,
                    "graded_by": "campaign_technique_attempts.state:observed",
                },
            }
        ]
    )

    async def factory(*_args, **_kwargs):
        return prepared

    service = CampaignService(store, object(), factory)  # type: ignore[arg-type]
    review = await service.prepare(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )

    assert review["review"]["technique_skips"] == ()
    learning = review["review"]["cross_campaign_learning"]
    assert learning["partial"] == 1
    assert [item["technique_id"] for item in learning["partial_cells"]] == ["cell/partial"]
    persisted = store.rows["campaign-1"]["manifest"]["assignment_context"]["techniques"]
    assert [item["id"] for item in persisted] == ["cell/untried", "cell/partial"]


async def test_terminal_gate_outcome_recorded_at_seal_skips_at_next_prepare() -> None:
    """Settlement flow: a cell gated to refuted in the sealed completion (with
    an inconclusive sibling attempt) lands in history as refuted, and the next
    prepare skips it."""
    techniques = (_technique("cell/a"), _technique("cell/b"))
    prepared = _preparation(techniques)
    generation = scenario_fingerprint_for(prepared.manifest.target, prepared.manifest.catalogue)
    envelope = {
        "manifest": _json_value(asdict(prepared.manifest)),
        "assignment_context": _json_value(asdict(prepared.assignment_context)),
        "adapter_generation": generation,
        "technique_skips": [],
    }
    store = _HistoryStore()
    store.rows["campaign-9"] = {"id": "campaign-9", "manifest": envelope}
    document = {
        "schema": "merlin.campaign-completion.v1",
        "cells": (
            ("campaign-9/adversarial-explorer/agenda-1/direct", "cell/a", "refuted"),
            ("campaign-9/wave-2/cell-a-retry", "cell/a", "unclear"),
            ("campaign-9/wave-1/cell-b", "cell/b", "unclear"),
        ),
    }

    service = CampaignService(store, object(), None)  # type: ignore[arg-type]
    await service._record_technique_history("campaign-9", document)  # noqa: SLF001

    outcomes = {str(row["technique_id"]): str(row["outcome"]) for row in store.history}
    assert outcomes == {"cell/a": "refuted", "cell/b": "unclear"}

    async def factory(*_args, **_kwargs):
        return prepared

    next_service = CampaignService(store, object(), factory)  # type: ignore[arg-type]
    review = await next_service.prepare(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )

    assert [item["technique_id"] for item in review["review"]["technique_skips"]] == ["cell/a"]
    remaining = store.rows["campaign-1"]["manifest"]["assignment_context"]["techniques"]
    assert [item["id"] for item in remaining] == ["cell/b"]


async def test_finish_campaign_records_cell_history_from_completion() -> None:
    techniques = (_technique("cell/a"), _technique("cell/b"))
    prepared = _preparation(techniques)
    generation = scenario_fingerprint_for(prepared.manifest.target, prepared.manifest.catalogue)
    envelope = {
        "manifest": _json_value(asdict(prepared.manifest)),
        "assignment_context": _json_value(asdict(prepared.assignment_context)),
        "adapter_generation": generation,
        "technique_skips": [],
    }
    store = _HistoryStore()
    store.rows["campaign-1"] = {"id": "campaign-1", "manifest": envelope}

    document = {
        "schema": "merlin.campaign-completion.v1",
        "saturation": {"ready": True},
        "cells": (
            ("task-1", "cell/a", "refuted"),
            ("task-2", "cell/b", "confirmed"),
        ),
    }

    class _Service(CampaignService):
        async def status(self, _campaign_id):
            digest = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
            return {"completion_document": document, "completion_digest": digest}

    service = _Service(store, object(), None)  # type: ignore[arg-type]
    await service._record_technique_history("campaign-1", document)  # noqa: SLF001

    assert store.history == [
        {
            "target_id": "target-1",
            "adapter_generation": generation,
            "technique_id": "cell/a",
            "controller_id": "direct",
            "payload_family": "baseline",
            "language_id": "und",
            "mutation_id": "none",
            "outcome": "refuted",
            "source_campaign_id": "campaign-1",
        },
        {
            "target_id": "target-1",
            "adapter_generation": generation,
            "technique_id": "cell/b",
            "controller_id": "direct",
            "payload_family": "baseline",
            "language_id": "und",
            "mutation_id": "none",
            "outcome": "confirmed",
            "source_campaign_id": "campaign-1",
        },
    ]


def _scenario_contract_document(
    *,
    interaction_endpoint: str,
    oracle_wording: tuple[str, ...],
    session_prose: str,
) -> ContextDocument:
    contract = {
        "interface": "http_api",
        "endpoint": "https://target.test",
        "authentication": session_prose,
        "session_constraint": session_prose,
        "api_steps": [
            {
                "step_id": "session-setup",
                "method": "POST",
                "endpoint": "https://target.test/start",
                "interaction": False,
                "cleanup": False,
                "success_witness": session_prose,
            },
            {
                "step_id": "interact",
                "method": "POST",
                "endpoint": interaction_endpoint,
                "interaction": True,
                "cleanup": False,
                "oracle_predicate": {"match_values": list(oracle_wording)},
            },
        ],
    }
    return ContextDocument.create(
        "target:reviewed-scenario-contract",
        "Platform-neutral reviewed scenario and oracle contract.\n"
        + json.dumps(contract, sort_keys=True),
    )


async def test_reworded_recon_prose_keeps_generation_and_skip_fires() -> None:
    """Same endpoints/methods with reworded LLM prose keep the generation.

    Regression for run8k/run11: recon rewrites oracle match_values wording,
    witnesses, and session prose on every pass; the raw content digest made
    each campaign a new generation and the skip never fired live.
    """
    techniques = (_technique("cell/a"), _technique("cell/b"))
    first = _preparation(
        techniques,
        catalogue=(
            _scenario_contract_document(
                interaction_endpoint="https://target.test/message",
                oracle_wording=("eligible", "we will refund"),
                session_prose="sessionId afd70cee-159a created once by start",
            ),
        ),
    )
    generation = scenario_fingerprint_for(first.manifest.target, first.manifest.catalogue)
    store = _HistoryStore(
        [
            {
                "target_id": "target-1",
                "adapter_generation": generation,
                "technique_id": "cell/a",
                "controller_id": "direct",
                "payload_family": "baseline",
                "outcome": "refuted",
                "source_campaign_id": "campaign-0",
            }
        ]
    )
    reworded = _preparation(
        techniques,
        catalogue=(
            _scenario_contract_document(
                interaction_endpoint="https://target.test/message",
                oracle_wording=("approved", "you qualify", "entitled"),
                session_prose="fresh session 9ce1c2, max 18 messages, conviction 45",
            ),
        ),
    )

    reworded_generation = scenario_fingerprint_for(
        reworded.manifest.target, reworded.manifest.catalogue
    )
    assert reworded_generation == generation

    async def factory(*_args, **_kwargs):
        return reworded

    service = CampaignService(store, object(), factory)  # type: ignore[arg-type]
    review = await service.prepare(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )

    assert [item["technique_id"] for item in review["review"]["technique_skips"]] == ["cell/a"]
    remaining = store.rows["campaign-1"]["manifest"]["assignment_context"]["techniques"]
    assert [item["id"] for item in remaining] == ["cell/b"]
    assert not [
        item for item in store.journal if item["payload"]["kind"] == "technique_skip_inapplicable"
    ]


async def test_new_interaction_endpoint_invalidates_history_and_journals_diagnostic() -> None:
    """A genuinely different interaction endpoint mints a new generation, the
    skip does not fire, and the mismatch is journaled for the live run."""
    techniques = (_technique("cell/a"),)
    first = _preparation(
        techniques,
        catalogue=(
            _scenario_contract_document(
                interaction_endpoint="https://target.test/message",
                oracle_wording=("eligible",),
                session_prose="original recon prose",
            ),
        ),
    )
    prior_generation = scenario_fingerprint_for(first.manifest.target, first.manifest.catalogue)
    store = _HistoryStore(
        [
            {
                "target_id": "target-1",
                "adapter_generation": prior_generation,
                "technique_id": "cell/a",
                "controller_id": "direct",
                "payload_family": "baseline",
                "outcome": "refuted",
                "source_campaign_id": "campaign-0",
            }
        ]
    )
    changed = _preparation(
        techniques,
        catalogue=(
            _scenario_contract_document(
                interaction_endpoint="https://target.test/start",
                oracle_wording=("eligible",),
                session_prose="original recon prose",
            ),
        ),
    )

    async def factory(*_args, **_kwargs):
        return changed

    service = CampaignService(store, object(), factory)  # type: ignore[arg-type]
    review = await service.prepare(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )

    assert review["review"]["technique_skips"] == ()
    envelope = store.rows["campaign-1"]["manifest"]
    new_generation = envelope["adapter_generation"]
    assert new_generation != prior_generation
    diagnostics = [
        item for item in store.journal if item["payload"]["kind"] == "technique_skip_inapplicable"
    ]
    assert len(diagnostics) == 1
    payload = diagnostics[0]["payload"]
    assert payload["adapter_generation"] == new_generation
    assert payload["known_generations"] == [{"adapter_generation": prior_generation, "rows": 1}]
    assert payload["history_rows"] == 1


def test_prepare_helper_uses_distinct_generations() -> None:
    """Guard: the generation-bump test only works if fingerprints differ."""
    first = _preparation((_technique("cell/a"),))
    second = replace(
        first,
        manifest=replace(
            first.manifest,
            catalogue=(ContextDocument.create("target:reviewed-interaction-contract", "new"),),
        ),
    )
    assert scenario_fingerprint_for(
        first.manifest.target, first.manifest.catalogue
    ) != scenario_fingerprint_for(second.manifest.target, second.manifest.catalogue)


async def test_prepare_refuses_when_terminal_history_skips_every_cell() -> None:
    techniques = (_technique("cell/a"), _technique("cell/b"))
    prepared = _preparation(techniques)
    generation = scenario_fingerprint_for(prepared.manifest.target, prepared.manifest.catalogue)
    store = _HistoryStore(
        [
            {
                "target_id": "target-1",
                "adapter_generation": generation,
                "technique_id": technique_id,
                "controller_id": "direct",
                "payload_family": "baseline",
                "outcome": "refuted",
                "source_campaign_id": "campaign-0",
            }
            for technique_id in ("cell/a", "cell/b")
        ]
    )

    async def factory(*_args, **_kwargs):
        return prepared

    service = CampaignService(store, object(), factory)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no runnable reviewed technique cells remain"):
        await service.prepare(
            "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
        )

    # The refusal happens before any campaign row is sealed.
    assert store.rows == {}


async def test_prepare_defers_exotic_language_cells_and_journals_reason() -> None:
    """A non-baseline language cell without target signal is deferred coverage:
    it leaves the runnable set, is journaled with its reason, and lands in the
    envelope ``technique_skips`` the completion document lists as skipped cells."""
    baseline = _technique("cell/a@lang=en@variant=native", language="en")
    exotic = _technique("cell/a@lang=ig@variant=native", language="ig")
    prepared = _preparation((baseline, exotic))
    store = _HistoryStore()

    async def factory(*_args, **_kwargs):
        return prepared

    service = CampaignService(store, object(), factory)  # type: ignore[arg-type]
    review = await service.prepare(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )

    skips = review["review"]["technique_skips"]
    assert [item["technique_id"] for item in skips] == [exotic.id]
    assert skips[0]["outcome"] == "deferred"
    assert "ig" in str(skips[0]["reason"])
    remaining = store.rows["campaign-1"]["manifest"]["assignment_context"]["techniques"]
    assert [item["id"] for item in remaining] == [baseline.id]
    skip_journal = [item for item in store.journal if item["payload"]["kind"] == "technique_skip"]
    assert len(skip_journal) == 1
    journaled = skip_journal[0]["payload"]["skips"][0]
    assert journaled["outcome"] == "deferred"
    assert "no recorded evidence" in str(journaled["reason"])


def _escalation_envelope(
    prepared: CampaignPreparation, languages: tuple[str, ...]
) -> dict[str, object]:
    return {
        "manifest": _json_value(asdict(prepared.manifest)),
        "assignment_context": _json_value(asdict(prepared.assignment_context)),
        "adapter_generation": "gen",
        "technique_skips": [
            {
                "technique_id": f"cell/a@lang={language}@variant=native",
                "controller_id": "direct",
                "payload_family": "native",
                "language_id": language,
                "mutation_id": "none",
                "outcome": "deferred",
                "source_campaign_id": "campaign-1",
            }
            for language in languages
        ],
    }


def _completed_task(outcome: str) -> AgentTask:
    return AgentTask(
        id="task-1",
        objective="Exercise cell/a",
        status="completed",
        result=json.dumps(
            {
                "attempted_techniques": [
                    {"technique_id": "cell/a", "outcome": outcome, "evidence_refs": ["e1"]}
                ]
            }
        ),
        technique_ids=("cell/a",),
    )


async def test_language_escalation_readmits_deferred_cells_for_promising_bases() -> None:
    """An unclear baseline earns bounded language mutations, signal-default first."""
    prepared = _preparation((_technique("cell/a"),))
    store = _HistoryStore()
    store.rows["campaign-1"] = {
        "id": "campaign-1",
        "manifest": _escalation_envelope(prepared, ("ig", "sw")),
    }
    service = CampaignService(store, object(), None)  # type: ignore[arg-type]

    cells = await service._language_escalation_cells(  # noqa: SLF001
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        (_completed_task("unclear"),),
        {"task-1": {"digest": "d"}},
    )

    # No probe-observed signal languages: the default set ("lzh", "sw") ranks
    # first, so sw precedes ig, and only the deferred native ids are re-admitted.
    assert [cell.id for cell in cells] == [
        "cell/a@lang=sw@variant=native",
        "cell/a@lang=ig@variant=native",
    ]

    context = replace(
        prepared.assignment_context,
        techniques=(*prepared.assignment_context.techniques, *cells),
    )
    followups = service._language_escalation_followups(  # noqa: SLF001
        prepared.manifest, context, cells
    )
    assert [followup.arm["technique_id"] for followup in followups] == [
        "cell/a@lang=sw@variant=native",
        "cell/a@lang=ig@variant=native",
    ]
    assert all(followup.arm["source"] == "language_escalation" for followup in followups)


@pytest.mark.parametrize("outcome", ("confirmed", "refuted", "blocked"))
async def test_language_escalation_never_mutates_conclusive_or_blocked_cells(
    outcome: str,
) -> None:
    """Conclusive outcomes and infrastructure blocks never earn escalations."""
    prepared = _preparation((_technique("cell/a"),))
    store = _HistoryStore()
    store.rows["campaign-1"] = {
        "id": "campaign-1",
        "manifest": _escalation_envelope(prepared, ("ig", "sw")),
    }
    service = CampaignService(store, object(), None)  # type: ignore[arg-type]

    cells = await service._language_escalation_cells(  # noqa: SLF001
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        (_completed_task(outcome),),
        {"task-1": {"digest": "d"}},
    )

    assert cells == ()


async def test_language_escalation_respects_budget() -> None:
    prepared = _preparation((_technique("cell/a"),))
    store = _HistoryStore()
    store.rows["campaign-1"] = {
        "id": "campaign-1",
        "manifest": _escalation_envelope(prepared, ("ig", "sw")),
    }
    service = CampaignService(store, object(), None, language_escalation_budget=1)  # type: ignore[arg-type]

    cells = await service._language_escalation_cells(  # noqa: SLF001
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        (_completed_task("unclear"),),
        {"task-1": {"digest": "d"}},
    )

    assert [cell.id for cell in cells] == ["cell/a@lang=sw@variant=native"]


async def test_escalated_cells_leave_deferred_accounting() -> None:
    """A cell escalated and then run is attempted coverage: it lands in history
    via the journaled escalation payload and drops out of deferred rows."""
    prepared = _preparation((_technique("cell/a"),))
    store = _HistoryStore()
    store.rows["campaign-1"] = {
        "id": "campaign-1",
        "manifest": _escalation_envelope(prepared, ("ig", "sw")),
    }
    service = CampaignService(store, object(), None)  # type: ignore[arg-type]
    cells = await service._language_escalation_cells(  # noqa: SLF001
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        (_completed_task("unclear"),),
        {"task-1": {"digest": "d"}},
    )
    escalated = next(cell for cell in cells if cell.id == "cell/a@lang=sw@variant=native")

    class _LeadStore(_HistoryStore):
        def list_lead(self, _campaign_id):
            return [
                {
                    "payload": {
                        "kind": "language_escalation",
                        "cells": [_json_value(asdict(escalated))],
                    }
                }
            ]

    lead_store = _LeadStore()
    lead_store.rows["campaign-1"] = store.rows["campaign-1"]
    lead_service = CampaignService(lead_store, object(), None)  # type: ignore[arg-type]
    document = {
        "cells": (("task-1", "cell/a@lang=sw@variant=native", "unclear"),),
    }

    await lead_service._record_technique_history("campaign-1", document)  # noqa: SLF001

    recorded = [row for row in lead_store.history if "@lang=" in str(row["technique_id"])]
    assert recorded == [
        {
            "target_id": "target-1",
            "adapter_generation": "gen",
            "technique_id": "cell/a@lang=sw@variant=native",
            "controller_id": "direct",
            "payload_family": "native",
            "language_id": "sw",
            "mutation_id": "none",
            "outcome": "unclear",
            "source_campaign_id": "campaign-1",
        }
    ]

    remaining = _deferred_coverage_rows(
        _escalation_envelope(prepared, ("ig", "sw"))["technique_skips"],  # type: ignore[arg-type]
        {"cell/a@lang=sw@variant=native"},
    )
    assert [row["technique_id"] for row in remaining] == ["cell/a@lang=ig@variant=native"]
