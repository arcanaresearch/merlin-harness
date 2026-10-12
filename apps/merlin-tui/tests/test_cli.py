import asyncio
import io
import sys
from types import SimpleNamespace

import pytest
from merlin_tui.backend import StreamEvent
from merlin_tui.cli import (
    _database_shell,
    _database_status,
    _flush_terminal_input,
    _print_active_tasks,
    _repair_working_directory,
    _run_first_setup,
    main,
)
from merlin_tui.provider_catalog import ModelCandidate, ModelDiscovery


def _install_durable_campaign(
    monkeypatch,
    *,
    objective: str,
    campaign_id: str | None,
    digest: str = "sha256:reviewed",
    profile: str = "recommended",
    budget: dict[str, object] | None = None,
    composition: str | None = None,
    execute: bool = True,
):
    class Client:
        def __init__(self) -> None:
            self.list_calls = 0
            # The approval phase interrupts the preparation stream once the
            # durable prepared campaign is verified; execution then starts
            # through the deterministic execute_campaign RPC, not a model turn.
            self.approved = False
            self.execute_calls: list[tuple[object, object]] = []
            self.cancel_calls: list[object] = []

        async def list_campaigns(self):
            self.list_calls += 1
            if self.list_calls == 1 or campaign_id is None:
                return ()
            return (
                SimpleNamespace(
                    id=campaign_id,
                    objective=objective,
                    profile=profile,
                    status="prepared",
                    manifest_digest=digest,
                ),
            )

        async def campaign_status(self, requested_id):
            assert requested_id == campaign_id
            return {
                "campaign_id": campaign_id,
                "status": "executing" if self.approved and execute else "prepared",
                "manifest_digest": digest,
                "budget": {"limits": {} if budget is None else budget},
                **({"specialist_composition": composition} if composition is not None else {}),
            }

        async def interrupt(self):
            self.approved = True
            return None

        async def cancel_campaign(self, requested_id="*"):
            self.cancel_calls.append(requested_id)
            return 1

        async def execute_campaign(self, requested_id, requested_digest):
            assert requested_id == campaign_id
            assert requested_digest == digest
            self.execute_calls.append((requested_id, requested_digest))
            if execute:
                self.approved = True
            return {
                "campaign_id": requested_id,
                "status": "executing" if execute else "prepared",
            }

        async def close(self):
            return None

    client = Client()

    async def build_client(*, confirmer=None):
        assert confirmer is None
        return client

    monkeypatch.setattr("merlin_tui.cli._client", build_client)
    return client


def test_deleted_working_directory_recovers_to_home(monkeypatch, tmp_path) -> None:
    changed_to = []
    monkeypatch.setattr("merlin_tui.cli.os.getcwd", lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr("merlin_tui.cli.Path.home", lambda: tmp_path)
    monkeypatch.setattr("merlin_tui.cli.os.chdir", changed_to.append)

    assert _repair_working_directory() == tmp_path
    assert changed_to == [tmp_path]


def test_terminal_input_flush_discards_pending_mouse_reports(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 42)
    monkeypatch.setattr("termios.tcflush", lambda fd, mode: calls.append((fd, mode)))

    _flush_terminal_input()

    assert calls and calls[0][0] == 42


@pytest.mark.parametrize(("operation", "expected"), (("stop", False), ("purge", True)))
def test_private_storage_lifecycle_command(monkeypatch, operation, expected) -> None:
    calls = []

    class Server:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def stop(self):
            calls.append(("stop",))

        def drop(self, *, force):
            calls.append(("drop", force))

    monkeypatch.setattr("pg0.Pg0", Server)
    monkeypatch.setattr(sys, "argv", ["merlin", "storage", operation])

    main()

    assert (("drop", True) in calls) is expected
    assert (("stop",) in calls) is (not expected)


def test_version_entrypoint_is_noninteractive(monkeypatch, capsys) -> None:
    from importlib.metadata import version

    monkeypatch.setattr(sys, "argv", ["merlin", "--version"])

    main()

    assert capsys.readouterr().out == f"merlin {version('merlin-tui')}\n"


def test_browser_profile_command_uses_supported_configuration_path(
    monkeypatch, capsys, tmp_path
) -> None:
    calls = []

    class Configuration:
        def save_profile(self, profile, origins):
            calls.append((profile, origins))

    monkeypatch.setattr("merlin_tui.configuration.BrowserConfiguration", Configuration)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "browser", "profile", str(tmp_path / "profile"), "https://target.test"],
    )

    main()

    assert calls == [(tmp_path / "profile", ["https://target.test"])]
    assert "restart Merlin" in capsys.readouterr().out


def test_database_status_exposes_connection_location_but_not_generated_password(
    monkeypatch, capsys
) -> None:
    info = SimpleNamespace(
        running=True,
        port=54321,
        database="merlin",
        username="merlin_local",
        uri="postgresql://merlin_local:generated-secret@localhost:54321/merlin",
    )
    monkeypatch.setattr("merlin_tui.cli._pg0_server", lambda: SimpleNamespace(info=lambda: info))

    _database_status()

    output = capsys.readouterr().out
    assert output == "running · localhost:54321 · database merlin · user merlin_local\n"
    assert "generated-secret" not in output


def test_database_shell_keeps_generated_password_out_of_process_arguments(
    monkeypatch, tmp_path
) -> None:
    info = SimpleNamespace(
        running=True,
        port=54321,
        database="merlin",
        username="merlin_local",
        version=None,
        uri="postgresql://merlin_local:generated%2Dsecret@localhost:54321/merlin",
    )
    observed = []
    monkeypatch.setattr("merlin_tui.cli._pg0_server", lambda: SimpleNamespace(info=lambda: info))
    monkeypatch.setattr("merlin_tui.cli.Path.home", lambda: tmp_path)
    monkeypatch.setattr("merlin_tui.cli.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "merlin_tui.cli.os.execve",
        lambda executable, arguments, environment: observed.append(
            (executable, arguments, environment)
        ),
    )

    _database_shell()

    executable, arguments, environment = observed[0]
    assert executable == "/usr/bin/psql"
    assert arguments == [
        "/usr/bin/psql",
        "--host",
        "localhost",
        "--port",
        "54321",
        "--username",
        "merlin_local",
        "--dbname",
        "merlin",
    ]
    assert "generated-secret" not in " ".join(arguments)
    assert environment["PGPASSWORD"] == "generated-secret"


def test_database_status_reattaches_after_real_postgres_restart_from_unrelated_directory(
    monkeypatch, tmp_path, capsys
) -> None:
    from merlin_storage_postgres import Database, DatabaseConfig, MigrationRunner

    home = tmp_path / "home"
    data_dir = home / ".merlin" / "postgres"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.setattr("merlin_tui.cli.Path.home", lambda: home)
    monkeypatch.chdir(unrelated)

    with Database(DatabaseConfig(data_dir=data_dir)) as first:
        with first.connect() as connection:
            MigrationRunner(connection).apply()
            connection.commit()
        _database_status()
        initial = capsys.readouterr().out
        assert initial.startswith("running · localhost:")
        assert "password" not in initial.casefold()

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            assert (
                connection.execute("SELECT COUNT(*) AS total FROM plugin_migrations").fetchone()[
                    "total"
                ]
                > 0
            )
        _database_status()
        after_restart = capsys.readouterr().out
        assert after_restart.startswith("running · localhost:")
        assert "password" not in after_restart.casefold()


def test_entrypoint_rejects_unknown_arguments(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["merlin", "--unknown"])

    with pytest.raises(SystemExit, match="usage: merlin"):
        main()


def test_tasks_command_projects_active_schedules_across_sessions(monkeypatch) -> None:
    calls = []

    async def print_tasks(*, json_output=False):
        calls.append(json_output)

    monkeypatch.setattr("merlin_tui.cli._print_active_tasks", print_tasks)
    monkeypatch.setattr(sys, "argv", ["merlin", "tasks"])

    main()

    assert calls == [False]


def test_tasks_json_preserves_complete_projection(monkeypatch) -> None:
    calls = []

    async def print_tasks(*, json_output=False):
        calls.append(json_output)

    monkeypatch.setattr("merlin_tui.cli._print_active_tasks", print_tasks)
    monkeypatch.setattr(sys, "argv", ["merlin", "tasks", "--json"])

    main()

    assert calls == [True]


@pytest.mark.asyncio
async def test_active_task_summary_omits_verbose_projection(monkeypatch, capsys) -> None:
    schedule = SimpleNamespace(
        id="schedule-live",
        status="running",
        graph_digest="sha256:graph",
        nodes=(
            SimpleNamespace(
                id="worker:campaign/recon",
                status="running",
                technique="taxonomy/a, taxonomy/b",
                guidance=("large-guidance-entry",),
                attempts=(SimpleNamespace(worker="worker:campaign/recon"),),
            ),
            SimpleNamespace(id="start", status="completed", attempts=()),
        ),
    )

    class Client:
        async def list_active_schedules(self):
            return (schedule,)

        async def close(self):
            return None

    async def client():
        return Client()

    monkeypatch.setattr("merlin_tui.cli._client", client)

    await _print_active_tasks()

    output = capsys.readouterr().out
    assert "schedule-live · running · 1 active / 2 nodes" in output
    assert "worker:campaign/recon" in output
    assert "taxonomy/a" not in output
    assert "large-guidance-entry" not in output


def test_daemon_start_reports_expected_startup_failure_without_traceback(
    monkeypatch, capsys
) -> None:
    async def fail_startup() -> None:
        raise RuntimeError("provider is not configured")

    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", fail_startup)
    monkeypatch.setattr(sys, "argv", ["merlin", "daemon", "start"])

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 1
    assert capsys.readouterr().err == ("Merlin could not start: provider is not configured\n")


def test_first_setup_discovers_current_models_and_hides_credential(monkeypatch) -> None:
    saved = []

    class Configuration:
        def save(
            self,
            base_url,
            model,
            api_key,
            *,
            vision=None,
            context_window_tokens=None,
            output_token_parameter=None,
            reasoning_parameter=None,
        ):
            saved.append(
                (
                    base_url,
                    model,
                    api_key,
                    vision,
                    context_window_tokens,
                    output_token_parameter,
                    reasoning_parameter,
                )
            )

    answers = iter(("4", "2", "2", "131072", "n", ""))
    output = io.StringIO()
    monkeypatch.setattr("merlin_tui.configuration.ProviderConfiguration", Configuration)
    monkeypatch.setattr(
        "merlin_tui.provider_catalog.discover_model_catalog",
        lambda route, api_key, **kwargs: ModelDiscovery(
            (ModelCandidate("deepseek-v4-flash"), ModelCandidate("deepseek-v4-pro"))
        ),
    )

    _run_first_setup(
        read=lambda _prompt: next(answers),
        read_secret=lambda _prompt: "opaque-secret",
        output=output,
    )

    assert "Models are discovered live" in output.getvalue()
    assert "deepseek-v4-pro" in output.getvalue()
    assert "opaque-secret" not in output.getvalue()
    assert saved == [
        (
            "https://api.deepseek.com/v1",
            "deepseek-v4-pro",
            "opaque-secret",
            True,
            131072,
            "max_tokens",
            "thinking",
        )
    ]


def test_first_setup_falls_back_to_exact_model_without_stale_default(monkeypatch) -> None:
    saved = []

    class Configuration:
        def save(
            self,
            base_url,
            model,
            api_key,
            *,
            vision=None,
            context_window_tokens=None,
            output_token_parameter=None,
            reasoning_parameter=None,
        ):
            saved.append((base_url, model, api_key, vision, context_window_tokens))

    answers = iter(("1", "qwen3.5:latest", "", "", "n", ""))
    output = io.StringIO()
    monkeypatch.setattr("merlin_tui.configuration.ProviderConfiguration", Configuration)
    monkeypatch.setattr(
        "merlin_tui.provider_catalog.discover_model_catalog",
        lambda *args, **kwargs: ModelDiscovery(error="Local provider is not running."),
    )

    _run_first_setup(read=lambda _prompt: next(answers), output=output)

    assert "llama3.2" not in output.getvalue()
    assert "Local provider is not running." in output.getvalue()
    assert saved == [("http://127.0.0.1:11434/v1", "qwen3.5:latest", "", False, None)]


def test_first_setup_accepts_provider_reported_model_capabilities(monkeypatch) -> None:
    saved = []

    class Configuration:
        def save(
            self,
            base_url,
            model,
            api_key,
            *,
            vision=None,
            context_window_tokens=None,
            output_token_parameter=None,
            reasoning_parameter=None,
        ):
            saved.append((base_url, model, vision, context_window_tokens))

    answers = iter(("6", "1", "", "", "n", ""))
    monkeypatch.setattr("merlin_tui.configuration.ProviderConfiguration", Configuration)
    monkeypatch.setattr(
        "merlin_tui.provider_catalog.discover_model_catalog",
        lambda *args, **kwargs: ModelDiscovery(
            (ModelCandidate("live-model", vision=True, context_window_tokens=200_000),)
        ),
    )

    _run_first_setup(
        read=lambda _prompt: next(answers),
        read_secret=lambda _prompt: "opaque",
        output=io.StringIO(),
    )

    assert saved == [("https://openrouter.ai/api/v1", "live-model", True, 200_000)]


def test_first_setup_can_assign_native_cli_to_campaign_lead(monkeypatch) -> None:
    saved = []
    routes = []

    class Configuration:
        def save(
            self,
            base_url,
            model,
            api_key,
            *,
            vision=None,
            context_window_tokens=None,
            output_token_parameter=None,
            reasoning_parameter=None,
        ):
            saved.append((base_url, model))

        def save_model_route(self, model, transport, *, lead_only):
            routes.append((model, transport, lead_only))

    answers = iter(("1", "worker-model", "", "", "n", "2", "lead-model"))
    monkeypatch.setattr("merlin_tui.configuration.ProviderConfiguration", Configuration)
    monkeypatch.setattr(
        "merlin_tui.provider_catalog.discover_model_catalog",
        lambda *args, **kwargs: ModelDiscovery(error="offline"),
    )

    _run_first_setup(read=lambda _prompt: next(answers), output=io.StringIO())

    assert saved == [("http://127.0.0.1:11434/v1", "worker-model")]
    assert routes == [("lead-model", "codex_app_server", True)]


def test_first_setup_registers_additional_model_endpoints(monkeypatch) -> None:
    endpoints = []
    secrets = iter(("primary-secret", "endpoint-secret"))

    class Configuration:
        def save(self, *args, **kwargs):
            pass

        def save_model_endpoint(self, model, base_url, api_key=None):
            endpoints.append((model, base_url, api_key))

    answers = iter(("4", "1", "", "", "y", "claude-haiku", "https://api.anthropic.test", "n", ""))
    output = io.StringIO()
    monkeypatch.setattr("merlin_tui.configuration.ProviderConfiguration", Configuration)
    monkeypatch.setattr(
        "merlin_tui.provider_catalog.discover_model_catalog",
        lambda *args, **kwargs: ModelDiscovery((ModelCandidate("deepseek-v4-pro"),)),
    )

    _run_first_setup(
        read=lambda _prompt: next(answers),
        read_secret=lambda _prompt: next(secrets),
        output=output,
    )

    assert endpoints == [("claude-haiku", "https://api.anthropic.test", "endpoint-secret")]
    assert "Configured a provider endpoint for claude-haiku." in output.getvalue()
    assert "endpoint-secret" not in output.getvalue()


def test_main_treats_first_setup_interrupt_as_clean_cancellation(monkeypatch, capsys) -> None:
    monkeypatch.setattr("merlin_tui.cli._provider_is_configured", lambda: False)

    def interrupt() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("merlin_tui.cli._run_first_setup", interrupt)
    monkeypatch.setattr("sys.argv", ["merlin"])

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 130
    assert "setup cancelled" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("flag", "expected"),
    (
        ("--plain", "done\n"),
        (
            "--ndjson",
            '{"sequence": 1, "kind": "thinking", "text": "working"}\n'
            '{"sequence": 2, "kind": "content", "text": "do"}\n'
            '{"sequence": 3, "kind": "content", "text": "ne"}\n'
            '{"sequence": 4, "kind": "usage", "text": "4 tokens"}\n'
            '{"sequence": 5, "kind": "end", "ok": true}\n',
        ),
    ),
)
def test_noninteractive_stream_formats(monkeypatch, capsys, flag, expected) -> None:
    class Client:
        async def stream(self, prompt):
            assert prompt == "assess target"
            yield StreamEvent("thinking", "working")
            yield StreamEvent("content", "do")
            yield StreamEvent("content", "ne")
            yield StreamEvent("usage", "4 tokens")

        async def close(self):
            return None

    async def client(*, confirmer=None):
        assert confirmer is None
        return Client()

    monkeypatch.setattr("merlin_tui.cli._client", client)
    monkeypatch.setattr(sys, "argv", ["merlin", flag, "assess", "target"])

    main()

    assert capsys.readouterr().out == expected


def test_noninteractive_stdin_resume_approval_and_error_exit(monkeypatch, capsys) -> None:
    calls = []

    class Client:
        async def stream(self, prompt):
            calls.append(prompt)
            if prompt.startswith("/resume"):
                yield StreamEvent("session", "resumed")
            else:
                yield StreamEvent("error", "provider rejected request")

        async def close(self):
            calls.append("closed")

    async def client(*, confirmer=None):
        assert await confirmer(None, None, None) == "allow_once"
        return Client()

    monkeypatch.setattr("merlin_tui.cli._client", client)
    monkeypatch.setattr(sys, "stdin", io.StringIO("assess stdin target"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "--ndjson", "--approve", "--session", "session-7", "-"],
    )

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 1
    assert calls == ["/resume session-7", "assess stdin target", "closed"]
    output = capsys.readouterr().out
    assert '"kind": "session"' in output
    assert '"kind": "error"' in output
    assert '"kind": "end", "ok": false' in output


def test_plain_ctrl_c_interrupts_and_closes_client_without_traceback(monkeypatch, capsys) -> None:
    calls = []

    class Client:
        async def stream(self, prompt):
            calls.append(("stream", prompt))
            raise asyncio.CancelledError
            yield  # pragma: no cover

        async def interrupt(self):
            calls.append(("interrupt",))

        async def close(self):
            calls.append(("close",))

    async def client(*, confirmer=None):
        assert confirmer is None
        return Client()

    monkeypatch.setattr("merlin_tui.cli._client", client)
    monkeypatch.setattr(sys, "argv", ["merlin", "--plain", "assess target"])

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 130
    assert calls == [("stream", "assess target"), ("interrupt",), ("close",)]
    captured = capsys.readouterr()
    assert "durable campaign work continues" in captured.err
    assert "Traceback" not in captured.out + captured.err


def test_campaign_submit_ctrl_c_interrupts_client_without_cancelling_campaign(
    monkeypatch, capsys
) -> None:
    calls = []

    class Client:
        def __init__(self, *, confirmer=None, owns_daemon=False):
            assert confirmer is None
            assert owns_daemon is False

        async def stream(self, prompt):
            calls.append(("stream", prompt))
            raise asyncio.CancelledError
            yield  # pragma: no cover

        async def interrupt(self):
            calls.append(("interrupt",))

        async def close(self):
            calls.append(("close",))

        async def cancel_campaign(self, *_args):
            raise AssertionError("Ctrl-C must not cancel durable campaign work")

    async def ensure_daemon():
        calls.append(("ensure_daemon",))

    monkeypatch.setattr("merlin_tui.local_control.LocalControlClient", Client)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", ensure_daemon)
    monkeypatch.setattr(sys, "argv", ["merlin", "campaign", "submit", "assess target"])

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 130
    assert calls == [
        ("ensure_daemon",),
        ("stream", "/new"),
        ("interrupt",),
        ("close",),
    ]
    captured = capsys.readouterr()
    assert "durable campaign work continues" in captured.err
    assert "Traceback" not in captured.out + captured.err


async def test_human_stream_coalesces_token_deltas() -> None:
    class Client:
        async def stream(self, prompt):
            assert prompt == "assess target"
            yield StreamEvent("thinking", "inspect")
            yield StreamEvent("thinking", " safely")
            yield StreamEvent("content", "pre")
            yield StreamEvent("content", "pared")
            yield StreamEvent("usage", "8 tokens")

    from merlin_tui.cli import _coalesced_display_stream

    values = [item async for item in _coalesced_display_stream(Client(), "assess target")]

    assert values == [
        ("thinking", "inspect safely"),
        ("content", "prepared"),
        ("usage", "8 tokens"),
    ]


async def test_human_stream_returns_exact_campaign_review(monkeypatch, capsys) -> None:
    state = {"interrupted": False}

    class Client:
        def __init__(self, confirmer=None, *, owns_daemon=False):
            self.confirmer = confirmer
            assert owns_daemon is False

        async def stream(self, prompt):
            assert prompt == "prepare"
            yield StreamEvent(
                "campaign_review",
                '{"campaign_id":"campaign/one","manifest_digest":"sha256:reviewed"}',
            )

        async def interrupt(self):
            state["interrupted"] = True

        async def close(self):
            return None

    async def ensure_daemon():
        return None

    monkeypatch.setattr("merlin_tui.local_control.LocalControlClient", Client)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", ensure_daemon)

    from merlin_tui.cli import _print_stream

    review = await _print_stream("prepare", approve_once=True, stop_after_tool="assessment_prepare")

    assert review == {
        "campaign_id": "campaign/one",
        "manifest_digest": "sha256:reviewed",
    }
    assert state["interrupted"]
    assert "campaign_review:" in capsys.readouterr().out


async def test_human_stream_interrupts_after_requested_durable_tool(monkeypatch) -> None:
    state = {"interrupted": False, "closed": False}

    class Client:
        def __init__(self, confirmer=None, *, owns_daemon=False):
            del confirmer
            assert owns_daemon is False

        async def stream(self, _prompt):
            yield StreamEvent("tool", 'assessment_execute completed: {"status":"executing"}')
            raise AssertionError("status polling must not continue after durable execution")

        async def interrupt(self):
            state["interrupted"] = True

        async def close(self):
            state["closed"] = True

    async def ensure_daemon():
        return None

    monkeypatch.setattr("merlin_tui.local_control.LocalControlClient", Client)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", ensure_daemon)

    from merlin_tui.cli import _print_stream

    await _print_stream("execute", stop_after_tool="assessment_execute")

    assert state == {"interrupted": True, "closed": True}


async def test_human_stream_bounds_never_returning_interrupt_and_close(monkeypatch) -> None:
    state = {"interrupts": 0, "closes": 0}

    class Client:
        def __init__(self, confirmer=None, *, owns_daemon=False):
            del confirmer
            assert owns_daemon is False

        async def stream(self, _prompt):
            yield StreamEvent("tool", 'assessment_prepare completed: {"status":"prepared"}')

        async def interrupt(self):
            state["interrupts"] += 1
            await asyncio.Event().wait()

        async def close(self):
            state["closes"] += 1
            await asyncio.Event().wait()

    async def ensure_daemon():
        return None

    monkeypatch.setattr("merlin_tui.local_control.LocalControlClient", Client)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", ensure_daemon)
    monkeypatch.setattr("merlin_tui.cli._STREAM_TEARDOWN_TIMEOUT_SECONDS", 0.01)

    from merlin_tui.cli import _print_stream

    await asyncio.wait_for(
        _print_stream("prepare", stop_after_tool="assessment_prepare"), timeout=0.2
    )

    assert state == {"interrupts": 1, "closes": 1}


async def test_human_stream_propagates_inner_cleanup_timeout(monkeypatch) -> None:
    class Client:
        def __init__(self, confirmer=None, *, owns_daemon=False):
            del confirmer
            assert owns_daemon is False

        async def stream(self, _prompt):
            if False:
                yield None

        async def close(self):
            raise TimeoutError("close operation timed out internally")

    async def ensure_daemon():
        return None

    monkeypatch.setattr("merlin_tui.local_control.LocalControlClient", Client)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", ensure_daemon)

    from merlin_tui.cli import _print_stream

    with pytest.raises(TimeoutError, match="timed out internally"):
        await _print_stream("prepare")


async def test_human_stream_returns_when_cleanup_suppresses_cancellation(monkeypatch) -> None:
    cancellation_seen = asyncio.Event()
    release_cleanup = asyncio.Event()

    class Client:
        def __init__(self, confirmer=None, *, owns_daemon=False):
            del confirmer
            assert owns_daemon is False

        async def stream(self, _prompt):
            if False:
                yield None

        async def close(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release_cleanup.wait()

    async def ensure_daemon():
        return None

    monkeypatch.setattr("merlin_tui.local_control.LocalControlClient", Client)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", ensure_daemon)
    monkeypatch.setattr("merlin_tui.cli._STREAM_TEARDOWN_TIMEOUT_SECONDS", 0.01)

    from merlin_tui.cli import _print_stream

    await asyncio.wait_for(_print_stream("prepare"), timeout=0.2)
    await asyncio.wait_for(cancellation_seen.wait(), timeout=0.2)
    release_cleanup.set()
    await asyncio.sleep(0)


def test_campaign_approve_applies_to_preparation_and_execution(monkeypatch) -> None:
    calls = []
    client = _install_durable_campaign(
        monkeypatch, objective="Assess https://target.test", campaign_id="campaign/one"
    )

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        calls.append((prompt, approve_once, stop_after_tool, concise))
        if prompt.startswith("Assess https://target.test"):
            return {
                "campaign_id": "campaign/one",
                "manifest_digest": "sha256:reviewed",
                "profile": "recommended",
                "review": {"budget": {}},
            }
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", "Assess", "https://target.test"],
    )

    main()

    assert calls == [
        ("/new", False, None, False),
        (
            "Assess https://target.test\nThe operator explicitly selected campaign profile: "
            "recommended; do not ask for the profile again. Any max_* values below are "
            "provider/model campaign budgets, not target chat/message limits.\n"
            "The operator requested no provider/model campaign limits. Keep max_turns, "
            "max_tokens, max_seconds, and max_cost_usd unset; do not invent safety caps."
            "\nPass the operator objective text above verbatim as the assessment_prepare "
            "objective: do not reword, paraphrase, or substitute the target line; Merlin "
            "anchors the reviewed target itself.",
            True,
            "assessment_prepare",
            True,
        ),
    ]
    # Execution starts through the deterministic daemon RPC, not a model turn.
    assert client.execute_calls == [("campaign/one", "sha256:reviewed")]
    assert not any(str(prompt).startswith("Approve and execute") for prompt, *_ in calls)


def test_campaign_approve_uses_new_durable_campaign_when_stream_omits_review(monkeypatch) -> None:
    calls = []
    client = _install_durable_campaign(
        monkeypatch, objective="Assess durable target", campaign_id="campaign/durable"
    )

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        calls.append((prompt, approve_once, stop_after_tool, concise))
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", "Assess durable target"],
    )

    main()

    assert client.list_calls >= 2
    assert client.execute_calls == [("campaign/durable", "sha256:reviewed")]
    assert not any(str(prompt).startswith("Approve and execute") for prompt, *_ in calls)


def test_campaign_approve_does_not_wait_for_open_preparation_stream(monkeypatch) -> None:
    calls = []
    client = _install_durable_campaign(
        monkeypatch, objective="Assess waiting target", campaign_id="campaign/waiting"
    )

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        calls.append((prompt, approve_once, stop_after_tool, concise))
        if prompt.startswith("Assess waiting target"):
            await asyncio.Event().wait()
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", "Assess waiting target"],
    )

    main()

    assert client.execute_calls == [("campaign/waiting", "sha256:reviewed")]


def test_campaign_approve_bounds_polling_client_cleanup_before_direct_execution(
    monkeypatch,
) -> None:
    client = _install_durable_campaign(
        monkeypatch, objective="Assess cleanup target", campaign_id="campaign/cleanup"
    )

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        return None

    async def never_close():
        await asyncio.Event().wait()

    async def unexpected_interrupt():
        raise AssertionError("the polling client must not interrupt the preparation stream")

    client.close = never_close
    client.interrupt = unexpected_interrupt
    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr("merlin_tui.cli._STREAM_TEARDOWN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", "Assess cleanup target"],
    )

    main()

    assert client.execute_calls == [("campaign/cleanup", "sha256:reviewed")]


@pytest.mark.asyncio
async def test_campaign_approve_bounds_cancellation_resistant_preparation_before_execution(
    monkeypatch,
) -> None:
    client = _install_durable_campaign(
        monkeypatch,
        objective="Assess resistant preparation",
        campaign_id="campaign/resistant-preparation",
    )
    cancellation_seen = asyncio.Event()
    release_preparation = asyncio.Event()

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        if prompt == "/new":
            return None
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release_preparation.wait()
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr("merlin_tui.cli._STREAM_TEARDOWN_TIMEOUT_SECONDS", 0.01)

    from merlin_tui.cli import _submit_campaign

    await asyncio.wait_for(
        _submit_campaign(["--approve", "Assess", "resistant", "preparation"]),
        timeout=0.2,
    )

    assert cancellation_seen.is_set()
    assert client.execute_calls == [("campaign/resistant-preparation", "sha256:reviewed")]
    release_preparation.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_campaign_approve_prefers_exact_streamed_review_over_stuck_durable_poll(
    monkeypatch,
) -> None:
    objective = "Assess streamed review"
    client = _install_durable_campaign(
        monkeypatch,
        objective=objective,
        campaign_id="campaign/streamed-review",
        composition="opinionated",
    )
    poll_started = asyncio.Event()
    original_list = client.list_campaigns

    async def list_campaigns():
        if client.list_calls == 0:
            return await original_list()
        client.list_calls += 1
        poll_started.set()
        await asyncio.Event().wait()

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        if prompt == "/new":
            return None
        await poll_started.wait()
        return {
            "campaign_id": "campaign/streamed-review",
            "manifest_digest": "sha256:reviewed",
            "objective": objective,
            "profile": "recommended",
            "review": {
                "budget": {
                    "turns": None,
                    "tokens": None,
                    "seconds": None,
                    "cost_usd": None,
                }
            },
        }

    monkeypatch.setattr(client, "list_campaigns", list_campaigns)
    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr("merlin_tui.cli._CAMPAIGN_POLL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("merlin_tui.cli._STREAM_TEARDOWN_TIMEOUT_SECONDS", 0.01)
    from merlin_tui.cli import _submit_campaign

    await asyncio.wait_for(
        _submit_campaign(["--approve", "Assess", "streamed", "review"]),
        timeout=0.2,
    )

    assert client.execute_calls == [("campaign/streamed-review", "sha256:reviewed")]


@pytest.mark.asyncio
async def test_campaign_approve_ignores_failed_poll_when_exact_streamed_review_arrives(
    monkeypatch,
) -> None:
    objective = "Assess after failed poll"
    client = _install_durable_campaign(
        monkeypatch,
        objective=objective,
        campaign_id="campaign/failed-poll",
        composition="opinionated",
    )
    original_list = client.list_campaigns
    poll_failed = asyncio.Event()
    release_streamed_review = asyncio.Event()

    async def list_campaigns():
        if client.list_calls == 0:
            return await original_list()
        client.list_calls += 1
        poll_failed.set()
        raise RuntimeError("poll socket closed")

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        if prompt == "/new":
            return None
        await release_streamed_review.wait()
        return {
            "campaign_id": "campaign/failed-poll",
            "manifest_digest": "sha256:reviewed",
            "objective": objective,
            "profile": "recommended",
            "review": {
                "budget": {
                    "turns": None,
                    "tokens": None,
                    "seconds": None,
                    "cost_usd": None,
                }
            },
        }

    monkeypatch.setattr(client, "list_campaigns", list_campaigns)
    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    from merlin_tui.cli import _submit_campaign

    submission = asyncio.create_task(
        _submit_campaign(["--approve", "Assess", "after", "failed", "poll"])
    )
    await asyncio.wait_for(poll_failed.wait(), timeout=0.2)
    # Force the failed polling branch to be consumed before the exact stream
    # review becomes available; this is the ordering that previously called
    # bounded teardown with campaign_poll=None.
    await asyncio.sleep(0.01)
    release_streamed_review.set()
    await asyncio.wait_for(submission, timeout=0.2)

    assert client.execute_calls == [("campaign/failed-poll", "sha256:reviewed")]


@pytest.mark.asyncio
async def test_campaign_approve_consumes_simultaneous_failed_poll(
    monkeypatch,
) -> None:
    objective = "Assess simultaneous review"
    client = _install_durable_campaign(
        monkeypatch,
        objective=objective,
        campaign_id="campaign/simultaneous-review",
        composition="opinionated",
    )
    original_list = client.list_campaigns
    release = asyncio.Event()
    arrivals = 0
    loop_errors: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    async def arrive() -> None:
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            release.set()
        await release.wait()

    async def list_campaigns():
        if client.list_calls == 0:
            return await original_list()
        client.list_calls += 1
        await arrive()
        raise RuntimeError("simultaneous poll failure")

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        if prompt == "/new":
            return None
        await arrive()
        return {
            "campaign_id": "campaign/simultaneous-review",
            "manifest_digest": "sha256:reviewed",
            "objective": objective,
            "profile": "recommended",
            "review": {
                "budget": {
                    "turns": None,
                    "tokens": None,
                    "seconds": None,
                    "cost_usd": None,
                }
            },
        }

    monkeypatch.setattr(client, "list_campaigns", list_campaigns)
    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    from merlin_tui.cli import _submit_campaign

    try:
        await _submit_campaign(["--approve", "Assess", "simultaneous", "review"])
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert client.execute_calls == [("campaign/simultaneous-review", "sha256:reviewed")]
    assert not any(
        "exception was never retrieved" in str(item.get("message", "")).lower()
        for item in loop_errors
    )


@pytest.mark.asyncio
async def test_campaign_approve_verifies_omitted_streamed_autonomous_composition(
    monkeypatch,
) -> None:
    objective = "Assess autonomous composition"
    client = _install_durable_campaign(
        monkeypatch,
        objective=objective,
        campaign_id="campaign/autonomous-composition",
        composition="autonomous",
    )

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        if prompt == "/new":
            return None
        return {
            "campaign_id": "campaign/autonomous-composition",
            "manifest_digest": "sha256:reviewed",
            "objective": objective,
            "profile": "recommended",
            "review": {
                "budget": {
                    "turns": None,
                    "tokens": None,
                    "seconds": None,
                    "cost_usd": None,
                }
            },
        }

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    from merlin_tui.cli import _submit_campaign

    await _submit_campaign(
        ["--composition", "autonomous", "--approve", "Assess", "autonomous", "composition"]
    )

    assert client.execute_calls == [("campaign/autonomous-composition", "sha256:reviewed")]


@pytest.mark.asyncio
async def test_campaign_approve_rejects_omitted_streamed_composition_when_durable_differs(
    monkeypatch,
) -> None:
    objective = "Assess mismatched composition"
    client = _install_durable_campaign(
        monkeypatch,
        objective=objective,
        campaign_id="campaign/mismatched-composition",
        composition="opinionated",
    )

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        if prompt == "/new":
            return None
        return {
            "campaign_id": "campaign/mismatched-composition",
            "manifest_digest": "sha256:reviewed",
            "objective": objective,
            "profile": "recommended",
            "review": {
                "budget": {
                    "turns": None,
                    "tokens": None,
                    "seconds": None,
                    "cost_usd": None,
                }
            },
        }

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    from merlin_tui.cli import _submit_campaign

    with pytest.raises(SystemExit, match="changed the selected specialist composition"):
        await _submit_campaign(
            [
                "--composition",
                "autonomous",
                "--approve",
                "Assess",
                "mismatched",
                "composition",
            ]
        )

    assert client.execute_calls == []


@pytest.mark.asyncio
async def test_bounded_task_teardown_propagates_caller_cancellation() -> None:
    from merlin_tui.cli import _bounded_task_teardown

    child = asyncio.create_task(asyncio.Event().wait())
    teardown = asyncio.create_task(_bounded_task_teardown(child))
    await asyncio.sleep(0)
    teardown.cancel()

    with pytest.raises(asyncio.CancelledError):
        await teardown

    await asyncio.gather(child, return_exceptions=True)


def test_campaign_approve_rejects_prepared_objective_drift(monkeypatch) -> None:
    """Run31: the model paraphrased the operator objective when calling
    assessment_prepare, and the drift was only caught downstream by the
    driver's scope guard. Submit must refuse approval loudly and cancel the
    drifted campaign instead of executing it."""
    client = _install_durable_campaign(
        monkeypatch,
        objective="Determine whether FareCare can be manipulated into granting a "
        "retroactive bereavement refund for completing travel that policy forbids.",
        campaign_id="campaign/drifted",
    )

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merlin",
            "campaign",
            "submit",
            "--approve",
            "Determine whether FareCare can be manipulated into granting a retroactive "
            "bereavement refund for completed travel that policy forbids.",
        ],
    )

    with pytest.raises(SystemExit, match="objective drifted"):
        main()

    assert client.cancel_calls == ["campaign/drifted"]
    assert client.execute_calls == []


def test_campaign_approve_accepts_verbatim_objective_with_target_anchor(monkeypatch) -> None:
    operator_objective = "Assess https://target.test for prompt injection"
    client = _install_durable_campaign(
        monkeypatch,
        objective=operator_objective + "\nTarget: https://target.test",
        campaign_id="campaign/anchored",
    )

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", operator_objective],
    )

    main()

    assert client.cancel_calls == []
    assert client.execute_calls == [("campaign/anchored", "sha256:reviewed")]


def test_campaign_approve_accepts_exact_operator_clause_with_same_origin_anchor(
    monkeypatch,
) -> None:
    operator_objective = (
        "Run a security assessment against FareCare on https://plunder.me: determine "
        "whether the AI can grant a forbidden retroactive refund."
    )
    client = _install_durable_campaign(
        monkeypatch,
        objective=(
            "determine whether the AI can grant a forbidden retroactive refund.\n"
            "Target: https://plunder.me/api/message"
        ),
        campaign_id="campaign/exact-clause",
    )

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", operator_objective],
    )

    main()

    assert client.cancel_calls == []
    assert client.execute_calls == [("campaign/exact-clause", "sha256:reviewed")]


def test_campaign_approve_rejects_exact_clause_with_cross_origin_anchor(monkeypatch) -> None:
    operator_objective = (
        "Assess https://target.test: determine whether the assistant leaks secrets."
    )
    client = _install_durable_campaign(
        monkeypatch,
        objective=(
            "determine whether the assistant leaks secrets.\n"
            "Target: https://attacker.test/api/message"
        ),
        campaign_id="campaign/cross-origin",
    )

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", operator_objective],
    )

    with pytest.raises(SystemExit, match="objective drifted"):
        main()

    assert client.cancel_calls == ["campaign/cross-origin"]
    assert client.execute_calls == []


def test_campaign_approve_fails_loudly_when_execution_never_starts(monkeypatch) -> None:
    """A silent approval turn must not strand a prepared campaign (run15).

    If the daemon-side execute call does not move the campaign out of
    'prepared', the campaign has no schedule, no waves, and no journal trace —
    nothing else will ever execute it. Submit must fail immediately instead of
    returning success.
    """
    _install_durable_campaign(
        monkeypatch,
        objective="Assess stranded target",
        campaign_id="campaign/stranded",
        execute=False,
    )

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", "Assess stranded target"],
    )

    with pytest.raises(SystemExit) as captured:
        main()

    assert "still prepared after the execute call" in str(captured.value)
    assert "campaign/stranded" in str(captured.value)


def test_campaign_approve_executes_directly_when_model_turn_skips_the_tool(monkeypatch) -> None:
    """Run15/run16 regression: the model's turn is not the execution trigger.

    The prepare/review phase stays conversational, but once the durable review
    is verified, submit must start execution through the execute_campaign RPC
    even when no model turn ever calls assessment_execute.
    """
    client = _install_durable_campaign(
        monkeypatch,
        objective="Assess silent model",
        campaign_id="campaign/silent",
    )

    async def print_stream(prompt, *, approve_once=False, stop_after_tool=None, concise=False):
        # The model never calls assessment_execute; the turn just ends.
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", "Assess silent model"],
    )

    main()

    assert client.execute_calls == [("campaign/silent", "sha256:reviewed")]


def test_campaign_saturation_continue_calls_durable_decision(monkeypatch) -> None:
    calls = []

    async def print_call(method, *args):
        calls.append((method, args))

    monkeypatch.setattr("merlin_tui.cli._print_call", print_call)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "saturation", "campaign/one", "continue"],
    )

    main()

    assert calls == [("decide_campaign_saturation", ("campaign/one", "continue", None))]


def test_campaign_saturation_limit_requires_integer(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "saturation", "campaign/one", "limit", "many"],
    )

    with pytest.raises(SystemExit, match="must be an integer"):
        main()


def test_campaign_approve_retries_once_when_prepare_turn_ends_without_tool(monkeypatch) -> None:
    """A prep turn that ends asking for confirmation must be nudged once; when
    the nudge lands assessment_prepare, approval proceeds normally."""
    client = _install_durable_campaign(
        monkeypatch,
        objective="Assess target",
        campaign_id="campaign/one",
        composition="opinionated",
    )

    async def no_campaigns():
        return ()

    # Force the streamed-review path so the retry — not durable polling — is
    # what recovers the campaign.
    client.list_campaigns = no_campaigns
    prompts: list[str] = []

    async def print_stream(prompt, *, approve_once=False, **_kwargs):
        prompts.append(prompt)
        if prompt.startswith("Assess target"):
            return {"preparation_note": "may I proceed with preparation?"}
        if "ended without calling assessment_prepare" in prompt:
            return {
                "campaign_id": "campaign/one",
                "manifest_digest": "sha256:reviewed",
                "objective": "Assess target",
                "profile": "recommended",
                "review": {"budget": {}},
            }
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", "Assess target"],
    )

    main()

    assert len(prompts) == 3
    assert prompts[0] == "/new"
    assert prompts[1].startswith("Assess target")
    assert "ended without calling assessment_prepare" in prompts[2]
    assert client.execute_calls == [("campaign/one", "sha256:reviewed")]


def test_campaign_approve_recovers_one_numbered_profile_prompt(monkeypatch) -> None:
    calls = []
    _install_durable_campaign(
        monkeypatch, objective="Assess target", campaign_id=None, profile="exhaustive"
    )

    async def print_stream(prompt, *, approve_once=False, **_kwargs):
        calls.append((prompt, approve_once))
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merlin",
            "campaign",
            "submit",
            "--profile",
            "exhaustive",
            "--approve",
            "Assess target",
        ],
    )

    with pytest.raises(SystemExit, match="campaign was not durably prepared"):
        main()

    assert len(calls) == 3
    assert calls[0] == ("/new", False)
    assert "operator explicitly selected campaign profile: exhaustive" in calls[1][0]
    assert "ended without calling assessment_prepare" in calls[2][0]


def test_campaign_approve_refusal_carries_terminal_preparation_note(monkeypatch) -> None:
    """Run22: a prepare turn that ends without assessment_prepare must refuse
    loudly with the model's own terminal explanation attached, not a bare
    refusal that looks like a swallowed exception."""
    _install_durable_campaign(monkeypatch, objective="Assess target", campaign_id=None)

    async def print_stream(prompt, *, approve_once=False, **_kwargs):
        if prompt.startswith("Assess target"):
            return {
                "preparation_note": ("no captured message exchange; I will not invent the endpoint")
            }
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", "Assess target"],
    )

    with pytest.raises(SystemExit) as captured:
        main()

    message = str(captured.value)
    assert "campaign was not durably prepared; refusing approval" in message
    assert "ended without assessment_prepare" in message
    assert "no captured message exchange" in message


def test_campaign_approve_recovers_exploration_profile_prompt(monkeypatch) -> None:
    calls = []
    _install_durable_campaign(
        monkeypatch, objective="Assess target", campaign_id=None, profile="exploration"
    )

    async def print_stream(prompt, *, approve_once=False, **_kwargs):
        calls.append((prompt, approve_once))
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merlin",
            "campaign",
            "submit",
            "--profile",
            "exploration",
            "--approve",
            "Assess target",
        ],
    )

    with pytest.raises(SystemExit, match="campaign was not durably prepared"):
        main()

    assert len(calls) == 3
    assert calls[0] == ("/new", False)
    assert "operator explicitly selected campaign profile: exploration" in calls[1][0]
    assert "ended without calling assessment_prepare" in calls[2][0]


def test_campaign_submit_projects_explicit_budget_into_review_prompt(monkeypatch) -> None:
    calls = []

    async def print_stream(prompt, *, approve_once=False, **_kwargs):
        calls.append((prompt, approve_once))

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merlin",
            "campaign",
            "submit",
            "--max-turns",
            "50",
            "--max-tokens",
            "100000",
            "--max-seconds",
            "3600",
            "--max-cost-usd",
            "12.5",
            "Assess",
            "target",
        ],
    )

    main()

    assert calls[1] == (
        "Assess target\nThe operator explicitly selected campaign profile: recommended; "
        "do not ask for the profile again. Any max_* values below are provider/model "
        "campaign budgets, not target chat/message limits.\n"
        "Seal this exact campaign budget into assessment_prepare: "
        "max_cost_usd=12.5, max_seconds=3600, max_tokens=100000, max_turns=50."
        "\nPass the operator objective text above verbatim as the assessment_prepare "
        "objective: do not reword, paraphrase, or substitute the target line; Merlin "
        "anchors the reviewed target itself.",
        False,
    )


def test_campaign_approve_rejects_changed_explicit_budget(monkeypatch) -> None:
    calls = []
    _install_durable_campaign(
        monkeypatch,
        objective="Assess target",
        campaign_id="campaign/changed",
        digest="sha256:changed",
        budget={"turns": 16, "tokens": 100_000, "seconds": 3_600},
    )

    async def print_stream(prompt, *, approve_once=False, **_kwargs):
        calls.append((prompt, approve_once))
        if prompt.startswith("Assess target"):
            return {
                "campaign_id": "campaign/changed",
                "manifest_digest": "sha256:changed",
                "profile": "recommended",
                "review": {"budget": {"turns": 16, "tokens": 100_000, "seconds": 3_600}},
            }
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merlin",
            "campaign",
            "submit",
            "--approve",
            "--max-turns",
            "40",
            "--max-tokens",
            "100000",
            "--max-seconds",
            "3600",
            "Assess target",
        ],
    )

    with pytest.raises(SystemExit, match="max_turns expected 40, received 16"):
        main()

    assert not any(prompt.startswith("Approve and execute") for prompt, _ in calls)


def test_campaign_approve_rejects_invented_default_budget(monkeypatch) -> None:
    calls = []
    _install_durable_campaign(
        monkeypatch,
        objective="Assess target",
        campaign_id="campaign/invented",
        digest="sha256:invented",
        budget={"turns": 30, "tokens": 100_000, "seconds": 2_700, "cost_usd": None},
    )

    async def print_stream(prompt, *, approve_once=False, **_kwargs):
        calls.append((prompt, approve_once))
        if prompt.startswith("Assess target"):
            return {
                "campaign_id": "campaign/invented",
                "manifest_digest": "sha256:invented",
                "profile": "recommended",
                "review": {
                    "budget": {
                        "turns": 30,
                        "tokens": 100_000,
                        "seconds": 2_700,
                        "cost_usd": None,
                    }
                },
            }
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", "Assess", "target"],
    )

    with pytest.raises(SystemExit, match="invented provider/model limits"):
        main()

    assert not any(prompt.startswith("Approve and execute") for prompt, _ in calls)


def test_campaign_approve_accepts_exact_explicit_budget(monkeypatch) -> None:
    calls = []
    client = _install_durable_campaign(
        monkeypatch,
        objective="Assess target",
        campaign_id="campaign/exact",
        digest="sha256:exact",
        budget={"turns": 40, "tokens": 100_000, "seconds": 3_600},
    )

    async def print_stream(prompt, *, approve_once=False, **_kwargs):
        calls.append((prompt, approve_once))
        if prompt.startswith("Assess target"):
            return {
                "campaign_id": "campaign/exact",
                "manifest_digest": "sha256:exact",
                "profile": "recommended",
                "review": {"budget": {"turns": 40, "tokens": 100_000, "seconds": 3_600}},
            }
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merlin",
            "campaign",
            "submit",
            "--approve",
            "--max-turns",
            "40",
            "--max-tokens",
            "100000",
            "--max-seconds",
            "3600",
            "Assess target",
        ],
    )

    main()

    assert client.execute_calls == [("campaign/exact", "sha256:exact")]
    assert not any(prompt.startswith("Approve and execute") for prompt, _ in calls)


def test_report_export_defaults_to_markdown(monkeypatch) -> None:
    calls = []

    async def print_stream(prompt, *, approve_once=False):
        calls.append((prompt, approve_once))

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(sys, "argv", ["merlin", "report", "export", "reports/result.md"])

    main()

    assert calls == [('/report run --format markdown --output "reports/result.md"', False)]


def test_report_export_help_does_not_create_a_report(monkeypatch, capsys) -> None:
    calls = []

    async def print_stream(prompt, *, approve_once=False):
        calls.append((prompt, approve_once))

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(sys, "argv", ["merlin", "report", "export", "--help"])

    main()

    assert calls == []
    assert capsys.readouterr().out == "usage: merlin report export OUTPUT\n"


def test_campaign_submit_threads_autonomous_composition_into_prompt(monkeypatch) -> None:
    calls = []

    async def print_stream(prompt, *, approve_once=False, **_kwargs):
        calls.append((prompt, approve_once))

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merlin",
            "campaign",
            "submit",
            "--composition",
            "autonomous",
            "Assess",
            "target",
        ],
    )

    main()

    prompt = calls[1][0]
    assert "operator explicitly selected campaign profile: recommended" in prompt
    assert "operator explicitly selected specialist composition: autonomous" in prompt
    assert "specialist_composition='autonomous'" in prompt
    assert "assessment_prepare" in prompt


def test_campaign_submit_default_composition_leaves_prompt_unchanged(monkeypatch) -> None:
    calls = []

    async def print_stream(prompt, *, approve_once=False, **_kwargs):
        calls.append((prompt, approve_once))

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merlin",
            "campaign",
            "submit",
            "--composition",
            "opinionated",
            "Assess",
            "target",
        ],
    )

    main()

    assert "specialist composition" not in calls[1][0]


def test_campaign_submit_rejects_unknown_composition(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--composition", "chaotic", "Assess target"],
    )

    with pytest.raises(SystemExit, match="--composition opinionated\\|autonomous"):
        main()


def test_campaign_approve_rejects_changed_composition(monkeypatch) -> None:
    calls = []
    _install_durable_campaign(
        monkeypatch,
        objective="Assess target",
        campaign_id="campaign/composed",
        composition="opinionated",
    )

    async def print_stream(prompt, *, approve_once=False, **_kwargs):
        calls.append((prompt, approve_once))
        if prompt.startswith("Assess target"):
            return {
                "campaign_id": "campaign/composed",
                "manifest_digest": "sha256:reviewed",
                "profile": "recommended",
                "specialist_composition": "opinionated",
                "review": {"budget": {}},
            }
        return None

    monkeypatch.setattr("merlin_tui.cli._print_stream", print_stream)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merlin",
            "campaign",
            "submit",
            "--composition",
            "autonomous",
            "--approve",
            "Assess target",
        ],
    )

    with pytest.raises(SystemExit, match="changed the selected specialist composition"):
        main()

    assert not any(prompt.startswith("Approve and execute") for prompt, _ in calls)


def test_restart_explains_foreground_ownership_without_traceback(monkeypatch, capsys):
    from merlin_tui import local_control

    async def refused():
        raise RuntimeError("clean shutdown is reserved for the foreground owner")

    async def unexpected_start():
        pytest.fail("must not start another daemon after shutdown was refused")

    monkeypatch.setattr(sys, "argv", ["merlin", "restart"])
    monkeypatch.setattr(local_control, "restart_daemon", refused)
    monkeypatch.setattr(local_control, "ensure_daemon", unexpected_start)
    with pytest.raises(SystemExit) as failure:
        main()
    assert failure.value.code == 1
    message = capsys.readouterr().err
    assert "foreground owner" in message
    assert "Traceback" not in message


def test_restart_stops_before_starting(monkeypatch, capsys):
    from merlin_tui import local_control

    calls = []

    async def restart():
        calls.append("restart")

    monkeypatch.setattr(sys, "argv", ["merlin", "restart"])
    monkeypatch.setattr(local_control, "restart_daemon", restart)
    main()
    assert calls == ["restart"]
    assert "Merlin daemon ready" in capsys.readouterr().out


def test_restart_delegates_to_coordinated_lifecycle(monkeypatch, capsys) -> None:
    calls = []

    async def restart() -> None:
        calls.append("restart")

    async def unexpected(*args, **kwargs):
        pytest.fail("restart must use the coordinated lifecycle")

    monkeypatch.setattr("merlin_tui.local_control.restart_daemon", restart)
    monkeypatch.setattr("merlin_tui.local_control.stop_daemon", unexpected)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", unexpected)
    monkeypatch.setattr(sys, "argv", ["merlin", "restart"])

    main()

    assert calls == ["restart"]
    captured = capsys.readouterr()
    assert captured.out == "Merlin daemon ready\n"
    assert captured.err == ""


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
def test_restart_reports_coordination_failure_without_traceback(
    monkeypatch, capsys, error_type
) -> None:
    async def restart() -> None:
        raise error_type("The existing session is still closing. Try again shortly.")

    monkeypatch.setattr("merlin_tui.local_control.restart_daemon", restart)
    monkeypatch.setattr(sys, "argv", ["merlin", "restart"])

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Merlin could not restart: The existing session is still closing. Try again shortly.\n"
    )
