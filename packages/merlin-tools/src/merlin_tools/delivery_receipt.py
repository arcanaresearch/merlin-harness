"""Typed receipt for language and transform facts at target delivery time."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from merlin_contracts import TargetDeliveryClass, ToolContext

_TEXT_KEYS = ("message", "prompt", "input", "content", "text", "body", "messages")
_SCRIPT_RANGES = {
    "am": ((0x1200, 0x137F),),
    "ar": ((0x0600, 0x06FF),),
    "el": ((0x0370, 0x03FF),),
    "he": ((0x0590, 0x05FF),),
    "hi": ((0x0900, 0x097F),),
    "ja": ((0x3040, 0x30FF),),
    "ka": ((0x10A0, 0x10FF),),
    "ko": ((0xAC00, 0xD7AF),),
    "ru": ((0x0400, 0x04FF),),
    "th": ((0x0E00, 0x0E7F),),
    "zh": ((0x3400, 0x4DBF), (0x4E00, 0x9FFF)),
}
_LATIN_MARKERS = {
    "en": {"and", "are", "for", "is", "not", "that", "the", "this", "to", "with"},
    "es": {"con", "de", "el", "en", "es", "la", "no", "para", "que", "y"},
    "fr": {"avec", "dans", "de", "est", "et", "la", "le", "pas", "pour", "que"},
    "ms": {"anda", "dan", "dengan", "ini", "itu", "sila", "tidak", "untuk", "yang"},
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
        "mi",
        "nasin",
        "ni",
        "o",
        "ona",
        "pana",
        "pona",
        "sina",
        "taso",
        "tawa",
        "tenpo",
        "toki",
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
}


# Cyrillic/Greek letters that are visual Latin lookalikes and get abused as
# homoglyphs. Folding applies only when Cyrillic is a sparse, all-lookalike
# minority inside Latin-dominant text (homoglyph evasion like "ignоre" with
# Cyrillic о); dense genuine Cyrillic keeps its codepoints and still scores
# as ru.
_HOMOGLYPH_FOLD = str.maketrans(
    {
        "а": "a",
        "с": "c",
        "е": "e",
        "і": "i",
        "ј": "j",
        "о": "o",
        "р": "p",
        "ѕ": "s",
        "х": "x",
        "у": "y",
        "к": "k",
        "м": "m",
        "н": "h",
        "т": "t",
        "А": "A",
        "В": "B",
        "С": "C",
        "Е": "E",
        "Н": "H",
        "І": "I",
        "Ј": "J",
        "К": "K",
        "М": "M",
        "О": "O",
        "Р": "P",
        "Ѕ": "S",
        "Т": "T",
        "Х": "X",
        "У": "Y",
        "α": "a",
        "ο": "o",
        "ρ": "p",
        "υ": "u",
        "ν": "v",
    }
)
_CYRILLIC_LOOKALIKES = frozenset("асеіјорѕхукмнтАВСЕНІЈКМОРЅТХУ")


def _fold_sparse_homoglyphs(text: str) -> str:
    alpha = [character for character in text if character.isalpha()]
    cyrillic = [character for character in alpha if 0x0400 <= ord(character) <= 0x04FF]
    if (
        cyrillic
        and len(cyrillic) * 4 <= len(alpha)
        and all(character in _CYRILLIC_LOOKALIKES for character in cyrillic)
    ):
        return text.translate(_HOMOGLYPH_FOLD)
    return text


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _request_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        selected = [value[key] for key in _TEXT_KEYS if key in value]
        return "\n".join(_request_text(item) for item in (selected or value.values()))
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return "\n".join(_request_text(item) for item in value)
    return ""


def _comparison_code(value: str) -> str:
    normalized = value.strip().casefold().replace("_", "-")
    return {"lzh": "zh", "cmn": "zh", "zho": "zh", "zh-cn": "zh", "zh-tw": "zh"}.get(
        normalized, normalized
    )


def _actual_language(value: object) -> tuple[str, float | None]:
    text = _fold_sparse_homoglyphs(_request_text(value))
    if sum(character.isalpha() for character in text) < 20:
        return "und", None
    scores: dict[str, float] = {}
    for language, ranges in _SCRIPT_RANGES.items():
        count = sum(
            any(start <= ord(character) <= end for start, end in ranges) for character in text
        )
        if count >= 4:
            scores[language] = float(count)
    tokens = set(re.findall(r"[^\W\d_]+", text.casefold(), flags=re.UNICODE))
    for language, markers in _LATIN_MARKERS.items():
        score = len(tokens & markers)
        if score >= 2:
            scores[language] = scores.get(language, 0.0) + score * 2.0
    if not scores:
        return "und", None
    language, score = max(scores.items(), key=lambda item: (item[1], item[0]))
    confidence = score / sum(scores.values())
    return (language, round(confidence, 6)) if confidence >= 0.55 else ("und", confidence)


@dataclass(frozen=True, slots=True)
class TargetDeliveryReceipt:
    schema: str
    actual_transport_id: str
    actual_delivery: str
    planned_language: str
    requested_language: str
    actual_language: str
    language_evidence_status: str
    language_confidence: float | None
    language_mismatch: bool | None
    requested_transform_id: str
    applied_transform_id: str | None
    transform_bindings: tuple[tuple[str, str, str], ...]
    payload_digest: str
    receipt_digest: str

    def __post_init__(self) -> None:
        unsigned = asdict(self)
        unsigned.pop("receipt_digest")
        if self.schema != "merlin.target-delivery-receipt.v1":
            raise ValueError("unsupported target delivery receipt")
        if self.receipt_digest != _digest(unsigned):
            raise ValueError("target delivery receipt digest mismatch")


def target_delivery_receipt(
    arguments: Mapping[str, object],
    context: ToolContext,
    *,
    transport_id: str = "unknown",
    actual_delivery: TargetDeliveryClass | None = None,
) -> TargetDeliveryReceipt:
    """Attest facts observable when the exact outbound arguments are committed."""
    planned = context.planned_language.strip().casefold() or "und"
    requested = context.requested_language.strip().casefold() or "und"
    requested_transform = context.requested_transform_id.strip() or "baseline"
    actual, confidence = _actual_language(arguments)
    comparable = _comparison_code(requested)
    mismatch = None if requested == "und" or actual == "und" else comparable != actual
    applied_transform = (
        requested_transform
        if context.transform_bindings or requested_transform in {"baseline", "native"}
        else None
    )
    unsigned = {
        "schema": "merlin.target-delivery-receipt.v1",
        "actual_transport_id": transport_id,
        "actual_delivery": actual_delivery.value if actual_delivery is not None else "unknown",
        "planned_language": planned,
        "requested_language": requested,
        "actual_language": actual,
        "language_evidence_status": "observed" if actual != "und" else "unavailable",
        "language_confidence": confidence,
        "language_mismatch": mismatch,
        "requested_transform_id": requested_transform,
        "applied_transform_id": applied_transform,
        "transform_bindings": context.transform_bindings,
        "payload_digest": _digest(arguments),
    }
    return TargetDeliveryReceipt(**unsigned, receipt_digest=_digest(unsigned))
