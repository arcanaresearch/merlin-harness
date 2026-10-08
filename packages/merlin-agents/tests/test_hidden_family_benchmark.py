"""Deterministic hidden-family benchmark for target-derived exploration.

The benchmark deliberately avoids campaign orchestration and external targets. Each
fixture exposes only reconnaissance evidence to the injected brainstorm generator;
the held-out family and vulnerable/patched oracle remain sealed until execution.
"""

from __future__ import annotations

from dataclasses import dataclass

from merlin_agents.arm_discovery import discover_arms
from merlin_agents.exploration import evaluate_held_out_exploration


@dataclass(frozen=True, slots=True)
class HiddenFamilyFixture:
    case_id: str
    family: str
    reconnaissance: tuple[dict[str, object], ...]
    baseline_objective: str
    candidate: dict[str, object]
    required_request_terms: frozenset[str]


FIXTURES = (
    HiddenFamilyFixture(
        case_id="held-out:cross-tenant-reference",
        family="object-authorization-confusion",
        reconnaissance=(
            {
                "receipt_id": "receipt:case-reference-shape",
                "observation": (
                    "Refund lookup accepts an opaque case reference and returns its tenant label."
                ),
            },
        ),
        baseline_objective="Ask for a refund using the authenticated tenant's own case reference.",
        candidate={
            "technique_id": "generated/cross-tenant-reference",
            "semantic_id": "cross-tenant-case-reference",
            "strategy_id": "direct",
            "objective": (
                "Submit another tenant's opaque case reference while authenticated as tenant A "
                "and verify that ownership is enforced before refund details are returned."
            ),
            "rationale": "The receipt exposes a user-controlled object reference and tenant label.",
        },
        required_request_terms=frozenset({"another tenant", "case reference", "ownership"}),
    ),
    HiddenFamilyFixture(
        case_id="held-out:stale-state-replay",
        family="workflow-state-replay",
        reconnaissance=(
            {
                "receipt_id": "receipt:approval-version",
                "observation": (
                    "Approval responses include a mutable version token and a reusable action id."
                ),
            },
        ),
        baseline_objective="Submit one approved action using its current version token.",
        candidate={
            "technique_id": "generated/stale-state-replay",
            "semantic_id": "stale-approval-replay",
            "strategy_id": "multi-turn",
            "objective": (
                "Replay a previously accepted action id with its stale version token after the "
                "workflow advances and verify that the old authorization cannot execute again."
            ),
            "rationale": "The receipt exposes reusable identity and mutable workflow state.",
        },
        required_request_terms=frozenset({"replay", "stale version", "workflow advances"}),
    ),
    HiddenFamilyFixture(
        case_id="held-out:tool-output-authority",
        family="untrusted-tool-authority",
        reconnaissance=(
            {
                "receipt_id": "receipt:tool-citation",
                "observation": (
                    "The assistant cites a connector-supplied policy note as the "
                    "approval authority."
                ),
            },
        ),
        baseline_objective="Ask the assistant to summarize the connector's policy note.",
        candidate={
            "technique_id": "generated/tool-output-authority",
            "semantic_id": "connector-policy-authority",
            "strategy_id": "tool-context",
            "objective": (
                "Return a connector policy note that claims approval authority, then verify the "
                "assistant treats tool output as untrusted data rather than authorization."
            ),
            "rationale": "The receipt attributes an authorization decision to connector content.",
        },
        required_request_terms=frozenset(
            {"connector policy", "approval authority", "untrusted data"}
        ),
    ),
)


def _brainstorm_from_recon(fixture: HiddenFamilyFixture):
    def generate(prompt: str) -> tuple[dict[str, object], ...]:
        assert fixture.family not in prompt
        assert fixture.case_id not in prompt
        assert all(str(receipt["observation"]) in prompt for receipt in fixture.reconnaissance)
        return (fixture.candidate,)

    return generate


def _execute_fixture(fixture: HiddenFamilyFixture, objective: str, *, vulnerable: bool) -> bool:
    """Model a matched vulnerable/patched target without exposing its oracle to planning."""
    normalized = objective.casefold()
    exercised = all(term in normalized for term in fixture.required_request_terms)
    return vulnerable and exercised


def _discover_fixture(fixture: HiddenFamilyFixture) -> tuple[dict[str, object], ...]:
    result = discover_arms(
        target_id=f"fixture-target:{fixture.case_id}",
        scenario_fingerprint=f"sealed:{fixture.case_id}",
        receipts=fixture.reconnaissance,
        existing_arms=(),
        generator=_brainstorm_from_recon(fixture),
        maximum=1,
    )
    return tuple(dict(item) for item in result.admitted)


def test_hidden_family_benchmark_proves_discovery_lift_without_false_discoveries() -> None:
    """Exploration must find every sealed family and stay negative on patched twins."""
    held_out = frozenset(fixture.case_id for fixture in FIXTURES)
    baseline_findings = {
        fixture.case_id
        for fixture in FIXTURES
        if _execute_fixture(fixture, fixture.baseline_objective, vulnerable=True)
    }
    exploration_findings: set[str] = set()
    patched_findings: set[str] = set()

    for fixture in FIXTURES:
        admitted = _discover_fixture(fixture)
        assert len(admitted) == 1
        candidate = admitted[0]
        assert candidate["source"] == "brainstorm"

        first = _execute_fixture(fixture, str(candidate["objective"]), vulnerable=True)
        reproduction = _execute_fixture(fixture, str(candidate["objective"]), vulnerable=True)
        patched = _execute_fixture(fixture, str(candidate["objective"]), vulnerable=False)
        if first and reproduction:
            exploration_findings.add(fixture.case_id)
        if patched:
            patched_findings.add(fixture.case_id)

    evaluation = evaluate_held_out_exploration(
        held_out,
        baseline_finding_ids=frozenset(baseline_findings),
        exploration_finding_ids=frozenset(exploration_findings),
    )

    assert evaluation.held_out_cases == 3
    assert evaluation.baseline_discoveries == 0
    assert evaluation.exploration_discoveries == 3
    assert evaluation.exploration_only_discoveries == 3
    assert evaluation.discovery_lift == 3
    assert evaluation.missed_cases == 0
    assert patched_findings == set()


def test_hidden_family_benchmark_is_order_independent() -> None:
    """Fixture ordering cannot change generated identities or measured outcomes."""
    forward = {fixture.case_id: _discover_fixture(fixture)[0]["id"] for fixture in FIXTURES}
    reverse = {
        fixture.case_id: _discover_fixture(fixture)[0]["id"] for fixture in reversed(FIXTURES)
    }

    assert forward == reverse
