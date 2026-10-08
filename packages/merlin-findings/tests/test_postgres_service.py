from pathlib import Path

import pytest
from merlin_contracts import (
    AttackPathStep,
    CampaignObservation,
    EvidenceWitness,
    FindingDraft,
    PartialComplianceSignal,
    ReportRequest,
)
from merlin_findings import FindingService
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    MigrationRunner,
    PostgresFindingStore,
    SessionRepository,
)


@pytest.mark.asyncio
async def test_completion_reader_is_unbounded_across_pg0_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres-unbounded-findings"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("unbounded completion findings")
            session_id = str(session.id)
            service = FindingService(
                PostgresFindingStore(connection), workspace_root=tmp_path / "workspace"
            )
            for index in range(101):
                await service.create_draft(
                    session_id,
                    FindingDraft(
                        title=f"Finding {index}",
                        severity="low",
                        summary="Pending validation must remain visible.",
                        reproduction=f"Replay attempt {index}.",
                        remediation="Reconcile the boundary.",
                        creator=f"worker-{index}",
                        target_id="target-1",
                        technique_id=f"technique-{index}",
                    ),
                )

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            service = FindingService(
                PostgresFindingStore(connection), workspace_root=tmp_path / "workspace"
            )
            assert len(await service.list_findings(session_id)) == 100
            assert len(await service.list_findings_for_completion(session_id)) == 101


@pytest.mark.asyncio
async def test_pg0_service_lifecycle_and_atomic_report(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("finding service")
            service = FindingService(
                PostgresFindingStore(connection),
                workspace_root=tmp_path / "workspace",
            )
            finding = await service.create_draft(
                session.id,
                FindingDraft(
                    title="Prompt disclosure",
                    severity="high",
                    summary="Protected instructions were disclosed.",
                    reproduction="Replay the evidence exchange.",
                    remediation="Harden instruction and output controls.",
                    creator="agent:red-team",
                    evidence_references=["evidence://turn/4"],
                ),
            )
            duplicate = await service.create_draft(
                session.id,
                FindingDraft(
                    title="Prompt disclosure",
                    severity="critical",
                    summary="Same claim from another worker.",
                    reproduction="Replay the evidence exchange.",
                    remediation="Alternative advice.",
                    creator="agent:validator",
                    evidence_references=["evidence://turn/5"],
                ),
            )
            assert duplicate.id == finding.id
            validated = await service.validate_finding(
                session.id,
                finding.id,
                summary="Independent replay disclosed the protected instructions.",
                witnesses=[
                    EvidenceWitness(
                        reference="evidence://turn/5",
                        digest="sha256:" + "b" * 64,
                        source="independent-validator",
                    )
                ],
            )
            accepted = await service.accept_finding(session.id, finding.id)
            result = await service.generate_report(
                ReportRequest(
                    session_id=session.id,
                    format="markdown",
                    output=Path("reports/accepted.md"),
                )
            )

            assert accepted.status == "accepted"
            assert validated.validation == "validated"
            assert result.finding_count == 1
            assert result.output.read_text().count("Prompt disclosure") == 1
            assert not list(result.output.parent.glob(f".{result.output.name}.*"))


@pytest.mark.asyncio
async def test_finding_reduction_and_absence_authority_survive_postgres_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres"
    session_id = finding_id = refuted_id = ""
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("durable finding reduction")
            session_id = str(session.id)
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                "campaign/findings-restart",
                session_id,
                {"objective": "prove durable finding state"},
                "sha256:reviewed-findings",
            )
            campaigns.record_observation(
                CampaignObservation(
                    id="observation-negative",
                    campaign_id="campaign/findings-restart",
                    task_id="task-negative",
                    target_id="target-1",
                    technique_id="negative-control",
                    outcome="not_observed",
                    absence_authority="bounded",
                    coverage="Three declared variants and their negative controls.",
                    summary="No bypass was observed within the declared bound.",
                )
            )
            campaigns.record_observation(
                CampaignObservation(
                    id="observation-unclear",
                    campaign_id="campaign/findings-restart",
                    task_id="task-unclear",
                    target_id="target-1",
                    technique_id="truncated-response",
                    outcome="inconclusive",
                    summary="The response ended before the oracle could be evaluated.",
                )
            )
            service = FindingService(
                PostgresFindingStore(connection),
                workspace_root=tmp_path / "workspace",
            )
            finding = await service.create_draft(
                session_id,
                FindingDraft(
                    title="Durable policy bypass",
                    severity="high",
                    summary="The reviewed boundary changed.",
                    reproduction="Replay the bounded exchange.",
                    remediation="Enforce the policy boundary.",
                    creator="worker-primary",
                    target_id="target-1",
                    technique_id="policy-override",
                    evidence_references=["evidence://exchange/1"],
                    attack_path=[
                        AttackPathStep(
                            ordinal=0,
                            action="Submit reviewed policy override",
                            observation="Target granted the forbidden outcome",
                            witness_references=["evidence://exchange/1"],
                        )
                    ],
                ),
            )
            finding_id = finding.id
            refuted = await service.create_draft(
                session_id,
                FindingDraft(
                    title="Suspected disclosure",
                    severity="medium",
                    summary="A worker suspected protected data disclosure.",
                    reproduction="Replay the control exchange.",
                    remediation="No change until independently assessed.",
                    creator="worker-primary",
                    target_id="target-1",
                    technique_id="data-disclosure",
                    evidence_references=["evidence://exchange/control"],
                ),
            )
            refuted_id = refuted.id

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            service = FindingService(
                PostgresFindingStore(connection), workspace_root=tmp_path / "workspace"
            )
            duplicate = await service.create_draft(
                session_id,
                FindingDraft(
                    title="Durable policy bypass",
                    severity="critical",
                    summary="Independent worker observed the same claim.",
                    reproduction="Replay the bounded exchange.",
                    remediation="Add defense in depth.",
                    creator="worker-validator",
                    target_id="target-1",
                    technique_id="policy-override",
                    evidence_references=["evidence://exchange/2"],
                ),
            )
            assert duplicate.id == finding_id
            assert duplicate.evidence_references == [
                "evidence://exchange/1",
                "evidence://exchange/2",
            ]
            validated = await service.validate_finding(
                session_id,
                finding_id,
                summary="Independent reproduction confirmed the same boundary change.",
                witnesses=[
                    EvidenceWitness(
                        reference="evidence://exchange/2",
                        digest="sha256:" + "d" * 64,
                        source="worker-validator",
                    )
                ],
            )
            assert validated.validation == "validated"
            assert validated.attack_path[0].witness_references == ["evidence://exchange/1"]
            refuted = await service.refute_finding(
                session_id,
                refuted_id,
                summary="Independent control showed the text was public and not protected data.",
                witnesses=[
                    EvidenceWitness(
                        reference="evidence://exchange/control",
                        digest="sha256:" + "e" * 64,
                        source="worker-validator",
                    )
                ],
            )
            assert refuted.validation == "refuted"
            observations = CampaignStore(connection).list_observations("campaign/findings-restart")
            negative = next(item for item in observations if item.outcome == "not_observed")
            unclear = next(item for item in observations if item.outcome == "inconclusive")
            assert negative.absence_authority == "bounded"
            assert negative.coverage == "Three declared variants and their negative controls."
            assert unclear.absence_authority == "none"


@pytest.mark.asyncio
async def test_validator_owned_reproduction_evidence_survives_postgres_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-validator"
    result_digest = "sha256:" + "e" * 64
    result_reference = (
        "merlin-result://schedule/validation/node/worker-validator/attempt/attempt-1"
        f"?digest={result_digest}"
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("validator evidence restart")
            session_id = str(session.id)
            service = FindingService(
                PostgresFindingStore(connection),
                workspace_root=tmp_path / "workspace",
                validator_evidence_verifier=lambda _item, _session, _target: True,
            )
            finding = await service.create_draft(
                session_id,
                FindingDraft(
                    title="Reproduced boundary failure",
                    severity="high",
                    summary="The boundary changed.",
                    reproduction="Replay the bounded exchange.",
                    remediation="Enforce the boundary.",
                    creator="worker-primary",
                    target_id="target-1",
                    technique_id="policy-override",
                    evidence_references=["evidence://source/1"],
                ),
            )
            finding_id = finding.id
            await service.validate_finding(
                session_id,
                finding_id,
                summary="Validator reproduced the target effect.",
                witnesses=[
                    EvidenceWitness(
                        reference=result_reference,
                        digest=result_digest,
                        source="independent-validator:validation:attempt-1:digest",
                    )
                ],
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            restored = await FindingService(
                PostgresFindingStore(connection), workspace_root=tmp_path / "workspace"
            ).list_findings(session_id)

    finding = next(item for item in restored if item.id == finding_id)
    assert finding.validation == "validated"
    assert result_reference in finding.evidence_references


@pytest.mark.asyncio
async def test_partial_compliance_survives_postgres_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres-partial"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("partial restart")
            session_id = str(session.id)
            service = FindingService(
                PostgresFindingStore(connection),
                workspace_root=tmp_path / "workspace",
            )
            finding = await service.create_draft(
                session_id,
                FindingDraft(
                    title="Partial boundary engagement",
                    severity="medium",
                    summary="The target engaged but refused the terminal step.",
                    reproduction="Replay the bounded exchange.",
                    remediation="Enforce the boundary consistently.",
                    creator="worker-primary",
                    evidence_references=["evidence://source/1"],
                ),
            )
            finding_id = finding.id
            partial = await service.mark_partial(
                session_id,
                finding_id,
                summary="Target engaged with the premise, refused the action.",
                signal=PartialComplianceSignal(
                    observed="Target restated the injected premise.",
                    refused="Target declined the unauthorized action.",
                    evidence_refs=("evidence://source/1",),
                ),
            )
            assert partial.validation == "partial"

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            restored = await FindingService(
                PostgresFindingStore(connection), workspace_root=tmp_path / "workspace"
            ).list_findings(session_id)

    finding = next(item for item in restored if item.id == finding_id)
    assert finding.validation == "partial"
    assert finding.partial_signal is not None
    assert finding.partial_signal.observed == "Target restated the injected premise."
    assert finding.partial_signal.refused == "Target declined the unauthorized action."
