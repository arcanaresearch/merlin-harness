from merlin_tui.bootstrap import _reviewed_scenario_contract


def test_reviewed_scenario_contract_is_platform_neutral_and_provenance_stable() -> None:
    adapter = {
        "interface": "browser",
        "endpoint": "https://chat.example.test/widget",
        "authentication": "authenticated visitor",
        "session_constraint": "one conversation per run",
        "scenario_identity": {
            "platform": "hosted-live-chat",
            "tenant": "tenant-a",
            "deployment": "website-a",
            "channel": "support",
            "agent_or_bot": "assistant-a",
            "scenario": "refund-policy",
            "evidence_refs": ["snapshot:first"],
        },
        "recon_handoff_id": "handoff:first",
        "observed_evidence": ["snapshot:first"],
        "browser_steps": [{"step_id": "send", "tool": "browser_fill", "arguments": {}}],
        "interaction_contract": {"success_indicator": "reply observed"},
    }

    first = _reviewed_scenario_contract(adapter)
    second = _reviewed_scenario_contract(
        {
            **adapter,
            "recon_handoff_id": "handoff:second",
            "observed_evidence": ["snapshot:second"],
            "scenario_identity": {
                **adapter["scenario_identity"],
                "evidence_refs": ["snapshot:second"],
            },
        }
    )
    other_tenant = _reviewed_scenario_contract(
        {
            **adapter,
            "scenario_identity": {
                **adapter["scenario_identity"],
                "tenant": "tenant-b",
            },
        }
    )

    assert first == second
    assert first != other_tenant
    assert "recon_handoff_id" not in first
    assert "evidence_refs" not in first["scenario_identity"]


def test_unidentified_scenarios_fail_closed_to_one_recon_scope() -> None:
    adapter = {
        "interface": "browser",
        "endpoint": "https://chat.example.test/widget",
        "authentication": "authenticated visitor",
        "recon_handoff_id": "handoff:first",
    }

    first = _reviewed_scenario_contract(adapter)
    second = _reviewed_scenario_contract({**adapter, "recon_handoff_id": "handoff:second"})

    assert first != second
    assert first["unqualified_recon_scope"] == "handoff:first"
