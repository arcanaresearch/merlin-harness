"""Typed, deterministic execution for the reviewed Parseltongue subset.

The vendored catalogue is an inventory, not an implementation claim.  This module
keeps those two facts separate: all entries receive a reachability record, while
only transforms implemented below may be executed locally.
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import html
import json
import quopri
import re
import unicodedata
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

_LANGUAGE = re.compile(r"[a-z]{2,8}(?:-[a-z0-9]{1,8})*", re.IGNORECASE)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _alternating(value: str) -> str:
    upper = False
    result: list[str] = []
    for character in value:
        if character.isalpha():
            result.append(character.upper() if upper else character.lower())
            upper = not upper
        else:
            result.append(character)
    return "".join(result)


def _words(value: str) -> list[str]:
    return re.findall(r"[^\W_]+", value, flags=re.UNICODE)


def _snake(value: str) -> str:
    return "_".join(word.casefold() for word in _words(value))


def _kebab(value: str) -> str:
    return "-".join(word.casefold() for word in _words(value))


def _camel(value: str) -> str:
    words = _words(value)
    return "" if not words else words[0].casefold() + "".join(word.title() for word in words[1:])


def _remove_accents(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    )


def _atbash(value: str) -> str:
    source = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    target = "ZYXWVUTSRQPONMLKJIHGFEDCBAzyxwvutsrqponmlkjihgfedcba"
    return value.translate(str.maketrans(source, target))


def _rot47(value: str) -> str:
    return "".join(
        chr(33 + (ord(character) - 33 + 47) % 94) if 33 <= ord(character) <= 126 else character
        for character in value
    )


def _a1z26(value: str) -> str:
    return " ".join(
        str(ord(character.casefold()) - 96)
        if character.isascii() and character.isalpha()
        else character
        for character in value
    )


def _binary(value: str) -> str:
    return " ".join(f"{byte:08b}" for byte in value.encode())


def _unicode_points(value: str) -> str:
    return " ".join(f"U+{ord(character):04X}" for character in value)


def _fullwidth(value: str) -> str:
    return "".join(
        "\u3000"
        if character == " "
        else chr(ord(character) + 0xFEE0)
        if 0x21 <= ord(character) <= 0x7E
        else character
        for character in value
    )


def _leetspeak(value: str) -> str:
    return value.translate(
        str.maketrans(
            {
                "a": "4",
                "A": "4",
                "e": "3",
                "E": "3",
                "i": "1",
                "I": "1",
                "o": "0",
                "O": "0",
                "s": "5",
                "S": "5",
                "t": "7",
                "T": "7",
            }
        )
    )


def _disemvowel(value: str) -> str:
    return re.sub(r"[aeiouAEIOU]", "", value)


def _homoglyph(value: str) -> str:
    return value.translate(
        str.maketrans(
            {
                "A": "Α",
                "B": "Β",
                "E": "Ε",
                "H": "Η",
                "K": "Κ",
                "M": "Μ",
                "O": "Ο",
                "P": "Ρ",
                "T": "Τ",
                "X": "Χ",
                "a": "а",
                "c": "с",
                "e": "е",
                "o": "о",
                "p": "р",
                "x": "х",
            }
        )
    )


_EXECUTORS: Mapping[str, Callable[[str], str]] = {
    "case/alternating-case": _alternating,
    "case/camel-case": _camel,
    "case/capitalize-words": str.title,
    "case/kebab-case": _kebab,
    "case/lowercase-all": str.lower,
    "case/sentence-case": str.capitalize,
    "case/snake-case": _snake,
    "case/title-case": str.title,
    "case/uppercase-all": str.upper,
    "case/uppercase-lowercase": str.swapcase,
    "cipher/a1z26": _a1z26,
    "cipher/atbash": _atbash,
    "cipher/rot13": lambda value: codecs.encode(value, "rot_13"),
    "cipher/rot47": _rot47,
    "encoding/ascii85": lambda value: base64.a85encode(value.encode()).decode("ascii"),
    "encoding/base32": lambda value: base64.b32encode(value.encode()).decode("ascii"),
    "encoding/base64": lambda value: base64.b64encode(value.encode()).decode("ascii"),
    "encoding/base64url": lambda value: base64.urlsafe_b64encode(value.encode()).decode("ascii"),
    "encoding/binary": _binary,
    "encoding/hex": lambda value: value.encode().hex(),
    "encoding/html": html.escape,
    "encoding/quoted-printable": lambda value: quopri.encodestring(value.encode()).decode("ascii"),
    "encoding/unicode-points": _unicode_points,
    "encoding/url": lambda value: urllib.parse.quote(value, safe=""),
    "format/letters-numbers-only": lambda value: "".join(
        character for character in value if character.isalnum()
    ),
    "format/remove-extra-spaces": lambda value: " ".join(value.split()),
    "format/remove-newlines": lambda value: value.replace("\r", "").replace("\n", ""),
    "format/remove-numbers": lambda value: "".join(
        character for character in value if not character.isdigit()
    ),
    "format/remove-punctuation": lambda value: "".join(
        character for character in value if character.isalnum() or character.isspace()
    ),
    "format/numbers-only": lambda value: "".join(
        character for character in value if character.isdigit()
    ),
    "format/remove-accents": _remove_accents,
    "format/remove-consonants": lambda value: "".join(
        character
        for character in value
        if not character.isascii() or not character.isalpha() or character.casefold() in "aeiou"
    ),
    "format/remove-tabs": lambda value: value.replace("\t", ""),
    "format/remove-zero-width": lambda value: "".join(
        character for character in value if character not in "\u200b\u200c\u200d\ufeff"
    ),
    "format/reverse": lambda value: value[::-1],
    "format/reverse-words": lambda value: " ".join(reversed(value.split())),
    "format/spaces-remover": lambda value: "".join(value.split()),
    "unicode/fullwidth": _fullwidth,
    "visual/disemvowel": _disemvowel,
    "visual/leetspeak": _leetspeak,
    "concealment/homoglyph": _homoglyph,
}


@dataclass(frozen=True, slots=True)
class ParseltongueReachability:
    transform_id: str
    category: str
    executable: bool
    executor_id: str | None
    reason: str
    production_reachable: bool = False
    execution_mode: str = "inventory-only"
    reason_class: str = "unreviewed"
    required_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParseltongueExecution:
    schema: str
    requested_transform_id: str
    applied_transform_id: str
    requested_language: str
    actual_language: str
    executor_id: str
    source_digest: str
    output_digest: str
    output: str
    receipt_digest: str

    def __post_init__(self) -> None:
        if self.schema != "merlin.parseltongue-execution.v1":
            raise ValueError("unsupported Parseltongue execution receipt")
        if self.requested_transform_id != self.applied_transform_id:
            raise ValueError("applied Parseltongue transform differs from request")
        if any(
            _LANGUAGE.fullmatch(language) is None
            for language in (self.requested_language, self.actual_language)
        ):
            raise ValueError("Parseltongue execution requires canonical language tokens")
        unsigned = {
            "schema": self.schema,
            "requested_transform_id": self.requested_transform_id,
            "applied_transform_id": self.applied_transform_id,
            "requested_language": self.requested_language,
            "actual_language": self.actual_language,
            "executor_id": self.executor_id,
            "source_digest": self.source_digest,
            "output_digest": self.output_digest,
        }
        if self.output_digest != _digest(self.output) or self.receipt_digest != _digest(unsigned):
            raise ValueError("Parseltongue execution receipt digest mismatch")


_PRESERVATION_DEPENDENT = frozenset(
    {
        "concealment/invisible-text",
        "concealment/whitespace-steganography",
        "concealment/zerowidth-steganography",
    }
)


def parseltongue_reachability(
    transform_id: str,
    *,
    category: str = "unknown",
    capabilities: Sequence[str] = (),
) -> ParseltongueReachability:
    executable = transform_id in _EXECUTORS
    required = ("unicode_tag_preservation",) if transform_id in _PRESERVATION_DEPENDENT else ()
    capability_ready = set(required) <= {item.strip() for item in capabilities}
    # Unknown preservation support is exactly what a harmless discovery
    # attempt should measure. Keep every reviewed transform reachable and
    # attach the preservation requirement as evidence instead of suppressing
    # the candidate before target delivery.
    production_reachable = True
    execution_mode = "locally_exact" if executable else "model_mediated_verified"
    reason_class = (
        "local-deterministic"
        if executable
        else "transport-preservation-unverified"
        if required and not capability_ready
        else f"model-mediated-{category}"
    )
    return ParseltongueReachability(
        transform_id=transform_id,
        category=category,
        executable=executable,
        executor_id="merlin.parseltongue.deterministic.v1" if executable else None,
        reason=(
            "implemented locally with deterministic UTF-8 semantics"
            if executable
            else (
                "reviewed attacker-model instruction path; target preservation is verified "
                "from the delivery receipt rather than assumed before execution"
                if required and not capability_ready
                else "reviewed attacker-model instruction path; no local executor parity is claimed"
            )
        ),
        production_reachable=production_reachable,
        execution_mode=execution_mode,
        reason_class=reason_class,
        required_capabilities=required,
    )


def audit_parseltongue_catalog(
    entries: Sequence[Mapping[str, object]],
) -> tuple[ParseltongueReachability, ...]:
    """Return one honest reachability decision for every catalogue row."""
    return tuple(
        parseltongue_reachability(
            str(item.get("id") or ""), category=str(item.get("category") or "unknown")
        )
        for item in entries
    )


def execute_parseltongue(
    source: str,
    transform_id: str,
    *,
    requested_language: str = "und",
    actual_language: str = "und",
) -> ParseltongueExecution:
    """Apply exactly one reviewed local transform and seal the resulting receipt."""
    executor = _EXECUTORS.get(transform_id)
    if executor is None:
        raise ValueError(f"Parseltongue transform is not locally executable: {transform_id}")
    requested_language = requested_language.strip().casefold()
    actual_language = actual_language.strip().casefold()
    if not requested_language or not actual_language:
        raise ValueError("Parseltongue execution requires requested and actual language")
    output = executor(source)
    unsigned = {
        "schema": "merlin.parseltongue-execution.v1",
        "requested_transform_id": transform_id,
        "applied_transform_id": transform_id,
        "requested_language": requested_language,
        "actual_language": actual_language,
        "executor_id": "merlin.parseltongue.deterministic.v1",
        "source_digest": _digest(source),
        "output_digest": _digest(output),
    }
    return ParseltongueExecution(**unsigned, output=output, receipt_digest=_digest(unsigned))
