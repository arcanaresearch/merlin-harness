from __future__ import annotations

import importlib.util
import time
from pathlib import Path


def _script_module():
    path = Path(__file__).parents[3] / "scripts" / "acceptance_tui_campaign_pty.py"
    spec = importlib.util.spec_from_file_location("acceptance_tui_campaign_pty", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_completed_coverage_compares_semantic_cells_from_saturation() -> None:
    module = _script_module()
    status = {
        "saturation": {
            "catalogue_cell_owners": [["cell/a", "task/1"], ["cell/b", "task/2"]],
            "untested_cell_ids": ["cell/b"],
        },
        # Completion cells use concrete technique identities and are deliberately
        # unrelated to the semantic-cell namespace used by saturation.
        "completion_document": {"cells": [["task/1", "technique/concrete-a", "confirmed"]]},
    }

    assert module._completed_coverage(status) == ({"cell/a", "cell/b"}, {"cell/a"})


def test_report_wait_accepts_exact_exported_html_marker() -> None:
    module = _script_module()
    campaign_id = "campaign/acceptance-id"
    rendered = "Scan stopped\nHTML report:\n/temporary/reports/campaign-acceptance-id.html\n"

    class Tui:
        def __init__(self) -> None:
            self.output = bytearray()
            self.emitted = False

        def _pump(self, _timeout: float) -> bool:
            if not self.emitted:
                self.output.extend(rendered.encode())
                self.emitted = True
            return True

        def _scan_permissions(self) -> None:
            return None

        def fresh(self, start: int) -> str:
            return bytes(self.output[start:]).decode()

        def visible(self) -> str:
            return rendered

    result, decisions = module._wait_report_through_coverage_decisions(
        Tui(), 0.1, campaign_id=campaign_id
    )

    assert "campaign-acceptance-id.html" in result
    assert decisions == ()


def test_report_wait_uses_durable_progress_when_screen_is_stale() -> None:
    module = _script_module()

    class Tui:
        def __init__(self) -> None:
            self.output = bytearray(b"Wave 69 \xc2\xb7 frontier \xc2\xb7 campaign totals below")
            self.pumps = 0

        def _pump(self, _timeout: float) -> bool:
            self.pumps += 1
            time.sleep(0.02)
            if self.pumps == 5:
                self.output.extend(b"Campaign report ready")
            return True

        def _scan_permissions(self) -> None:
            return None

        def fresh(self, start: int) -> str:
            return bytes(self.output[start:]).decode()

        def visible(self) -> str:
            return bytes(self.output).decode()

    tui = Tui()
    status_calls = 0

    def status_reader():
        nonlocal status_calls
        status_calls += 1
        return {
            "wave_number": 69 + status_calls,
            "target_progress": {"target_exchange_count": status_calls},
        }

    result, decisions = module._wait_report_through_coverage_decisions(
        tui,
        0.1,
        campaign_id="campaign/acceptance-id",
        stall_timeout=0.05,
        status_reader=status_reader,
        status_poll_seconds=0,
    )

    assert "Campaign report ready" in result
    assert decisions == ()
    assert tui.pumps == 5
    assert status_calls >= 4


def test_approval_uses_durable_execution_without_mock_execute_response() -> None:
    module = _script_module()

    class Tui:
        def __init__(self) -> None:
            self.output = bytearray(b"review")
            self.sent: list[str] = []

        def wait_idle(self, _timeout: float) -> None:
            return None

        def send(self, text: str) -> None:
            self.sent.append(text)

        def _pump(self, _timeout: float) -> bool:
            return True

        def _scan_permissions(self) -> None:
            return None

    tui = Tui()

    def status_reader(_campaign_id: str):
        return {"status": "executing" if tui.sent else "prepared"}

    result = module._approve_prepared_campaign(
        tui,
        {"campaign/hybrid"},
        binary=Path("unused"),
        home=Path("unused"),
        workspace=Path("unused"),
        status_reader=status_reader,
    )

    assert result == ("campaign/hybrid", len(b"review"))
    assert tui.sent == ["approve"]
