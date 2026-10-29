from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_MODULE_PATH = Path(__file__).parents[3] / "scripts" / "mock_llm_server.py"
_SPEC = importlib.util.spec_from_file_location("merlin_mock_llm_server", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
MockBrain = _MODULE.MockBrain


def test_terminal_worker_dispatch_wins_over_retained_baseline_markers() -> None:
    brain = MockBrain(origin="http://127.0.0.1:8080")
    technique_id = "merlin-injection-taxonomy/cat/direct/override-baseline"

    response = brain.decide(
        {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are Merlin. Perform no more target actions. "
                        "Return the required structured result."
                    ),
                },
                {
                    "role": "user",
                    "content": f"DIRECT BASELINE for {technique_id}",
                },
            ]
        }
    )

    assert "tool" not in response
    result = json.loads(response["content"])
    assert result["attempted_techniques"] == [
        {
            "technique_id": "cat/direct/override-baseline",
            "outcome": "confirmed",
            "evidence_refs": ["mock-recon-evidence-0"],
        }
    ]


def test_initial_composition_is_not_reissued_after_task_reviews_arrive() -> None:
    brain = MockBrain(origin="http://127.0.0.1:8080")
    brief = {
        "tasks": [
            {
                "id": "campaign/x/lead-control/specialist_composition_get",
                "status": "completed",
                "result": json.dumps(
                    {
                        "initial_composition_request": "partition the sealed frontier",
                        "sealed_technique_ids": ["cat/direct/example"],
                    }
                ),
            }
        ],
        "task_reviews": [{"task_id": "campaign/x/task/1", "finding": "observed"}],
    }

    response = brain._lead([{"role": "user", "content": json.dumps(brief)}])

    decision = json.loads(response["content"])
    assert decision["coverage_complete"] is True
    assert decision["followups"] == []


def test_interrupted_target_call_is_retried_instead_of_terminalized() -> None:
    brain = MockBrain(origin="http://127.0.0.1:8080")
    response = brain._worker_direct(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "target_request",
                            "arguments": json.dumps({"input": "probe"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": (
                    "checkpoint_interrupted_before_execution: this tool call has no "
                    "acknowledged effect"
                ),
            },
        ]
    )

    assert response["tool"][0] == "target_request"


def test_acknowledged_target_call_is_cited_by_canonical_evidence_ref() -> None:
    brain = MockBrain(origin="http://127.0.0.1:8080")
    response = brain._worker_direct(
        [
            {
                "role": "user",
                "content": "DIRECT OBSERVATION for cat/direct/example",
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-fixture-1",
                        "function": {
                            "name": "target_request",
                            "arguments": json.dumps({"input": "probe"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-fixture-1",
                "content": '"fixture-bot reply — fixture reviewed win"',
            },
        ]
    )

    result = json.loads(response["content"])
    assert result["attempted_techniques"][0]["evidence_refs"] == ["target-call:call-fixture-1"]
