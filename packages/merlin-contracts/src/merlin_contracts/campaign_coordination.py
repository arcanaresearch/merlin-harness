"""Shared classification for authenticated browser handback coordination."""

from __future__ import annotations

import re

_URL_CANDIDATE_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s]+", re.IGNORECASE)


def has_url_candidate(content: str) -> bool:
    """Return whether text contains any URL-like scheme token, valid or malformed."""

    return _URL_CANDIDATE_RE.search(content) is not None


def is_explicit_campaign_launch(content: str) -> bool:
    """Return whether text explicitly launches a campaign rather than continuing one."""

    normalized = " ".join(content.casefold().split())
    if normalized.startswith("/"):
        return False
    continuation_prefix = (
        re.match(r"^(?:please\s+)?(?:resume|retry|continue)\b", normalized) is not None
    )
    explicit_fresh_launch = (
        re.search(
            r"\b(?:new|fresh|another)\s+"
            r"(?:(?:ai|application|security|red[- ]team|merlin|"
            r"targeted|full|quick|exhaustive)\s+){0,3}"
            r"(?:campaign|scan|assessment|evaluation)\b",
            normalized,
        )
        is not None
    )
    launch_rejected = (
        re.search(
            r"\b(?:do\s+not|don't|never)\s+"
            r"(?:start|launch|run|begin|create|open|use)\b.{0,32}"
            r"\b(?:campaign|scan|assessment|evaluation)\b",
            normalized,
        )
        is not None
        or re.search(
            r"\bno\s+(?:(?:new|fresh|another)\s+)?"
            r"(?:campaign|scan|assessment|evaluation)\b",
            normalized,
        )
        is not None
    )
    if launch_rejected:
        return False
    requests_assessment = any(
        marker in normalized
        for marker in (
            "assess ",
            "assessment against ",
            "run merlin",
            "use merlin",
            "merlin scan",
            "campaign profile",
            "red pen scan",
            "redpen scan",
            "red-team scan",
            "red team scan",
            "red-team assessment",
            "red team assessment",
            "security assessment",
            "security evaluation",
        )
    )
    return explicit_fresh_launch or (requests_assessment and not continuation_prefix)


def is_recon_handback_coordination(content: str) -> bool:
    """Return whether an operator completed auth and asked Merlin to continue."""

    normalized = " ".join(content.casefold().split())
    continuation_rejected = (
        re.search(
            r"\b(?:do\s+not|don't|never|not)\s+(?:continue|resume|proceed)\b",
            normalized,
        )
        is not None
    )
    auth_failed = any(
        pattern.search(normalized) is not None
        for pattern in (
            re.compile(r"\b(?:not|never)\s+(?:signed|logged)\s+in\b"),
            re.compile(
                r"\b(?:could\s+not|couldn't|cannot|can't|unable\s+to|failed\s+to)\b"
                r".{0,40}\b(?:sign(?:ed)?|log(?:ged)?)\s+in\b"
            ),
            re.compile(
                r"\b(?:login|auth|authentication|mfa|captcha)\b.{0,40}"
                r"\b(?:fail(?:ed|ure)?|error|incomplete|"
                r"(?:not|never|isn't|wasn't)\s+(?:complete|completed|done))\b"
            ),
        )
    )
    cancellation_requested = (
        re.search(
            r"\b(?:cancel|abort)\b|"
            r"\b(?:stop|end|terminate)\b.{0,24}\b(?:campaign|scan|assessment|work|testing)\b",
            normalized,
        )
        is not None
        and re.search(
            r"\b(?:do\s+not|don't|never)\s+"
            r"(?:cancel|abort|stop|end|terminate)\b",
            normalized,
        )
        is None
    )
    if continuation_rejected or auth_failed or cancellation_requested:
        return False
    continues = re.search(r"\b(?:continue|resume|proceed)\b", normalized) is not None
    auth_completed = (
        re.search(r"\b(?:signed|logged)\s+in\b", normalized) is not None
        or re.search(
            r"\b(?:login|auth|authentication|mfa|captcha)\b.{0,40}"
            r"\b(?:complete|completed|done)\b",
            normalized,
        )
        is not None
        or re.search(
            r"\b(?:hand|give|return|take)\b.{0,40}\bbrowser\b.{0,20}\bback\b",
            normalized,
        )
        is not None
    )
    return continues and auth_completed
