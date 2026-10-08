from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _script_module():
    path = Path(__file__).parents[3] / "scripts" / "acceptance_swarm.py"
    spec = importlib.util.spec_from_file_location("acceptance_swarm", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recommended_preparation_bound_requires_positive_frontier() -> None:
    module = _script_module()

    assert (
        module._assert_recommended_preparation_bound(  # noqa: SLF001
            {"saturation": {"admitted_techniques": 12}}
        )
        == 12
    )
    with pytest.raises(AssertionError, match="no techniques"):
        module._assert_recommended_preparation_bound(  # noqa: SLF001
            {"saturation": {"admitted_techniques": 0}}
        )


@pytest.mark.asyncio
async def test_live_acceptance_preflight_finds_missing_restart_suites(tmp_path: Path) -> None:
    module = _script_module()
    present = tmp_path / "present.json"
    present.write_text("{}", encoding="utf-8")
    missing = tmp_path / "missing.json"

    assert await module._missing_suite_files([present, missing]) == [str(missing)]


@pytest.mark.asyncio
async def test_live_acceptance_timeout_cancels_exact_campaign_with_last_status() -> None:
    module = _script_module()

    class Client:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        async def campaign_status(self, campaign_id: str):
            return {"campaign_id": campaign_id, "status": "executing", "lead_events": 9}

        async def cancel_campaign(self, campaign_id: str):
            self.cancelled.append(campaign_id)
            return 1

    client = Client()
    with pytest.raises(AssertionError, match="missed its terminal deadline") as caught:
        await module._wait_for_terminal_campaign(
            client,
            "campaign/slow",
            timeout_seconds=0.001,
        )

    assert client.cancelled == ["campaign/slow"]
    assert '"lead_events": 9' in str(caught.value)


@pytest.mark.asyncio
async def test_live_acceptance_continues_required_coverage_extension() -> None:
    module = _script_module()

    class Client:
        def __init__(self) -> None:
            self.decisions: list[tuple[str, str, object]] = []

        async def campaign_status(self, campaign_id: str):
            if self.decisions:
                return {"campaign_id": campaign_id, "status": "completed"}
            return {
                "campaign_id": campaign_id,
                "status": "executing",
                "saturation_decision": {
                    "state": "awaiting_operator",
                    "purpose": "coverage_extension",
                    "request_id": f"coverage/{len(self.decisions)}",
                },
            }

        async def decide_campaign_saturation(
            self, campaign_id: str, decision: str, additional_waves: object
        ):
            self.decisions.append((campaign_id, decision, additional_waves))

    client = Client()
    result = await module._wait_for_terminal_campaign(  # noqa: SLF001
        client,
        "campaign/coverage",
        timeout_seconds=2,
    )

    assert result["status"] == "completed"
    assert client.decisions == [("campaign/coverage", "continue", None)]


@pytest.mark.asyncio
async def test_live_acceptance_stops_after_three_unresolved_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _script_module()
    # Pin the unattended extension cap: the helper otherwise reads the
    # operator's real config file (max_coverage_extensions).
    monkeypatch.setenv("MERLIN_LIVE_MAX_EXTENSIONS", "3")

    class Client:
        def __init__(self) -> None:
            self.decisions: list[tuple[str, str, object]] = []

        async def campaign_status(self, campaign_id: str):
            if self.decisions and self.decisions[-1][1] == "stop":
                return {"campaign_id": campaign_id, "status": "completed"}
            return {
                "campaign_id": campaign_id,
                "status": "executing",
                "saturation_decision": {
                    "state": "awaiting_operator",
                    "purpose": "coverage_extension",
                    "request_id": f"coverage/{len(self.decisions)}",
                },
            }

        async def decide_campaign_saturation(
            self, campaign_id: str, decision: str, additional_waves: object
        ):
            self.decisions.append((campaign_id, decision, additional_waves))

    client = Client()
    result = await module._wait_for_terminal_campaign(  # noqa: SLF001
        client,
        "campaign/unresolved",
        timeout_seconds=6,
    )

    assert result["status"] == "completed"
    assert [decision for _, decision, _ in client.decisions] == [
        "continue",
        "continue",
        "continue",
        "stop",
    ]


@pytest.mark.asyncio
async def test_existing_campaign_postcheck_uses_owning_session_and_durable_report() -> None:
    module = _script_module()

    class Client:
        def __init__(self) -> None:
            self.browser_session = None

        async def list_browser_contexts(self, session_id: str):
            self.browser_session = session_id
            return [{"latest_observation": "FareCare replied", "evidence": ["browser://1"]}]

        async def list_reports(self):
            return [
                {
                    "run": "campaign/completed",
                    "status": "ready · saturated · cleanup completed",
                    "exchange_count": 2,
                }
            ]

    status = {
        "status": "completed",
        "stop_reason": "saturated",
        "session_id": "owning-session",
        "lead_events": 2,
        "latest_lead": {"stop_reason": "saturated"},
        "latest_lead_stop_reason": "saturated",
        "completion_document": {
            "saturation": {
                "ready": True,
                "catalogue_cells_blocked": 0,
                "catalogue_cells_unclear": 0,
                "hypothesis_cells_blocked": 0,
                "hypothesis_cells_unclear": 0,
                "adaptive_unresolved": [],
            },
            "receipts": [{"kind": "target-interaction"}],
        },
        "waves": [
            {
                "wave": 0,
                "status": "completed",
                "tasks": [
                    {
                        "status": "completed",
                        "result": "Website chatbot refund evidence",
                    }
                ],
            }
        ],
    }
    client = Client()

    await module._assert_terminal_campaign_evidence(  # noqa: SLF001
        client, "campaign/completed", status
    )


@pytest.mark.asyncio
async def test_terminal_acceptance_allows_fully_executed_inconclusive_campaign() -> None:
    module = _script_module()

    class Client:
        async def list_reports(self):
            return [
                {
                    "run": "campaign/limited",
                    "status": "ready · completed with limitations · cleanup completed",
                    "exchange_count": 1,
                }
            ]

    status = {
        "status": "completed",
        "stop_reason": "completed_with_limitations",
        "lead_events": 2,
        "latest_lead": {"stop_reason": "completed_with_limitations"},
        "latest_lead_stop_reason": "completed_with_limitations",
        "completion_document": {
            "saturation": {
                "ready": False,
                "execution_complete": True,
                "catalogue_cells_blocked": 0,
                "catalogue_cells_unclear": 1,
                "hypothesis_cells_blocked": 0,
                "hypothesis_cells_unclear": 1,
                "adaptive_unresolved": ["hypothesis/example"],
            },
            "receipts": [{"kind": "target-interaction"}],
        },
        "waves": [
            {
                "wave": 0,
                "status": "completed",
                "tasks": [
                    {
                        "status": "completed",
                        "result": "Website chatbot refund evidence was inconclusive",
                    }
                ],
            }
        ],
    }

    await module._assert_terminal_campaign_evidence(  # noqa: SLF001
        Client(), "campaign/limited", status
    )


@pytest.mark.asyncio
async def test_terminal_acceptance_rejects_limitations_with_unfinished_execution() -> None:
    module = _script_module()

    class Client:
        async def list_reports(self):
            return []

    status = {
        "status": "completed",
        "stop_reason": "completed_with_limitations",
        "lead_events": 2,
        "latest_lead": {"stop_reason": "completed_with_limitations"},
        "latest_lead_stop_reason": "completed_with_limitations",
        "completion_document": {
            "saturation": {
                "ready": False,
                "execution_complete": False,
                "catalogue_cells_blocked": 0,
                "hypothesis_cells_blocked": 0,
            },
            "receipts": [{"kind": "target-interaction"}],
        },
        "waves": [
            {
                "wave": 0,
                "status": "completed",
                "tasks": [{"status": "completed", "result": "Website chatbot refund evidence"}],
            }
        ],
    }

    with pytest.raises(AssertionError, match="required execution remained"):
        await module._assert_terminal_campaign_evidence(  # noqa: SLF001
            Client(), "campaign/limited", status
        )
