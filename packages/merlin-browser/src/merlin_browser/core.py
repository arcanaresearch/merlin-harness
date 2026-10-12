from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections import deque
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
    Frame,
    Locator,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .models import (
    BrowserAction,
    BrowserBounds,
    BrowserMetadata,
    BrowserObservation,
    BrowserOwner,
    ElementRef,
    TabMetadata,
)

_NETWORK_JSON_BYTES = 32 * 1024
_SECRET_JSON_KEYS = ("authorization", "cookie", "credential", "password", "secret", "token")


def _json_string_paths(value: object, prefix: str = "") -> tuple[tuple[str, str], ...]:
    """Return RFC 6901 pointers for string leaves without retaining the containing JSON."""
    if isinstance(value, str):
        return ((prefix or "/", value),)
    if isinstance(value, dict):
        paths: list[tuple[str, str]] = []
        for key, child in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            paths.extend(_json_string_paths(child, f"{prefix}/{escaped}"))
        return tuple(paths)
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(_json_string_paths(child, f"{prefix}/{index}"))
        return tuple(paths)
    return ()


def _bounded_json_body(value: str | bytes | None) -> object | None:
    if value is None:
        return None
    payload = value.encode() if isinstance(value, str) else value
    if len(payload) > _NETWORK_JSON_BYTES:
        return {"$merlin": "json body exceeds capture limit", "bytes": len(payload)}
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    def redact(item: object) -> object:
        if isinstance(item, dict):
            return {
                str(key): (
                    "[redacted]"
                    if any(word in str(key).lower() for word in _SECRET_JSON_KEYS)
                    else redact(child)
                )
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [redact(child) for child in item]
        return item

    return redact(parsed)


class BrowserBoundaryError(RuntimeError):
    pass


class OriginDenied(BrowserBoundaryError):
    pass


class StaleReference(BrowserBoundaryError):
    pass


class ManualBlocker(BrowserBoundaryError):
    pass


def _playwright_transport_is_closed(error: PlaywrightError) -> bool:
    detail = str(error).casefold()
    return any(
        marker in detail
        for marker in (
            "connection closed while reading from the driver",
            "target page, context or browser has been closed",
            "browser has been closed",
        )
    )


@dataclass(slots=True)
class _Element:
    generation: int
    frame_ref: str
    role: str
    name: str
    locator: Locator


class BrowserSession:
    """One bounded Playwright context. It exposes no cookie or credential-reading API."""

    def __init__(
        self,
        context: BrowserContext,
        bounds: BrowserBounds,
        owner: BrowserOwner,
        *,
        mode: str,
        browser: Browser | None = None,
        playwright: Playwright | None = None,
        context_id: str | None = None,
        created_at: datetime | None = None,
        actor_wait_seconds: float = 30.0,
        launch_mode: str = "unknown",
        launch_fallback_reason: str | None = None,
    ) -> None:
        self.context = context
        self.bounds, self.owner, self.mode = bounds, owner, mode
        self.context_id = context_id or uuid4().hex
        self._browser, self._playwright = browser, playwright
        self._created_at = created_at or datetime.now(UTC)
        self.launch_mode = launch_mode
        self.launch_fallback_reason = launch_fallback_reason
        self._tabs: dict[str, Page] = {}
        self._page_refs: dict[Page, str] = {}
        self._openers: dict[str, str | None] = {}
        self._owned_pages: set[Page] = set()
        self._elements: dict[str, _Element] = {}
        self._frame_refs: dict[Frame, str] = {}
        # Frame origins admitted because they are embedded by an already
        # bounds-checked page (e.g. vendor chat widgets). Session-local only.
        self._admitted_frame_origins: set[str] = set()
        self._generation: dict[str, int] = {}
        self._blocked_download = False
        self._download_signal = asyncio.Event()
        self._recovery_tab_ref: str | None = None
        self._closed = False
        self._route_installed = False
        self._lock = asyncio.Lock()
        self._actor_condition = asyncio.Condition()
        self._actor_id: str | None = None
        self._actor_wait_seconds = actor_wait_seconds
        self._network_events: deque[dict[str, object]] = deque(maxlen=256)
        # Raw filled text is held only long enough to causally associate a browser submit
        # with the request it initiates. It is never serialized, hashed, or recorded.
        self._recent_fills: deque[tuple[str, str, str, str | None, int, datetime]] = deque(
            maxlen=32
        )
        self._active_submit: tuple[str, str, int, datetime] | None = None
        self._request_provenance: dict[int, dict[str, object]] = {}
        self._download_tasks: set[asyncio.Task[None]] = set()
        context.on("page", self._register_popup)
        context.on("response", self._record_response)

    async def request_json(
        self,
        method: str,
        url: str,
        payload: object,
        headers: Mapping[str, str] | None = None,
        *,
        timeout_ms: float = 120_000,
    ) -> tuple[int, object]:
        """Execute a request through the context client without exposing its cookies."""
        self._check_open()
        self._require_url(url)
        response = await self.context.request.fetch(
            url,
            method=method,
            data=payload,
            headers=dict(headers or {}),
            timeout=timeout_ms,
        )
        response_text = await response.text()
        try:
            response_body: object = json.loads(response_text)
        except json.JSONDecodeError:
            response_body = response_text
        return response.status, response_body

    async def new_tab(self, url: str = "about:blank") -> str:
        self._check_open()
        self._require_url(url, allow_blank=True)
        for tab_ref, page in tuple(self._tabs.items()):
            if not page.is_closed():
                continue
            self._tabs.pop(tab_ref, None)
            self._page_refs.pop(page, None)
            self._openers.pop(tab_ref, None)
            self._owned_pages.discard(page)
            self._generation.pop(tab_ref, None)
            self._elements = {
                ref: element
                for ref, element in self._elements.items()
                if not ref.startswith(f"{tab_ref}:")
            }
        if self.mode == "persistent":
            # A persistent target represents one authenticated interaction world. Reuse its
            # existing live tab across serialized workers instead of consuming tabs and losing
            # the one-chat state behind a succession of stale references.
            for tab_ref, page in reversed(tuple(self._tabs.items())):
                if page.is_closed():
                    continue
                if url != "about:blank" and page.url != url:
                    await page.goto(url)
                    await self._enforce_page(page)
                return tab_ref
        if len(self._tabs) >= self.bounds.max_tabs:
            raise BrowserBoundaryError("tab limit reached")
        page = await self.context.new_page()
        tab_ref = self._register(page, None, owned=True)
        if url != "about:blank":
            await page.goto(url)
            await self._enforce_page(page)
        return tab_ref

    async def act(self, action: BrowserAction, *, actor_id: str = "console") -> BrowserObservation:
        self._check_action(action)
        if self.mode == "persistent":
            await self._claim_actor(actor_id)
        async with self._lock:
            if action.kind == "open_tab":
                if action.value is None:
                    raise BrowserBoundaryError("open_tab requires a URL")
                tab_ref = await self.new_tab(action.value)
                return self._observation(
                    tab_ref,
                    "navigation",
                    text=action.value,
                    evidence=(self._evidence(action),),
                    step=action.step,
                )
            page = self._page(action.tab_ref)
            if action.kind == "navigate":
                if action.value is None:
                    raise BrowserBoundaryError("navigate requires a URL")
                self._require_url(action.value)
                await page.goto(action.value)
            elif action.kind in {
                "click",
                "fill",
                "press",
                "select",
                "check",
                "uncheck",
                "hover",
                "upload",
            }:
                locator = await self._resolve(action, page)
                if action.kind == "click":
                    self._blocked_download = False
                    self._download_signal.clear()
                    self._active_submit = (
                        self._evidence(action),
                        action.tab_ref,
                        action.step,
                        action.created_at,
                    )
                    try:
                        await locator.click()
                    finally:
                        self._active_submit = None
                    if not self.bounds.allow_downloads:
                        try:
                            await asyncio.wait_for(self._download_signal.wait(), timeout=0.1)
                        except TimeoutError:
                            pass
                    if self._blocked_download:
                        raise BrowserBoundaryError("downloads are outside browser bounds")
                elif action.kind == "fill":
                    if action.value is None:
                        raise BrowserBoundaryError("fill requires a value")
                    input_type = await locator.get_attribute("type")
                    if input_type in {"password", "file"}:
                        raise BrowserBoundaryError("credential and upload fields are unavailable")
                    await locator.fill(action.value)
                    probe_refs = tuple(
                        reference
                        for reference in action.evidence_refs
                        if reference.startswith("browser-input-probe:")
                    )
                    self._recent_fills.append(
                        (
                            self._evidence(action),
                            action.tab_ref,
                            action.value,
                            probe_refs[0] if len(probe_refs) == 1 else None,
                            action.step,
                            action.created_at,
                        )
                    )
                elif action.kind == "press":
                    if action.value is None:
                        raise BrowserBoundaryError("press requires a key")
                    self._active_submit = (
                        self._evidence(action),
                        action.tab_ref,
                        action.step,
                        action.created_at,
                    )
                    try:
                        await locator.press(action.value)
                    finally:
                        self._active_submit = None
                elif action.kind == "select":
                    if action.value is None:
                        raise BrowserBoundaryError("select requires an option value")
                    await locator.select_option(action.value)
                elif action.kind == "check":
                    await locator.check()
                elif action.kind == "uncheck":
                    await locator.uncheck()
                elif action.kind == "hover":
                    await locator.hover()
                else:
                    if not self.bounds.allow_uploads:
                        raise BrowserBoundaryError("uploads are outside browser bounds")
                    if action.value is None:
                        raise BrowserBoundaryError("upload requires a file path")
                    upload = await asyncio.to_thread(os.path.realpath, action.value)
                    allowed = set(
                        await asyncio.gather(
                            *(
                                asyncio.to_thread(os.path.realpath, value)
                                for value in self.bounds.allowed_upload_paths
                            )
                        )
                    )
                    if upload not in allowed:
                        raise BrowserBoundaryError("upload path is outside browser bounds")
                    await locator.set_input_files(upload)
            elif action.kind == "wait_for_text":
                if action.value is None:
                    raise BrowserBoundaryError("wait_for_text requires expected visible text")
                frame, _ = self._frame(page, action.frame_ref)
                try:
                    await frame.get_by_text(action.value, exact=False).first.wait_for(
                        state="visible", timeout=action.timeout_ms
                    )
                except PlaywrightTimeoutError as error:
                    raise BrowserBoundaryError(
                        f"expected visible text did not appear: {action.value}"
                    ) from error
            elif action.kind == "network_events":
                events = [
                    event
                    for event in self._network_events
                    if event["tab_ref"] in {action.tab_ref, "unknown"}
                ][-50:]
                return self._observation(
                    action.tab_ref,
                    "network",
                    text=json.dumps(events, sort_keys=True, separators=(",", ":")),
                    evidence=(self._evidence(action),),
                    step=action.step,
                )
            elif action.kind == "close_tab":
                await page.close()
                return self._observation(
                    action.tab_ref, "cleanup", text="tab closed", step=action.step
                )
            await self._enforce_all_pages()
            if action.kind == "snapshot":
                return await self.snapshot(action.tab_ref, action.frame_ref, step=action.step)
            return self._observation(
                action.tab_ref,
                "navigation" if action.kind == "navigate" else "snapshot",
                text=(
                    f"verified visible text: {action.value}"
                    if action.kind == "wait_for_text"
                    else page.url
                ),
                evidence=(self._evidence(action),),
                step=action.step,
            )

    async def release_actor(self, actor_id: str) -> None:
        async with self._actor_condition:
            if self._actor_id == actor_id:
                self._actor_id = None
                self._actor_condition.notify_all()

    async def _claim_actor(self, actor_id: str) -> None:
        async with self._actor_condition:
            if self._actor_id in {None, actor_id}:
                self._actor_id = actor_id
                return
            try:
                async with asyncio.timeout(self._actor_wait_seconds):
                    await self._actor_condition.wait_for(lambda: self._actor_id in {None, actor_id})
            except TimeoutError as error:
                raise BrowserBoundaryError(
                    "shared authenticated browser is busy with another campaign worker; "
                    "retry this browser step after that actor releases the one-chat session"
                ) from error
            self._actor_id = actor_id

    async def snapshot(
        self, tab_ref: str, frame_ref: str | None = None, *, step: int = 0
    ) -> BrowserObservation:
        page = self._page(tab_ref)
        await self._enforce_page(page)
        frame, resolved_frame_ref = self._frame(page, frame_ref)
        text = await frame.locator("body").inner_text(timeout=2_000)
        lowered = text.lower()
        if "captcha" in lowered or "verify you are human" in lowered:
            return self._observation(
                tab_ref,
                "blocker",
                frame_ref=resolved_frame_ref,
                text="manual CAPTCHA blocker",
                step=step,
            )
        generation = self._generation.get(tab_ref, 0) + 1
        self._generation[tab_ref] = generation
        elements: list[ElementRef] = []
        for role in ("button", "link", "textbox", "checkbox", "radio", "combobox"):
            locators = frame.get_by_role(role)
            for index in range(min(await locators.count(), 50)):
                locator = locators.nth(index)
                name = (
                    (await locator.get_attribute("aria-label"))
                    or (await locator.get_attribute("placeholder"))
                    or (await locator.get_attribute("name"))
                    or (await locator.inner_text())
                    or f"{role.title()} {index + 1}"
                )
                name = " ".join(name.split())[:200]
                input_type = await locator.get_attribute("type")
                raw_max_length = await locator.get_attribute("maxlength")
                max_length = (
                    int(raw_max_length)
                    if raw_max_length is not None and raw_max_length.isdigit()
                    else None
                )
                ref = f"{tab_ref}:{generation}:{len(elements)}"
                self._elements[ref] = _Element(generation, resolved_frame_ref, role, name, locator)
                elements.append(
                    ElementRef(
                        ref=ref,
                        tab_ref=tab_ref,
                        frame_ref=resolved_frame_ref,
                        role=role,
                        name=name,
                        input_type=input_type,
                        max_length=max_length,
                        disabled=await locator.is_disabled(),
                        read_only=(await locator.get_attribute("readonly")) is not None,
                    )
                )
        uploads = frame.locator('input[type="file"]')
        for index in range(min(await uploads.count(), 20)):
            locator = uploads.nth(index)
            name = (await locator.get_attribute("aria-label")) or f"File upload {index + 1}"
            ref = f"{tab_ref}:{generation}:{len(elements)}"
            self._elements[ref] = _Element(generation, resolved_frame_ref, "file", name, locator)
            elements.append(
                ElementRef(
                    ref=ref,
                    tab_ref=tab_ref,
                    frame_ref=resolved_frame_ref,
                    role="file",
                    name=name,
                )
            )
        # Visible target text may legitimately discuss tokens (especially in security tests).
        # Do not erase the entire observation merely because that word appears. A concrete
        # password control is the boundary that requires fail-closed snapshot redaction.
        redacted = await frame.locator('input[type="password"]').count() > 0
        safe_text = (
            "[redacted sensitive form content]" if redacted else _bounded_snapshot_text(text)
        )
        return self._observation(
            tab_ref,
            "snapshot",
            frame_ref=resolved_frame_ref,
            text=safe_text,
            elements=tuple(elements),
            redacted=redacted,
            step=step,
        )

    async def fill_requires_redaction(self, tab_ref: str, element_ref: str | None) -> bool:
        """Fail closed unless the fill target is positively a non-sensitive control.

        Mirrors the snapshot redaction boundary: a concrete password control (or an
        autocomplete-marked credential field) is redacted; anything that cannot be
        positively identified keeps the strict redaction behavior.
        """
        page = self._tabs.get(tab_ref)
        if page is None or page.is_closed():
            return True
        element = self._elements.get(element_ref or "")
        if element is None:
            return True
        try:
            input_type = await element.locator.get_attribute("type")
            autocomplete = await element.locator.get_attribute("autocomplete")
        except PlaywrightError:
            return True
        if (input_type or "").strip().casefold() == "password":
            return True
        return (autocomplete or "").strip().casefold() in {"current-password", "new-password"}

    async def verify_input_value(
        self,
        tab_ref: str,
        element_ref: str,
        expected: str,
        frame_ref: str | None = None,
    ) -> None:
        """Fail closed unless a reviewed input contains the exact value to submit."""
        page = self._page(tab_ref)
        action = BrowserAction(
            context_id=self.context_id,
            target_id=self.bounds.target_id,
            owner=self.owner,
            kind="fill",
            tab_ref=tab_ref,
            frame_ref=frame_ref,
            element_ref=element_ref,
            value=expected,
            step=0,
        )
        locator = await self._resolve(action, page)
        actual = await locator.input_value()
        if actual != expected:
            raise BrowserBoundaryError("reviewed browser input value changed before submit")

    async def text_occurrences(self, tab_ref: str, value: str, frame_ref: str | None = None) -> int:
        """Count an exact reviewed witness in the bounded target frame."""
        page = self._page(tab_ref)
        await self._enforce_page(page)
        frame, _ = self._frame(page, frame_ref)
        return (await frame.locator("body").inner_text(timeout=2_000)).count(value)

    async def wait_for_text_increase(
        self,
        tab_ref: str,
        value: str,
        previous_count: int,
        frame_ref: str | None = None,
        *,
        timeout_ms: int,
    ) -> int:
        """Wait until submission produces a new occurrence of an exact witness."""
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1_000
        while True:
            count = await self.text_occurrences(tab_ref, value, frame_ref)
            if count > previous_count:
                return count
            if asyncio.get_running_loop().time() >= deadline:
                raise BrowserBoundaryError(
                    "reviewed response witness did not appear after submission"
                )
            await asyncio.sleep(0.1)

    async def capture_failure_diagnostics(
        self, tab_ref: str, frame_ref: str | None = None, *, step: int = 0
    ) -> tuple[BrowserObservation, BrowserObservation, dict[str, object]]:
        """Capture bounded visual, DOM, and redacted network state after a failed action."""
        page = self._page(tab_ref)
        snapshot = await self.snapshot(tab_ref, frame_ref, step=step)
        events = [
            event for event in self._network_events if event["tab_ref"] in {tab_ref, "unknown"}
        ][-50:]
        network = self._observation(
            tab_ref,
            "network",
            text=json.dumps(events, sort_keys=True, separators=(",", ":")),
            step=step,
        )
        directory = Path.home() / ".merlin" / "browser-diagnostics" / self.context_id
        await asyncio.to_thread(directory.mkdir, mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / f"{uuid4().hex}.jpg"
        await page.screenshot(
            path=str(path),
            type="jpeg",
            quality=60,
            full_page=False,
            animations="disabled",
            timeout=5_000,
        )
        os.chmod(path, 0o600)
        payload = await asyncio.to_thread(path.read_bytes)
        digest = hashlib.sha256(payload).hexdigest()
        screenshot = {
            "evidence_ref": f"sha256:{digest}",
            "sha256": digest,
            "media_type": "image/jpeg",
            "byte_length": len(payload),
            "path": str(path),
        }
        snapshot = snapshot.model_copy(
            update={"evidence_refs": (*snapshot.evidence_refs, screenshot["evidence_ref"])}
        )
        return snapshot, network, screenshot

    def metadata(self, *, restart: bool = False) -> BrowserMetadata:
        now = datetime.now(UTC)
        tabs = tuple(
            TabMetadata(
                tab_ref=tab_ref,
                url=page.url,
                opener_ref=self._openers[tab_ref],
                state="closed" if page.is_closed() else "open",
            )
            for tab_ref, page in sorted(self._tabs.items())
        )
        state = "cleaned" if self._closed else "restart_required" if restart else "active"
        return BrowserMetadata(
            context_id=self.context_id,
            mode=self.mode,
            target_id=self.bounds.target_id,
            owner=self.owner,
            allowed_origins=self.bounds.allowed_origins,
            admitted_frame_origins=tuple(sorted(self._admitted_frame_origins)),
            tabs=tabs,
            state=state,
            active_actor_id=self._actor_id,
            launch_mode=self.launch_mode,
            launch_fallback_reason=self.launch_fallback_reason,
            created_at=self._created_at,
            updated_at=now,
        )

    def frame_refs(self, tab_ref: str) -> tuple[str, ...]:
        page = self._page(tab_ref)
        return tuple(self._register_frame(frame) for frame in page.frames)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []

        async def finish(awaitable: Awaitable[object]) -> None:
            try:
                await awaitable
            except PlaywrightError as error:
                # Process shutdown can close Playwright's transport before the
                # resource closer reaches this session.  The remote browser is
                # already gone in that case, so continue releasing the remaining
                # local handles instead of turning normal daemon exit into an
                # ExceptionGroup.
                if not _playwright_transport_is_closed(error):
                    errors.append(error)
            except Exception as error:  # preserve unexpected cleanup failures
                errors.append(error)

        if self._route_installed:
            try:
                await finish(self.context.unroute("**/*", self._route_request))
            finally:
                self._route_installed = False
        if self._download_tasks:
            await asyncio.gather(*tuple(self._download_tasks), return_exceptions=True)
        if self.mode in {"isolated", "persistent"}:
            await finish(self.context.close())
            if self._browser is not None:
                await finish(self._browser.close())
            if self._playwright is not None:
                await finish(self._playwright.stop())
        else:
            for page in tuple(self._owned_pages):
                if not page.is_closed():
                    await finish(page.close())
        if errors:
            raise ExceptionGroup("browser session cleanup failed", errors)

    async def __aenter__(self) -> BrowserSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        await asyncio.shield(self.close())

    def _register_popup(self, page: Page) -> None:
        opener = page.opener
        opener_ref = self._page_refs.get(opener) if opener is not None else None
        tab_ref, self._recovery_tab_ref = self._recovery_tab_ref, None
        self._register(page, opener_ref, owned=True, tab_ref=tab_ref)

    def _register(
        self,
        page: Page,
        opener_ref: str | None,
        *,
        owned: bool,
        tab_ref: str | None = None,
    ) -> str:
        if page in self._page_refs:
            return self._page_refs[page]
        tab_ref = tab_ref or "tab:" + uuid4().hex
        if tab_ref in self._tabs:
            raise BrowserBoundaryError("duplicate tab reference during recovery")
        self._tabs[tab_ref], self._page_refs[page] = page, tab_ref
        self._openers[tab_ref] = opener_ref
        if owned:
            self._owned_pages.add(page)
        page.on("download", self._download)
        return tab_ref

    def _download(self, download: object) -> None:
        if self.bounds.allow_downloads:
            return
        # Playwright schedules coroutine event handlers after the click has returned. Mark the
        # denied effect synchronously so BrowserSession.act cannot report a successful click.
        self._blocked_download = True
        self._download_signal.set()
        task = asyncio.create_task(self._cancel_download(download))
        self._download_tasks.add(task)
        task.add_done_callback(self._download_tasks.discard)

    @staticmethod
    async def _cancel_download(download: object) -> None:
        try:
            await download.cancel()
        except Exception:
            # The browser may have already discarded a denied download. The deterministic denial
            # above remains authoritative; callback cleanup must not leak an event-loop exception.
            return

    async def _record_response(self, response: Response) -> None:
        if self.owner.worker_id == "operator":
            return  # Manual sign-in traffic is not reconnaissance evidence.
        parsed = urlsplit(response.url)
        event: dict[str, object] = {
            # A navigation response can arrive before Playwright has created its Frame.
            # Keep it target-context scoped instead of dereferencing an unavailable frame.
            "tab_ref": "unknown",
            "method": response.request.method,
            "resource_type": response.request.resource_type,
            "status": response.status,
            "observed_at": datetime.now(UTC).isoformat(),
            # Query strings and fragments may contain credentials or target content.
            "url": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
        }
        event.update(self._request_provenance.pop(id(response.request), {}))
        try:
            raw_body = response.request.post_data_buffer
        except (PlaywrightError, UnicodeDecodeError):
            raw_body = None
        request_body = _bounded_json_body(raw_body)
        if request_body is not None:
            event["request_json"] = request_body
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type:
            try:
                body = await response.body()
            except PlaywrightError:
                body = b""
            response_body = _bounded_json_body(body)
            if response_body is not None:
                event["response_json"] = response_body
        self._network_events.append(event)

    def api_exchange_events(self) -> tuple[dict[str, object], ...]:
        """Return bounded, redacted JSON request/response events for adapter compilation."""
        return tuple(dict(item) for item in self._network_events if "request_json" in item)

    async def _install_bounds(self) -> None:
        await self.context.route("**/*", self._route_request)
        self._route_installed = True

    async def _route_request(self, route: Route, request: Request) -> None:
        if self.owner.worker_id == "operator" and urlsplit(request.url).scheme in {"http", "https"}:
            # Takeover is manual browser control, including external OAuth.
            # Tool acquisition remains blocked until explicit hand-back.
            await route.continue_()
            return
        denied = False
        try:
            self._require_url(request.url, allow_blank=True)
        except OriginDenied:
            denied = not self._admit_embedded_request(request)
        if denied:
            await route.abort("blockedbyclient")
        else:
            submit = self._active_submit
            try:
                raw_body = request.post_data_buffer
            except PlaywrightError:
                raw_body = None
            request_json = _bounded_json_body(raw_body)
            if submit is not None and isinstance(request_json, (dict, list)):
                submit_ref, submit_tab, submit_step, submit_at = submit
                input_refs: dict[str, str] = {}
                input_probe_refs: dict[str, str] = {}
                paths = _json_string_paths(request_json)
                for (
                    fill_ref,
                    fill_tab,
                    fill_value,
                    probe_ref,
                    fill_step,
                    fill_at,
                ) in reversed(self._recent_fills):
                    if probe_ref is None:
                        continue
                    matching_pointers = tuple(
                        pointer for pointer, value in paths if value == fill_value
                    )
                    if len(matching_pointers) > 1 and set(matching_pointers) != {
                        "/message",
                        "/userMessage",
                    }:
                        continue
                    if (
                        matching_pointers
                        and fill_tab == submit_tab
                        and (fill_step, fill_at) < (submit_step, submit_at)
                        and (submit_at - fill_at).total_seconds() <= 600
                    ):
                        for pointer in matching_pointers:
                            input_refs[pointer] = fill_ref
                            input_probe_refs[pointer] = probe_ref
                        break
                if input_refs:
                    if len(self._request_provenance) >= 256:
                        self._request_provenance.pop(next(iter(self._request_provenance)))
                    self._request_provenance[id(request)] = {
                        "tab_ref": submit_tab,
                        "trigger_action_ref": submit_ref,
                        "trigger_action_step": submit_step,
                        "input_action_refs": input_refs,
                        "input_probe_refs": input_probe_refs,
                    }
            await route.continue_()

    async def _resolve(self, action: BrowserAction, page: Page) -> Locator:
        if action.element_ref is None:
            raise BrowserBoundaryError(f"{action.kind} requires an element reference")
        element = self._elements.get(action.element_ref)
        if element is None or element.frame_ref != (action.frame_ref or "main"):
            raise StaleReference("element reference is unknown or belongs to another frame")
        if element.generation == self._generation.get(action.tab_ref):
            return element.locator
        frame, _ = self._frame(page, element.frame_ref)
        recovered = (
            frame.locator('input[type="file"]')
            if element.role == "file"
            else frame.get_by_role(element.role, name=element.name, exact=True)
        )
        if await recovered.count() != 1:
            raise StaleReference("element reference could not be recovered uniquely")
        return recovered

    def _frame(self, page: Page, frame_ref: str | None) -> tuple[Frame, str]:
        if frame_ref in (None, "main"):
            return page.main_frame, "main"
        for frame in page.frames:
            candidate = self._register_frame(frame)
            if candidate == frame_ref:
                self._require_frame_url(frame)
                return frame, candidate
        raise StaleReference("frame reference is stale")

    def _require_frame_url(self, frame: Frame) -> None:
        url = frame.url
        if url.startswith("about:"):
            # about:blank/srcdoc frames inherit the embedder's origin and contain
            # only parent-controlled DOM; the parent page was already checked.
            return
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self.bounds.allowed_origins or origin in self._admitted_frame_origins:
            return
        # Embedded chat widgets (LiveChat, Intercom, ...) load from a vendor origin
        # distinct from the target page, sometimes nested under about:blank wrappers.
        # Admit a frame origin only when its nearest non-about ancestor is inside
        # the reviewed bounds; page-level navigation stays restricted to
        # allowed_origins.
        ancestor = frame.parent_frame
        while ancestor is not None and ancestor.url.startswith("about:"):
            ancestor = ancestor.parent_frame
        if ancestor is not None:
            parsed_ancestor = urlsplit(ancestor.url)
            ancestor_origin = f"{parsed_ancestor.scheme}://{parsed_ancestor.netloc}"
            if (
                ancestor_origin in self.bounds.allowed_origins
                or ancestor_origin in self._admitted_frame_origins
            ):
                self._admitted_frame_origins.add(origin)
                return
        raise OriginDenied(f"origin is outside target bounds: {origin}")

    def _register_frame(self, frame: Frame) -> str:
        if frame == frame.page.main_frame:
            return "main"
        return self._frame_refs.setdefault(frame, "frame:" + uuid4().hex)

    async def _enforce_all_pages(self) -> None:
        for page in tuple(self._page_refs):
            if page.is_closed():
                continue
            try:
                await self._enforce_page(page)
            except OriginDenied:
                await page.close()
                raise

    async def _enforce_page(self, page: Page) -> None:
        self._require_url(page.url, allow_blank=True)

    def _require_url(self, url: str, *, allow_blank: bool = False) -> None:
        if allow_blank and url == "about:blank":
            return
        if url.startswith("chrome-error://"):
            return  # browser-internal error page after a blocked navigation; inert
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.bounds.allowed_origins:
            raise OriginDenied(f"origin is outside target bounds: {origin}")

    def _admit_embedded_request(self, request: Request) -> bool:
        """Allow a request that a real visitor's browser would make from this page.

        Recon must see the page as a user sees it: third-party subresources
        (CDN scripts, widget bootstraps) and embedded iframe documents
        (LiveChat/Intercom widgets) load from vendor origins outside
        allowed_origins. Admit any request whose frame-ancestor chain reaches an
        in-bounds frame, except top-level navigation (document request with no
        parent frame), which stays restricted to allowed_origins. Admitted
        origins are session-local and recorded on the session for audit.
        """
        try:
            frame = request.frame
        except PlaywrightError:
            return False
        if frame is None:
            return False
        if request.resource_type == "document" and frame.parent_frame is None:
            return False  # top-level navigation stays restricted to allowed_origins
        candidate: Frame | None = frame
        while candidate is not None:
            url = candidate.url
            if url.startswith("about:"):
                candidate = candidate.parent_frame
                continue
            origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
            if origin in self.bounds.allowed_origins or origin in self._admitted_frame_origins:
                parsed = urlsplit(request.url)
                self._admitted_frame_origins.add(f"{parsed.scheme}://{parsed.netloc}")
                return True
            candidate = candidate.parent_frame
        return False

    def _check_action(self, action: BrowserAction) -> None:
        self._check_open()
        if action.context_id != self.context_id or action.target_id != self.bounds.target_id:
            raise BrowserBoundaryError("action context or target does not match")
        if action.owner != self.owner:
            raise BrowserBoundaryError("action owner does not match")

    def _check_open(self) -> None:
        if self._closed:
            raise BrowserBoundaryError("browser context is closed")

    def _page(self, tab_ref: str) -> Page:
        page = self._tabs.get(tab_ref)
        if page is not None and not page.is_closed():
            return page
        if self.mode != "persistent":
            raise StaleReference("tab reference is stale")

        # Chromium can assign a fresh runtime page to a restored persistent profile while a
        # resumed worker still holds the durable tab reference recorded before restart. Recover
        # only when the mapping is unambiguous and remains inside the reviewed target bounds.
        live_pages: list[tuple[str, Page]] = []
        for candidate_ref, candidate in self._tabs.items():
            if candidate.is_closed():
                continue
            try:
                self._require_url(candidate.url, allow_blank=True)
            except OriginDenied:
                continue
            live_pages.append((candidate_ref, candidate))
        if len(live_pages) != 1:
            raise StaleReference("tab reference is stale")

        current_ref, recovered = live_pages[0]
        if current_ref != tab_ref:
            self._tabs.pop(current_ref, None)
            self._tabs[tab_ref] = recovered
            self._page_refs[recovered] = tab_ref
            self._openers[tab_ref] = self._openers.pop(current_ref, None)
            if current_ref in self._generation:
                self._generation[tab_ref] = self._generation.pop(current_ref)
            self._elements = {
                ref: element
                for ref, element in self._elements.items()
                if not ref.startswith(f"{current_ref}:")
            }
            for event in self._network_events:
                if event.get("tab_ref") == current_ref:
                    event["tab_ref"] = tab_ref
        return recovered

    def _observation(
        self,
        tab_ref: str,
        kind: str,
        *,
        frame_ref: str | None = None,
        text: str = "",
        elements: tuple[ElementRef, ...] = (),
        evidence: tuple[str, ...] = (),
        redacted: bool = False,
        step: int = 0,
    ) -> BrowserObservation:
        page = self._tabs.get(tab_ref)
        origin = ""
        if page is not None:
            parsed = urlsplit(page.url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
        return BrowserObservation(
            context_id=self.context_id,
            target_id=self.bounds.target_id,
            origin=origin,
            owner=self.owner,
            tab_ref=tab_ref,
            frame_ref=frame_ref,
            kind=kind,
            step=step,
            text=text,
            elements=elements,
            evidence_refs=evidence,
            redaction_status="redacted" if redacted else "not_needed",
        )

    @staticmethod
    def _evidence(action: BrowserAction) -> str:
        stable = tuple(
            reference
            for reference in action.evidence_refs
            if reference.startswith("browser-action:sha256:")
        )
        if len(stable) == 1:
            return stable[0]
        data = action.model_dump_json().encode()
        return "browser-action:sha256:" + hashlib.sha256(data).hexdigest()


def _bounded_snapshot_text(text: str, *, limit: int = 8_000) -> str:
    if len(text) <= limit:
        return text
    digest = hashlib.sha256(text.encode()).hexdigest()
    marker = f"\n...[snapshot truncated; original_chars={len(text)}; sha256:{digest}]...\n"
    retained = limit - len(marker)
    head = retained // 2
    return text[:head] + marker + text[-(retained - head) :]


async def attach_context(
    context: BrowserContext, bounds: BrowserBounds, owner: BrowserOwner
) -> BrowserSession:
    session = BrowserSession(
        context,
        bounds,
        owner,
        mode="attached",
        launch_mode="attached",
    )
    await session._install_bounds()
    try:
        for page in context.pages:
            session._register(page, None, owned=False)
            await session._enforce_page(page)
    except BaseException:
        await session.close()
        raise
    return session


async def launch_isolated(
    bounds: BrowserBounds, owner: BrowserOwner, *, headless: bool = True
) -> BrowserSession:
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context(accept_downloads=bounds.allow_downloads)
    except BaseException:
        await playwright.stop()
        raise
    session = BrowserSession(
        context,
        bounds,
        owner,
        mode="isolated",
        browser=browser,
        playwright=playwright,
        launch_mode="headless" if headless else "headful",
    )
    try:
        await session._install_bounds()
    except BaseException:
        await session.close()
        raise
    return session


async def launch_persistent(
    profile_directory: Path,
    bounds: BrowserBounds,
    owner: BrowserOwner,
    *,
    headless: bool = False,
    actor_wait_seconds: float = 30.0,
) -> BrowserSession:
    """Launch one shared authenticated profile; callers serialize through its actor lease."""
    playwright = await async_playwright().start()
    await asyncio.to_thread(profile_directory.mkdir, mode=0o700, parents=True, exist_ok=True)
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_directory),
            headless=headless,
            accept_downloads=bounds.allow_downloads,
        )
    except BaseException:
        await playwright.stop()
        raise
    session = BrowserSession(
        context,
        bounds,
        owner,
        mode="persistent",
        playwright=playwright,
        actor_wait_seconds=actor_wait_seconds,
        launch_mode="headless" if headless else "headful",
    )
    try:
        await session._install_bounds()
        for page in context.pages:
            session._register(page, None, owned=False)
            await session._enforce_page(page)
    except BaseException:
        await session.close()
        raise
    return session


async def restart_isolated(
    metadata: BrowserMetadata,
    bounds: BrowserBounds,
    owner: BrowserOwner,
    *,
    headless: bool = True,
) -> BrowserSession:
    if metadata.mode != "isolated":
        raise BrowserBoundaryError("attached contexts require explicit reattachment")
    if (
        metadata.target_id != bounds.target_id
        or metadata.owner != owner
        or metadata.allowed_origins != bounds.allowed_origins
    ):
        raise BrowserBoundaryError("recovery metadata does not match current bounds")
    session = await launch_isolated(bounds, owner, headless=headless)
    session.context_id = metadata.context_id
    session._created_at = metadata.created_at
    try:
        await _restore_tabs(session, metadata)
    except BaseException:
        await session.close()
        raise
    return session


async def restart_persistent(
    metadata: BrowserMetadata,
    bounds: BrowserBounds,
    owner: BrowserOwner,
    profile_directory: Path,
    *,
    headless: bool = False,
    actor_wait_seconds: float = 30.0,
) -> BrowserSession:
    if metadata.mode != "persistent":
        raise BrowserBoundaryError("only persistent contexts can use persistent recovery")
    if (
        metadata.target_id != bounds.target_id
        or metadata.owner != owner
        or metadata.allowed_origins != bounds.allowed_origins
    ):
        raise BrowserBoundaryError("recovery metadata does not match current bounds")
    session = await launch_persistent(
        profile_directory,
        bounds,
        owner,
        headless=headless,
        actor_wait_seconds=actor_wait_seconds,
    )
    session.context_id = metadata.context_id
    session._created_at = metadata.created_at
    try:
        # Chromium may restore profile tabs with transient references. Recreate only the reviewed
        # durable tab projection so recovered workers keep exact tab identities and bounds.
        for page in tuple(session.context.pages):
            await page.close()
        session._tabs.clear()
        session._page_refs.clear()
        session._openers.clear()
        session._owned_pages.clear()
        await _restore_tabs(session, metadata)
    except BaseException:
        await session.close()
        raise
    return session


async def _restore_tabs(session: BrowserSession, metadata: BrowserMetadata) -> None:
    for tab in metadata.tabs:
        if tab.state != "open":
            continue
        session._require_url(tab.url, allow_blank=True)
        session._recovery_tab_ref = tab.tab_ref
        page = await session.context.new_page()
        session._register(
            page,
            tab.opener_ref,
            owned=True,
            tab_ref=tab.tab_ref,
        )
        session._openers[tab.tab_ref] = tab.opener_ref
        if tab.url != "about:blank":
            await page.goto(tab.url)
            await session._enforce_page(page)
