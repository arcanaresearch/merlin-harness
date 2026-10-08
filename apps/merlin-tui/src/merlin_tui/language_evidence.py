"""Deterministic observations of the language actually sent to a target."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_TEXT_KEYS = (
    "message",
    "prompt",
    "input",
    "content",
    "text",
    "body",
    "messages",
)
_MIN_LETTERS = 20
_VERIFY_CONFIDENCE = 0.55
_MISMATCH_CONFIDENCE = 0.80
_SCRIPT_RANGES = {
    "am": ((0x1200, 0x137F),),
    "ar": ((0x0600, 0x06FF),),
    "bn": ((0x0980, 0x09FF),),
    "el": ((0x0370, 0x03FF),),
    "gu": ((0x0A80, 0x0AFF),),
    "he": ((0x0590, 0x05FF),),
    "hi": ((0x0900, 0x097F),),
    "ja": ((0x3040, 0x30FF),),
    "ka": ((0x10A0, 0x10FF),),
    "km": ((0x1780, 0x17FF),),
    "kn": ((0x0C80, 0x0CFF),),
    "ko": ((0xAC00, 0xD7AF),),
    "ml": ((0x0D00, 0x0D7F),),
    "my": ((0x1000, 0x109F),),
    "pa": ((0x0A00, 0x0A7F),),
    "ru": ((0x0400, 0x04FF),),
    "si": ((0x0D80, 0x0DFF),),
    "ta": ((0x0B80, 0x0BFF),),
    "te": ((0x0C00, 0x0C7F),),
    "th": ((0x0E00, 0x0E7F),),
    "zh": ((0x3400, 0x4DBF), (0x4E00, 0x9FFF)),
}
# Letters that occur in Urdu orthography but not Arabic; presence reassigns the
# Arabic-script score to "ur".
_URDU_ONLY_CHARS = frozenset("ےںٹڈڑھ")
# Marathi shares Devanagari with Hindi; a couple of distinct function words
# reassign the score to "mr".
_MR_MARKERS = frozenset({"आहे", "नाही", "मी", "तुम्ही", "आणि", "मध्ये", "करु", "आहेत"})
# Letters that occur in Persian orthography but not Arabic; presence reassigns
# the Arabic-script score to "fa" (Urdu reassignment above takes precedence).
_PERSIAN_ONLY_CHARS = frozenset("پچگژ")
# Letters that occur in Ukrainian orthography but not Russian; presence
# reassigns the Cyrillic score to "uk".
_UKRAINIAN_ONLY_CHARS = frozenset("іїєґ")
# Nepali shares Devanagari with Hindi and Marathi; a couple of distinct
# function words reassign the score to "ne".
_NE_MARKERS = frozenset({"छ", "हो", "म", "तपाईं", "र", "को", "गर्न"})
_LATIN_MARKERS = {
    "az": {"bu", "deyil", "ilə", "ki", "mən", "və", "üçün"},
    "cs": {"aby", "jako", "je", "který", "nebo", "pro", "se", "že"},
    "da": {"at", "det", "er", "ikke", "med", "og", "på", "som"},
    "de": {"aber", "das", "der", "die", "ist", "mit", "nicht", "und"},
    "en": {"and", "are", "for", "is", "not", "that", "the", "this", "to", "with"},
    "es": {"con", "de", "el", "en", "es", "la", "no", "para", "que", "y"},
    "fi": {"ei", "ja", "joka", "kun", "myös", "on", "se", "tai"},
    "fr": {"avec", "dans", "de", "est", "et", "la", "le", "pas", "pour", "que"},
    "ha": {"a", "ba", "ce", "da", "don", "kada", "na", "ne"},
    "id": {"anda", "dan", "dengan", "ini", "itu", "tidak", "untuk", "yang"},
    "it": {"che", "con", "di", "il", "la", "non", "per", "un", "una"},
    "ig": {"bụ", "ga", "gị", "ma", "maka", "na", "nke", "ọ", "ya"},
    "jv": {"aku", "iki", "ing", "kanggo", "lan", "ora", "sing", "wis"},
    "la": {"cum", "et", "est", "in", "non", "quod", "sed", "sunt", "ut"},
    "ms": {"anda", "dan", "dengan", "ini", "itu", "sila", "tidak", "untuk", "yang"},
    "nl": {"dat", "de", "een", "en", "het", "is", "met", "niet", "voor"},
    "no": {"at", "det", "er", "ikke", "med", "og", "på", "som"},
    "pl": {"ale", "dla", "jest", "nie", "oraz", "się", "to", "że"},
    "pt": {"com", "de", "e", "em", "não", "o", "para", "que", "uma"},
    "ro": {"cu", "de", "este", "la", "nu", "pentru", "și", "un"},
    "sv": {"att", "det", "en", "inte", "med", "och", "på", "som"},
    "sw": {"ili", "kwa", "na", "safari", "samahani", "tafadhali", "ya", "yako"},
    "tok": {
        "e",
        "ike",
        "jan",
        "jo",
        "ken",
        "kepeken",
        "la",
        "li",
        "lipu",
        "lon",
        "mani",
        "mi",
        "moli",
        "nasin",
        "ni",
        "o",
        "ona",
        "pana",
        "pini",
        "pona",
        "sina",
        "sona",
        "taso",
        "tawa",
        "tenpo",
        "toki",
        "weka",
        "wile",
    },
    "tpi": {
        "bekim",
        "bilong",
        "dispela",
        "em",
        "harim",
        "inap",
        "long",
        "mi",
        "nogat",
        "ol",
        "tasol",
        "wanem",
        "wantaim",
        "yu",
    },
    "tl": {"ang", "at", "ay", "hindi", "ito", "mga", "ng", "para", "sa"},
    "tr": {"ama", "bir", "bu", "değil", "için", "ile", "ve", "ya"},
    "uz": {"bilan", "bu", "emas", "men", "ning", "siz", "uchun", "va"},
    "vi": {"cho", "của", "không", "là", "một", "này", "trong", "và"},
    "yo": {"ati", "fun", "jẹ", "kò", "ni", "ní", "ṣe", "ti", "rẹ"},
}


def observe_request_language(request: object, planned_language: str) -> dict[str, Any]:
    """Compare a planned language with the language in the durable target request.

    Short or ambiguous inputs remain unverified. A mismatch is asserted only when
    the detector has strong evidence for a different language; this prevents a
    guessed label from becoming report authority.
    """
    planned = _planned_language_set(planned_language)
    text = _request_text(request)
    letters = sum(character.isalpha() for character in text)
    if not planned or letters < _MIN_LETTERS:
        return {
            "status": "unverified",
            "planned_language": planned_language.strip().casefold() or "und",
            "observed_language": None,
            "confidence": None,
        }
    candidates = _detect_candidates(text)
    if not candidates:
        return {
            "status": "unverified",
            "planned_language": planned_language.strip().casefold() or "und",
            "observed_language": None,
            "confidence": None,
        }
    observed, confidence = candidates[0]
    matched = {code for code, probability in candidates if probability >= 0.10} & planned
    if len(planned) > 1:
        status = "verified" if len(matched) > 1 else "unverified"
    elif matched and confidence >= _VERIFY_CONFIDENCE:
        status = "verified"
    elif confidence >= _MISMATCH_CONFIDENCE:
        status = "mismatch"
    else:
        status = "unverified"
    return {
        "status": status,
        "planned_language": planned_language.strip().casefold() or "und",
        "observed_language": observed,
        "confidence": confidence,
        "candidates": tuple(
            {"language": code, "confidence": probability} for code, probability in candidates[:3]
        ),
    }


def _request_text(value: object) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"{", "["}:
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return stripped
            return _request_text(decoded)
        return stripped
    if isinstance(value, Mapping):
        selected = [value[key] for key in _TEXT_KEYS if key in value]
        values = selected or list(value.values())
        return "\n".join(filter(None, (_request_text(item) for item in values)))
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return "\n".join(filter(None, (_request_text(item) for item in value)))
    return ""


def _planned_language_set(value: str) -> set[str]:
    normalized = value.strip().casefold()
    if normalized in {"", "und"}:
        return set()
    parts = re.split(r"[-_+]", normalized.removesuffix("-mixed"))
    return {_comparison_code(part) for part in parts if len(part) in {2, 3}}


def _normalize_code(value: str) -> str:
    normalized = value.strip().casefold().replace("_", "-")
    return {"zh-cn": "zh", "zh-tw": "zh", "iw": "he"}.get(normalized, normalized)


def _comparison_code(value: str) -> str:
    normalized = _normalize_code(value)
    return {"lzh": "zh", "cmn": "zh", "zho": "zh"}.get(normalized, normalized)


def _detect_candidates(text: str) -> tuple[tuple[str, float], ...]:
    """Return conservative deterministic candidates without a runtime dependency."""
    scores: dict[str, float] = {}
    for language, ranges in _SCRIPT_RANGES.items():
        count = sum(
            any(start <= ord(character) <= end for start, end in ranges) for character in text
        )
        if count >= 4:
            scores[language] = float(count)

    tokens = re.findall(r"[^\W\d_]+", text.casefold(), flags=re.UNICODE)
    token_set = set(tokens)

    # Urdu and Marathi share scripts with Arabic and Hindi; reassign the
    # script score when the orthography/function words disambiguate.
    if "ar" in scores and any(character in _URDU_ONLY_CHARS for character in text):
        scores["ur"] = scores.pop("ar")
    if "ar" in scores and any(character in _PERSIAN_ONLY_CHARS for character in text):
        scores["fa"] = scores.pop("ar")
    if "ru" in scores and any(character in _UKRAINIAN_ONLY_CHARS for character in text):
        scores["uk"] = scores.pop("ru")
    devanagari_words = set(re.findall(r"[\u0900-\u097F]+", text))
    if "hi" in scores and len(devanagari_words & _MR_MARKERS) >= 2:
        scores["mr"] = scores.pop("hi")
    if "hi" in scores and len(devanagari_words & _NE_MARKERS) >= 2:
        scores["ne"] = scores.pop("hi")

    for language, markers in _LATIN_MARKERS.items():
        score = len(token_set & markers)
        if score >= 2:
            scores[language] = scores.get(language, 0.0) + score * 2.0

    if not scores:
        return ()
    total = sum(scores.values())
    return tuple(
        (language, round(score / total, 6))
        for language, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:3]
    )
