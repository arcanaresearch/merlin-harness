#!/usr/bin/env python3
"""Run the focused provider-effort production-path checks."""

import subprocess
import sys


def main() -> None:
    checks = (
        "packages/merlin-providers/tests/test_openai_compatible.py::test_deepseek_effort_uses_native_thinking_toggle_on_the_streaming_path",
        "packages/merlin-providers/tests/test_anthropic_messages.py::test_legacy_anthropic_requires_exact_operator_budget_not_categorical_effort",
        "packages/merlin-providers/tests/test_gemini.py::test_gemini_effort_uses_native_levels_and_rejects_invented_legacy_budgets",
        "packages/merlin-providers/tests/test_kimi_acp.py::test_kimi_acp_explicitly_rejects_categorical_effort",
        "packages/merlin-providers/tests/test_openai_responses.py::test_payload_shapes_input_tools_and_reasoning",
        "packages/merlin-providers/tests/test_codex_app_server.py",
        "packages/merlin-providers/tests/test_claude_cli.py",
    )
    subprocess.run([sys.executable, "-m", "pytest", "-q", *checks], check=True)


if __name__ == "__main__":
    main()
