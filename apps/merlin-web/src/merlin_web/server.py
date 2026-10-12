from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shlex
import webbrowser
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aiohttp import web
from merlin_tui.local_control import _ALLOWED, LocalControlClient, ensure_daemon
from merlin_tui.local_control import _json as _protocol_json


def _json(value: Any) -> Any:
    """Reverse local-control view decoding before returning browser JSON.

    LocalControlClient returns SimpleNamespace records, including nested records.
    The protocol encoder intentionally stringifies unknown types; applying it
    directly here silently turns populated operator tables into empty pages.
    """
    if isinstance(value, SimpleNamespace):
        return {key: _json(item) for key, item in vars(value).items()}
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json(item) for item in value]
    return _protocol_json(value)


class Workspace:
    """One browser workspace owns one daemon foreground; tabs share its state."""

    def __init__(
        self,
        backend: Any = None,
        *,
        close_backend: bool = True,
        external_busy: Any = None,
        stream_handler: Any = None,
    ) -> None:
        self.close_backend = close_backend
        self.external_busy = external_busy
        self.stream_handler = stream_handler
        self.token = secrets.token_urlsafe(32)
        # Cookies share a host across ports: isolate concurrent local workspaces.
        self.cookie_name = f"merlin_workspace_{secrets.token_hex(12)}"
        self.backend = backend or LocalControlClient(confirmer=self.confirm)
        self.events: deque[dict[str, Any]] = deque(maxlen=20000)
        self.cursor = 0
        self.pending: dict[str, asyncio.Future[str]] = {}
        self.turn: asyncio.Task[None] | None = None
        self.requests: dict[str, str] = {}
        self.latest_review: Any = None
        self.latest_input: Any = None
        self.permission_values: dict[str, Any] = {}
        self.connected = backend is not None
        self.connect_lock = asyncio.Lock()
        self.submission_lock = asyncio.Lock()
        self.files: set[Path] = set()

    @property
    def busy(self) -> bool:
        return bool(
            (self.turn and not self.turn.done()) or (self.external_busy and self.external_busy())
        )

    async def connect(self) -> None:
        async with self.connect_lock:
            if not self.connected:
                owns_daemon = await ensure_daemon(self.backend._socket_path)
                self.backend._owns_daemon = owns_daemon
                await self.backend.acquire_foreground_lease()
                self.connected = True

    def emit(self, kind: str, value: Any) -> None:
        if kind == "user_input":
            self.latest_input = json.loads(value) if isinstance(value, str) else value
        elif kind == "user_input_resolved":
            self.latest_input = None
        elif kind == "campaign_review":
            self.latest_review = value
        elif kind in {"session_reset", "campaign_configure"}:
            self.latest_review = None
            if kind == "session_reset":
                self.latest_input = None
        elif kind == "permission":
            self.permission_values[value["id"]] = value
        elif kind == "permission_resolved":
            self.permission_values.pop(str(value), None)
        self.cursor += 1
        self.events.append({"cursor": self.cursor, "kind": kind, "value": value})

    async def confirm(self, spec: Any, context: Any, arguments: Any) -> str:
        identity = secrets.token_urlsafe(16)
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        # Share the terminal's reviewed, redacted display, never raw credentials.
        from merlin_tui.app import _render_permission_details

        tool_name = str(getattr(spec, "name", "unknown"))
        context_values = _json(context)
        workspace_root = str(context_values.get("workspace_root", "."))
        self.emit(
            "permission",
            {
                "id": identity,
                "tool": {"name": tool_name},
                "effect": arguments.get("__permission_effect", "unknown"),
                "details": _render_permission_details(tool_name, workspace_root, arguments),
                "allow_command_session": (
                    tool_name == "shell_command"
                    and arguments.get("__permission_command_scope") is True
                ),
            },
        )
        try:
            return await future
        finally:
            self.pending.pop(identity, None)
            self.emit("permission_resolved", identity)

    async def produce(self, prompt: str) -> None:
        self.emit("user", prompt)
        try:
            command = shlex.split(prompt) if prompt.startswith("/") else []
            if command and (
                command[0] in {"/campaign", "/cancel", "/pause", "/mode"}
                or command[:2] == ["/resume", "campaign"]
                or command == ["/agents", "kill", "all"]
            ):
                value = await self.control_command(command)
                self.emit("status", _json(value))
                return
            if self.stream_handler:
                await self.stream_handler(prompt)
                return
            async for event in self.backend.stream(prompt):
                if event.kind == "status" and event.text in {"new session", "session resumed"}:
                    self.emit("session_reset", None)
                self.emit(event.kind, event.text)
        except Exception as error:
            self.emit("error", str(error))
        finally:
            self.emit("turn_end", None)

    async def control_command(self, command: list[str]) -> Any:
        if command == ["/agents", "kill", "all"]:
            return await self.backend.cancel_campaign("*")
        if command[:2] == ["/resume", "campaign"] and len(command) == 3:
            return await self.backend.resume_campaign(command[2])
        if command[0] == "/mode" and len(command) == 2:
            return await self.backend.set_permission_mode(command[1])
        if command[0] == "/campaign" and len(command) >= 2:
            action, rest = command[1], command[2:]
            if action == "cancel" and not rest:
                rest = ["*"]
            methods = {
                "status": "campaign_status",
                "cancel": "cancel_campaign",
                "pause": "pause_campaign",
                "resume": "resume_campaign",
            }
            if action in methods and len(rest) == 1:
                return await getattr(self.backend, methods[action])(
                    "*" if rest[0] == "all" else rest[0]
                )
            if action == "message" and len(rest) >= 2:
                return await self.backend.guide_campaign(rest[0], " ".join(rest[1:]))
            if action == "saturation" and len(rest) in {2, 3}:
                return await self.backend.decide_campaign_saturation(
                    rest[0], rest[1], int(rest[2]) if len(rest) == 3 else None
                )
        if command[0] == "/cancel" and len(command) == 2 and command[1] == "all":
            return await self.backend.cancel_campaign("*")
        if len(command) == 3 and command[1] in {"campaign", "task"}:
            method = {
                ("/cancel", "campaign"): "cancel_campaign",
                ("/cancel", "task"): "cancel_task",
                ("/pause", "campaign"): "pause_campaign",
            }.get(tuple(command[:2]))
            if method:
                return await getattr(self.backend, method)(command[2])
        raise ValueError("Use /campaign status|pause|resume|cancel ID or message ID TEXT")

    async def close(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_result("deny")
        if self.turn and not self.turn.done():
            # End the foreground deliberately; campaign recovery remains daemon-owned.
            await self.backend.interrupt()
            self.turn.cancel()
            await asyncio.gather(self.turn, return_exceptions=True)
        if self.connected and self.close_backend:
            await self.backend.close()


def create_app(workspace: Workspace, static: Path) -> web.Application:
    @web.middleware
    async def boundary(request: web.Request, handler: Any) -> web.StreamResponse:
        # Resist DNS rebinding and cross-origin access to local credentials/control.
        if request.host.split(":")[0] not in {"127.0.0.1", "localhost"}:
            raise web.HTTPForbidden(text="Use the local Merlin URL")
        origin = request.headers.get("Origin")
        if origin and origin != f"http://{request.host}":
            raise web.HTTPForbidden(text="Cross-origin requests are refused")
        if request.path.startswith("/api/") and request.path != "/api/connect":
            if not secrets.compare_digest(
                request.cookies.get(workspace.cookie_name, ""), workspace.token
            ):
                raise web.HTTPUnauthorized(text="Open the URL printed by merlin-web")
        try:
            return await handler(request)
        except (ValueError, TypeError, RuntimeError, OSError) as error:
            return web.json_response({"error": str(error)}, status=400)

    static = static.resolve()
    app = web.Application(middlewares=[boundary], client_max_size=1_000_000)

    async def payload_for(request: web.Request) -> dict[str, Any]:
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object")
        return value

    async def connect(request: web.Request) -> web.Response:
        payload = await payload_for(request)
        if not secrets.compare_digest(str(payload.get("token", "")), workspace.token):
            raise web.HTTPUnauthorized()
        response = web.json_response({"connected": True})
        response.set_cookie(
            workspace.cookie_name, workspace.token, httponly=True, samesite="Strict", path="/"
        )
        return response

    async def rpc(request: web.Request) -> web.Response:
        payload = await payload_for(request)
        method = payload.get("method")
        if method in {"configure_provider", "configure_model_endpoint"}:
            from merlin_tui.configuration import ProviderConfiguration

            configuration = ProviderConfiguration()
            operation = (
                configuration.save
                if method == "configure_provider"
                else configuration.save_model_endpoint
            )
            args = list(payload.get("args", []))
            if method == "configure_provider" and len(args) >= 2:
                from merlin_config import load_config

                config = await asyncio.to_thread(load_config)
                current_url = config.values.get("provider", {}).get("base_url", "")
                if len(args) == 2:
                    args.append(None)
                # A blank field may preserve an existing key only for the same
                # destination. Never send the old provider's key to a new host.
                if not args[2] and str(args[0]).strip() != current_url:
                    args[2] = ""
            await asyncio.to_thread(operation, *args, **payload.get("kwargs", {}))
            return web.json_response(
                {"value": "Saved securely. Reconnect to apply provider access."}
            )
        if method == "command_catalog":
            from merlin_tui.app import COMMANDS

            return web.json_response({"value": list(COMMANDS)})
        if method == "setup_status":
            from merlin_config import ConfigPaths, CredentialStore, load_config
            from merlin_tui.provider_catalog import PROVIDER_ROUTES

            config = await asyncio.to_thread(load_config)
            credentials = await asyncio.to_thread(CredentialStore(ConfigPaths.default()).load)
            return web.json_response(
                {
                    "value": {
                        "providers": _json(PROVIDER_ROUTES),
                        "model_endpoints": [
                            {
                                "model": model,
                                "base_url": endpoint.get("base_url", ""),
                                "key_saved": bool(endpoint.get("api_key")),
                            }
                            for model, endpoint in config.provider_model_endpoints.items()
                        ],
                        "jev_configured": bool(config.judgments.jev_token),
                        "provider": {
                            "key_saved": bool(credentials.get("api_key")),
                            **{
                                k: v
                                for k, v in config.values.get("provider", {}).items()
                                if k
                                in {
                                    "base_url",
                                    "model",
                                    "role_models",
                                    "model_transports",
                                    "model_efforts",
                                }
                            },
                        },
                    }
                }
            )
        if method == "save_jev":
            from merlin_tui.configuration import ProviderConfiguration

            await asyncio.to_thread(
                ProviderConfiguration().save_jev_token, *payload.get("args", [])
            )
            return web.json_response({"value": "JEV key saved securely."})
        if method not in _ALLOWED and method != "apply_saved_model_routing":
            raise ValueError("Unknown operator method")
        await workspace.connect()
        value = await getattr(workspace.backend, method)(
            *payload.get("args", []), **payload.get("kwargs", {})
        )
        result = _json(value)
        if method == "list_agents" and isinstance(result, list):
            from merlin_tui.app import _agent_display_state, _agent_state_group

            result = [
                {
                    **agent,
                    "status": _agent_display_state(str(agent.get("latest_event", ""))),
                    "status_group": _agent_state_group(str(agent.get("latest_event", ""))),
                }
                for agent in result
            ]
        if method in {"execute_campaign", "cancel_campaign"}:
            workspace.latest_review = None
        if method in {"list_reports", "export_campaign_report"}:
            reports = result if isinstance(result, list) else [result]
            for report in reports:
                if isinstance(report, dict) and report.get("output"):
                    path = await asyncio.to_thread(Path(report["output"]).resolve)
                    if path.is_file():
                        workspace.files.add(path)
        return web.json_response({"value": result})

    async def chat(request: web.Request) -> web.Response:
        payload = await payload_for(request)
        prompt = str(payload.get("prompt", "")).strip()
        identity = str(payload.get("request_id", ""))
        if not prompt or not identity:
            raise ValueError("Prompt and request identity are required")
        async with workspace.submission_lock:
            if identity in workspace.requests:
                if workspace.requests[identity] != prompt:
                    raise ValueError("Request identity has already been used for another prompt")
                return web.json_response({"accepted": True, "duplicate": True})
            await workspace.connect()
            if workspace.busy:
                if prompt.startswith("/") or prompt.startswith("MERLIN_USER_INPUT_RESPONSE\n"):
                    raise ValueError(
                        "Wait for this foreground turn or use the explicit interrupt control"
                    )
                accepted = await workspace.backend.steer(prompt)
                if not accepted:
                    raise ValueError(
                        "The active turn did not accept guidance; try again when it ends"
                    )
                workspace.emit("user", prompt)
                workspace.emit("status", "Guidance delivered to the active foreground turn.")
            else:
                workspace.turn = asyncio.create_task(workspace.produce(prompt))
            workspace.requests[identity] = prompt
            return web.json_response({"accepted": True})

    async def history(request: web.Request) -> web.Response:
        await workspace.connect()
        initial = request.query.get("initial") == "1"
        busy = workspace.busy
        cursor = workspace.cursor
        live = list(workspace.events)
        start = next((i for i in range(len(live) - 1, -1, -1) if live[i]["kind"] == "user"), -1)
        reset = next(
            (i for i in range(len(live) - 1, -1, -1) if live[i]["kind"] == "session_reset"), -1
        )
        active_turn = busy and start > reset and not str(live[start]["value"]).startswith("/")
        if initial and active_turn:
            # Commands such as /resume do not represent a persisted model turn.
            live = live[start:]
        loader = getattr(workspace.backend, "conversation_history", None)
        if loader is None:
            page = {"messages": [], "before": None, "has_more": False}
        else:
            page = _json(
                await loader(
                    limit=50,
                    before=int(request.query["before"]) if "before" in request.query else None,
                    session_id=request.query.get("session_id"),
                    exclude_current_turn=initial and active_turn,
                )
            )
        if initial:
            # A turn/session boundary during the DB read invalidates the split
            # between durable messages and live events. Stream deltas alone do not.
            boundaries = {"user", "session_reset"}
            if workspace.busy != busy or any(
                event["cursor"] > cursor and event["kind"] in boundaries
                for event in workspace.events
            ):
                return await history(request)
        return web.json_response(
            {
                **page,
                "cursor": cursor,
                "events": live if initial and (active_turn or loader is None) else [],
            }
        )

    async def events(request: web.Request) -> web.Response:
        cursor = max(0, int(request.query.get("after", "0")))
        return web.json_response(
            {
                "events": [e for e in workspace.events if e["cursor"] > cursor][:500],
                "busy": workspace.busy,
                "review": workspace.latest_review,
                "user_input": workspace.latest_input,
                "permissions": list(workspace.permission_values.values()),
                "expired": bool(workspace.events and cursor < workspace.events[0]["cursor"] - 1),
            }
        )

    async def decide(request: web.Request) -> web.Response:
        payload = await payload_for(request)
        future = workspace.pending.get(str(payload.get("id", "")))
        decision = payload.get("decision")
        if future is None or future.done():
            raise ValueError("This permission request is no longer pending")
        if decision not in {"allow_once", "allow_command_session", "deny"}:
            raise ValueError("Choose allow once, exact command for this session, or deny")
        if decision == "allow_command_session" and not workspace.permission_values.get(
            str(payload.get("id", "")), {}
        ).get("allow_command_session"):
            raise ValueError("This request does not support an exact command session allowance")
        future.set_result(decision)
        return web.json_response({"accepted": True})

    async def download(request: web.Request) -> web.StreamResponse:
        path = await asyncio.to_thread(Path(request.query.get("path", "")).resolve)
        if path not in workspace.files or not await asyncio.to_thread(path.is_file):
            raise web.HTTPNotFound()
        if path.suffix == ".html":
            # Reports are untrusted rendered evidence, isolated from operator cookies.
            return web.FileResponse(
                path,
                headers={
                    "Content-Security-Policy": (
                        "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
                    ),
                    "X-Content-Type-Options": "nosniff",
                },
            )
        return web.FileResponse(
            path, headers={"Content-Disposition": f'attachment; filename="{path.name}"'}
        )

    async def index(request: web.Request) -> web.StreamResponse:
        relative = request.match_info.get("path", "") or "index.html"
        path = await asyncio.to_thread((static / relative).resolve)
        if not path.is_relative_to(static) or not await asyncio.to_thread(path.is_file):
            raise web.HTTPNotFound()
        return web.FileResponse(
            path,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
            },
        )

    app.router.add_post("/api/connect", connect)
    app.router.add_post("/api/rpc", rpc)
    app.router.add_post("/api/chat", chat)
    app.router.add_get("/api/events", events)
    app.router.add_get("/api/history", history)
    app.router.add_post("/api/permission", decide)
    app.router.add_get("/api/file", download)
    app.router.add_get("/{path:.*}", index)

    async def cleanup(_app: web.Application) -> None:
        await workspace.close()

    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Merlin local browser workspace")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--socket-path", type=Path, help="Use an isolated Merlin daemon socket")
    parser.add_argument("--static-dir", type=Path, help="Built UI directory")
    arguments = parser.parse_args()
    packaged = Path(__file__).parent / "static"
    source = Path(__file__).parents[2] / "frontend/dist"
    static = arguments.static_dir or (packaged if packaged.is_dir() else source)
    if not (static / "index.html").is_file():
        parser.error(
            "Build the UI first: cd apps/merlin-web/frontend && pnpm install && pnpm build"
        )
    if arguments.socket_path:
        os.environ["MERLIN_SOCKET_PATH"] = str(arguments.socket_path)
    workspace = Workspace()
    if arguments.socket_path:
        workspace.backend._socket_path = arguments.socket_path
    url = f"http://127.0.0.1:{arguments.port}/#token={workspace.token}"
    print(f"Open Merlin: {url}", flush=True)
    web.run_app(
        create_app(workspace, static),
        host="127.0.0.1",
        port=arguments.port,
        access_log=None,
        print=(lambda _: webbrowser.open(url)) if not arguments.no_open else None,
    )
