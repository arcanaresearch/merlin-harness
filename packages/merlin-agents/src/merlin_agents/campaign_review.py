"""Recover an approval display from an unchanged, verified durable plan."""

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any


def recover_campaign_review(
    row: Mapping[str, Any], *, session_id: str, campaign_id: str, manifest_digest: str
) -> dict[str, Any]:
    # Use the execution path's parsers and compatibility digests. Recovery only
    # reads the sealed plan; approval and execution remain explicit TUI actions.
    from .campaign_service import (
        _assignment_context,
        _manifest,
        _reviewed_preparation_digest,
        _reviewed_preparation_digest_values,
    )

    if (
        str(row["session_id"]) != session_id
        or str(row["id"]) != campaign_id
        or row["status"] != "prepared"
        or row["manifest_digest"] != manifest_digest
    ):
        raise ValueError("Saved campaign does not match the pending approval")
    envelope = row["manifest"]
    manifest = _manifest(envelope["manifest"])
    context = _assignment_context(envelope["assignment_context"])
    manifest.validate()
    if manifest.id != campaign_id or manifest.session_id != session_id:
        raise ValueError("Saved manifest belongs to another campaign or session")
    if manifest_digest not in {
        manifest.digest,
        _reviewed_preparation_digest(manifest, context),
        _reviewed_preparation_digest_values(manifest.digest, envelope["assignment_context"]),
    }:
        raise ValueError("Persisted campaign review failed digest validation")
    # Exclude document bodies, raw schemas and credential handles from the UI.
    fields = {
        "Session": session_id,
        "Agent composition": manifest.specialist_composition,
        "Provider": manifest.provider_id,
        "Model": manifest.provider_model,
        "Model effort": manifest.provider_effort,
        "Model routes": dict(context.provider_role_models),
        "Target": manifest.target.endpoint,
        "Interface": manifest.target.interface,
        "Adapter": manifest.target.id,
        "Authentication": manifest.target.authentication or "Not declared",
        "Session policy": manifest.target.session_constraint or "Not declared",
        "Campaign limits": asdict(manifest.budget),
        "Tools": manifest.tool_ids,
        "Authorities": manifest.authority_ids,
        "Coverage": tuple(technique.id for technique in context.techniques),
        "Agent profiles": tuple(profile.id for profile in context.additional_profiles),
        "Packs": tuple(
            {"id": document.id, "digest": document.digest} for document in manifest.catalogue
        ),
        "Skills": tuple(
            {"id": document.id, "digest": document.digest} for document in manifest.skill_documents
        ),
        "Preflight estimate": envelope.get("preflight_estimate", {}),
        "Recorded coverage skips": envelope.get("technique_skips", ()),
    }
    return {
        "campaign_id": campaign_id,
        "manifest_digest": manifest_digest,
        "objective": manifest.objective,
        "profile": manifest.profile.value,
        "approval_required": True,
        "review": {
            "budget": {
                "turns": manifest.budget.max_turns,
                "tokens": manifest.budget.max_tokens,
                "seconds": manifest.budget.max_seconds,
                "cost_usd": manifest.budget.max_cost_usd,
            },
            "recovered_fields": fields,
            "target_contract": {"interface": manifest.target.interface, "execution_options": []},
        },
    }
