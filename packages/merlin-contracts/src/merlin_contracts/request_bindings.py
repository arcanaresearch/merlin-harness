"""Explicit, target-independent values generated once per HTTP request."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Literal
from uuid import uuid4

RequestValueGenerator = Literal["uuid4", "hex32"]


def validate_capture_binding(template: object, pointer: object, binding_name: str) -> None:
    """Require one exact request destination for an ephemeral reviewed capture."""
    parent, key = _destination(template, pointer)
    if parent[key] != f"{{{{capture:{binding_name}}}}}":
        raise ValueError("checkpoint restore binding must address its exact capture placeholder")


def validate_input_binding(template: object, pointer: object) -> None:
    """Require one canonical input placeholder at one exact request destination."""
    parent, key = _destination(template, pointer)
    if parent[key] != "{{input}}":
        raise ValueError("content injection must address its exact canonical input placeholder")
    if _count_value(template, "{{input}}") != 1:
        raise ValueError(
            "content injection request must contain exactly one canonical input placeholder"
        )


def validate_request_bindings(
    template: object, bindings: object
) -> dict[str, RequestValueGenerator]:
    """Validate exact leaf destinations; never replace input or captured state."""
    if not isinstance(bindings, Mapping):
        raise ValueError("request_bindings must map JSON Pointers to generators")
    validated: dict[str, RequestValueGenerator] = {}
    for pointer, generator in bindings.items():
        if generator not in ("uuid4", "hex32"):
            raise ValueError("request binding generator must be uuid4 or hex32")
        parent, key = _destination(template, pointer)
        value = parent[key]
        if not isinstance(value, str) or "{{" in value:
            raise ValueError(
                "request binding must address a literal string, not input or a capture"
            )
        validated[pointer] = generator
    return validated


def bind_request_values(template: object, bindings: Mapping[str, RequestValueGenerator]) -> object:
    """Create a detached request; callers persist it before sending or retrying."""
    request = deepcopy(template)
    generated: dict[tuple[str, str], str] = {}
    for pointer, generator in bindings.items():
        parent, key = _destination(request, pointer)
        identity = (generator, parent[key])
        if identity not in generated:
            value = uuid4()
            generated[identity] = str(value) if generator == "uuid4" else value.hex
        parent[key] = generated[identity]
    return request


def _destination(value: object, pointer: object) -> tuple[dict | list, str | int]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("request binding destination must be a non-root JSON Pointer")
    tokens = pointer[1:].split("/")
    current = value
    for index, token in enumerate(tokens):
        if any(not part or part[0] not in "01" for part in token.split("~")[1:]):
            raise ValueError("request binding contains an invalid JSON Pointer escape")
        key: str | int = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not key.isdecimal() or (len(key) > 1 and key.startswith("0")):
                raise ValueError("request binding has an invalid array index")
            key = int(key)
            if key >= len(current):
                raise ValueError("request binding destination does not exist")
        elif not isinstance(current, dict) or key not in current:
            raise ValueError("request binding destination does not exist")
        if index == len(tokens) - 1:
            return current, key
        current = current[key]
    raise ValueError("request binding destination does not exist")


def _count_value(value: object, expected: object) -> int:
    if isinstance(value, Mapping):
        return sum(_count_value(item, expected) for item in value.values())
    if isinstance(value, list):
        return sum(_count_value(item, expected) for item in value)
    return int(value == expected)
