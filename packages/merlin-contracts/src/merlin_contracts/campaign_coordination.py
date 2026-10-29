"""Shared, ordered classification for campaign coordination messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_URL_CANDIDATE_RE = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_CLAUSE_BOUNDARY_RE = re.compile(
    r"\s*;\s*|(?<=[.!?])\s+|"
    r"\s*,?\s+\b(?:but|however|instead|and\s+then|then)\b\s+|"
    r"(?<!not)\s*,?\s+\byet\b\s+",
    re.IGNORECASE,
)
_CONTINUATION_RE = re.compile(r"^(?:please\s+)?(?:continue|resume|retry|proceed)\b")
_TARGET_NOUNS = r"(?:campaign|scan|assessment|evaluation)"
_FRESH_RE = re.compile(
    r"\b(?:new|fresh|another)\s+"
    r"(?:(?:ai|application|security|red[- ]team|merlin|targeted|full|quick|exhaustive)\s+){0,3}"
    + _TARGET_NOUNS
    + r"\b"
)


@dataclass(frozen=True)
class CampaignCoordinationIntent:
    """Final authoritative meaning after reducing ordered clauses."""

    kind: Literal["launch", "continuation", "handback", "targeted_barrier", "barrier"]
    clause: str
    clause_only: bool = False


def has_url_candidate(content: str) -> bool:
    """Return whether text contains any URL-like scheme token, valid or malformed."""

    return _URL_CANDIDATE_RE.search(content) is not None


def _clauses(content: str) -> tuple[str, ...]:
    return tuple(
        clause.strip(" ,") for clause in _CLAUSE_BOUNDARY_RE.split(content) if clause.strip(" ,")
    )


def _normalized(clause: str) -> str:
    return " ".join(clause.casefold().split())


def _is_narrative_or_negative_launch(clause: str) -> bool:
    text = _normalized(clause)
    patterns = (
        r"^(?:this\s+is(?:n't|\s+not)|i\s+do(?:n't|\s+not)\s+want|i\s+am\s+not\s+asking)\b",
        r"^(?:do\s+not\s+obey|ignore\b|(?:the\s+)?(?:docs?|documentation)\s+(?:say|says|said))\b",
        r"\b(?:only\s+an?\s+example|is\s+an?\s+example|is\s+the\s+heading)\b",
        r"^(?:security\s+assessment\s+(?:documentation|status)|merlin\s+scan\s+report|"
        r"red[- ]team\s+assessment\s+results)\b",
        r"^assessment\s+against\b.*\balready\s+complete\b",
        r"^(?:explain\b|why\b|is\b|are\b|should\b|if\b|when\b|after\b|before\b)",
        r"\b(?:do\s+not|don't|never)\b.{0,32}\b(?:assess|scan|evaluate|run|use|start|launch|create)\b",
        r"\b(?:no|without)\b.{0,40}\b(?:new|fresh|another|start(?:ing)?|launch(?:ing)?)\b"
        r".{0,40}\b" + _TARGET_NOUNS + r"\b",
    )
    return any(re.search(pattern, text) is not None for pattern in patterns)


def _is_explicit_launch_clause(clause: str) -> bool:
    text = _normalized(clause)
    if not text or text.startswith("/") or _is_narrative_or_negative_launch(clause):
        return False
    if _CONTINUATION_RE.match(text):
        return _FRESH_RE.search(text) is not None
    lead = (
        r"^(?:(?:ok(?:ay)?|yes|now|go\s+ahead(?:\s+and)?|do\s+not\s+delay|don't\s+delay|without\s+delay)"
        r"\s*[, :]\s*)?(?:(?:please|kindly)\s+)?"
        r"(?:(?:can|could|would|will)\s+(?:you|merlin)\s+(?:(?:please|kindly)\s+)?|"
        r"(?:i|we)\s+(?:want|need)\s+(?:(?:you|merlin)\s+to|to)\s+|"
        r"i(?:'d|’d|\s+would)\s+like\s+(?:(?:you|merlin)\s+to|to)\s+|"
        r"(?:let's|let\s+us)\s+|(?:please\s+)?help\s+(?:me\s+)?(?:to\s+)?)?"
    )
    directive = (
        r"(?:assess\b|scan\b|evaluate\b|(?:run|use)\s+merlin\b|"
        r"run\s+(?:a\s+)?(?:(?:full|quick|exhaustive)\s+)?"
        r"(?:merlin|red[ -]?pen|red[- ]team)\s+scan\b|"
        r"run\s+(?:a\s+)?red[- ]team\b|"
        r"(?:start|launch|create|begin|perform|conduct)\b.{0,48}\b"
        + _TARGET_NOUNS
        + r"\b|assessment\s+against\b|merlin\s+scan\b|campaign\s+profile\b|"
        r"red[ -]?pen\s+scan\b|red[- ]team\s+(?:scan|assessment)\b|"
        r"security\s+(?:assessment|evaluation)\b)"
    )
    matched = re.search(lead + directive, text) is not None
    if (
        matched
        and re.search(r"\b(?:assess|scan|evaluate)\b", text)
        and not has_url_candidate(clause)
        and re.search(r"\btarget\b", text) is None
    ):
        return False
    return matched


def _is_cancellation_clause(clause: str) -> bool:
    text = _normalized(clause)
    if re.search(r"\b(?:do\s+not|don't|never)\s+(?:cancel|abort|stop|end|terminate)\b", text):
        return False
    if any(
        phrase in text
        for phrase in ("continue to the end", "end-to-end", "abort handling", "stop condition")
    ):
        return False
    return (
        re.search(
            r"^(?:please\s+)?(?:cancel|abort|stop|end|terminate)"
            r"(?:\s+(?:this|the|current|old))?\s+(?:campaign|scan|assessment|job|work|testing)\b|"
            r"^(?:cancel|abort|stop|end|terminate)(?:\s+now)?[.!]?$",
            text,
        )
        is not None
    )


def _auth_state(clause: str) -> Literal["affirmative", "failed", "none"]:
    text = _normalized(clause)
    if re.match(r"^(?:if|when|after|before)\b", text) or text.endswith("?"):
        return (
            "failed"
            if re.search(r"\b(?:login|auth|authentication|mfa|captcha|logged|signed)\b", text)
            else "none"
        )
    if re.search(
        r"\b(?:session\s+expired|logged\s+out|signed\s+out|login\s+(?:failed|did\s+not\s+succeed)|"
        r"auth(?:entication)?\s+(?:failed|was\s+not\s+successful|is\s+unfinished)|"
        r"(?:login|auth|authentication|mfa|captcha)\b.{0,20}\b(?:not|never)\b.{0,12}"
        r"\b(?:complete|completed|done|successful|passed|solved|finished)|"
        r"mfa\s+(?:failed|did\s+not\s+pass)|captcha\s+(?:failed|was\s+not\s+solved)|"
        r"(?:not|never|haven't|hasn't|hadn't)\b.{0,24}\b(?:logged|signed)\s+in|"
        r"not\s+yet\s+(?:logged|signed)\s+in|"
        r"(?:could\s+not|couldn't|cannot|can't|unable\s+to|failed\s+to)\b.{0,32}"
        r"\b(?:get\s+)?(?:logged|signed)\s+in|"
        r"(?:did\s+not|didn't)\b.{0,24}\b(?:complete|finish|pass|solve)\b.{0,20}"
        r"\b(?:login|auth|authentication|mfa|captcha)\b)",
        text,
    ):
        return "failed"
    affirmative = re.search(
        r"^(?:(?:i(?:\s+am|'m|\s+have|'ve)|we(?:\s+are|'re|\s+have|'ve))\s+|now\s+)?"
        r"(?:(?:successfully\s+)?(?:signed|logged)\s+in\b|login\s+succeeded\b|"
        r"authentication\s+(?:was\s+)?successful\b|"
        r"(?:login|auth|authentication|mfa|captcha)\b.{0,40}\b(?:complete|completed|done|"
        r"succeeded|successful|passed|solved|finished)|(?:i|we)\s+"
        r"(?:completed|finished|solved|passed)\b.{0,20}"
        r"\b(?:login|auth|authentication|mfa|captcha)|(?:hand|give|return|take)\b.{0,80}"
        r"\bbrowser\b.{0,40}\bback\b)",
        text,
    )
    if affirmative:
        return "affirmative"
    return "none"


def _is_denied_fresh_continuation(clause: str) -> bool:
    text = _normalized(clause)
    return (
        _CONTINUATION_RE.match(text) is not None
        and re.search(
            r"\b(?:no|without)\b.{0,48}\b(?:new|fresh|another|start(?:ing)?|launch(?:ing)?)\b"
            r".{0,48}\b" + _TARGET_NOUNS + r"\b",
            text,
        )
        is not None
    )


def campaign_coordination_intent(content: str) -> CampaignCoordinationIntent:
    """Reduce ordered clauses to one fail-closed campaign coordination intent."""

    result = CampaignCoordinationIntent("barrier", content.strip())
    auth: Literal["affirmative", "failed", "none"] = "none"
    cancellation = False
    barrier = False
    saw_launch = False
    for clause in _clauses(content):
        if _is_explicit_launch_clause(clause):
            result = CampaignCoordinationIntent(
                "launch", clause, clause_only=barrier or cancellation or saw_launch
            )
            saw_launch = True
            auth = "none"
            cancellation = False
            barrier = False
            continue
        if _is_cancellation_clause(clause):
            result = CampaignCoordinationIntent("barrier", clause)
            auth = "none"
            cancellation = True
            barrier = True
            continue
        clause_auth = _auth_state(clause)
        if clause_auth != "none":
            auth = clause_auth
            if clause_auth == "failed":
                result = CampaignCoordinationIntent("barrier", clause)
                barrier = True
            else:
                barrier = cancellation
                if re.search(r"\b(?:continue|resume|proceed)\b", _normalized(clause)):
                    result = CampaignCoordinationIntent("handback", clause)
            continue
        if _is_denied_fresh_continuation(clause):
            result = CampaignCoordinationIntent("targeted_barrier", clause)
            barrier = True
            continue
        if _CONTINUATION_RE.match(_normalized(clause)):
            if cancellation or auth == "failed" or barrier:
                continue
            result = CampaignCoordinationIntent(
                "handback" if auth == "affirmative" else "continuation", clause
            )
            continue
        if result.kind != "launch":
            result = CampaignCoordinationIntent("barrier", clause)
            barrier = True
    return result


def is_explicit_campaign_launch(content: str) -> bool:
    """Return whether ordered clauses end in an explicit campaign launch."""

    return campaign_coordination_intent(content).kind == "launch"


def is_recon_handback_coordination(content: str) -> bool:
    """Return whether ordered clauses prove auth completion and continuation."""

    return campaign_coordination_intent(content).kind == "handback"
