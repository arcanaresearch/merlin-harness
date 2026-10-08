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
            "outcome": "refuted",
            "evidence_refs": ["mock-recon-evidence-0"],
        }
    ]
