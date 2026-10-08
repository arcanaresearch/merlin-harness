from types import SimpleNamespace

import pytest
from merlin_agents import (
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    DurableAgentService,
    DynamicSpecialistProposal,
    WorkerAssignment,
    compile_dynamic_specialist,
)
from merlin_contracts import SwarmRequest


class Provider:
    async def stream(self, request):
        raise AssertionError("composition tests never reach the provider")
        yield


class Tools:
    def describe(self):
        return ()

    async def invoke(self, name, context, arguments):
        raise AssertionError("no tools admitted")


class Admitter:
    def __init__(self, records):
        self.records = tuple(records)
        self.calls = 0

    async def __call__(self, request, context):
        self.calls += 1
        return self.records


def _technique(technique_id: str) -> AssignmentTechnique:
    return AssignmentTechnique(
        id=technique_id,
        objective=f"Assess {technique_id}",
        tags=frozenset({technique_id}),
        guidance_ids=(),
    )


def _context(technique_count: int = 2) -> AssignmentContext:
    return AssignmentContext(
        target_profile_id="target-1",
        techniques=tuple(_technique(f"case-{index}") for index in range(technique_count)),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider-1",
        authority_ids=("target:target-1",),
        budget=AssignmentBudget(max_turns=8, max_tokens=8_000, max_seconds=120),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        provider_model="model-1",
    )


def _parent() -> WorkerAssignment:
    return WorkerAssignment(
        id="parent-ceiling",
        objective="Campaign authority ceiling",
        profile_id="injection",
        target_profile_id="target-1",
        technique_ids=("case-0",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider-1",
        authority_ids=("target:target-1",),
        budget=AssignmentBudget(max_turns=8, max_tokens=8_000, max_seconds=120),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        provider_model="model-1",
    )


def _wave0_record(context: AssignmentContext, parent: WorkerAssignment, *, label: str):
    proposal = DynamicSpecialistProposal(
        campaign_id="campaign-1",
        label=label,
        objective=f"Probe the reviewed surface for {label}.",
        source_task_ids=("recon-1",),
        evidence_refs=("merlin-result://recon/recon-1",),
        evidence_expectation="Cite the admitted recon evidence for every claim.",
    )
    return compile_dynamic_specialist(
        proposal,
        parent=parent,
        context=context,
        graph_id="campaign-1",
        wave=0,
        active_count=0,
        campaign_count=0,
    )


def _composition_request(*, composition: str, max_workers: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        session_id="session-1",
        objective="Assess the reviewed target",
        max_workers=max_workers,
        allow_recursive_delegation=False,
        max_delegation_depth=0,
        specialist_composition=composition,
    )


def _service(admitter: Admitter | None) -> DurableAgentService:
    return DurableAgentService(
        Provider(),  # type: ignore[arg-type]
        Tools(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        initial_composition_admitter=admitter,
    )


@pytest.mark.parametrize("composition", ["opinionated", "autonomous"])
async def test_initial_composition_comes_from_wave0_dynamic_specialists(
    composition: str,
) -> None:
    context = _context()
    record = _wave0_record(context, _parent(), label="dynamic-auth-probe")
    admitter = Admitter((record,))
    service = _service(admitter)
    request = _composition_request(composition=composition)

    assignments, max_workers, _profiles = await service._compose_initial_assignments(  # noqa: SLF001
        request, context
    )

    assert admitter.calls == 1
    assert assignments == (record.assignment,)
    assert max_workers == 1


async def test_missing_composition_field_fails_closed() -> None:
    context = _context()
    admitter = Admitter((_wave0_record(context, _parent(), label="dynamic-auth-probe"),))
    service = _service(admitter)
    request = SimpleNamespace(
        session_id="session-1",
        objective="Assess the reviewed target",
        max_workers=3,
        allow_recursive_delegation=False,
        max_delegation_depth=0,
    )

    with pytest.raises(ValueError, match="specialist composition"):
        await service._compose_initial_assignments(request, context)  # noqa: SLF001
    assert admitter.calls == 0


async def test_missing_admitter_fails_closed() -> None:
    service = _service(None)
    request = _composition_request(composition="opinionated")

    with pytest.raises(ValueError, match="specialist composition"):
        await service._compose_initial_assignments(request, _context())  # noqa: SLF001


async def test_empty_admitted_composition_fails_closed() -> None:
    service = _service(Admitter(()))
    request = _composition_request(composition="opinionated")

    with pytest.raises(ValueError, match="specialist composition"):
        await service._compose_initial_assignments(request, _context())  # noqa: SLF001


async def test_swarm_request_default_composition_invokes_admitter() -> None:
    context = _context()
    record = _wave0_record(context, _parent(), label="dynamic-auth-probe")
    admitter = Admitter((record,))
    service = _service(admitter)
    request = SwarmRequest(session_id="session-1", objective="Assess the reviewed target")
    assert request.specialist_composition == "opinionated"

    assignments, max_workers, _profiles = await service._compose_initial_assignments(  # noqa: SLF001
        request, context
    )

    assert admitter.calls == 1
    assert assignments == (record.assignment,)
    assert max_workers == 1


async def test_concurrency_is_derived_from_the_admitted_set_under_the_ceiling() -> None:
    context = _context(technique_count=3)
    parent = _parent()
    records = tuple(
        _wave0_record(context, parent, label=f"dynamic-probe-{index}") for index in range(6)
    )
    service = _service(Admitter(records))
    request = _composition_request(composition="autonomous", max_workers=3)

    assignments, max_workers, _profiles = await service._compose_initial_assignments(  # noqa: SLF001
        request, context
    )

    assert assignments == tuple(record.assignment for record in records)
    assert max_workers == 6


async def test_unknown_composition_policy_fails_closed() -> None:
    service = _service(Admitter(()))
    request = _composition_request(composition="experimental")

    with pytest.raises(ValueError, match="specialist_composition"):
        await service._compose_initial_assignments(request, _context())  # noqa: SLF001


async def test_admitter_must_return_wave0_dynamic_records() -> None:
    context = _context()
    record = _wave0_record(context, _parent(), label="dynamic-auth-probe")
    late = compile_dynamic_specialist(
        DynamicSpecialistProposal(
            campaign_id="campaign-1",
            label="dynamic-late-probe",
            objective="Probe after wave zero.",
            source_task_ids=("recon-1",),
            evidence_refs=("merlin-result://recon/recon-1",),
            evidence_expectation="Cite the admitted recon evidence for every claim.",
        ),
        parent=_parent(),
        context=context,
        graph_id="campaign-1",
        wave=2,
        active_count=1,
        campaign_count=1,
    )
    service = _service(Admitter((record, late)))
    request = _composition_request(composition="autonomous")

    with pytest.raises(ValueError, match="wave-0"):
        await service._compose_initial_assignments(request, context)  # noqa: SLF001
