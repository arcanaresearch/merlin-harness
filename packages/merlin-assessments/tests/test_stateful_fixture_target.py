from __future__ import annotations

import asyncio

import httpx
import pytest
from merlin_assessments import StatefulTargetFixtureServer
from merlin_contracts.target_adapter_compiler import compile_target_adapter


@pytest.mark.asyncio
async def test_many_logical_attempts_can_share_fewer_physical_states() -> None:
    async with StatefulTargetFixtureServer(maximum_parallel_states=2) as target:
        capabilities = target.adapter()["state_capabilities"]
        assert capabilities["maximum_parallel_states"] == 2
        assert [item["kind"] for item in capabilities["affordances"]] == ["provision_state"]
        async with httpx.AsyncClient(base_url=target.origin) as client:
            starts = await asyncio.gather(*(client.post("/api/start", json={}) for _ in range(5)))

    leases = [response.json()["leaseId"] for response in starts]
    physical_states = [response.json()["state"]["id"] for response in starts]
    assert len(set(leases)) == 5
    assert len(set(physical_states)) == 2
    assert physical_states == [
        "physical-state-1",
        "physical-state-2",
        "physical-state-1",
        "physical-state-2",
        "physical-state-1",
    ]


@pytest.mark.asyncio
async def test_input_limit_is_advertised_and_rejection_does_not_mutate_state() -> None:
    async with StatefulTargetFixtureServer(input_limit=5) as target:
        adapter = target.adapter()
        constraint = adapter["request_constraints"]
        assert constraint == [
            {
                "path": "/message",
                "kind": "string_length",
                "step_id": "interaction",
                "evaluation_root": "request_body",
                "maximum": 5,
                "unit": "unicode_characters",
                "source": "server_validation_response",
                "evidence_refs": ["evidence:fixture-input-limit"],
                "evidence_bindings": [
                    {
                        "evidence_ref": "evidence:fixture-input-limit",
                        "observed_maximum": 5,
                    }
                ],
                "confidence": 1.0,
            }
        ]
        compiled = compile_target_adapter(adapter).executable
        assert compiled is not None
        assert compiled.request_constraints[0].maximum == 5
        assert compiled.request_constraints[0].path == "/message"
        assert compiled.request_constraints[0].step_id == "interaction"
        async with httpx.AsyncClient(base_url=target.origin) as client:
            lease = (await client.post("/api/start", json={})).json()["leaseId"]
            rejected = await client.post(
                "/api/message", json={"leaseId": lease, "message": "123456"}
            )
            accepted = await client.post(
                "/api/message", json={"leaseId": lease, "message": "12345"}
            )

    assert rejected.status_code == 422
    assert rejected.json()["error"] == {
        "code": "input_too_long",
        "path": "/message",
        "maximum": 5,
        "unit": "unicode_characters",
    }
    assert rejected.json()["state"]["score"] == 0
    assert accepted.json()["state"]["score"] == 10
    assert accepted.json()["state"]["turnsRemaining"] == 3


@pytest.mark.asyncio
async def test_authoritative_snapshots_expose_before_after_delta_and_aliasing() -> None:
    async with StatefulTargetFixtureServer(maximum_parallel_states=1) as target:
        adapter = target.adapter()
        observations = adapter["state_capabilities"]["observations"]
        assert [item["role"] for item in observations] == [
            "state_identity",
            "progress_ordinal",
            "score",
            "budget_remaining",
            "terminal_status",
        ]
        score = observations[2]
        assert score["baseline_selector"] == {
            "step_id": "start",
            "evaluation_root": "response_body",
            "pointer": "/state/score",
        }
        assert score["result_selector"] == {
            "step_id": "interaction",
            "evaluation_root": "response_body",
            "pointer": "/state/score",
        }
        compiled = compile_target_adapter(adapter).executable
        assert compiled is not None
        assert compiled.state_capabilities is not None
        assert compiled.state_capabilities.observations[2].name == "objective_score"
        async with httpx.AsyncClient(base_url=target.origin) as client:
            first = (await client.post("/api/start", json={})).json()
            second = (await client.post("/api/start", json={})).json()
            after_first = (
                await client.post(
                    "/api/message",
                    json={"leaseId": first["leaseId"], "message": "candidate one"},
                )
            ).json()
            before_second = second["state"]
            after_second = (
                await client.post(
                    "/api/message",
                    json={"leaseId": second["leaseId"], "message": "candidate two"},
                )
            ).json()

    assert first["state"]["id"] == second["state"]["id"]
    assert after_first["state"]["score"] == before_second["score"] + 10
    assert after_second["state"]["score"] == after_first["state"]["score"] + 10
    assert after_second["state"]["stage"] == 2


@pytest.mark.asyncio
async def test_no_oracle_contract_omits_observations_without_blocking_execution() -> None:
    async with StatefulTargetFixtureServer(include_observations=False) as target:
        adapter = target.adapter()
        capabilities = adapter["state_capabilities"]
        assert "observations" not in capabilities
        assert capabilities["identity"] == {
            "baseline_selector": {
                "pointer": "/state/id",
                "step_id": "start",
                "evaluation_root": "response_body",
            },
            "result_selector": {
                "pointer": "/state/id",
                "step_id": "interaction",
                "evaluation_root": "response_body",
            },
        }
        compiled = compile_target_adapter(adapter).executable
        assert compiled is not None
        assert compiled.state_capabilities is not None
        assert compiled.state_capabilities.observations == ()
        async with httpx.AsyncClient(base_url=target.origin) as client:
            lease = (await client.post("/api/start", json={})).json()["leaseId"]
            response = await client.post(
                "/api/message", json={"leaseId": lease, "message": "opaque outcome"}
            )

    assert response.status_code == 200
    assert response.json()["state"]["status"] == "active"


@pytest.mark.asyncio
async def test_proven_fresh_state_supports_bounded_parallel_sessions() -> None:
    async with StatefulTargetFixtureServer(maximum_parallel_states=2, state_mode="fresh") as target:
        capabilities = target.adapter()["state_capabilities"]
        assert capabilities["maximum_parallel_states"] == 2
        assert capabilities["affordances"][0]["kind"] == "provision_fresh_state"
        assert capabilities["freshness_rules"] == [
            {
                "selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/state/fresh",
                },
                "match_values": [True],
                "evidence_refs": ["evidence:fixture-start"],
            }
        ]
        compiled = compile_target_adapter(target.adapter()).executable
        assert compiled is not None
        assert compiled.state_capabilities is not None
        assert compiled.state_capabilities.maximum_parallel_states == 2
        async with httpx.AsyncClient(base_url=target.origin) as client:
            starts = await asyncio.gather(*(client.post("/api/start", json={}) for _ in range(4)))

    assert len({response.json()["state"]["id"] for response in starts}) == 4
    assert all(response.json()["state"]["fresh"] is True for response in starts)


@pytest.mark.asyncio
async def test_checkpoint_restore_reestablishes_a_shared_baseline() -> None:
    async with StatefulTargetFixtureServer(
        maximum_parallel_states=1, state_mode="checkpoint"
    ) as target:
        adapter = target.adapter()
        capabilities = adapter["state_capabilities"]
        assert capabilities["affordances"][0]["kind"] == "restore_checkpoint"
        assert capabilities["identity"]["baseline_selector"]["step_id"] == "restore"
        assert [step["step_id"] for step in adapter["api_steps"]] == [
            "start",
            "restore",
            "interaction",
            "cleanup",
        ]
        compiled = compile_target_adapter(adapter).executable
        assert compiled is not None
        assert compiled.state_capabilities is not None
        assert compiled.state_capabilities.identity is not None
        assert compiled.state_capabilities.identity.baseline_selector.step_id == "restore"
        async with httpx.AsyncClient(base_url=target.origin) as client:
            first = (await client.post("/api/start", json={})).json()
            changed = (
                await client.post(
                    "/api/message",
                    json={"leaseId": first["leaseId"], "message": "change state"},
                )
            ).json()
            second = (await client.post("/api/start", json={})).json()
            restored = (
                await client.post("/api/restore", json={"leaseId": second["leaseId"]})
            ).json()

    assert first["state"]["id"] == second["state"]["id"]
    assert changed["state"]["score"] == 10
    assert second["state"]["score"] == 10
    assert restored["state"]["score"] == 0
    assert restored["state"]["turnsRemaining"] == 4


@pytest.mark.asyncio
async def test_runtime_rejection_exposes_an_unadvertised_constraint_for_regeneration() -> None:
    async with StatefulTargetFixtureServer(input_limit=7, advertise_input_limit=False) as target:
        adapter = target.adapter()
        assert "request_constraints" not in adapter
        compiled = compile_target_adapter(adapter).executable
        assert compiled is not None
        rejection = compiled.steps[1].structured_constraint_rejections[0]
        assert rejection.status_codes == (422,)
        assert rejection.request_path == "/message"
        assert rejection.maximum_pointer == "/error/maximum"
        async with httpx.AsyncClient(base_url=target.origin) as client:
            lease = (await client.post("/api/start", json={})).json()["leaseId"]
            rejected = await client.post(
                "/api/message", json={"leaseId": lease, "message": "too long"}
            )
            regenerated = await client.post(
                "/api/message", json={"leaseId": lease, "message": "shorter"}
            )

    assert rejected.status_code == 422
    assert rejected.json()["error"] == {
        "code": "input_too_long",
        "path": "/message",
        "maximum": 7,
        "unit": "unicode_characters",
    }
    assert regenerated.status_code == 200
    assert regenerated.json()["state"]["score"] == 10
