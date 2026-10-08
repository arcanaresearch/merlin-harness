#!/usr/bin/env python3
"""Production portable-memory export/import and specialist-use acceptance."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from merlin_agents import (
    AssignmentBudget,
    PortableMemoryQuery,
    WorkerAssignment,
    WorkerHandoff,
    WorkerProfile,
    WorkerRuntime,
)
from merlin_config import ConfigPaths, CredentialStore
from merlin_contracts import ChatRequest, ModelCompleted, ModelEvent, TextDelta
from merlin_memory import (
    Applicability,
    BundleProvenance,
    ImportPolicy,
    MemoryRecord,
    NonSecretConfig,
    PortableMemoryService,
    ReviewedSkill,
    build_bundle,
    encode_bundle,
)
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    MigrationRunner,
    PortableMemoryRepository,
)
from pydantic import ValidationError


class InspectingProvider:
    def __init__(self, record_id: str, provenance: BundleProvenance, digest: str) -> None:
        self.record_id = record_id
        self.provenance = provenance
        self.digest = digest
        self.requests: list[ChatRequest] = []

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        system = request.messages[0].content
        assert system.count(f"Memory {self.record_id} (reviewed_skill)") == 1
        assert system.count(self.provenance.source) == 1
        assert system.count(self.provenance.reviewed_by[0]) == 1
        assert system.count(self.digest) == 1
        assert "private transcript prose" not in system
        assert "source-api-secret" not in system
        assert "allow-always" not in system
        yield TextDelta(text='{"used_reviewed_memory":true}')
        yield ModelCompleted()


class NoTools:
    def describe(self):
        return ()

    async def invoke(self, name, context, arguments):
        raise AssertionError("no tools admitted")


async def main() -> None:
    now = datetime.now(UTC)
    provenance = BundleProvenance(
        source="merlin://reviewed/source-repository",
        exported_by="operator/source",
        reviewed_by=("operator/reviewer",),
        created_at=now,
    )
    reviewed = MemoryRecord(
        id="skill/reviewed-exact",
        payload=ReviewedSkill(
            name="reviewed-exact",
            instructions="Check the exact reviewed response boundary and cite direct evidence.",
        ),
        provenance=provenance,
        applicability=Applicability(
            repository_ids=("repo/accepted",),
            target_ids=("target/chat",),
            provider_ids=("provider/attacker",),
            operator_ids=("operator/destination",),
            scope_ids=("scope/reviewed",),
            tags=("acceptance",),
        ),
        redactions=("private prose and credentials removed",),
        expires_at=now + timedelta(days=1),
    )
    policy = ImportPolicy(
        allowed_kinds=frozenset({"reviewed_skill", "non_secret_config"}),
        allowed_scope_ids=frozenset({"scope/reviewed"}),
        max_bytes=64_000,
    )

    with TemporaryDirectory(prefix="merlin-memory-acceptance-") as temporary:
        root = Path(temporary)
        source_home = root / "source-home"
        destination_home = root / "destination-home"
        CredentialStore(ConfigPaths(source_home)).save({"api_key": "source-api-secret"})
        (source_home / "private-prose.txt").write_text("private transcript prose", encoding="utf-8")
        source_db = Database(DatabaseConfig(data_dir=source_home / "postgres"))
        destination_db = Database(DatabaseConfig(data_dir=destination_home / "postgres"))
        with source_db, destination_db:
            with (
                source_db.connect() as source_connection,
                destination_db.connect() as dest_connection,
            ):
                MigrationRunner(source_connection).apply()
                MigrationRunner(dest_connection).apply()
                source_repository = PortableMemoryRepository(source_connection)
                source_bundle = build_bundle("bundle/source-reviewed", provenance, (reviewed,))
                source_repository.quarantine(source_bundle, operator_id="operator/source")
                source_repository.accept(
                    source_bundle.bundle_id, (reviewed.id,), operator_id="operator/reviewer"
                )
                source_service = PortableMemoryService(
                    source_repository,
                    policy,
                    non_secret_config=lambda: {
                        "provider": {"model": "reviewed-model"},
                        "permission_mode": "prompt",
                    },
                    operator_id="operator/source",
                    workspace_root=root,
                )
                exported_path = root / "reviewed-memory.json"
                await source_service.export_file(exported_path)
                exported = exported_path.read_bytes()
                assert b"source-api-secret" not in exported
                assert b"private transcript prose" not in exported
                assert b"allow-always" not in exported

                destination_repository = PortableMemoryRepository(dest_connection)
                destination_service = PortableMemoryService(
                    destination_repository,
                    policy,
                    operator_id="operator/destination",
                    workspace_root=root,
                )
                imported = await destination_service.import_file(exported_path)
                pending = await destination_service.list_memory("quarantined")
                accepted_ids = [
                    str(item["record_id"]) for item in pending if item["kind"] == "reviewed_skill"
                ]
                rejected_ids = [
                    str(item["record_id"]) for item in pending if item["kind"] != "reviewed_skill"
                ]
                assert accepted_ids == [reviewed.id] and len(rejected_ids) == 1
                await destination_service.accept(str(imported["bundle_id"]), accepted_ids)
                await destination_service.reject(
                    str(imported["bundle_id"]), rejected_ids, "config not selected for this run"
                )

                query = PortableMemoryQuery(
                    session_id="session/production-acceptance",
                    task_id="task/reviewed-specialist",
                    repository_id="repo/accepted",
                    target_id="target/chat",
                    provider_id="provider/attacker",
                    operator_id="operator/destination",
                    scope_ids=("scope/reviewed",),
                    tags=("acceptance",),
                )
                active = await destination_service.retrieve(query)
                assert tuple(record.id for record in active) == (reviewed.id,)
                provider = InspectingProvider(reviewed.id, provenance, reviewed.digest)
                profile = WorkerProfile(
                    "reviewer", 1, "review", "Use only attributable evidence.", frozenset()
                )
                assignment = WorkerAssignment(
                    id="task/reviewed-specialist",
                    objective="Apply the reviewed response-boundary check.",
                    profile_id="reviewer",
                    target_profile_id="target/chat",
                    technique_ids=("reviewed-boundary",),
                    guidance_ids=(),
                    tool_ids=(),
                    skill_ids=(),
                    provider_id="provider/attacker",
                    authority_ids=("scope/reviewed",),
                    budget=AssignmentBudget(1, 1_000, 10),
                    input_schema='{"type":"object"}',
                    output_schema=(
                        '{"type":"object","required":["used_reviewed_memory"],'
                        '"properties":{"used_reviewed_memory":{"const":true}}}'
                    ),
                )
                result = await WorkerRuntime(provider, NoTools()).run(
                    WorkerHandoff(
                        query.session_id,
                        str(root),
                        "acceptance-model",
                        profile,
                        assignment,
                        active,
                    )
                )
                assert result.status == "completed"
                assert result.memory_record_ids == (reviewed.id,)
                turn_id = "turn/reviewed-specialist/exact"
                await destination_service.record_turn_use(query, turn_id, result.memory_record_ids)
                assert destination_repository.turn_uses(query.session_id, turn_id) == (reviewed.id,)

                wrong_scope = replace(query, repository_id="repo/wrong")
                assert await destination_service.retrieve(wrong_scope) == ()
                try:
                    await destination_service.record_turn_use(
                        wrong_scope, "turn/wrong-scope", (reviewed.id,)
                    )
                except ValueError:
                    pass
                else:
                    raise AssertionError("wrong-scope turn use did not fail closed")

                await _negative_import_checks(
                    destination_service, exported_path, exported, root, provenance, policy, now
                )

        print(
            json.dumps(
                {
                    "export_import": "reviewed-only",
                    "subset_review": True,
                    "specialist_provenance_visible": True,
                    "exact_turn_use": reviewed.id,
                    "negative_boundaries": "fail-closed",
                },
                sort_keys=True,
            )
        )


async def _negative_import_checks(
    service: PortableMemoryService,
    exported_path: Path,
    exported: bytes,
    root: Path,
    provenance: BundleProvenance,
    policy: ImportPolicy,
    now: datetime,
) -> None:
    async def rejected(path: Path) -> None:
        try:
            await service.import_file(path)
        except (ValueError, ValidationError):
            return
        raise AssertionError(f"unsafe import was accepted: {path.name}")

    tampered = json.loads(exported)
    tampered["records"][0]["payload"]["instructions"] = "tampered"
    tampered_path = root / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    await rejected(tampered_path)
    oversize_path = root / "oversize.json"
    oversize_path.write_bytes(b"x" * (policy.max_bytes + 1))
    await rejected(oversize_path)
    expired = MemoryRecord(
        id="lesson/expired",
        payload=ReviewedSkill(name="expired", instructions="Expired reviewed memory."),
        provenance=provenance,
        expires_at=now - timedelta(seconds=1),
    )
    expired_path = root / "expired.json"
    expired_path.write_bytes(encode_bundle(build_bundle("bundle/expired", provenance, (expired,))))
    await rejected(expired_path)
    await rejected(exported_path)  # existing record ids conflict in the destination

    try:
        NonSecretConfig(values={"permission_grant": "allow-always"})
    except ValidationError:
        pass
    else:
        raise AssertionError("portable authority grant was accepted")
    try:
        BundleProvenance(source="local", exported_by="operator", reviewed_by=(), created_at=now)
    except ValidationError:
        pass
    else:
        raise AssertionError("unreviewed provenance was accepted")


if __name__ == "__main__":
    asyncio.run(main())
