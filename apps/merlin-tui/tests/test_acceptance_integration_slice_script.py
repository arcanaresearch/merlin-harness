from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest


def _script_module():
    path = Path(__file__).parents[3] / "scripts" / "acceptance_integration_slice.py"
    spec = importlib.util.spec_from_file_location("acceptance_integration_slice", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checkpoint_write_is_atomic_and_owner_only(tmp_path: Path) -> None:
    module = _script_module()
    path = tmp_path / "private" / "state.json"

    module._write_state(path, {"status": "running"})

    assert json.loads(path.read_text()) == {"status": "running"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.glob(".state-*")) == []


def test_source_identity_uses_real_git_subprocess_output() -> None:
    module = _script_module()

    identity = module._source_identity()

    assert identity == module._source_identity()
    assert len(identity["head"]) == 40
    assert identity["dirty_diff_digest"].startswith("sha256:")
    assert len(identity["dirty_diff_digest"]) == len("sha256:") + 64


def test_runner_checkpoints_each_group_and_writes_durable_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _script_module()
    state_path = tmp_path / "state.json"
    calls: list[str] = []
    monkeypatch.setattr(module, "CHECKPOINTS", {"first": ("node-a",), "second": ("node-b",)})
    monkeypatch.setattr(
        module, "_source_identity", lambda: {"head": "abc", "dirty_diff_digest": "sha256:123"}
    )

    def run_group(name: str, _nodes: tuple[str, ...], log_path: Path):
        calls.append(name)
        log_path.write_text(f"{name} passed\n")
        return ["pytest", name], 0

    monkeypatch.setattr(module, "_run_group", run_group)

    assert module.main(["--state", str(state_path)]) == 0
    state = json.loads(state_path.read_text())
    assert calls == ["first", "second"]
    assert state["schema"] == module.SCHEMA
    assert state["version"] == module.VERSION
    assert state["status"] == "passed"
    assert [state["groups"][name]["status"] for name in calls] == ["passed", "passed"]
    assert (tmp_path / "first.log").read_text() == "first passed\n"
    assert (tmp_path / "second.log").read_text() == "second passed\n"


def test_resume_skips_only_passed_groups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _script_module()
    state_path = tmp_path / "state.json"
    source = {"head": "abc", "dirty_diff_digest": "sha256:123"}
    module._write_state(
        state_path,
        {
            "schema": module.SCHEMA,
            "version": module.VERSION,
            "source": source,
            "started_at": "earlier",
            "finished_at": None,
            "status": "interrupted",
            "groups": {
                "passed": {"status": "passed"},
                "running": {"status": "running"},
            },
        },
    )
    monkeypatch.setattr(module, "CHECKPOINTS", {"passed": ("node-a",), "running": ("node-b",)})
    monkeypatch.setattr(module, "_source_identity", lambda: source)
    calls: list[str] = []

    def run_group(name: str, _nodes: tuple[str, ...], log_path: Path):
        calls.append(name)
        log_path.write_text("recovered\n")
        return ["pytest", name], 0

    monkeypatch.setattr(module, "_run_group", run_group)

    assert module.main(["--resume", "--state", str(state_path)]) == 0
    assert calls == ["running"]
    assert json.loads(state_path.read_text())["status"] == "passed"


def test_resume_rejects_source_drift(tmp_path: Path) -> None:
    module = _script_module()
    path = tmp_path / "state.json"
    module._write_state(
        path,
        {
            "schema": module.SCHEMA,
            "version": module.VERSION,
            "source": {"head": "old", "dirty_diff_digest": "sha256:old"},
            "groups": {},
        },
    )

    with pytest.raises(SystemExit, match="repository source identity changed"):
        module._load_resume_state(path, {"head": "new", "dirty_diff_digest": "sha256:new"})


def test_failed_group_stops_runner_and_remains_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _script_module()
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(module, "CHECKPOINTS", {"failed": ("node-a",), "later": ("node-b",)})
    monkeypatch.setattr(
        module, "_source_identity", lambda: {"head": "abc", "dirty_diff_digest": "sha256:123"}
    )
    calls: list[str] = []

    def run_group(name: str, _nodes: tuple[str, ...], log_path: Path):
        calls.append(name)
        log_path.write_text("failed\n")
        return ["pytest", name], 7

    monkeypatch.setattr(module, "_run_group", run_group)

    assert module.main(["--state", str(state_path)]) == 7
    state = json.loads(state_path.read_text())
    assert calls == ["failed"]
    assert state["status"] == "failed"
    assert state["groups"]["failed"]["status"] == "failed"
    assert state["groups"]["failed"]["returncode"] == 7
    assert "later" not in state["groups"]


def test_interrupted_group_is_never_marked_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _script_module()
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(module, "CHECKPOINTS", {"active": ("node-a",)})
    monkeypatch.setattr(
        module, "_source_identity", lambda: {"head": "abc", "dirty_diff_digest": "sha256:123"}
    )

    def interrupt(_name: str, _nodes: tuple[str, ...], _log_path: Path):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "_run_group", interrupt)

    with pytest.raises(KeyboardInterrupt):
        module.main(["--state", str(state_path)])
    state = json.loads(state_path.read_text())
    assert state["status"] == "interrupted"
    assert state["groups"]["active"]["status"] == "interrupted"
    assert state["groups"]["active"]["returncode"] is None
