"""Shared, ordered classification for campaign coordination messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_URL_CANDIDATE_RE = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_HTTP_URL_RE = re.compile(r"https?://(?:\[[0-9a-f:.]+\]|[^\s<>\]\[(){}\"']+)+", re.IGNORECASE)
_CLAUSE_BOUNDARY_RE = re.compile(
    r"\s*;\s*|(?<=[.!?])\s+|"
    r"\s*,?\s+\b(?:but|however|instead)\b\s+|"
    r"\s*,\s*(?:and\s+)?(?:then|yet)\s+|\s+and\s+now\s+|"
    r"\s+and\s+(?=(?:assess|scan|evaluate|run|use|start|launch|create|"
    r"cancel|abort|stop|end|terminate)\b)",
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
    target_clause: str | None = None


def has_url_candidate(content: str) -> bool:
    """Return whether text contains any URL-like scheme token, valid or malformed."""

    return _URL_CANDIDATE_RE.search(content) is not None


def _clauses(content: str) -> tuple[str, ...]:
    content = re.sub(r'"[^"\n]*"|“[^”\n]*”|(?<!\w)\'[^\'\n]+\'(?!\w)', " ", content)
    urls: list[str] = []

    def protect_url(match: re.Match[str]) -> str:
        url = match.group(0)
        suffix = ""
        while url.endswith((".", ",", "!", ";")):
            suffix = url[-1] + suffix
            url = url[:-1]
        token = f"MERLINURLTOKEN{len(urls)}"
        urls.append(url)
        return token + suffix

    protected = _HTTP_URL_RE.sub(protect_url, content)
    clauses = tuple(
        clause.strip(" ,") for clause in _CLAUSE_BOUNDARY_RE.split(protected) if clause.strip(" ,")
    )
    return tuple(
        re.sub(
            r"MERLINURLTOKEN(\d+)",
            lambda match: urls[int(match.group(1))],
            clause,
        )
        for clause in clauses
    )


def _normalized(clause: str) -> str:
    return " ".join(clause.casefold().replace("’", "'").split())


def _is_narrative_or_negative_launch(clause: str) -> bool:
    text = _normalized(clause)
    patterns = (
        r"^(?:this\s+is(?:n't|\s+not)|i\s+do(?:n't|\s+not)\s+want|i\s+am\s+not\s+asking)\b",
        r"^(?:do\s+not\s+obey|ignore\b|(?:the\s+)?(?:docs?|documentation)\s+(?:say|says|said))\b",
        r"\b(?:only\s+an?\s+example|is\s+an?\s+example|is\s+the\s+heading)\b",
        r"^(?:security\s+(?:assessment|evaluation)|merlin\s+scan|"
        r"red[- ]team\s+(?:scan|assessment))\s+"
        r"(?:documentation|report|results?|summary|status)\b",
        r"^(?:security\s+(?:assessment|evaluation)|merlin\s+scan|"
        r"red[- ]team\s+(?:scan|assessment)|assessment\s+against\b.+)\b.*"
        r"\b(?:was\s+cancelled|(?:is\s+)?not\s+needed|should\s+not\s+run)\b",
        r"^assessment\s+against\b.*\balready\s+complete\b",
        r"^(?:explain\b|why\b|is\b|are\b|should\b|if\b|after\b|before\b|"
        r"when\b(?!\s+ready\s*,))",
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
        return (
            re.match(
                r"^(?:please\s+)?(?:continue|resume|retry)\s+(?:with\s+)?(?:a|an)?\s*"
                r"(?:new|fresh|another)\s+"
                r"(?:(?:ai|application|security|red[- ]team|merlin|targeted|full|quick|"
                r"exhaustive)\s+){0,3}"
                + _TARGET_NOUNS
                + r"(?!\s+(?:documentation|report|results?|summary|status))",
                text,
            )
            is not None
        )
    lead = (
        r"^(?:(?:when\s+ready\s*,|ok(?:ay)?|yes|now|go\s+ahead(?:\s+and)?|"
        r"do\s+not\s+delay|don't\s+delay|without\s+delay)"
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
        r"run\s+(?:a\s+)?(?:new|fresh|another)\s+"
        r"(?:(?:ai|application|security|red[- ]team|merlin|targeted|full|quick|"
        r"exhaustive)\s+){0,3}" + _TARGET_NOUNS + r"\b|"
        r"(?:run|start|launch|create|begin|perform|conduct)\b.{0,48}\b"
        + _TARGET_NOUNS
        + r"\b|assessment\s+against\b|merlin\s+scan\b|campaign\s+profile\b|"
        r"red[ -]?pen\s+scan\b|red[- ]team\s+(?:scan|assessment)\b|"
        r"security\s+(?:assessment|evaluation)\b)"
    )
    matched = re.search(lead + directive, text) is not None
    if matched and not has_url_candidate(clause) and re.search(r"\btarget\b", text) is None:
        return False
    return matched


def _is_cancellation_clause(clause: str) -> bool:
    text = _normalized(clause)
    if re.search(r"\b(?:do\s+not|don't|never)\s+(?:cancel|abort|stop|end|terminate)\b", text):
        return False
    return (
        re.search(
            r"^(?:please\s+)?(?:cancel|abort|stop|end|terminate)"
            r"(?:\s+(?:this|the|current|old))?\s+(?:campaign|scan|assessment|job|work|testing)\b|"
            r"^(?:please\s+)?(?:cancel|abort|stop|end|terminate)"
            r"(?:\s+(?:this|the|current|old))?\s+(?:new|fresh|another)\s+"
            r"(?:campaign|scan|assessment|evaluation)\b|"
            r"^(?:please\s+)?(?:cancel|abort|stop|end|terminate)\s+it\b|"
            r"^(?:please\s+)?(?:cancel|abort|stop|end|terminate)\s+"
            r"(?:(?:this|that|the)\s+)?launch\b|"
            r"^(?:cancel|abort|stop|end|terminate)(?:\s+now)?[.!]?$",
            text,
        )
        is not None
    )


def _is_negative_cancellation_clause(clause: str) -> bool:
    return (
        re.match(
            r"^(?:please\s+)?(?:do\s+not|don't|never)\s+"
            r"(?:cancel|abort|stop|end|terminate)(?:\s+(?:this|the|current)\s+"
            r"(?:campaign|scan|assessment|job|work|testing))?[.!]?$",
            _normalized(clause),
        )
        is not None
    )


def _is_continuation_clause(clause: str) -> bool:
    text = _normalized(clause)
    return (
        _CONTINUATION_RE.match(text) is not None
        or re.match(
            r"^(?:stop\s+waiting|end\s+the\s+wait|stop\s+the\s+delay)\s+and\s+"
            r"(?:please\s+)?(?:continue|resume|proceed)\b",
            text,
        )
        is not None
    )


def _has_target_evidence(clause: str) -> bool:
    return has_url_candidate(clause) or re.search(r"\btarget\b", _normalized(clause)) is not None


def _has_command_continuation(clause: str) -> bool:
    return (
        re.search(
            r"(?:^|,\s*|\band\s+(?:then\s+)?)(?:please\s+)?"
            r"(?:continue|resume|proceed)\b",
            _normalized(clause),
        )
        is not None
    )


def _is_retraction_clause(clause: str) -> bool:
    text = _normalized(clause)
    return any(
        re.match(pattern, text) is not None
        for pattern in (
            r"^(?:actually\s+|no\s*,?\s*)?(?:cancel|abort)[.!]?$",
            r"^(?:do\s+not|don't|never)\s+(?:continue|proceed)\b",
            r"^(?:never\s+mind|nevermind|forget\s+it|scratch\s+that|disregard\s+that)\b",
            r"^(?:please\s+)?explain\s+(?:(?:the|current|runtime)\s+)?"
            r"status(?:\s+instead)?[.!]?$",
            r"^(?:actually\s*,?\s*)?(?:do\s+not|don't|never)\s+"
            r"(?:assess|scan|evaluate|run)\s+"
            r"(?:(?:this|the)\s+target|it)\b",
            r"^this\s+was\s+only\s+an?\s+example\b",
        )
    )


def _auth_state(clause: str) -> Literal["affirmative", "failed", "none"]:
    text = _normalized(clause)
    narrative_text = re.sub(r"[a-z][a-z0-9+.-]*://\S+", "", text)
    auth_words = re.search(
        r"\b(?:login|auth|authentication|authenticated|mfa|captcha|logged|signed|browser)\b",
        text,
    )
    if auth_words and re.search(
        r"\b(?:maybe|example|report|users?|people|tests?|checks?|errors?)\b", narrative_text
    ):
        return "failed"
    if auth_words and re.search(r"\b(?:if|unless|when)\b", narrative_text):
        return "failed"
    if re.match(r"^(?:if|when|after|before)\b", text) or text.endswith("?"):
        return (
            "failed"
            if re.search(r"\b(?:login|auth|authentication|mfa|captcha|logged|signed)\b", text)
            else "none"
        )
    if re.search(
        r"\b(?:session\s+expired|logged\s+out|signed\s+out|login\s+(?:failed|did\s+not\s+succeed)|"
        r"(?:login|auth|authentication|mfa|captcha)\b.{0,20}\b"
        r"(?:isn't|wasn't|aren't|weren't)\b.{0,12}\b"
        r"(?:complete|completed|done|successful|passed|solved|finished)|"
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
        r"^(?:(?:okay|thanks)\s*,\s*)?(?:please\s+)?"
        r"(?:(?:i(?:\s+am|'m|\s+have|'ve)|we(?:\s+are|'re|\s+have|'ve)|i|we)\s+|now\s+)?"
        r"(?:(?:i've|we've)\s+(?:(?:completed|finished)\s+(?:login|authentication)|"
        r"passed\s+mfa)\b|(?:successfully\s+)?(?:signed|logged)\s+in\b"
        r"(?!\s+(?:users?|people|accounts?))|login\s+succeeded\b|"
        r"i\s+authenticated\s+successfully\b|done\s+with\s+mfa\b|"
        r"the\s+browser\s+is\s+yours\s+again\b|"
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


def _is_discussion_continuation_clause(clause: str) -> bool:
    return (
        re.match(
            r"^(?:please\s+)?(?:continue|resume|retry)\s+"
            r"(?:(?:reading|discussing|parsing|reviewing)\b|"
            r"(?:with\s+)?(?:a|an)?\s*(?:new|fresh|another)\b.{0,48}"
            r"\b(?:documentation|report|results?|summary|status)\b)",
            _normalized(clause),
        )
        is not None
    )


def campaign_coordination_intent(content: str) -> CampaignCoordinationIntent:
    """Reduce ordered clauses to one fail-closed campaign coordination intent."""

    result = CampaignCoordinationIntent("barrier", content.strip())
    auth: Literal["affirmative", "failed", "none"] = "none"
    auth_clause: str | None = None
    cancellation = False
    barrier = False
    saw_launch = False
    for clause in _clauses(content):
        if _is_explicit_launch_clause(clause):
            result = CampaignCoordinationIntent(
                "launch",
                clause,
                clause_only=barrier or cancellation or saw_launch,
                target_clause=clause,
            )
            saw_launch = True
            auth = "none"
            cancellation = False
            barrier = False
            continue
        if _is_cancellation_clause(clause):
            result = CampaignCoordinationIntent("barrier", clause, target_clause=clause)
            auth = "none"
            auth_clause = None
            cancellation = True
            barrier = True
            continue
        if _is_negative_cancellation_clause(clause):
            continue
        if _is_retraction_clause(clause):
            result = CampaignCoordinationIntent("barrier", clause, target_clause=clause)
            auth = "none"
            auth_clause = None
            cancellation = True
            barrier = True
            continue
        clause_auth = _auth_state(clause)
        if clause_auth != "none":
            auth = clause_auth
            if clause_auth == "failed":
                result = CampaignCoordinationIntent("barrier", clause, target_clause=clause)
                barrier = True
            else:
                barrier = cancellation
                if auth_clause is None or _has_target_evidence(clause):
                    auth_clause = clause
                if (
                    not cancellation
                    and result.kind != "launch"
                    and _has_command_continuation(clause)
                ):
                    result = CampaignCoordinationIntent(
                        "handback", clause, target_clause=auth_clause or clause
                    )
            continue
        if _is_denied_fresh_continuation(clause):
            if not barrier and not cancellation:
                result = CampaignCoordinationIntent(
                    "targeted_barrier", clause, target_clause=clause
                )
            barrier = True
            continue
        if _is_discussion_continuation_clause(clause):
            result = CampaignCoordinationIntent("barrier", clause, target_clause=clause)
            barrier = True
            continue
        if _is_continuation_clause(clause):
            if cancellation or auth == "failed" or barrier:
                continue
            if result.kind == "launch":
                continue
            result = CampaignCoordinationIntent(
                "handback" if auth == "affirmative" else "continuation",
                clause,
                target_clause=(clause if _has_target_evidence(clause) else auth_clause or clause)
                if auth == "affirmative"
                else clause,
            )
            continue
        if result.kind != "launch":
            result = CampaignCoordinationIntent("barrier", clause, target_clause=clause)
            barrier = True
    return result


def is_explicit_campaign_launch(content: str) -> bool:
    """Return whether ordered clauses end in an explicit campaign launch."""

    return campaign_coordination_intent(content).kind == "launch"


def is_recon_handback_coordination(content: str) -> bool:
    """Return whether ordered clauses prove auth completion and continuation."""

    return campaign_coordination_intent(content).kind == "handback"
