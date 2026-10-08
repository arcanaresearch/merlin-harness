import pytest
from merlin_contracts import ModelJsonError, parse_model_json, strip_code_fences


def test_parse_bare_json() -> None:
    assert parse_model_json('{"decision": "continue"}') == {"decision": "continue"}


def test_parse_fenced_json_with_language_tag() -> None:
    text = '```json\n{"decision": "continue", "wave": 1}\n```'
    assert parse_model_json(text) == {"decision": "continue", "wave": 1}


def test_parse_fenced_json_without_language_tag() -> None:
    text = "```\n[1, 2, 3]\n```"
    assert parse_model_json(text) == [1, 2, 3]


def test_parse_json_padded_with_prose() -> None:
    text = 'Here is my decision:\n{"decision": "stop"}\nHope that helps.'
    assert parse_model_json(text) == {"decision": "stop"}


def test_parse_markdown_heading_before_fence() -> None:
    # Observed live from claude-haiku: a Markdown heading precedes the fence.
    text = '# Campaign Decision: Attempt 2\n\n```json\n{"decision": "extend"}\n```\n'
    assert parse_model_json(text) == {"decision": "extend"}


def test_strip_code_fences_leaves_plain_text_alone() -> None:
    assert strip_code_fences('{"a": 1}') == '{"a": 1}'


def test_strip_code_fences_without_closing_fence() -> None:
    assert strip_code_fences('```json\n{"a": 1}') == '{"a": 1}'


def test_junk_raises_error_quoting_reply_head() -> None:
    with pytest.raises(ModelJsonError) as excinfo:
        parse_model_json("I cannot help with that request, sorry.")
    message = str(excinfo.value)
    assert "reply head" in message
    assert "I cannot help" in message


def test_empty_reply_raises_error_with_visible_head() -> None:
    with pytest.raises(ModelJsonError) as excinfo:
        parse_model_json("")
    assert "reply head" in str(excinfo.value)
