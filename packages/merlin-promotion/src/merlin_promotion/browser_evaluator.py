from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import unquote, urlsplit

from merlin_browser import (
    BrowserBounds,
    BrowserOwner,
    BrowserSession,
    browser_tools,
    launch_isolated,
)
from merlin_contracts import (
    EvaluationMetrics,
    PromotionCandidate,
    ToolContext,
    parse_model_json,
)

from .adapter_evaluator import _matches_expected, _render_template
from .provider_evaluator import _budget_int, _read_owned_file
from .service import EvaluationBinding, EvaluationCase

BrowserLauncher = Callable[[BrowserBounds, BrowserOwner], Awaitable[BrowserSession]]
PersistentBrowserResolver = Callable[[str, BrowserBounds], Awaitable[BrowserSession]]
BrowserActorReleaser = Callable[[str], Awaitable[None]]
CONFIGURED_BROWSER_PROFILE_HANDLE = "browser-profile:configured"


@dataclass(frozen=True, slots=True)
class BrowserEvaluationStep:
    id: str
    tool: str
    arguments: Mapping[str, object]
    response_pointer: str
    expected_value: object
    capture_as: str | None


@dataclass(frozen=True, slots=True)
class BrowserEvaluationFixture:
    adapter_id: str
    endpoint: str
    credential_handle: str | None
    steps: tuple[BrowserEvaluationStep, ...]


def load_browser_evaluation_fixture(
    case: EvaluationCase, *, max_bytes: int = 1_048_576
) -> BrowserEvaluationFixture:
    parsed = urlsplit(case.target_fixture)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("browser evaluation fixture must use an absolute local file URI")
    path = Path(unquote(parsed.path))
    if not path.is_absolute():
        raise ValueError("browser evaluation fixture path must be absolute")
    payload = _read_owned_file(path, max_bytes=max_bytes)
    if f"sha256:{sha256(payload).hexdigest()}" != case.fixture_digest:
        raise ValueError("browser evaluation fixture digest mismatch")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("browser evaluation fixture is not valid UTF-8 JSON") from error
    expected = {
        "schema_version",
        "adapter_id",
        "interface",
        "endpoint",
        "credential_handle",
        "steps",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("browser evaluation fixture has an unexpected schema")
    if value["schema_version"] != "merlin.browser-adapter-evaluation.v1":
        raise ValueError("browser evaluation fixture has an unsupported schema version")
    if value["interface"] != "browser":
        raise ValueError("browser evaluation fixture requires the browser interface")
    for field in ("adapter_id", "endpoint"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise TypeError(f"browser evaluation {field} must be a non-empty string")
    endpoint = urlsplit(value["endpoint"])
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
        raise ValueError("browser evaluation endpoint must be an absolute HTTP(S) URL")
    if endpoint.username is not None or endpoint.password is not None:
        raise ValueError("browser evaluation endpoint must not contain credentials")
    credential_handle = value["credential_handle"]
    if credential_handle is not None and (
        not isinstance(credential_handle, str) or not credential_handle.strip()
    ):
        raise TypeError("browser evaluation credential_handle must be null or non-empty")
    raw_steps = value["steps"]
    if not isinstance(raw_steps, list) or not raw_steps:
        raise TypeError("browser evaluation steps must be a non-empty array")
    steps = tuple(_step(item) for item in raw_steps)
    if len({step.id for step in steps}) != len(steps):
        raise ValueError("browser evaluation step ids must be unique")
    captures = [step.capture_as for step in steps if step.capture_as is not None]
    if len(set(captures)) != len(captures):
        raise ValueError("browser evaluation capture names must be unique")
    first = steps[0]
    if first.tool != "browser_open_tab" or first.arguments.get("url") != value["endpoint"]:
        raise ValueError("browser evaluation must open the exact reviewed endpoint first")
    return BrowserEvaluationFixture(
        adapter_id=value["adapter_id"],
        endpoint=value["endpoint"],
        credential_handle=credential_handle,
        steps=steps,
    )


class BrowserAdapterCaseEvaluator:
    """Execute an immutable browser fixture in one exact-origin isolated context."""

    def __init__(
        self,
        launcher: BrowserLauncher | None = None,
        *,
        persistent_resolver: PersistentBrowserResolver | None = None,
        actor_releaser: BrowserActorReleaser | None = None,
        persistent_profile_handle: str = CONFIGURED_BROWSER_PROFILE_HANDLE,
    ) -> None:
        self._launcher = launcher or _launch_headless
        self._persistent_resolver = persistent_resolver
        self._actor_releaser = actor_releaser
        self._persistent_profile_handle = persistent_profile_handle

    async def __call__(
        self,
        candidate: PromotionCandidate,
        case: EvaluationCase,
        binding: EvaluationBinding,
    ) -> EvaluationMetrics:
        if candidate.kind != "adapter" or candidate.content.get("interface") != "browser":
            raise ValueError("browser evaluation suites accept browser adapter candidates only")
        if not binding.session_id:
            raise PermissionError("browser evaluation requires an authorized session binding")
        fixture = load_browser_evaluation_fixture(case)
        exact = {
            "adapter_id": fixture.adapter_id,
            "interface": "browser",
            "endpoint": fixture.endpoint,
            "credential_handle": fixture.credential_handle,
        }
        if any(candidate.content.get(key) != value for key, value in exact.items()):
            raise ValueError("browser adapter candidate does not match the evaluation fixture")
        if binding.route != fixture.adapter_id:
            raise ValueError("browser evaluation route must name the exact adapter id")
        timeout = _budget_int(binding, "case_timeout_seconds", default=120, minimum=1, maximum=900)
        max_steps = _budget_int(binding, "max_steps", default=32, minimum=1, maximum=1_000)
        if len(fixture.steps) > max_steps:
            raise ValueError("browser evaluation fixture exceeds the suite step cap")
        parsed = urlsplit(fixture.endpoint)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        owner = BrowserOwner(
            session_id=binding.session_id,
            worker_id=f"promotion:{candidate.id}",
            technique_id=case.id,
        )
        bounds = BrowserBounds(
            target_id=f"promotion:{fixture.adapter_id}",
            allowed_origins=frozenset({origin}),
            max_tabs=1,
            requires_persistent_profile=fixture.credential_handle is not None,
            single_chat_session=fixture.credential_handle is not None,
        )
        started = perf_counter()
        session: BrowserSession | None = None
        succeeded = True
        attributed = 0
        cleanup_succeeded = False
        variables: dict[str, object] = {}
        pending_error: BaseException | None = None
        persistent = fixture.credential_handle is not None
        if persistent and (
            fixture.credential_handle != self._persistent_profile_handle
            or self._persistent_resolver is None
            or self._actor_releaser is None
        ):
            raise ValueError("browser evaluation credential handle is unavailable")
        try:
            async with asyncio.timeout(timeout):
                if persistent:
                    assert self._persistent_resolver is not None
                    session = await self._persistent_resolver(binding.session_id, bounds)
                else:
                    session = await self._launcher(bounds, owner)
                tools = {tool.name: tool for tool in browser_tools(lambda _: session)}
                context = ToolContext(
                    session_id=binding.session_id,
                    workspace_root=".",
                    agent_id=owner.worker_id,
                    target_id=bounds.target_id,
                )
                for index, step in enumerate(fixture.steps):
                    arguments = _render_template(dict(step.arguments), variables)
                    if not isinstance(arguments, dict):
                        raise TypeError("browser evaluation arguments must render to an object")
                    arguments["step"] = index
                    result = await tools[step.tool].invoke(context, arguments)
                    if result.is_error:
                        succeeded = False
                        break
                    observed = _observe_result(result.content, step)
                    step_ok = _matches_expected(observed, step.expected_value)
                    succeeded = succeeded and step_ok
                    attributed += int(step_ok)
                    if step_ok and step.capture_as is not None:
                        variables[step.capture_as] = observed
                    if not step_ok:
                        break
        except (Exception, asyncio.CancelledError) as error:
            pending_error = error
            succeeded = False
        finally:
            if session is not None:
                try:
                    if persistent:
                        assert self._actor_releaser is not None
                        await asyncio.shield(self._actor_releaser(owner.worker_id))
                    else:
                        await asyncio.shield(session.close())
                    cleanup_succeeded = True
                except Exception:
                    cleanup_succeeded = False
        if pending_error is not None:
            raise pending_error
        latency_ms = max(0, int((perf_counter() - started) * 1_000))
        return EvaluationMetrics(
            task_success_rate=float(succeeded and cleanup_succeeded),
            false_positive_rate=float(not succeeded),
            evidence_attribution_rate=attributed / len(fixture.steps),
            authority_violations=0,
            mean_cost=0,
            p95_latency_ms=latency_ms,
            cancellation_recovery_rate=float(cleanup_succeeded),
        )


async def _launch_headless(bounds: BrowserBounds, owner: BrowserOwner) -> BrowserSession:
    return await launch_isolated(bounds, owner, headless=True)


def _step(value: object) -> BrowserEvaluationStep:
    expected = {
        "id",
        "tool",
        "arguments",
        "response_pointer",
        "expected_value",
        "capture_as",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("browser evaluation step has an unexpected schema")
    if not isinstance(value["id"], str) or not value["id"].strip():
        raise TypeError("browser evaluation step id must be non-empty")
    supported = {
        "browser_open_tab",
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_fill",
        "browser_press",
        "browser_select",
        "browser_check",
        "browser_uncheck",
        "browser_hover",
        "browser_wait_for_text",
        "browser_network_events",
        "browser_close_tab",
    }
    if value["tool"] not in supported:
        raise ValueError("browser evaluation step tool is unsupported")
    if not isinstance(value["arguments"], dict):
        raise TypeError("browser evaluation step arguments must be an object")
    if not isinstance(value["response_pointer"], str):
        raise TypeError("browser evaluation response_pointer must be a string")
    capture_as = value["capture_as"]
    if capture_as is not None and (
        not isinstance(capture_as, str)
        or not capture_as.isidentifier()
        or capture_as.startswith("_")
    ):
        raise ValueError("browser evaluation capture_as must be null or a public identifier")
    return BrowserEvaluationStep(
        id=value["id"],
        tool=value["tool"],
        arguments=value["arguments"],
        response_pointer=value["response_pointer"],
        expected_value=value["expected_value"],
        capture_as=capture_as,
    )


def _observe_result(content: str, step: BrowserEvaluationStep) -> object:
    try:
        value: Any = parse_model_json(content)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"browser evaluation step {step.id} returned invalid JSON") from error
    if step.response_pointer == "":
        return value
    if not step.response_pointer.startswith("/"):
        raise ValueError("browser evaluation response_pointer must be a JSON pointer")
    for token in step.response_pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            raise ValueError(f"browser evaluation response pointer missing at step {step.id}")
    return value
