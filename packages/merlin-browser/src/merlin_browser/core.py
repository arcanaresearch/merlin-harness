from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
from collections import deque
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
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
_VISUAL_CANDIDATE_LIMIT = 4
_VISUAL_INSPECTION_LIMIT = 100
_VISUAL_CAPTURE_ATTEMPT_LIMIT = 12
_VISUAL_CAPTURE_TIMEOUT_MS = 1_500
_VISUAL_MIN_AXIS_PX = 96
_VISUAL_MIN_AREA_PX = 24_000
_VISUAL_MAX_BYTES = 5_242_880
_VISUAL_VIEWPORT_MAX_WIDTH_PX = 2_048
_VISUAL_VIEWPORT_MAX_HEIGHT_PX = 1_440
_VISUAL_CONTEXT_MAX_FILES = 16
_VISUAL_CONTEXT_MAX_BYTES = 20 * 1_024 * 1_024
_VISUAL_CONTEXT_MAX_IDENTITIES = 32
_VISUAL_ROOT_MAX_FILES = _VISUAL_CONTEXT_MAX_FILES * 4
_VISUAL_ROOT_MAX_BYTES = _VISUAL_CONTEXT_MAX_BYTES * 4
_VISUAL_ROOT_MAX_IDENTITIES = _VISUAL_CONTEXT_MAX_IDENTITIES * 4
_VISUAL_ROOT_SCAN_MAX_CONTEXTS = 256
_VISUAL_ROOT_SCAN_MAX_ENTRIES = 512
_SENSITIVE_FRAME_INSPECTION_LIMIT = 64
_SENSITIVE_CONTROL_INSPECTION_LIMIT = 256
_SENSITIVE_CONTROL_ATTRIBUTE_MAX_CHARS = 256
_SENSITIVE_AUTOCOMPLETE_TOKENS = (
    "current-password",
    "new-password",
    "one-time-code",
    "cc-name",
    "cc-given-name",
    "cc-additional-name",
    "cc-family-name",
    "cc-number",
    "cc-exp",
    "cc-exp-month",
    "cc-exp-year",
    "cc-csc",
    "cc-type",
    "transaction-currency",
    "transaction-amount",
)
_SENSITIVE_CONTROL_SINGLE_TOKENS = (
    "pin",
    "otp",
    "totp",
    "mfa",
    "passcode",
    "password",
    "credential",
    "secret",
    "token",
    "apikey",
    "cvv",
    "cvc",
    "csc",
    "pan",
    "card",
    "payment",
    "iban",
    "routing",
)
_SENSITIVE_CONTROL_COMPACT_PATTERNS = (
    "apikey",
    "apitoken",
    "accesstoken",
    "refreshtoken",
    "authtoken",
    "clientsecret",
    "privatekey",
    "secretkey",
    "onetimecode",
    "verificationcode",
    "securitycode",
    "cardnumber",
    "creditcard",
    "debitcard",
    "cardexpiry",
    "cardexpiration",
    "expirationdate",
    "routingnumber",
    "bankaccount",
    "accountnumber",
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _VisualRootRetention:
    storage_root: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    retained_files: dict[str, int] = field(default_factory=dict)
    identities: set[str] = field(default_factory=set)
    retained_bytes: int = 0
    initialized: bool = False
    exhausted: bool = False
    exhaustion_reason: str | None = None


# Merlin's process-lifetime daemon lock guarantees one writer process. This
# registry serializes every BrowserSession inside that daemon. A filesystem scan
# restores retained totals after a daemon restart.
_VISUAL_ROOT_RETENTION_STATES: dict[str, _VisualRootRetention] = {}

_ELEMENT_SURFACE_METADATA_SCRIPT = """
    (elements, limit) => {
        const composedParent = (node) => {
            if (node.assignedSlot) return node.assignedSlot;
            if (node.parentElement) return node.parentElement;
            const root = node.getRootNode ? node.getRootNode() : null;
            return root && root.host ? root.host : null;
        };
        const ownsRetargetedHit = (element, hit) => {
            if (!hit) return false;
            if (hit === element || element.contains(hit)) return true;
            // document.elementFromPoint retargets an open-shadow descendant to
            // its host. Only exact hosts in the element's composed ancestry are
            // accepted; unrelated light-DOM descendants of a host remain blockers.
            let root = element.getRootNode ? element.getRootNode() : null;
            while (root && root.host) {
                if (hit === root.host) return true;
                root = root.host.getRootNode ? root.host.getRootNode() : null;
            }
            return false;
        };
        return elements.slice(0, limit).map((element) => {
            let scope = "document";
            const style = element.ownerDocument.defaultView.getComputedStyle(element);
            const rendered = element.getClientRects().length > 0
                && style.display !== "none" && style.visibility !== "hidden";
            // Use the nearest meaningful composed ancestor. A transcript nested
            // in a persistent dialog stays transcript-like, while an inner modal
            // or popover still wins because it is encountered first.
            for (let node = rendered ? element : null; node; node = composedParent(node)) {
                const role = (node.getAttribute("role") || "").toLowerCase();
                const tag = node.tagName.toLowerCase();
                const ariaModal = node.getAttribute("aria-modal") === "true";
                let cssModal = false;
                let openPopover = false;
                try {
                    cssModal = node.matches(":modal");
                    openPopover = node.matches(":popover-open");
                } catch (_) {
                    // Older browser engines can omit these structural selectors.
                }
                if (cssModal || ariaModal) {
                    scope = "modal";
                    break;
                }
                if (role === "dialog" || role === "alertdialog" || tag === "dialog") {
                    scope = "dialog";
                    break;
                }
                if (openPopover) {
                    scope = "top_layer";
                    break;
                }
                if (["log", "feed"].includes(role)) {
                    scope = "transcript";
                    break;
                }
                if (["status", "alert"].includes(role)) {
                    scope = "status";
                    break;
                }
                if (["polite", "assertive"].includes(node.getAttribute("aria-live"))) {
                    // A generic live region is common response chrome. It is a
                    // neutral boundary: do not promote its actions merely
                    // because an outer application shell is a dialog.
                    scope = "document";
                    break;
                }
            }
            const rect = element.getBoundingClientRect();
            const left = Math.max(rect.left, 0);
            const right = Math.min(rect.right, window.innerWidth);
            const top = Math.max(rect.top, 0);
            const bottom = Math.min(rect.bottom, window.innerHeight);
            const visible = rendered && right > left && bottom > top;
            const hit = visible
                ? element.ownerDocument.elementFromPoint((left + right) / 2, (top + bottom) / 2)
                : null;
            return {scope, occluded: visible && !ownsRetargetedHit(element, hit)};
        });
    }
"""

_FRAME_SURFACE_OCCLUSION_SCRIPT = """
    (element) => {
        const rect = element.getBoundingClientRect();
        const left = Math.max(rect.left, 0);
        const right = Math.min(rect.right, window.innerWidth);
        const top = Math.max(rect.top, 0);
        const bottom = Math.min(rect.bottom, window.innerHeight);
        const visible = element.getClientRects().length > 0 && right > left && bottom > top;
        if (!visible) return false;
        const hit = element.ownerDocument.elementFromPoint(
            (left + right) / 2,
            (top + bottom) / 2,
        );
        if (!hit) return true;
        if (hit === element || element.contains(hit)) return false;
        let root = element.getRootNode ? element.getRootNode() : null;
        while (root && root.host) {
            if (hit === root.host) return false;
            root = root.host.getRootNode ? root.host.getRootNode() : null;
        }
        return true;
    }
"""


async def _element_surface_metadata(
    locators: Locator,
    count: int,
) -> tuple[tuple[str, bool], ...]:
    """Classify bounded structure and hit-test state in one browser round trip."""

    try:
        raw = await locators.evaluate_all(_ELEMENT_SURFACE_METADATA_SCRIPT, count)
    except PlaywrightError:
        return tuple(("document", False) for _ in range(count))
    if not isinstance(raw, list) or len(raw) != count:
        return tuple(("document", False) for _ in range(count))
    allowed = {"document", "transcript", "status", "dialog", "modal", "top_layer"}
    return tuple(
        (
            str(item.get("scope")) if item.get("scope") in allowed else "document",
            item.get("occluded") is True,
        )
        if isinstance(item, Mapping)
        else ("document", False)
        for item in raw
    )


async def _frame_surface_occluded(frame: Frame) -> bool:
    """Detect a parent-document overlay that blocks an embedded target frame."""

    current = frame
    while current.parent_frame is not None:
        try:
            frame_element = await current.frame_element()
            try:
                if await frame_element.evaluate(_FRAME_SURFACE_OCCLUSION_SCRIPT) is True:
                    return True
            finally:
                await frame_element.dispose()
        except PlaywrightError:
            return False
        current = current.parent_frame
    return False


def _sanitized_visual_source_url(value: str) -> str:
    """Keep visual attribution without persisting URL credentials or inline image bytes."""
    parsed = urlsplit(value)
    scheme = parsed.scheme.casefold()

    def sanitized_network_url(parts: object) -> str:
        hostname = str(getattr(parts, "hostname", "") or "")
        if ":" in hostname:
            hostname = f"[{hostname}]"
        try:
            port = getattr(parts, "port", None)
        except ValueError:
            port = None
        netloc = f"{hostname}:{port}" if port is not None else hostname
        return urlunsplit(
            (
                str(getattr(parts, "scheme", "")),
                netloc,
                str(getattr(parts, "path", "")),
                "",
                "",
            )
        )

    if scheme in {"http", "https"}:
        return sanitized_network_url(parsed)[:2_048]
    if scheme == "blob":
        inner = urlsplit(value.removeprefix(f"{parsed.scheme}:"))
        if inner.scheme.casefold() in {"http", "https"}:
            return ("blob:" + sanitized_network_url(inner))[:2_048]
        return "blob:sha256:" + hashlib.sha256(value.encode()).hexdigest()
    if parsed.scheme.casefold() == "data":
        return "data:sha256:" + hashlib.sha256(value.encode()).hexdigest()
    return value.split("?", 1)[0].split("#", 1)[0][:2_048]


def _useful_visual_label(value: object) -> bool:
    label = " ".join(str(value or "").split()).casefold()
    return bool(label) and label not in {
        "background",
        "decoration",
        "decorative",
        "hero",
        "image",
    }


def _bounded_visual_metadata(value: object) -> str:
    return " ".join(str(value or "").split())[:512]


def _has_negative_pixel_offset(value: object) -> bool:
    for token in str(value or "").replace(",", " ").split():
        if not token.casefold().endswith("px"):
            continue
        try:
            if float(token[:-2]) < 0:
                return True
        except ValueError:
            continue
    return False


def _visual_candidate_admitted(candidate: Mapping[str, object]) -> bool:
    if not candidate.get("visible") or not candidate.get("loaded"):
        return False
    visible_width = float(candidate.get("visible_width") or 0)
    visible_height = float(candidate.get("visible_height") or 0)
    visible_area = float(candidate.get("visible_area") or 0)
    if (
        visible_width < _VISUAL_MIN_AXIS_PX
        or visible_height < _VISUAL_MIN_AXIS_PX
        or visible_area < _VISUAL_MIN_AREA_PX
    ):
        return False
    role = str(candidate.get("role", "")).casefold()
    tag = str(candidate.get("tag", "")).casefold()
    useful_label = any(
        _useful_visual_label(candidate.get(field))
        for field in ("alt", "aria_label", "title", "caption")
    )
    in_figure = bool(candidate.get("in_figure"))
    viewport_coverage = float(candidate.get("viewport_coverage") or 0)
    if candidate.get("source_kind") != "css_background":
        explicitly_decorative = bool(candidate.get("aria_hidden")) or role in {
            "none",
            "presentation",
        }
        if explicitly_decorative and not (
            in_figure and _useful_visual_label(candidate.get("caption"))
        ):
            return False
        semantic = in_figure or role == "img" or useful_label
        return viewport_coverage < 0.7 or semantic
    explicitly_decorative = bool(candidate.get("aria_hidden")) or role in {
        "none",
        "presentation",
    }
    if explicitly_decorative and not (
        tag == "figure" and _useful_visual_label(candidate.get("caption"))
    ):
        return False
    if str(candidate.get("background_repeat", "")).casefold() != "no-repeat":
        return False
    if _has_negative_pixel_offset(candidate.get("background_position")):
        return False
    if role != "img" and tag != "figure":
        return False
    if not useful_label:
        return False
    return viewport_coverage < 0.7 or (useful_label and (role == "img" or tag == "figure"))


async def _page_contains_sensitive_rendered_control(page: Page) -> bool:
    """Fail closed when bounded inspection finds a sensitive rendered form control.

    This gate cannot inspect closed shadow roots or semantic values painted into canvas,
    and it does not classify ordinary rendered text. A pre/post screenshot check narrows
    but cannot eliminate the race where the target mutates after the final inspection.
    """
    inspection_script = """
        (elements, config) => {
            if (elements.length > config.limit) return true;
            const autocompleteTokens = new Set(config.autocompleteTokens);
            const singleTokens = new Set(config.singleTokens);
            const compactPatterns = config.compactPatterns;
            const normalize = value => String(value || "").normalize("NFKC").toLowerCase();
            const bounded = value => {
                const raw = String(value || "");
                return raw.length <= config.maxChars ? normalize(raw) : null;
            };
            for (const element of elements.slice(0, config.limit)) {
                const style = getComputedStyle(element);
                const rendered = element.type !== "hidden"
                    && element.getClientRects().length > 0
                    && style.display !== "none"
                    && style.visibility !== "hidden"
                    && style.visibility !== "collapse"
                    && Number(style.opacity || 1) > 0.05;
                if (!rendered) continue;
                const type = bounded(element.getAttribute("type"));
                const autocomplete = bounded(element.getAttribute("autocomplete"));
                const name = bounded(element.getAttribute("name"));
                const id = bounded(element.id);
                const ariaLabel = bounded(element.getAttribute("aria-label"));
                const placeholder = bounded(element.getAttribute("placeholder"));
                if ([type, autocomplete, name, id, ariaLabel, placeholder].some(
                    value => value === null
                )) return true;
                if (type === "password") return true;
                if (autocomplete.split(/\\s+/).some(token => autocompleteTokens.has(token))) {
                    return true;
                }
                const identifier = `${name} ${id} ${ariaLabel} ${placeholder}`;
                const tokens = identifier.split(/[^a-z0-9]+/).filter(Boolean);
                const compact = tokens.join("");
                if (tokens.some(token => singleTokens.has(token))) return true;
                if (compactPatterns.some(pattern => compact.includes(pattern))) return true;
            }
            return false;
        }
    """
    config = {
        "limit": _SENSITIVE_CONTROL_INSPECTION_LIMIT,
        "maxChars": _SENSITIVE_CONTROL_ATTRIBUTE_MAX_CHARS,
        "autocompleteTokens": _SENSITIVE_AUTOCOMPLETE_TOKENS,
        "singleTokens": _SENSITIVE_CONTROL_SINGLE_TOKENS,
        "compactPatterns": _SENSITIVE_CONTROL_COMPACT_PATTERNS,
    }
    candidate_frames = page.frames
    if len(candidate_frames) > _SENSITIVE_FRAME_INSPECTION_LIMIT:
        return True
    for candidate_frame in candidate_frames:
        if await candidate_frame.locator("input, textarea, select").evaluate_all(
            inspection_script,
            config,
        ):
            return True
    return False


async def _rendered_viewport_dimensions(page: Page) -> tuple[int, int]:
    raw_viewport = await page.evaluate(
        """() => ({
            width: window.innerWidth > 0
                ? window.innerWidth
                : document.documentElement.clientWidth || 0,
            height: window.innerHeight > 0
                ? window.innerHeight
                : document.documentElement.clientHeight || 0,
        })"""
    )
    if not isinstance(raw_viewport, dict):
        return 0, 0
    return int(raw_viewport.get("width", 0)), int(raw_viewport.get("height", 0))


def _visual_identity_digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _visual_root_retention_state(storage_root: Path) -> _VisualRootRetention:
    absolute_root = Path(os.path.abspath(os.fspath(storage_root)))
    key = os.fspath(absolute_root)
    state = _VISUAL_ROOT_RETENTION_STATES.get(key)
    if state is None:
        state = _VisualRootRetention(storage_root=absolute_root)
        _VISUAL_ROOT_RETENTION_STATES[key] = state
    return state


def _exhaust_visual_root(state: _VisualRootRetention, reason: str) -> None:
    if state.exhausted:
        return
    state.exhausted = True
    state.exhaustion_reason = reason
    _LOGGER.warning(
        "browser screenshot retention saturated: reason=%s files=%d bytes=%d identities=%d; "
        "DOM and network capture continue without screenshots. Retained evidence is never "
        "deleted automatically; to reclaim capacity, stop Merlin, archive or remove %s and %s "
        "only when their evidence references are no longer needed, then restart Merlin",
        reason,
        len(state.retained_files),
        state.retained_bytes,
        len(state.identities),
        state.storage_root / "browser-evidence",
        state.storage_root / "browser-diagnostics",
    )


def _visual_root_is_exhausted(state: _VisualRootRetention) -> bool:
    if state.exhausted:
        return True
    if (
        len(state.retained_files) >= _VISUAL_ROOT_MAX_FILES
        or state.retained_bytes >= _VISUAL_ROOT_MAX_BYTES
        or len(state.identities) >= _VISUAL_ROOT_MAX_IDENTITIES
    ):
        _exhaust_visual_root(state, "root_budget_exhausted")
    return state.exhausted


def _visual_root_would_exceed(
    state: _VisualRootRetention,
    *,
    added_files: int,
    added_bytes: int,
    identity: str,
) -> bool:
    if _visual_root_is_exhausted(state):
        return True
    added_identities = int(identity not in state.identities)
    if (
        len(state.retained_files) + added_files > _VISUAL_ROOT_MAX_FILES
        or state.retained_bytes + added_bytes > _VISUAL_ROOT_MAX_BYTES
        or len(state.identities) + added_identities > _VISUAL_ROOT_MAX_IDENTITIES
    ):
        _exhaust_visual_root(state, "root_budget_exhausted")
    return state.exhausted


def _scan_visual_root(
    storage_root: Path,
) -> tuple[dict[str, int], int, set[str], str | None]:
    """Stat a bounded evidence tree without reading retained target bytes."""
    retained_files: dict[str, int] = {}
    identities: set[str] = set()
    retained_bytes = 0
    inspected_entries = 0
    context_directories = 0
    try:
        storage_stat = os.lstat(storage_root)
    except FileNotFoundError:
        return retained_files, retained_bytes, identities, None
    except OSError:
        return retained_files, retained_bytes, identities, "root_scan_failed"
    if stat.S_ISLNK(storage_stat.st_mode) or not stat.S_ISDIR(storage_stat.st_mode):
        return retained_files, retained_bytes, identities, "unsafe_storage_root"

    for directory_name in ("browser-evidence", "browser-diagnostics"):
        root = storage_root / directory_name
        try:
            root_stat = os.lstat(root)
        except FileNotFoundError:
            continue
        except OSError:
            return retained_files, retained_bytes, identities, "root_scan_failed"
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            return retained_files, retained_bytes, identities, "unsafe_retention_root"
        try:
            contexts = os.scandir(root)
        except OSError:
            return retained_files, retained_bytes, identities, "root_scan_failed"
        with contexts:
            for context_entry in contexts:
                inspected_entries += 1
                context_directories += 1
                if (
                    inspected_entries > _VISUAL_ROOT_SCAN_MAX_ENTRIES
                    or context_directories > _VISUAL_ROOT_SCAN_MAX_CONTEXTS
                ):
                    return retained_files, retained_bytes, identities, "root_scan_bound_exhausted"
                try:
                    if context_entry.is_symlink() or not context_entry.is_dir(
                        follow_symlinks=False
                    ):
                        return retained_files, retained_bytes, identities, "unsafe_context_entry"
                    files = os.scandir(context_entry.path)
                except OSError:
                    return retained_files, retained_bytes, identities, "root_scan_failed"
                with files:
                    for entry in files:
                        inspected_entries += 1
                        if inspected_entries > _VISUAL_ROOT_SCAN_MAX_ENTRIES:
                            return (
                                retained_files,
                                retained_bytes,
                                identities,
                                "root_scan_bound_exhausted",
                            )
                        try:
                            if (
                                entry.is_symlink()
                                or not entry.name.casefold().endswith(".jpg")
                                or not entry.is_file(follow_symlinks=False)
                            ):
                                return (
                                    retained_files,
                                    retained_bytes,
                                    identities,
                                    "unsafe_retention_entry",
                                )
                            entry_stat = entry.stat(follow_symlinks=False)
                        except OSError:
                            return retained_files, retained_bytes, identities, "root_scan_failed"
                        if (
                            not stat.S_ISREG(entry_stat.st_mode)
                            or entry_stat.st_nlink != 1
                            or entry_stat.st_size <= 0
                            or entry_stat.st_size > _VISUAL_MAX_BYTES
                        ):
                            return (
                                retained_files,
                                retained_bytes,
                                identities,
                                "unsafe_retention_entry",
                            )
                        path = Path(entry.path)
                        path_key = os.fspath(path)
                        retained_files[path_key] = entry_stat.st_size
                        retained_bytes += entry_stat.st_size
                        identities.add(
                            hashlib.sha256(b"retained-file\0" + os.fsencode(path)).hexdigest()
                        )
                        if (
                            len(retained_files) >= _VISUAL_ROOT_MAX_FILES
                            or retained_bytes >= _VISUAL_ROOT_MAX_BYTES
                            or len(identities) >= _VISUAL_ROOT_MAX_IDENTITIES
                        ):
                            return (
                                retained_files,
                                retained_bytes,
                                identities,
                                "root_budget_exhausted",
                            )
    return retained_files, retained_bytes, identities, None


def _prepare_private_visual_directory(
    storage_root: Path,
    directory_name: str,
    context_id: str,
) -> Path:
    if not context_id or context_id in {".", ".."} or Path(context_id).name != context_id:
        raise OSError("visual retention context id is not a path component")
    directory = storage_root / directory_name / context_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for candidate in (storage_root, storage_root / directory_name, directory):
        candidate_stat = os.lstat(candidate)
        if stat.S_ISLNK(candidate_stat.st_mode) or not stat.S_ISDIR(candidate_stat.st_mode):
            raise OSError("visual retention directory is not a regular directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory, flags)
    try:
        directory_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise OSError("visual retention directory changed during validation")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    return directory


def _write_private_visual_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    opened_stat = os.fstat(descriptor)
    try:
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink != 1:
            raise OSError("visual evidence destination is not a private regular file")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            current_stat = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if (current_stat.st_dev, current_stat.st_ino) == (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ):
                path.unlink(missing_ok=True)
        raise


def _write_private_visual_capture(
    storage_root: Path,
    directory_name: str,
    context_id: str,
    digest: str,
    payload: bytes,
) -> Path:
    directory = _prepare_private_visual_directory(storage_root, directory_name, context_id)
    path = directory / f"{digest}.jpg"
    _write_private_visual_file(path, payload)
    return path


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
    match_index: int
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
        # Screenshot retention is first-write-wins per logical context_id and is also
        # bounded across every context in the daemon. Files are retained because
        # durable evidence can reference them; capacity is reclaimed only by offline
        # operator cleanup followed by a daemon restart.
        self._visual_evidence_seen: set[tuple[str, str]] = set()
        self._visual_retained_files: dict[str, int] = {}
        self._visual_digest_paths: dict[str, Path] = {}
        self._visual_trusted_digest_paths: set[str] = set()
        self._visual_retained_bytes = 0
        self._visual_retention_exhausted = False
        self._visual_retention_context_id: str | None = None
        self._visual_retention_lock = asyncio.Lock()
        self._visual_root_retention: _VisualRootRetention | None = None
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
        frame_surface_occluded = await _frame_surface_occluded(frame)
        title = " ".join((await frame.title()).split())[:512]
        text = await frame.locator("body").inner_text(timeout=2_000)
        lowered = text.lower()
        if "captcha" in lowered or "verify you are human" in lowered:
            return self._observation(
                tab_ref,
                "blocker",
                frame_ref=resolved_frame_ref,
                title=title,
                text="manual CAPTCHA blocker",
                step=step,
            )
        generation = self._generation.get(tab_ref, 0) + 1
        self._generation[tab_ref] = generation
        elements: list[ElementRef] = []
        match_counts: dict[tuple[str, str], int] = {}
        for role in ("button", "link", "textbox", "checkbox", "radio", "combobox"):
            locators = frame.get_by_role(role)
            role_count = min(await locators.count(), 50)
            surface_metadata = await _element_surface_metadata(locators, role_count)
            for index in range(role_count):
                locator = locators.nth(index)
                name = await self._element_name(locator, role, index)
                match_key = (role, name)
                match_index = match_counts.get(match_key, 0)
                match_counts[match_key] = match_index + 1
                input_type = await locator.get_attribute("type")
                raw_max_length = await locator.get_attribute("maxlength")
                max_length = (
                    int(raw_max_length)
                    if raw_max_length is not None and raw_max_length.isdigit()
                    else None
                )
                ref = f"{tab_ref}:{generation}:{len(elements)}"
                self._elements[ref] = _Element(
                    generation,
                    resolved_frame_ref,
                    role,
                    name,
                    match_index,
                    locator,
                )
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
                        surface_scope=surface_metadata[index][0],
                        occluded=surface_metadata[index][1] or frame_surface_occluded,
                    )
                )
        uploads = frame.locator('input[type="file"]')
        upload_count = min(await uploads.count(), 20)
        upload_surface_metadata = await _element_surface_metadata(uploads, upload_count)
        for index in range(upload_count):
            locator = uploads.nth(index)
            name = (await locator.get_attribute("aria-label")) or f"File upload {index + 1}"
            ref = f"{tab_ref}:{generation}:{len(elements)}"
            match_key = ("file", name)
            match_index = match_counts.get(match_key, 0)
            match_counts[match_key] = match_index + 1
            self._elements[ref] = _Element(
                generation,
                resolved_frame_ref,
                "file",
                name,
                match_index,
                locator,
            )
            elements.append(
                ElementRef(
                    ref=ref,
                    tab_ref=tab_ref,
                    frame_ref=resolved_frame_ref,
                    role="file",
                    name=name,
                    surface_scope=upload_surface_metadata[index][0],
                    occluded=upload_surface_metadata[index][1] or frame_surface_occluded,
                )
            )
        structural_surfaces = frame.locator(
            "dialog[open], [role=dialog], [role=alertdialog], [role=status], "
            "[role=alert], [popover]"
        )
        structural_count = min(await structural_surfaces.count(), 20)
        structural_metadata = await _element_surface_metadata(
            structural_surfaces, structural_count
        )
        for index in range(structural_count):
            surface_scope, occluded = structural_metadata[index]
            if surface_scope not in {"status", "dialog", "modal", "top_layer"}:
                continue
            locator = structural_surfaces.nth(index)
            name = f"{surface_scope.replace('_', ' ').title()} surface {index + 1}"
            ref = f"{tab_ref}:{generation}:{len(elements)}"
            match_key = ("surface", name)
            match_index = match_counts.get(match_key, 0)
            match_counts[match_key] = match_index + 1
            self._elements[ref] = _Element(
                generation,
                resolved_frame_ref,
                "surface",
                name,
                match_index,
                locator,
            )
            elements.append(
                ElementRef(
                    ref=ref,
                    tab_ref=tab_ref,
                    frame_ref=resolved_frame_ref,
                    role="surface",
                    name=name,
                    surface_scope=surface_scope,
                    occluded=occluded or frame_surface_occluded,
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
            title=title,
            text=safe_text,
            elements=tuple(elements),
            redacted=redacted,
            step=step,
        )

    @staticmethod
    def _scan_visual_retention(
        storage_root: Path,
        context_id: str,
    ) -> tuple[dict[str, int], dict[str, Path], int, bool]:
        retained_files: dict[str, int] = {}
        digest_paths: dict[str, Path] = {}
        retained_bytes = 0
        roots = (
            storage_root / "browser-evidence" / context_id,
            storage_root / "browser-diagnostics" / context_id,
        )
        for root in roots:
            try:
                root_stat = os.lstat(root)
                if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                    return retained_files, digest_paths, retained_bytes, True
                entries = os.scandir(root)
            except FileNotFoundError:
                continue
            except OSError:
                return retained_files, digest_paths, retained_bytes, True
            with entries:
                for entry in entries:
                    try:
                        if (
                            entry.is_symlink()
                            or not entry.name.casefold().endswith(".jpg")
                            or not entry.is_file(follow_symlinks=False)
                        ):
                            return retained_files, digest_paths, retained_bytes, True
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        return retained_files, digest_paths, retained_bytes, True
                    if (
                        not stat.S_ISREG(entry_stat.st_mode)
                        or entry_stat.st_nlink != 1
                        or entry_stat.st_size <= 0
                        or entry_stat.st_size > _VISUAL_MAX_BYTES
                    ):
                        return retained_files, digest_paths, retained_bytes, True
                    path = Path(entry.path)
                    size = entry_stat.st_size
                    retained_files[str(path)] = size
                    retained_bytes += size
                    stem = path.stem.casefold()
                    if len(stem) == 64 and all(
                        character in "0123456789abcdef" for character in stem
                    ):
                        digest_paths.setdefault(stem, path)
                    if (
                        len(retained_files) >= _VISUAL_CONTEXT_MAX_FILES
                        or retained_bytes >= _VISUAL_CONTEXT_MAX_BYTES
                    ):
                        return retained_files, digest_paths, retained_bytes, True
        return retained_files, digest_paths, retained_bytes, False

    def _visual_root_state(self) -> _VisualRootRetention:
        if self._visual_root_retention is None:
            self._visual_root_retention = _visual_root_retention_state(Path.home() / ".merlin")
        return self._visual_root_retention

    async def _ensure_visual_retention(self) -> None:
        root_state = self._visual_root_state()
        if not root_state.initialized:
            (
                root_state.retained_files,
                root_state.retained_bytes,
                root_state.identities,
                scan_failure,
            ) = await asyncio.to_thread(_scan_visual_root, root_state.storage_root)
            root_state.initialized = True
            if scan_failure is not None:
                _exhaust_visual_root(root_state, scan_failure)
        if self._visual_retention_context_id == self.context_id:
            _visual_root_is_exhausted(root_state)
            return
        (
            self._visual_retained_files,
            self._visual_digest_paths,
            self._visual_retained_bytes,
            self._visual_retention_exhausted,
        ) = await asyncio.to_thread(
            self._scan_visual_retention,
            root_state.storage_root,
            self.context_id,
        )
        self._visual_evidence_seen = set()
        self._visual_trusted_digest_paths = set()
        self._visual_retention_context_id = self.context_id
        _visual_root_is_exhausted(root_state)

    def _visual_retention_rejection(self) -> str | None:
        root_state = self._visual_root_retention
        if root_state is not None and _visual_root_is_exhausted(root_state):
            return "root_budget_exhausted"
        if not self._visual_retention_exhausted:
            self._visual_retention_exhausted = (
                len(self._visual_retained_files) >= _VISUAL_CONTEXT_MAX_FILES
                or self._visual_retained_bytes >= _VISUAL_CONTEXT_MAX_BYTES
                or len(self._visual_evidence_seen) >= _VISUAL_CONTEXT_MAX_IDENTITIES
            )
        if self._visual_retention_exhausted:
            return "context_budget_exhausted"
        return None

    def _visual_retention_is_exhausted(self) -> bool:
        return self._visual_retention_rejection() is not None

    async def _retain_visual_capture(
        self,
        payload: bytes,
        *,
        identity_source: str,
        directory_name: str,
    ) -> tuple[dict[str, object] | None, str | None]:
        if not payload or len(payload) > _VISUAL_MAX_BYTES:
            return None, "attachment_size_out_of_bounds"
        digest = hashlib.sha256(payload).hexdigest()
        identity = (identity_source, digest)
        if identity in self._visual_evidence_seen:
            return None, "duplicate"
        rejection = self._visual_retention_rejection()
        if rejection is not None:
            return None, rejection

        root_state = self._visual_root_state()
        global_identity = _visual_identity_digest(
            "capture",
            self.context_id,
            identity_source,
            digest,
        )
        path = self._visual_digest_paths.get(digest)
        if path is None:
            if (
                len(self._visual_retained_files) + 1 > _VISUAL_CONTEXT_MAX_FILES
                or self._visual_retained_bytes + len(payload) > _VISUAL_CONTEXT_MAX_BYTES
            ):
                self._visual_retention_exhausted = True
                return None, "context_budget_exhausted"
            if _visual_root_would_exceed(
                root_state,
                added_files=1,
                added_bytes=len(payload),
                identity=global_identity,
            ):
                return None, "root_budget_exhausted"
            cancelled: asyncio.CancelledError | None = None
            write_future = asyncio.get_running_loop().run_in_executor(
                None,
                partial(
                    _write_private_visual_capture,
                    root_state.storage_root,
                    directory_name,
                    self.context_id,
                    digest,
                    payload,
                ),
            )
            while True:
                try:
                    path = await asyncio.shield(write_future)
                    break
                except asyncio.CancelledError as error:
                    if write_future.cancelled():
                        self._visual_retention_exhausted = True
                        _exhaust_visual_root(root_state, "write_cancelled")
                        current_task = asyncio.current_task()
                        if current_task is not None and current_task.cancelling():
                            raise error from None
                        return None, "root_budget_exhausted"
                    cancelled = cancelled or error
                    continue
                except FileExistsError:
                    _exhaust_visual_root(root_state, "write_collision")
                    if cancelled is not None:
                        raise cancelled from None
                    return None, "root_budget_exhausted"
                except OSError:
                    _exhaust_visual_root(root_state, "unsafe_write_path")
                    if cancelled is not None:
                        raise cancelled from None
                    return None, "root_budget_exhausted"
                except BaseException:
                    self._visual_retention_exhausted = True
                    _exhaust_visual_root(root_state, "write_failed")
                    if cancelled is not None:
                        raise cancelled from None
                    raise
            path_key = str(path)
            if path_key not in self._visual_retained_files:
                self._visual_retained_files[path_key] = len(payload)
                self._visual_retained_bytes += len(payload)
            self._visual_digest_paths[digest] = path
            self._visual_trusted_digest_paths.add(path_key)
            if path_key not in root_state.retained_files:
                root_state.retained_files[path_key] = len(payload)
                root_state.retained_bytes += len(payload)
            root_state.identities.add(global_identity)
            self._visual_evidence_seen.add(identity)
            self._visual_retention_is_exhausted()
            if cancelled is not None:
                raise cancelled
        else:
            if _visual_root_would_exceed(
                root_state,
                added_files=0,
                added_bytes=0,
                identity=global_identity,
            ):
                return None, "root_budget_exhausted"
            root_state.identities.add(global_identity)
            self._visual_evidence_seen.add(identity)
            # A stat-only restart scan can account for a retained digest but cannot
            # prove that the filename matches the file contents. Existing evidence
            # remains retained, but it is not rebound to a new attachment.
            if str(path) not in self._visual_trusted_digest_paths:
                self._visual_retention_is_exhausted()
                return None, "duplicate"

        self._visual_evidence_seen.add(identity)
        self._visual_retention_is_exhausted()
        return (
            {
                "media_type": "image/jpeg",
                "path": str(path),
                "sha256": digest,
                "byte_length": len(payload),
            },
            None,
        )

    async def capture_visual_evidence(
        self,
        tab_ref: str,
        frame_ref: str | None = None,
    ) -> tuple[tuple[dict[str, object], ...], tuple[str, ...]]:
        """Capture one bounded viewport plus supplemental visible content images."""
        root_state = self._visual_root_state()
        async with root_state.lock:
            async with self._visual_retention_lock:
                await self._ensure_visual_retention()
                rejection = self._visual_retention_rejection()
                if rejection is not None:
                    return (), (f"visual_capture:{rejection}",)
                return await self._capture_visual_evidence(tab_ref, frame_ref)

    async def _capture_visual_evidence(
        self,
        tab_ref: str,
        frame_ref: str | None = None,
    ) -> tuple[tuple[dict[str, object], ...], tuple[str, ...]]:
        page = self._page(tab_ref)
        await self._enforce_page(page)
        frame, resolved_frame_ref = self._frame(page, frame_ref)
        evidence: list[dict[str, object]] = []
        errors: list[str] = []

        async def retain_capture(
            payload: bytes,
            *,
            source_kind: str,
            source_url: str,
            evidence_frame_ref: str | None = resolved_frame_ref,
        ) -> dict[str, object] | None:
            identity_source = f"viewport:{source_url}" if source_kind == "viewport" else source_url
            attachment, rejection = await self._retain_visual_capture(
                payload,
                identity_source=identity_source,
                directory_name="browser-evidence",
            )
            if attachment is None:
                if rejection not in {None, "duplicate"}:
                    capture_kind = (
                        "viewport_capture" if source_kind == "viewport" else "candidate_capture"
                    )
                    errors.append(f"{capture_kind}:{rejection}")
                return None
            digest = str(attachment["sha256"])
            evidence_digest = hashlib.sha256(f"{identity_source}\0{digest}".encode()).hexdigest()
            return {
                "evidence_ref": f"sha256:{evidence_digest}",
                "source_url": source_url,
                "source_kind": source_kind,
                "tab_ref": tab_ref,
                "frame_ref": evidence_frame_ref,
                "untrusted_target_evidence": True,
                "attachment": attachment,
            }

        sensitive_control = False
        try:
            sensitive_control = await _page_contains_sensitive_rendered_control(page)
        except PlaywrightError as error:
            sensitive_control = True
            errors.append(f"viewport_inspection:{type(error).__name__}")
        if sensitive_control:
            errors.append("viewport_capture:sensitive_form_redacted")
            return (), tuple(dict.fromkeys(errors))
        else:
            viewport_width = 0
            viewport_height = 0
            viewport_within_bounds = False
            try:
                viewport_width, viewport_height = await _rendered_viewport_dimensions(page)
                viewport_within_bounds = (
                    0 < viewport_width <= _VISUAL_VIEWPORT_MAX_WIDTH_PX
                    and 0 < viewport_height <= _VISUAL_VIEWPORT_MAX_HEIGHT_PX
                )
                if not viewport_within_bounds:
                    errors.append("viewport_capture:viewport_size_out_of_bounds")
                else:
                    screenshot = await page.screenshot(
                        type="jpeg",
                        quality=68,
                        animations="allow",
                        caret="initial",
                        full_page=False,
                        scale="css",
                        timeout=_VISUAL_CAPTURE_TIMEOUT_MS,
                    )
                    try:
                        sensitive_after_capture = (
                            await _page_contains_sensitive_rendered_control(page)
                        )
                    except PlaywrightError as error:
                        errors.append(f"viewport_post_inspection:{type(error).__name__}")
                        sensitive_after_capture = True
                    if sensitive_after_capture:
                        errors.append("viewport_capture:sensitive_form_redacted")
                        return (), tuple(dict.fromkeys(errors))
                    retained = await retain_capture(
                        bytes(screenshot),
                        source_kind="viewport",
                        source_url=_sanitized_visual_source_url(page.url),
                        evidence_frame_ref=None,
                    )
                    if retained is not None:
                        evidence.append(
                            {
                                **retained,
                                "viewport_width": viewport_width,
                                "viewport_height": viewport_height,
                                "full_page": False,
                                "requested_frame_ref": resolved_frame_ref,
                            }
                        )
                    if self._visual_retention_is_exhausted():
                        return tuple(evidence), tuple(dict.fromkeys(errors))
            except PlaywrightError as error:
                errors.append(f"viewport_capture:{type(error).__name__}")
        if not viewport_within_bounds:
            return tuple(evidence), tuple(dict.fromkeys(errors))
        candidate_script = """
            (element, sourceKind) => {
                const rect = element.getBoundingClientRect();
                const viewportWidth = window.innerWidth > 0
                    ? window.innerWidth
                    : document.documentElement.clientWidth || 0;
                const viewportHeight = window.innerHeight > 0
                    ? window.innerHeight
                    : document.documentElement.clientHeight || 0;
                let clipLeft = Math.max(rect.left, 0);
                let clipRight = Math.min(rect.right, viewportWidth);
                let clipTop = Math.max(rect.top, 0);
                let clipBottom = Math.min(rect.bottom, viewportHeight);
                let visible = rect.width > 0 && rect.height > 0;
                let ariaHidden = false;
                for (let node = element; node && visible; node = node.parentElement) {
                    const computed = getComputedStyle(node);
                    ariaHidden = ariaHidden || node.getAttribute("aria-hidden") === "true";
                    visible = computed.display !== "none"
                        && computed.visibility !== "hidden"
                        && computed.visibility !== "collapse"
                        && Number(computed.opacity || 1) > 0.05;
                    if (node !== element) {
                        const ancestorRect = node.getBoundingClientRect();
                        if (["auto", "clip", "hidden", "scroll"].includes(computed.overflowX)) {
                            clipLeft = Math.max(clipLeft, ancestorRect.left);
                            clipRight = Math.min(clipRight, ancestorRect.right);
                        }
                        if (["auto", "clip", "hidden", "scroll"].includes(computed.overflowY)) {
                            clipTop = Math.max(clipTop, ancestorRect.top);
                            clipBottom = Math.min(clipBottom, ancestorRect.bottom);
                        }
                    }
                }
                const visibleWidth = Math.max(0, clipRight - clipLeft);
                const visibleHeight = Math.max(0, clipBottom - clipTop);
                const style = getComputedStyle(element);
                const figure = element.closest("figure");
                const captionNode = figure && figure.querySelector("figcaption");
                const tag = element.tagName.toLowerCase();
                let sourceUrl = "";
                let loaded = true;
                let naturalWidth = 0;
                let naturalHeight = 0;
                if (sourceKind === "img") {
                    sourceUrl = element.currentSrc || element.src || "";
                    naturalWidth = Number(element.naturalWidth || 0);
                    naturalHeight = Number(element.naturalHeight || 0);
                    loaded = Boolean(element.complete && naturalWidth && naturalHeight);
                } else {
                    const match = style.backgroundImage.match(
                        /^url\\((["']?)(.*?)\\1\\)$/
                    );
                    sourceUrl = match ? match[2] : "";
                    loaded = Boolean(sourceUrl);
                }
                const visibleArea = visibleWidth * visibleHeight;
                const viewportArea = Math.max(1, viewportWidth * viewportHeight);
                return {
                    source_kind: sourceKind,
                    source_url: sourceUrl,
                    loaded,
                    visible: visible && visibleWidth > 0 && visibleHeight > 0,
                    tag,
                    role: element.getAttribute("role") || "",
                    aria_hidden: ariaHidden,
                    in_figure: Boolean(figure),
                    alt: element.getAttribute("alt") || "",
                    aria_label: element.getAttribute("aria-label") || "",
                    title: element.getAttribute("title") || "",
                    caption: captionNode ? (captionNode.innerText || "").trim() : "",
                    visible_offset_x: clipLeft - rect.left,
                    visible_offset_y: clipTop - rect.top,
                    rendered_width: rect.width,
                    rendered_height: rect.height,
                    visible_width: visibleWidth,
                    visible_height: visibleHeight,
                    visible_area: visibleArea,
                    viewport_coverage: visibleArea / viewportArea,
                    natural_width: naturalWidth,
                    natural_height: naturalHeight,
                    background_repeat: style.backgroundRepeat,
                    background_position: style.backgroundPosition,
                };
            }
        """
        candidates: list[tuple[Locator, dict[str, object]]] = []
        sources = (
            ("img", frame.locator("img")),
            ("css_background", frame.locator('figure, [role="img"]')),
        )
        for source_kind, locators in sources:
            count = min(await locators.count(), _VISUAL_INSPECTION_LIMIT)
            for index in range(count):
                locator = locators.nth(index)
                try:
                    raw_candidate = await locator.evaluate(candidate_script, source_kind)
                except PlaywrightError as error:
                    errors.append(f"candidate_inspection:{type(error).__name__}")
                    continue
                if not isinstance(raw_candidate, dict):
                    continue
                candidate = {str(key): value for key, value in raw_candidate.items()}
                if _visual_candidate_admitted(candidate):
                    candidates.append((locator, candidate))

        def rank(item: tuple[Locator, dict[str, object]]) -> tuple[int, int, float]:
            candidate = item[1]
            strong_semantics = int(
                str(candidate.get("tag", "")).casefold() == "figure"
                or _useful_visual_label(candidate.get("caption"))
            )
            normal_image = int(candidate.get("source_kind") == "img")
            return normal_image, strong_semantics, float(candidate.get("visible_area") or 0)

        capture_attempts = 0
        for locator, candidate in sorted(candidates, key=rank, reverse=True):
            rejection = self._visual_retention_rejection()
            if rejection is not None:
                errors.append(f"candidate_capture:{rejection}")
                break
            if len(evidence) >= _VISUAL_CANDIDATE_LIMIT:
                break
            if capture_attempts >= _VISUAL_CAPTURE_ATTEMPT_LIMIT:
                errors.append("candidate_capture:attempt_limit")
                break
            capture_attempts += 1
            source_url = _sanitized_visual_source_url(str(candidate.get("source_url", "")))
            try:
                if await _page_contains_sensitive_rendered_control(page):
                    errors.append("candidate_capture:sensitive_form_redacted")
                    break
            except PlaywrightError as error:
                errors.append(f"candidate_pre_inspection:{type(error).__name__}")
                break
            try:
                bounding_box = await locator.bounding_box()
                if bounding_box is None:
                    errors.append("candidate_capture:bounding_box_unavailable")
                    continue
                viewport_x = max(
                    0.0,
                    float(bounding_box["x"])
                    + float(candidate.get("visible_offset_x") or 0),
                )
                viewport_y = max(
                    0.0,
                    float(bounding_box["y"])
                    + float(candidate.get("visible_offset_y") or 0),
                )
                clip_width = min(
                    float(candidate.get("visible_width") or 0),
                    max(0.0, viewport_width - viewport_x),
                )
                clip_height = min(
                    float(candidate.get("visible_height") or 0),
                    max(0.0, viewport_height - viewport_y),
                )
                if clip_width <= 0 or clip_height <= 0:
                    errors.append("candidate_capture:clip_out_of_viewport")
                    continue
                screenshot = await page.screenshot(
                    type="jpeg",
                    quality=72,
                    animations="allow",
                    caret="initial",
                    full_page=False,
                    scale="css",
                    clip={
                        "x": viewport_x,
                        "y": viewport_y,
                        "width": clip_width,
                        "height": clip_height,
                    },
                    timeout=_VISUAL_CAPTURE_TIMEOUT_MS,
                )
            except PlaywrightError as error:
                errors.append(f"candidate_capture:{type(error).__name__}")
                continue
            try:
                if await _page_contains_sensitive_rendered_control(page):
                    errors.append("candidate_capture:sensitive_form_redacted")
                    break
            except PlaywrightError as error:
                errors.append(f"candidate_post_inspection:{type(error).__name__}")
                break
            payload = bytes(screenshot)
            retained = await retain_capture(
                payload,
                source_kind=str(candidate.get("source_kind", "img")),
                source_url=source_url,
            )
            if retained is None:
                continue
            evidence.append(
                {
                    **retained,
                    "role": _bounded_visual_metadata(candidate.get("role")),
                    "alt": _bounded_visual_metadata(candidate.get("alt")),
                    "aria_label": _bounded_visual_metadata(candidate.get("aria_label")),
                    "title": _bounded_visual_metadata(candidate.get("title")),
                    "caption": _bounded_visual_metadata(candidate.get("caption")),
                    "rendered_width": round(float(candidate.get("rendered_width") or 0), 2),
                    "rendered_height": round(float(candidate.get("rendered_height") or 0), 2),
                    "visible_width": round(float(candidate.get("visible_width") or 0), 2),
                    "visible_height": round(float(candidate.get("visible_height") or 0), 2),
                    "viewport_coverage": round(float(candidate.get("viewport_coverage") or 0), 4),
                    "natural_width": int(float(candidate.get("natural_width") or 0)),
                    "natural_height": int(float(candidate.get("natural_height") or 0)),
                }
            )
        return tuple(evidence), tuple(dict.fromkeys(errors))

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
        result = await frame.locator("body").evaluate(
            """(body, needle) => {
                if (!needle) return 0;
                const text = body.innerText;
                let count = 0;
                let offset = 0;
                while ((offset = text.indexOf(needle, offset)) !== -1) {
                    count += 1;
                    offset += needle.length;
                }
                return count;
            }""",
            value,
        )
        if type(result) is not int or result < 0:
            raise BrowserBoundaryError("reviewed response witness count is invalid")
        return result

    async def bounded_rendered_text(
        self,
        tab_ref: str,
        frame_ref: str | None = None,
        *,
        max_chars: int = 262_144,
    ) -> str:
        """Read exact rendered text for reviewed extraction without persisting an unbounded DOM."""

        page = self._page(tab_ref)
        await self._enforce_page(page)
        frame, _ = self._frame(page, frame_ref)
        if await frame.locator('input[type="password"]').count() > 0:
            raise BrowserBoundaryError(
                "reviewed rendered text is unavailable across the snapshot redaction boundary"
            )
        result = await frame.locator("body").evaluate(
            """(body, limit) => {
                const text = body.innerText;
                return text.length <= limit ? {text} : {length: text.length};
            }""",
            max_chars,
        )
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            observed = result.get("length") if isinstance(result, dict) else None
            raise BrowserBoundaryError(
                "reviewed rendered text exceeds the bounded extraction limit"
                + (f" ({observed} characters)" if isinstance(observed, int) else "")
            )
        return result["text"]

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

    async def _capture_failure_screenshot(
        self,
        page: Page,
        *,
        tab_ref: str,
    ) -> dict[str, object] | None:
        if self._visual_retention_is_exhausted():
            return None
        try:
            if await _page_contains_sensitive_rendered_control(page):
                return None
            viewport_width, viewport_height = await _rendered_viewport_dimensions(page)
        except PlaywrightError:
            return None
        if not (
            0 < viewport_width <= _VISUAL_VIEWPORT_MAX_WIDTH_PX
            and 0 < viewport_height <= _VISUAL_VIEWPORT_MAX_HEIGHT_PX
        ):
            return None
        payload = bytes(
            await page.screenshot(
                type="jpeg",
                quality=60,
                full_page=False,
                scale="css",
                animations="allow",
                caret="initial",
                timeout=5_000,
            )
        )
        try:
            if await _page_contains_sensitive_rendered_control(page):
                return None
        except PlaywrightError:
            return None
        attachment, _rejection = await self._retain_visual_capture(
            payload,
            identity_source=f"diagnostic:{tab_ref}",
            directory_name="browser-diagnostics",
        )
        if attachment is None:
            return None
        digest = str(attachment["sha256"])
        return {
            "evidence_ref": f"sha256:{digest}",
            **attachment,
        }

    async def capture_failure_diagnostics(
        self,
        tab_ref: str,
        frame_ref: str | None = None,
        *,
        step: int = 0,
        capture_screenshot: bool = False,
    ) -> tuple[BrowserObservation, BrowserObservation, dict[str, object] | None]:
        """Capture bounded DOM/network diagnostics and an authorized screenshot."""
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
        if not capture_screenshot:
            return snapshot, network, None
        root_state = self._visual_root_state()
        async with root_state.lock:
            async with self._visual_retention_lock:
                await self._ensure_visual_retention()
                screenshot = await self._capture_failure_screenshot(page, tab_ref=tab_ref)
        if screenshot is None:
            return snapshot, network, None
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
        return tuple(
            dict(item)
            for item in self._network_events
            if "request_json" in item or "response_json" in item
        )

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
            else frame.get_by_role(element.role)
        )
        matches: list[Locator] = []
        limit = 20 if element.role == "file" else 50
        for index in range(min(await recovered.count(), limit)):
            candidate = recovered.nth(index)
            if await self._element_name(candidate, element.role, index) == element.name:
                matches.append(candidate)
        if len(matches) == 1:
            return matches[0]
        if element.match_index >= len(matches):
            raise StaleReference("element reference could not be recovered uniquely")
        # Repeated labels are common in responsive navigation and wallet
        # dialogs. Preserve the occurrence selected in the reviewed snapshot
        # instead of rejecting every action after an unrelated re-render.
        candidate = matches[element.match_index]
        if not await candidate.is_visible():
            visible = [item for item in matches if await item.is_visible()]
            if len(visible) != 1:
                raise StaleReference("element reference could not be recovered uniquely")
            return visible[0]
        return candidate

    async def _element_name(self, locator: Locator, role: str, index: int) -> str:
        name = (
            (await locator.get_attribute("aria-label"))
            or (await locator.get_attribute("placeholder"))
            or (await locator.get_attribute("name"))
            or (await locator.inner_text())
            or f"{role.title()} {index + 1}"
        )
        return " ".join(name.split())[:200]

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
        title: str = "",
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
            title=title,
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
