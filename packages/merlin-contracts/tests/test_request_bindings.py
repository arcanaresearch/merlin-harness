import json
from copy import deepcopy
from uuid import UUID

import pytest
from merlin_contracts.request_bindings import bind_request_values, validate_request_bindings
from merlin_contracts.target_adapter import executable_adapter_digest
from merlin_contracts.target_adapter_compiler import (
    AdapterCompileError,
    compile_target_adapter,
    compiled_target_adapter_digest,
    read_target_adapter,
)


def adapter():
    return {
        "interface": "http_api",
        "endpoint": "http://localhost:8000/exchange",
        "api_steps": [
            {
                "method": "POST",
                "interaction": True,
                "request_template": {
                    "envelope": [{"text": "{{input}}", "opaque/key": "recorded-id"}],
                    "session": "fixed-session",
                },
                "request_bindings": {"/envelope/0/opaque~1key": "uuid4"},
            }
        ],
    }


def test_nested_request_bindings_are_sealed_and_survive_round_trip():
    raw = adapter()
    compiled = compile_target_adapter(raw)
    restored = read_target_adapter(json.loads(compiled.model_dump_json()))
    assert compiled_target_adapter_digest(restored) == compiled_target_adapter_digest(compiled)
    step = restored.executable.steps[0]
    first = bind_request_values(step.request_template, step.request_bindings)
    second = bind_request_values(step.request_template, step.request_bindings)
    first_id = first["envelope"][0]["opaque/key"]
    assert UUID(first_id).version == 4
    assert first_id != second["envelope"][0]["opaque/key"]
    assert first["session"] == second["session"] == "fixed-session"
    assert raw["api_steps"][0]["request_template"]["envelope"][0]["opaque/key"] == "recorded-id"
    changed = deepcopy(raw)
    changed["api_steps"][0]["request_bindings"] = {"/session": "hex32"}
    assert executable_adapter_digest(raw) != executable_adapter_digest(changed)
    assert compiled_target_adapter_digest(compiled) != compiled_target_adapter_digest(
        compile_target_adapter(changed)
    )


@pytest.mark.parametrize(
    "bindings",
    [
        {"/missing": "uuid4"},
        {"/envelope/0/text": "uuid4"},
        {"/envelope": "uuid4"},
        {"/session": "python"},
        {"/envelope/00/opaque~1key": "uuid4"},
        {"": "uuid4"},
        {"/session~": "uuid4"},
    ],
)
def test_invalid_binding_cannot_silently_change_the_effect(bindings):
    raw = adapter()
    raw["api_steps"][0]["request_bindings"] = bindings
    with pytest.raises(AdapterCompileError):
        compile_target_adapter(raw)


def test_capture_binding_cannot_be_replaced_and_hex_format_is_supported():
    with pytest.raises(ValueError, match="capture"):
        validate_request_bindings({"session": "{{capture:session_id}}"}, {"/session": "uuid4"})
    result = bind_request_values({"nonce": "observed"}, {"/nonce": "hex32"})
    assert len(result["nonce"]) == 32 and UUID(result["nonce"]).version == 4


def test_generated_aliases_keep_their_observed_relationship():
    template = {"a": "observed-id", "b": ["observed-id"], "constant": "observed-id"}
    bindings = {"/a": "uuid4", "/b/0": "uuid4"}
    first = bind_request_values(template, bindings)
    second = bind_request_values(template, bindings)
    assert first["a"] == first["b"][0] and first["a"] != second["a"]
    assert first["constant"] == "observed-id"


def test_persisted_compilation_cannot_bypass_binding_validation():
    payload = json.loads(compile_target_adapter(adapter()).model_dump_json())
    payload["executable"]["steps"][0]["request_bindings"] = {"/session/missing": "uuid4"}
    with pytest.raises(AdapterCompileError):
        read_target_adapter(payload)


def test_empty_bindings_preserve_legacy_digests():
    raw = adapter()
    raw["api_steps"][0].pop("request_bindings")
    compiled = compile_target_adapter(raw)
    # Reconstruct the serialization used before request bindings existed.
    import hashlib

    payload = compiled.executable.model_dump(mode="json")
    for step in payload["steps"]:
        step.pop("request_bindings", None)
    legacy_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert compiled_target_adapter_digest(compiled) == legacy_digest
    changed = deepcopy(raw)
    changed["api_steps"][0]["request_bindings"] = {}
    assert executable_adapter_digest(raw) == executable_adapter_digest(changed)
