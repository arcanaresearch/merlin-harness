from __future__ import annotations

import asyncio
import getpass
import json
import os
import shutil
import sys
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO
from urllib.parse import unquote, urlsplit

from rich.console import Group
from rich.live import Live
from rich.text import Text

_STREAM_TEARDOWN_TIMEOUT_SECONDS = 1.0
_CAMPAIGN_POLL_TIMEOUT_SECONDS = 5.0
_STREAM_CLIENT_OVERRIDE: ContextVar[object | None] = ContextVar(
    "merlin_cli_stream_client", default=None
)


async def _bounded_stream_teardown(operation: Coroutine[Any, Any, object]) -> None:
    """Bound best-effort stream teardown without hiding operation failures."""
    task = asyncio.create_task(operation)

    def consume_exception(finished: asyncio.Task[object]) -> None:
        if finished.cancelled():
            return
        try:
            finished.exception()
        except Exception:
            pass

    try:
        done, _ = await asyncio.wait({task}, timeout=_STREAM_TEARDOWN_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(consume_exception)
        raise
    if task in done:
        await task
        return
    task.cancel()
    task.add_done_callback(consume_exception)


async def _bounded_task_teardown(task: asyncio.Task[object]) -> None:
    """Cancel one in-flight stream task without blocking durable follow-up work."""

    def consume_exception(finished: asyncio.Task[object]) -> None:
        if finished.cancelled():
            return
        try:
            finished.exception()
        except Exception:
            pass

    if not task.done():
        task.cancel()
    try:
        done, _ = await asyncio.wait({task}, timeout=_STREAM_TEARDOWN_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(consume_exception)
        raise
    if task in done:
        await asyncio.gather(task, return_exceptions=True)
        return
    task.add_done_callback(consume_exception)


def main() -> None:
    _repair_working_directory()
    if sys.argv[1:] == ["--version"]:
        from importlib.metadata import version

        print(f"merlin {version('merlin-tui')}")
        return
    if sys.argv[1:] == ["daemon"]:
        from .local_control import configured_local_control_tuning, serve

        asyncio.run(
            serve(tuning=configured_local_control_tuning(), hold_lock_until_process_exit=True)
        )
        return
    if sys.argv[1:] in (
        ["daemon", "stop"],
        ["stop"],
        ["daemon", "stop", "--force"],
        ["stop", "--force"],
    ):
        from .local_control import stop_daemon

        force = "--force" in sys.argv[1:]
        stopped = asyncio.run(stop_daemon(force=force))
        print("Merlin daemon stopped" if stopped else "Merlin daemon is not running")
        return
    if sys.argv[1:] in (["daemon", "start"], ["start"]):
        from .local_control import ensure_daemon

        try:
            asyncio.run(ensure_daemon())
        except (RuntimeError, TimeoutError) as error:
            print(f"Merlin could not start: {error}", file=sys.stderr)
            raise SystemExit(1) from None
        print("Merlin daemon ready")
        return
    if sys.argv[1:] in (["daemon", "status"], ["status"]):
        from .local_control import _SOCKET, _ping

        print("running" if asyncio.run(_ping(_SOCKET)) else "stopped")
        return
    if sys.argv[1:] == ["restart"]:
        from .local_control import restart_daemon

        try:
            asyncio.run(restart_daemon())
        except (RuntimeError, TimeoutError) as error:
            print(f"Merlin could not restart: {error}", file=sys.stderr)
            raise SystemExit(1) from None
        print("Merlin daemon restarted; it will stop automatically if no UI connects")
        return
    if sys.argv[1:] in (["storage", "stop"], ["storage", "purge"]):
        _manage_embedded_storage(purge=sys.argv[2] == "purge")
        return
    if sys.argv[1:] == ["database", "status"]:
        _database_status()
        return
    if sys.argv[1:] == ["database", "shell"]:
        _database_shell()
        return
    if len(sys.argv) >= 4 and sys.argv[1:3] == ["browser", "profile"]:
        from .configuration import BrowserConfiguration

        BrowserConfiguration().save_profile(Path(sys.argv[3]), sys.argv[4:])
        print("Configured persistent browser profile; restart Merlin to apply it")
        return
    if len(sys.argv) == 4 and sys.argv[1:3] == ["campaign", "status"]:
        asyncio.run(_print_call("campaign_status", sys.argv[3]))
        return
    if sys.argv[1:] == ["campaign", "list"]:
        asyncio.run(_print_call("list_campaigns"))
        return
    if len(sys.argv) in {3, 4} and sys.argv[1:3] == ["campaign", "cancel"]:
        requested = sys.argv[3] if len(sys.argv) == 4 else "*"
        campaign_id = "*" if requested.lower() == "all" else requested
        asyncio.run(_print_call("cancel_campaign", campaign_id))
        return
    if len(sys.argv) == 4 and sys.argv[1:3] == ["campaign", "pause"]:
        asyncio.run(_print_call("pause_campaign", sys.argv[3]))
        return
    if len(sys.argv) == 4 and sys.argv[1:3] == ["campaign", "resume"]:
        asyncio.run(_print_call("resume_campaign", sys.argv[3]))
        return
    if len(sys.argv) in {5, 6} and sys.argv[1:3] == ["campaign", "saturation"]:
        campaign_id, decision = sys.argv[3:5]
        if decision not in {"stop", "continue", "limit"}:
            raise SystemExit(
                "usage: merlin campaign saturation ID stop|continue|limit [ADDITIONAL_WAVES]"
            )
        additional_waves = None
        if decision == "limit":
            if len(sys.argv) != 6:
                raise SystemExit("usage: merlin campaign saturation ID limit ADDITIONAL_WAVES")
            try:
                additional_waves = int(sys.argv[5])
            except ValueError as error:
                raise SystemExit("ADDITIONAL_WAVES must be an integer") from error
        elif len(sys.argv) != 5:
            raise SystemExit(
                "usage: merlin campaign saturation ID stop|continue|limit [ADDITIONAL_WAVES]"
            )
        asyncio.run(
            _print_call("decide_campaign_saturation", campaign_id, decision, additional_waves)
        )
        return
    if sys.argv[1:] == ["lessons"]:
        asyncio.run(_print_call("list_lessons"))
        return
    if sys.argv[1:] == ["agents"]:
        asyncio.run(_print_call("list_agents"))
        return
    if sys.argv[1:] == ["agents", "kill", "all"]:
        asyncio.run(_print_call("cancel_campaign", "*"))
        return
    if sys.argv[1:] == ["tasks"]:
        asyncio.run(_print_active_tasks())
        return
    if sys.argv[1:] == ["tasks", "--json"]:
        asyncio.run(_print_active_tasks(json_output=True))
        return
    if len(sys.argv) == 3 and sys.argv[1] == "resume":
        asyncio.run(_print_stream(f"/resume {sys.argv[2]}"))
        return
    if sys.argv[1:] == ["interrupt"]:
        asyncio.run(_print_call("interrupt"))
        return
    if len(sys.argv) >= 2 and sys.argv[1] in {"--plain", "--ndjson"}:
        output_format = sys.argv[1].removeprefix("--")
        prompt, approve, session_id = _parse_noninteractive(sys.argv[2:])
        try:
            exit_code = asyncio.run(
                _output_stream(prompt, output_format, approve_once=approve, session_id=session_id)
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\nMerlin interrupted; durable campaign work continues.", file=sys.stderr)
            raise SystemExit(130) from None
        if exit_code:
            raise SystemExit(exit_code)
        return
    if len(sys.argv) >= 3 and sys.argv[1:3] == ["campaign", "submit"]:
        try:
            asyncio.run(_submit_campaign(sys.argv[3:]))
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\nMerlin interrupted; durable campaign work continues.", file=sys.stderr)
            raise SystemExit(130) from None
        return
    if sys.argv[1:] == ["report", "export", "--help"]:
        print("usage: merlin report export OUTPUT")
        return
    if len(sys.argv) == 4 and sys.argv[1:3] == ["report", "export"]:
        asyncio.run(_print_stream(f'/report run --format markdown --output "{sys.argv[3]}"'))
        return
    if len(sys.argv) == 3 and sys.argv[1] == "support":
        asyncio.run(_print_stream(f"/support {json.dumps(sys.argv[2])}"))
        return
    if sys.argv[1:]:
        raise SystemExit(
            "usage: merlin [--version|start|stop [--force]|restart|status|"
            "daemon [start|status|stop [--force]]|campaign submit|list|status|cancel|"
            "lessons|agents [kill all]|tasks|resume|"
            "interrupt|report export|support OUTPUT.zip|database [status|shell]|"
            "browser profile DIRECTORY ORIGIN...|"
            "--plain PROMPT|--ndjson PROMPT]"
        )

    from .bootstrap import build_client_app

    try:
        if not _provider_is_configured():
            _run_first_setup()
        asyncio.run(_ensure_daemon_with_splash())
        try:
            app = build_client_app()
            app.run()
            if app.return_code:
                raise SystemExit(app.return_code)
        finally:
            _flush_terminal_input()
    except (EOFError, KeyboardInterrupt):
        # Interactive cancellation is an expected operator action, including in
        # the frozen launcher.  Converting it to a quiet shell exit prevents
        # PyInstaller from presenting a misleading application-crash traceback.
        print("\nMerlin setup cancelled; no incomplete provider configuration was saved.")
        raise SystemExit(130) from None
    except (RuntimeError, OSError) as exc:
        print(f"\nMerlin could not start: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def _repair_working_directory() -> Path:
    """Recover when the shell's current directory was deleted or replaced."""
    try:
        return Path(os.getcwd())
    except OSError:
        fallback = Path.home()
        os.chdir(fallback)
        return fallback


def _flush_terminal_input() -> None:
    """Discard queued mouse-report bytes before the shell restores input echo."""
    if not sys.stdin.isatty():
        return
    try:
        import termios

        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (ImportError, OSError):
        return


def _manage_embedded_storage(*, purge: bool) -> None:
    """Private launcher hook for stopping or removing Merlin's pg0 instance."""
    from hashlib import sha256

    from pg0 import Pg0

    data_dir = Path.home() / ".merlin" / "postgres"
    name = f"merlin-{sha256(str(data_dir.resolve()).encode()).hexdigest()[:12]}"
    server = Pg0(name=name, data_dir=str(data_dir))
    if purge:
        server.drop(force=True)
        print("Merlin storage purged")
    else:
        server.stop()
        print("Merlin storage stopped")


def _pg0_server():
    from hashlib import sha256

    from pg0 import Pg0

    data_dir = Path.home() / ".merlin" / "postgres"
    name = f"merlin-{sha256(str(data_dir.resolve()).encode()).hexdigest()[:12]}"
    return Pg0(name=name, data_dir=str(data_dir))


def _database_status() -> None:
    info = _pg0_server().info()
    if not info.running:
        print("stopped")
        return
    print(f"running · localhost:{info.port} · database {info.database} · user {info.username}")


def _database_shell() -> None:
    """Open psql without placing pg0's generated credential in output or argv."""
    server = _pg0_server()
    info = server.info()
    if not info.running or not info.uri:
        raise SystemExit("Merlin PostgreSQL is not running; run `merlin start` first")
    version = info.version or "18.1.0"
    bundled = Path.home() / ".pg0" / "installation" / version / "bin" / "psql"
    executable = str(bundled) if bundled.is_file() else shutil.which("psql")
    if executable is None:
        raise SystemExit("psql is unavailable in the Merlin bundle")
    connection = urlsplit(info.uri)
    if not connection.hostname or not connection.username or not connection.path.removeprefix("/"):
        raise SystemExit("Merlin PostgreSQL connection metadata is incomplete")
    environment = dict(os.environ)
    if connection.password:
        environment["PGPASSWORD"] = unquote(connection.password)
    arguments = [
        executable,
        "--host",
        connection.hostname,
        "--port",
        str(connection.port or info.port),
        "--username",
        unquote(connection.username),
        "--dbname",
        unquote(connection.path.removeprefix("/")),
    ]
    os.execve(executable, arguments, environment)


def _provider_is_configured() -> bool:
    from collections.abc import Mapping

    from merlin_config import load_config

    provider = load_config().values.get("provider", {})
    return bool(
        isinstance(provider, Mapping)
        and str(provider.get("base_url", "")).strip()
        and str(provider.get("model", "")).strip()
    )


def _run_first_setup(
    *,
    read: Callable[[str], str] = input,
    read_secret: Callable[[str], str] = getpass.getpass,
    output: TextIO = sys.stdout,
) -> None:
    """Run the first-use provider wizard before the daemon needs a provider."""
    from .configuration import ProviderConfiguration
    from .provider_catalog import PROVIDER_ROUTES, discover_model_catalog

    print("Merlin needs an LLM provider before its first session.", file=output)
    print("Models are discovered live; Merlin does not use a baked model list.\n", file=output)
    for index, route in enumerate(PROVIDER_ROUTES, start=1):
        print(f"{index}. {route.label}", file=output)
    while True:
        raw = read(f"Provider (1-{len(PROVIDER_ROUTES)}): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(PROVIDER_ROUTES):
            route = PROVIDER_ROUTES[int(raw) - 1]
            break
        print(f"Choose a provider by number (1-{len(PROVIDER_ROUTES)}).", file=output)
    base_url = route.base_url or read("OpenAI-compatible base URL: ").strip()
    while not base_url:
        base_url = read("Base URL is required: ").strip()
    api_key = read_secret("API key (hidden; stored locally): ") if route.needs_key else ""
    discovery = discover_model_catalog(route, api_key, base_url=base_url)
    models = discovery.models
    if models:
        print(f"\nModels currently reported by {route.label}:", file=output)
        for index, candidate in enumerate(models, start=1):
            details = []
            if candidate.context_window_tokens is not None:
                details.append(f"{candidate.context_window_tokens:,} context tokens")
            if candidate.vision is not None:
                details.append("vision" if candidate.vision else "text-only")
            suffix = f" — {', '.join(details)}" if details else ""
            print(f"{index}. {candidate.id}{suffix}", file=output)
        print(f"{len(models) + 1}. Enter an exact model ID manually", file=output)
        while True:
            raw_model = read(f"Model (1-{len(models) + 1}): ").strip()
            if raw_model.isdigit() and 1 <= int(raw_model) <= len(models):
                selected = models[int(raw_model) - 1]
                model = selected.id
                break
            if raw_model == str(len(models) + 1):
                model = read("Exact model ID: ").strip()
                if model:
                    break
            print("Choose a listed model number or the manual-entry option.", file=output)
    else:
        if discovery.error:
            print(f"\n{discovery.error}", file=output)
        print(
            "\nCould not retrieve a model catalog from this provider. "
            "Enter an exact model ID from its current documentation.",
            file=output,
        )
        model = read("Exact model ID: ").strip()
        while not model:
            model = read("Model ID is required: ").strip()
    observed_vision = selected.vision if models and "selected" in locals() else None
    print("\nModel input capability:", file=output)
    default_label = " [default]" if observed_vision is not True else ""
    print(f"1. Text only{default_label}", file=output)
    vision_label = " [provider-reported default]" if observed_vision is True else ""
    print(f"2. Text and images{vision_label}", file=output)
    while True:
        raw_vision = read("Capability (Enter or 1-2): ").strip()
        if raw_vision == "":
            vision = observed_vision is True
            break
        if raw_vision == "1":
            vision = False
            break
        if raw_vision == "2":
            vision = True
            break
        print("Press Enter or 1 for text only; 2 for text and images.", file=output)
    print("\nModel context capability:", file=output)
    observed_context = selected.context_window_tokens if models and "selected" in locals() else None
    context_hint = (
        f" Provider reports {observed_context:,}; Enter accepts it."
        if observed_context is not None
        else ""
    )
    print(
        "Enter the exact documented context-window token count, or Enter if unknown."
        + context_hint,
        file=output,
    )
    while True:
        raw_window = read("Context window tokens (optional): ").strip()
        if not raw_window:
            context_window_tokens = observed_context
            break
        if raw_window.isdigit() and int(raw_window) >= 1_024:
            context_window_tokens = int(raw_window)
            break
        print("Enter an integer of at least 1024, or press Enter for unknown.", file=output)
    configuration = ProviderConfiguration()
    configuration.save(
        base_url,
        model,
        api_key,
        vision=vision,
        context_window_tokens=context_window_tokens,
        output_token_parameter=route.output_token_parameter,
        reasoning_parameter=route.reasoning_parameter,
    )
    print(f"\nConfigured {route.label} with model {model}. Use /setup to reconfigure.", file=output)
    while True:
        add = read("Add another provider endpoint for a different model? (y/N): ").strip()
        if add.lower() not in {"y", "yes"}:
            break
        endpoint_model = read("Model ID served by that endpoint: ").strip()
        while not endpoint_model:
            endpoint_model = read("Model ID is required: ").strip()
        endpoint_base_url = read("Base URL for that provider: ").strip()
        while not endpoint_base_url:
            endpoint_base_url = read("Base URL is required: ").strip()
        endpoint_key = read_secret("API key for that endpoint (hidden; stored locally): ")
        configuration.save_model_endpoint(endpoint_model, endpoint_base_url, endpoint_key)
        print(f"Configured a provider endpoint for {endpoint_model}.", file=output)
    print("\nOptional Campaign Lead model transport:", file=output)
    print("1. Keep the configured provider model for every role [default]", file=output)
    print("2. Codex App Server (Merlin tools supported)", file=output)
    print("3. Claude CLI (no Merlin tools)", file=output)
    print("4. Kimi ACP (no tools; unavailable to token-capped campaigns)", file=output)
    while True:
        raw_transport = read("Lead transport (Enter or 1-4): ").strip()
        if raw_transport in {"", "1"}:
            return
        transport = {
            "2": "codex_app_server",
            "3": "claude_cli",
            "4": "kimi_acp",
        }.get(raw_transport)
        if transport is not None:
            break
        print("Press Enter or 1 for the provider model; choose 2-4 for a native CLI.", file=output)
    lead_model = read("Exact Campaign Lead model ID: ").strip()
    while not lead_model:
        lead_model = read("Campaign Lead model ID is required: ").strip()
    configuration.save_model_route(lead_model, transport, lead_only=True)
    print(
        f"Configured Campaign Lead model {lead_model} via {transport}. ",
        "Workers retain the tool-capable provider model.",
        sep="",
        file=output,
    )


async def _ensure_daemon_with_splash() -> None:
    from .app import WORDMARK
    from .local_control import _arm_unclaimed_shutdown, ensure_daemon

    frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    status = Text("Preparing Merlin…", style="dim")
    splash = Group(Text("\n".join(WORDMARK), style="cyan"), status)
    with Live(splash, refresh_per_second=12, transient=True) as live:

        def progress(elapsed: float) -> None:
            if elapsed < 2:
                step = "Starting local daemon"
            elif elapsed < 8:
                step = "Initializing embedded PostgreSQL"
            else:
                step = "Preparing first-run storage"
            frame = frames[int(elapsed * 8) % len(frames)]
            status.plain = f"{frame} {step} ({int(elapsed)}s)"
            live.refresh()

        try:
            started_daemon = await ensure_daemon(progress=progress)
            if started_daemon:
                await _arm_unclaimed_shutdown()
        except RuntimeError:
            from .local_control import _SOCKET, _daemon_lock_released, _ping

            if await _ping(_SOCKET):
                return
            if not await asyncio.to_thread(_daemon_lock_released, _SOCKET):
                # Let the TUI offer recovery; do not launch through a closing owner.
                return
            raise


async def _client(*, confirmer: object | None = None):
    from .local_control import LocalControlClient, ensure_daemon

    owns_daemon = bool(await ensure_daemon())
    return LocalControlClient(confirmer=confirmer, owns_daemon=owns_daemon)


async def _print_call(method: str, *args: str) -> None:
    client = await _client()
    try:
        value = await getattr(client, method)(*args)
        print(json.dumps(_plain(value), indent=2, sort_keys=True))
    finally:
        await client.close()


async def _print_active_tasks(*, json_output: bool = False) -> None:
    client = await _client()
    try:
        value = _plain(await client.list_active_schedules())
    finally:
        await client.close()
    if json_output:
        print(json.dumps(value, indent=2, sort_keys=True))
        return
    schedules = value if isinstance(value, list) else []
    if not schedules:
        print("No active schedules.")
        return
    terminal = {"completed", "failed", "cancelled", "quarantined"}
    for schedule in schedules:
        if not isinstance(schedule, dict):
            continue
        nodes = schedule.get("nodes")
        rows = [item for item in nodes if isinstance(item, dict)] if isinstance(nodes, list) else []
        active = [item for item in rows if str(item.get("status", "")) not in terminal]
        print(
            f"{schedule.get('id', 'unknown')} · {schedule.get('status', 'unknown')} · "
            f"{len(active)} active / {len(rows)} nodes"
        )
        for node in active:
            attempts = node.get("attempts")
            latest = attempts[-1] if isinstance(attempts, list) and attempts else {}
            owner = latest.get("worker", "unassigned") if isinstance(latest, dict) else "unassigned"
            print(f"  {node.get('status', 'unknown'):>9}  {node.get('id', 'unknown')} · {owner}")


async def _print_stream(
    prompt: str,
    *,
    approve_once: bool = False,
    stop_after_tool: str | None = None,
    concise: bool = False,
) -> dict[str, object] | None:
    from .local_control import LocalControlClient, ensure_daemon

    client = _STREAM_CLIENT_OVERRIDE.get()
    created_client = client is None
    original_confirmer: object | None = None
    if client is None:
        owns_daemon = bool(await ensure_daemon())

        async def confirmer(*_args: object) -> str:
            return "allow_once"

        client = LocalControlClient(
            confirmer=confirmer if approve_once else None,
            owns_daemon=owns_daemon,
        )
    elif approve_once:
        original_confirmer = getattr(client, "_confirmer", None)

        async def confirmer(*_args: object) -> str:
            return "allow_once"

        client._confirmer = confirmer  # type: ignore[attr-defined]
    campaign_review: dict[str, object] | None = None
    last_message = ""
    display_stream = _coalesced_display_stream(client, prompt)

    async def interrupt_stream() -> None:
        await _bounded_stream_teardown(client.interrupt())

    try:
        async for kind, value in display_stream:
            if not concise or kind in {"campaign_review", "error", "status"}:
                print(f"{kind}: {value}")
            if kind in {"content", "error"} and str(value).strip():
                # Retain the terminal explanation so a turn that ends without a
                # durable outcome fails with the model's own reason attached.
                last_message = " ".join(str(value).split())[-500:]
            if kind == "campaign_review":
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    campaign_review = parsed
                    if stop_after_tool == "assessment_prepare":
                        await interrupt_stream()
                        break
            if (
                kind == "tool"
                and stop_after_tool
                and value.startswith(f"{stop_after_tool} completed")
            ):
                # The durable daemon owns the campaign now. End this model turn
                # before it can poll the full status projection back into itself.
                await interrupt_stream()
                break
    except asyncio.CancelledError:
        await interrupt_stream()
        raise
    finally:
        await _bounded_stream_teardown(display_stream.aclose())
        close = getattr(client, "close", None)
        if created_client and close is not None:
            await _bounded_stream_teardown(close())
        elif approve_once:
            client._confirmer = original_confirmer  # type: ignore[attr-defined]
    if campaign_review is None and last_message:
        return {"preparation_note": last_message}
    return campaign_review


async def _print_stream_on(
    client: object,
    prompt: str,
    *,
    approve_once: bool = False,
    stop_after_tool: str | None = None,
    concise: bool = False,
) -> dict[str, object] | None:
    """Run one stream on a command-owned client without transferring ownership."""
    token = _STREAM_CLIENT_OVERRIDE.set(client)
    try:
        return await _print_stream(
            prompt,
            approve_once=approve_once,
            stop_after_tool=stop_after_tool,
            concise=concise,
        )
    finally:
        _STREAM_CLIENT_OVERRIDE.reset(token)


async def _coalesced_display_stream(client: object, prompt: str):
    """Keep human CLI output readable without changing machine-stream semantics."""
    pending_kind: str | None = None
    pending: list[str] = []
    async for event in client.stream(prompt):  # type: ignore[attr-defined]
        if event.kind in {"thinking", "content"}:
            if pending_kind is not None and pending_kind != event.kind:
                yield pending_kind, "".join(pending)
                pending.clear()
            pending_kind = event.kind
            pending.append(event.text)
            continue
        if pending_kind is not None:
            yield pending_kind, "".join(pending)
            pending_kind = None
            pending.clear()
        yield event.kind, event.text
    if pending_kind is not None:
        yield pending_kind, "".join(pending)


def _parse_noninteractive(arguments: list[str]) -> tuple[str, bool, str | None]:
    approve = False
    session_id: str | None = None
    prompt_parts: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--approve":
            approve = True
            index += 1
        elif argument == "--session" and index + 1 < len(arguments):
            session_id = arguments[index + 1]
            index += 2
        else:
            prompt_parts.extend(arguments[index:])
            break
    if not prompt_parts:
        raise SystemExit("usage: merlin --plain|--ndjson [--approve] [--session ID] PROMPT|-")
    prompt = " ".join(prompt_parts)
    if prompt == "-":
        prompt = sys.stdin.read()
    if not prompt.strip():
        raise SystemExit("non-interactive prompt must not be empty")
    return prompt, approve, session_id


async def _output_stream(
    prompt: str,
    output_format: str,
    *,
    approve_once: bool = False,
    session_id: str | None = None,
) -> int:
    """Non-interactive stream with stable plain or one-JSON-object-per-line output."""

    async def confirmer(*_args: object) -> str:
        return "allow_once"

    client = await _client(confirmer=confirmer if approve_once else None)
    sequence = 0
    failed = False
    wrote_content = False

    async def consume(value: str):
        nonlocal sequence, failed, wrote_content
        async for event in client.stream(value):
            sequence += 1
            failed |= event.kind == "error"
            if output_format == "ndjson":
                print(
                    json.dumps(
                        {"sequence": sequence, "kind": event.kind, "text": event.text},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            elif event.kind == "content":
                print(event.text, end="", flush=True)
                wrote_content = True
            elif event.kind == "error":
                print(event.text, file=sys.stderr, flush=True)

    try:
        if session_id is not None:
            await consume(f"/resume {session_id}")
        if not failed:
            await consume(prompt)
    except asyncio.CancelledError:
        await client.interrupt()
        raise
    except Exception as error:
        failed = True
        sequence += 1
        if output_format == "ndjson":
            print(
                json.dumps(
                    {"sequence": sequence, "kind": "error", "text": str(error)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        else:
            print(str(error), file=sys.stderr, flush=True)
    finally:
        await client.close()
    if output_format == "plain" and wrote_content:
        print()
    elif output_format == "ndjson":
        print(
            json.dumps({"sequence": sequence + 1, "kind": "end", "ok": not failed}),
            flush=True,
        )
    return 1 if failed else 0


def _prepared_objective_preserves_operator_scope(operator: str, prepared: str) -> bool:
    """Accept verbatim scope or an exact operator clause plus a same-origin target anchor."""

    typography = str.maketrans(
        {
            "\N{LEFT SINGLE QUOTATION MARK}": "'",
            "\N{RIGHT SINGLE QUOTATION MARK}": "'",
            "\N{SINGLE LOW-9 QUOTATION MARK}": "'",
            "\N{LEFT DOUBLE QUOTATION MARK}": '"',
            "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
            "\N{DOUBLE LOW-9 QUOTATION MARK}": '"',
        }
    )

    def normalized(value: str) -> str:
        return " ".join(value.translate(typography).split())

    operator_normalized = normalized(operator)
    prepared_normalized = normalized(prepared)
    if operator_normalized in prepared_normalized:
        return True

    # ``campaign submit`` is itself the Merlin invocation, so providers may
    # reasonably omit the leading product command while preserving the actual
    # assessment objective.  Admit only this exact wrapper removal, and only
    # through the same-origin anchored path below.  The remaining operator text
    # still has to survive unchanged apart from typography, whitespace, and case.
    operator_variants = [operator_normalized]
    invocation_prefix = "use merlin to "
    if operator_normalized.casefold().startswith(invocation_prefix):
        operator_variants.append(operator_normalized[len(invocation_prefix) :])

    lines = [line.strip() for line in prepared.splitlines() if line.strip()]
    target_lines = [line for line in lines if line.casefold().startswith("target:")]
    semantic_lines = [line for line in lines if line not in target_lines]
    if not target_lines or not semantic_lines:
        return False
    if not any(
        all(normalized(line).casefold() in variant.casefold() for line in semantic_lines)
        for variant in operator_variants
    ):
        return False

    operator_origins = {
        (parsed.scheme.casefold(), parsed.netloc.casefold())
        for token in operator.replace("(", " ").replace(")", " ").split()
        if (parsed := urlsplit(token.rstrip(".,:;"))).scheme in {"http", "https"} and parsed.netloc
    }
    for line in target_lines:
        parsed = urlsplit(line.split(":", 1)[1].strip())
        if (parsed.scheme.casefold(), parsed.netloc.casefold()) not in operator_origins:
            return False
    return True


async def _submit_campaign(arguments: list[str]) -> None:
    profile = "recommended"
    composition = "opinionated"
    approve = False
    limits: dict[str, int | float] = {}
    objective_parts = []
    index = 0
    while index < len(arguments):
        if arguments[index] == "--profile" and index + 1 < len(arguments):
            profile = arguments[index + 1]
            index += 2
        elif arguments[index] == "--composition" and index + 1 < len(arguments):
            composition = arguments[index + 1]
            index += 2
        elif arguments[index] == "--approve":
            approve = True
            index += 1
        elif arguments[index] in {
            "--max-turns",
            "--max-tokens",
            "--max-seconds",
            "--max-cost-usd",
        }:
            option = arguments[index]
            expected = "positive number" if option == "--max-cost-usd" else "positive integer"
            if index + 1 >= len(arguments):
                raise SystemExit(f"{option} requires a {expected}")
            try:
                value = (
                    float(arguments[index + 1])
                    if option == "--max-cost-usd"
                    else int(arguments[index + 1])
                )
            except ValueError:
                value = 0
            if value <= 0:
                raise SystemExit(f"{option} requires a {expected}")
            limits[option.removeprefix("--max-").replace("-", "_")] = value
            index += 2
        else:
            objective_parts.append(arguments[index])
            index += 1
    if (
        profile not in {"recommended", "exhaustive", "exploration", "quickscan"}
        or composition not in {"opinionated", "autonomous"}
        or not objective_parts
    ):
        raise SystemExit(
            "usage: merlin campaign submit "
            "[--profile recommended|exhaustive|exploration|quickscan] "
            "[--composition opinionated|autonomous] [--approve] "
            "[--max-turns N] [--max-tokens N] [--max-seconds N] "
            "[--max-cost-usd USD] OBJECTIVE"
        )
    objective = " ".join(objective_parts)
    # A submitted campaign is a new reviewed workflow. Reusing whatever
    # conversation happened to be active can leak stale permission failures,
    # target assumptions, or an interrupted manifest into its reconnaissance.
    campaign_client = None
    if approve:
        campaign_client = await _client()
        acquire_foreground_lease = getattr(campaign_client, "acquire_foreground_lease", None)
        if callable(acquire_foreground_lease):
            await acquire_foreground_lease()
        await _print_stream_on(campaign_client, "/new")
    else:
        await _print_stream("/new")
    existing_campaign_ids = (
        {
            str(item.get("id", ""))
            for item in _plain(await campaign_client.list_campaigns())
            if isinstance(item, dict)
        }
        if campaign_client is not None
        else set()
    )
    budget_instruction = ""
    if limits:
        budget_instruction = (
            "\nSeal this exact campaign budget into assessment_prepare: "
            + ", ".join(f"max_{name}={value}" for name, value in sorted(limits.items()))
            + "."
        )
    else:
        budget_instruction = (
            "\nThe operator requested no provider/model campaign limits. Keep max_turns, "
            "max_tokens, max_seconds, and max_cost_usd unset; do not invent safety caps."
        )
    composition_instruction = ""
    if composition != "opinionated":
        composition_instruction = (
            f"\nThe operator explicitly selected specialist composition: {composition} "
            "(orthogonal to the coverage profile); pass "
            f"specialist_composition={composition!r} to assessment_prepare; do not ask again."
        )
    preparation_prompt = (
        f"{objective}\nThe operator explicitly selected campaign profile: {profile}; "
        "do not ask for the profile again. Any max_* values below are provider/model "
        f"campaign budgets, not target chat/message limits.{budget_instruction}"
        f"{composition_instruction}"
        "\nPass the operator objective text above verbatim as the assessment_prepare "
        "objective: do not reword, paraphrase, or substitute the target line; Merlin "
        "anchors the reviewed target itself. Before assessment_prepare, verify that the "
        "sealed handoff has executable state observations for every explicitly requested "
        "state dimension (for example tier, character, score, status, and remaining turns). "
        "If an exact captured response contains those fields but the handoff omits their "
        "selectors, repair the handoff from that captured evidence first. Include one reviewed "
        "phase_gate for each explicit setup or normal-play prerequisite whenever the reviewed "
        "adapter supplies an exact action and authoritative success observation. Do not prepare "
        "a manifest that silently drops an explicit prerequisite or cannot measure an explicit "
        "state objective."
    )
    if not approve:
        await _print_stream(preparation_prompt, concise=True)
        return
    assert campaign_client is not None
    durable_review: dict[str, object] | None = None
    streamed_review: dict[str, object] | None = None
    preparation_note: str | None = None
    campaign_poll: asyncio.Task[object] | None = None
    campaign_poll_error: BaseException | None = None
    objective_repair_needed: tuple[str, str] | None = None
    try:
        # The preparing model occasionally ends its turn asking for confirmation
        # instead of calling assessment_prepare even though --approve was given.
        # Retry once with an explicit nudge before refusing approval.
        for preparation_attempt in range(3):
            if objective_repair_needed is not None:
                preparation_prompt = (
                    "The previous prepared campaign was cancelled because its objective changed "
                    "the operator's wording and therefore changed the sealed requirements. Reuse "
                    "the already-sealed reconnaissance handoff from this session; do not repeat "
                    "reconnaissance. Call assessment_prepare exactly once now. Set its objective "
                    "argument to the exact JSON string below, byte-for-byte, without adding a "
                    "Target line or changing verbs, punctuation, or quotes:\n"
                    f"operator_objective_json={json.dumps(objective, ensure_ascii=False)}\n"
                    f"Use campaign_profile={profile!r}"
                    + (
                        f" and specialist_composition={composition!r}"
                        if composition != "opinionated"
                        else ""
                    )
                    + (
                        ". Keep the exact previously selected budget: "
                        + ", ".join(
                            f"max_{name}={value}" for name, value in sorted(limits.items())
                        )
                        if limits
                        else ". Keep all provider/model budget limits unset"
                    )
                    + ". Do not ask for confirmation."
                )
            elif preparation_attempt:
                preparation_prompt = (
                    "The previous turn ended without calling assessment_prepare. "
                    "Approval is already given; call assessment_prepare exactly once "
                    "now with the already-sealed recon handoff for this session, the "
                    "operator objective verbatim, "
                    f"campaign_profile={profile!r}"
                    + (
                        f", specialist_composition={composition!r}"
                        if composition != "opinionated"
                        else ""
                    )
                    + (
                        ", and "
                        + (
                            "budget "
                            + ", ".join(
                                f"max_{name}={value}" for name, value in sorted(limits.items())
                            )
                            if limits
                            else "no budget caps"
                        )
                    )
                    + ". Verify executable state observations and include every evidenced "
                    "operator-required setup or normal-play phase_gate before preparing. Do not "
                    "ask for confirmation."
                )
            preparation = asyncio.create_task(
                _print_stream_on(
                    campaign_client,
                    preparation_prompt,
                    approve_once=True,
                    stop_after_tool="assessment_prepare",
                    concise=True,
                )
            )
            # Let the preparation request start before polling durable state. This
            # also keeps mocked/local transports from being cancelled before they
            # can issue assessment_prepare.
            await asyncio.sleep(0)
            streamed_review = None
            campaign_poll = None
            campaign_poll_error = None
            while True:
                if campaign_poll_error is None:
                    campaign_poll = asyncio.create_task(campaign_client.list_campaigns())
                    watched = {campaign_poll, preparation}
                    timeout = _CAMPAIGN_POLL_TIMEOUT_SECONDS
                else:
                    # A failed fallback must not overrule the exact review event
                    # emitted by a successful assessment_prepare tool call.
                    watched = {preparation}
                    timeout = None
                done, _ = await asyncio.wait(
                    watched, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if preparation in done:
                    streamed_review = await preparation
                    if (
                        isinstance(streamed_review, dict)
                        and isinstance(streamed_review.get("campaign_id"), str)
                        and isinstance(streamed_review.get("manifest_digest"), str)
                        and isinstance(streamed_review.get("objective"), str)
                        and isinstance(streamed_review.get("review"), dict)
                    ):
                        if campaign_poll is not None:
                            if campaign_poll not in done:
                                await _bounded_task_teardown(campaign_poll)
                            else:
                                # FIRST_COMPLETED may return both tasks. Consume a
                                # simultaneous fallback failure before trusting the
                                # independently valid streamed durable review.
                                await asyncio.gather(campaign_poll, return_exceptions=True)
                            campaign_poll = None
                        streamed_objective = str(streamed_review["objective"])
                        if not _prepared_objective_preserves_operator_scope(
                            objective, streamed_objective
                        ):
                            drifted_id = str(streamed_review["campaign_id"])
                            await campaign_client.cancel_campaign(drifted_id)
                            existing_campaign_ids.add(drifted_id)
                            objective_repair_needed = (drifted_id, streamed_objective)
                            streamed_review = None
                            break
                        objective_repair_needed = None
                        status_task = asyncio.create_task(
                            campaign_client.campaign_status(str(streamed_review["campaign_id"]))
                        )
                        status_done, _ = await asyncio.wait(
                            {status_task}, timeout=_CAMPAIGN_POLL_TIMEOUT_SECONDS
                        )
                        if status_task not in status_done:
                            await _bounded_task_teardown(status_task)
                            raise SystemExit(
                                "durable campaign status verification timed out; refusing approval"
                            )
                        status = _plain(await status_task)
                        if not isinstance(status, dict) or status.get("status") != "prepared":
                            raise SystemExit("durable campaign left prepared state before approval")
                        if status.get("manifest_digest") != streamed_review["manifest_digest"]:
                            raise SystemExit(
                                "streamed campaign review conflicts with durable manifest digest"
                            )
                        durable_composition = status.get("specialist_composition")
                        if durable_composition not in {"opinionated", "autonomous"}:
                            raise SystemExit(
                                "durable prepared campaign omitted specialist composition; "
                                "refusing approval"
                            )
                        durable_review = {
                            **streamed_review,
                            "specialist_composition": durable_composition,
                        }
                        break
                if campaign_poll is not None and campaign_poll not in done:
                    # Durable polling is recovery for providers that omit the
                    # campaign_review event. It must never hold an exact streamed
                    # review hostage behind a stuck daemon read.
                    await _bounded_task_teardown(campaign_poll)
                    campaign_poll = None
                    if preparation.done():
                        break
                    continue
                try:
                    campaigns = _plain(await campaign_poll)
                except Exception as error:
                    campaign_poll_error = error
                    campaign_poll = None
                    if preparation.done():
                        break
                    continue
                campaign_poll = None
                candidates = [
                    item
                    for item in campaigns
                    if isinstance(item, dict)
                    and str(item.get("id", "")) not in existing_campaign_ids
                    and item.get("profile") == profile
                    and item.get("status") == "prepared"
                ]
                if len(candidates) > 1:
                    raise SystemExit(
                        "multiple new prepared campaigns match the requested objective/profile; "
                        "refusing ambiguous approval"
                    )
                if len(candidates) == 1:
                    candidate = candidates[0]
                    campaign_id = candidate.get("id")
                    candidate_objective = str(candidate.get("objective") or "")
                    if not _prepared_objective_preserves_operator_scope(
                        objective, candidate_objective
                    ):
                        if isinstance(campaign_id, str) and campaign_id:
                            await campaign_client.cancel_campaign(campaign_id)
                            existing_campaign_ids.add(campaign_id)
                            objective_repair_needed = (campaign_id, candidate_objective)
                        await _bounded_task_teardown(preparation)
                        break
                    objective_repair_needed = None
                    list_digest = candidate.get("manifest_digest")
                    if not isinstance(campaign_id, str) or not isinstance(list_digest, str):
                        raise SystemExit(
                            "durable prepared campaign omitted its ID or manifest digest"
                        )
                    status = _plain(await campaign_client.campaign_status(campaign_id))
                    if not isinstance(status, dict) or status.get("status") != "prepared":
                        raise SystemExit("durable campaign left prepared state before approval")
                    status_digest = status.get("manifest_digest")
                    if status_digest != list_digest or not list_digest.startswith("sha256:"):
                        raise SystemExit(
                            "durable prepared campaign manifest digest is inconsistent"
                        )
                    budget_state = status.get("budget")
                    budget = budget_state.get("limits") if isinstance(budget_state, dict) else None
                    durable_review = {
                        "campaign_id": campaign_id,
                        "manifest_digest": list_digest,
                        "profile": profile,
                        "review": {"budget": budget},
                        **(
                            {"specialist_composition": status["specialist_composition"]}
                            if status.get("specialist_composition") is not None
                            else {}
                        ),
                    }
                    # The durable prepared campaign is the approval authority.  A
                    # provider may keep the conversational stream open while it
                    # waits for an answer to its rendered approval question, so do
                    # not wait for that stream after the exact durable review has
                    # been recovered and checked.
                    await _bounded_task_teardown(preparation)
                    break
                if preparation.done():
                    break
                await asyncio.sleep(0.1)
            if streamed_review is None and preparation.done() and not preparation.cancelled():
                streamed_review = await preparation
            if isinstance(streamed_review, dict) and isinstance(
                streamed_review.get("preparation_note"), str
            ):
                # Keep the model's own terminal explanation even if a retry
                # attempt ends with no note at all.
                preparation_note = str(streamed_review["preparation_note"])
            if durable_review is not None:
                break
            if objective_repair_needed is not None:
                continue
            if preparation_attempt >= 1:
                break
            if campaign_poll is not None and not campaign_poll.done():
                await _bounded_task_teardown(campaign_poll)
                campaign_poll = None
    finally:
        if campaign_poll is not None and not campaign_poll.done():
            await _bounded_task_teardown(campaign_poll)
        if not preparation.done():
            await _bounded_task_teardown(preparation)
        # Keep the client that owns the preparation daemon alive through direct
        # execution. Closing it here can stop that daemon; a replacement daemon
        # then has no active preparation session and correctly rejects the
        # campaign as belonging to a different session.
        if durable_review is None:
            await _bounded_stream_teardown(campaign_client.close())
    if durable_review is None:
        if objective_repair_needed is not None:
            drifted_id, drifted_objective = objective_repair_needed
            raise SystemExit(
                "prepared campaign objective drifted from the operator objective after repair "
                f"attempts; refusing approval. Last cancelled campaign: {drifted_id}.\n"
                f"operator objective: {objective!r}\n"
                f"prepared objective: {drifted_objective!r}"
            )
        note = ""
        if isinstance(streamed_review, dict):
            tail = streamed_review.get("preparation_note")
            if isinstance(tail, str) and tail.strip():
                note = f"; the preparation turn ended without assessment_prepare: {tail}"
        if not note and preparation_note:
            note = f"; the preparation turn ended without assessment_prepare: {preparation_note}"
        if campaign_poll_error is not None:
            note += "; durable campaign polling also failed"
        raise SystemExit("campaign was not durably prepared; refusing approval" + note)
    campaign_active = False
    try:
        if (
            streamed_review is not None
            and "campaign_id" in streamed_review
            and (
                streamed_review.get("campaign_id") != durable_review["campaign_id"]
                or streamed_review.get("manifest_digest") != durable_review["manifest_digest"]
            )
        ):
            raise SystemExit("streamed campaign review conflicts with durable prepared campaign")
        _validate_campaign_review(
            durable_review,
            expected_profile=profile,
            expected_limits=limits,
            expected_composition=composition,
        )
        campaign_id = durable_review.get("campaign_id")
        digest = durable_review.get("manifest_digest")
        if not isinstance(campaign_id, str) or not isinstance(digest, str):
            raise SystemExit("prepared campaign review omitted its ID or manifest digest")
        # The durable review is verified; from here execution must not depend on
        # a model turn choosing to call assessment_execute. Keep using the client
        # attached to the preparation daemon so the reviewed session identity is
        # preserved through the execute RPC.
        try:
            execution = _plain(await campaign_client.execute_campaign(campaign_id, digest))
        except RuntimeError as error:
            raise SystemExit(f"campaign {campaign_id} execution was rejected: {error}") from None
        campaign_active = (
            isinstance(execution, dict) and execution.get("status") == "executing"
        )
        final = _plain(await campaign_client.campaign_status(campaign_id))
        if not isinstance(final, dict) or final.get("status") == "prepared":
            raise SystemExit(
                f"campaign {campaign_id} is still prepared after the execute call: "
                "no wave was compiled and the campaign will never start on its own; "
                "re-run merlin campaign submit or cancel the campaign"
            )
        campaign_active = final.get("status") == "executing"
        print(f"Campaign started: {campaign_id}")
        print(f"Monitor: merlin campaign status {campaign_id}")
    finally:
        if campaign_active:
            # Submission is finished, but the daemon now owns live campaign work.
            # Releasing the foreground lease arms the daemon's campaign-aware idle
            # watcher; closing an owning client here would bypass that watcher and
            # tear down the campaign immediately after printing "Campaign started".
            release = getattr(campaign_client, "release_foreground_lease", None)
            if callable(release):
                await _bounded_stream_teardown(release())
        else:
            await _bounded_stream_teardown(campaign_client.close())


def _validate_campaign_review(
    review: dict[str, object],
    *,
    expected_profile: str,
    expected_limits: dict[str, int | float],
    expected_composition: str = "opinionated",
) -> None:
    """Fail closed when the prepared manifest changes operator-selected controls."""
    actual_profile = review.get("profile")
    if actual_profile != expected_profile:
        raise SystemExit(
            "prepared campaign changed the selected profile: "
            f"expected {expected_profile}, received {actual_profile!r}; refusing approval"
        )
    # Tolerant until every backend projects specialist_composition into its review: only
    # a present-but-different value proves the prepared manifest changed the selection.
    actual_composition = review.get("specialist_composition")
    if actual_composition is not None and actual_composition != expected_composition:
        raise SystemExit(
            "prepared campaign changed the selected specialist composition: "
            f"expected {expected_composition}, received {actual_composition!r}; "
            "refusing approval"
        )
    review_detail = review.get("review")
    budget = review_detail.get("budget") if isinstance(review_detail, dict) else None
    if expected_limits and not isinstance(budget, dict):
        raise SystemExit(
            "prepared campaign omitted the explicit provider/model budget; refusing approval"
        )
    if not expected_limits:
        if not isinstance(budget, dict):
            raise SystemExit(
                "prepared campaign omitted its unlimited budget projection; refusing approval"
            )
        unexpected = {
            name: budget.get(name)
            for name in ("turns", "tokens", "seconds", "cost_usd")
            if budget.get(name) is not None
        }
        if unexpected:
            raise SystemExit(
                "prepared campaign invented provider/model limits that the operator did not "
                f"request: {unexpected!r}; refusing approval"
            )
    for name, expected in expected_limits.items():
        actual = budget.get(name) if isinstance(budget, dict) else None
        if actual != expected:
            raise SystemExit(
                "prepared campaign changed an explicit provider/model budget: "
                f"max_{name} expected {expected}, received {actual!r}; refusing approval"
            )


def _plain(value: object) -> object:
    if isinstance(value, SimpleNamespace):
        return {key: _plain(item) for key, item in vars(value).items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value
