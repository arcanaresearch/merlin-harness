from datetime import UTC, datetime
from types import SimpleNamespace

from merlin_browser import BrowserMetadata, BrowserOwner
from merlin_storage_postgres import CampaignApiSession
from merlin_tui.bootstrap import (
    _campaign_api_sessions_for_completion,
    _completion_cleanup_authority_document,
)


def _cleaned_api_session(scope: str, *, target_id: str = "target-1") -> CampaignApiSession:
    return CampaignApiSession(
        "campaign-1",
        target_id,
        "sha256:adapter",
        "cleaned",
        {
            "__cleanup_exchange": {
                "id": f"cleanup:{scope}",
                "cleanup": True,
                "outcome": "acknowledged",
            }
        },
        session_scope_id=scope,
        cleanup_disposition="cleanup_completed",
    )


def test_api_cleanup_authority_requires_exact_acknowledged_exchange() -> None:
    target = SimpleNamespace(id="target-1", interface="http_api")
    active = CampaignApiSession(
        "campaign-1", "target-1", "sha256:adapter", "ambiguous", {}, {"id": "cleanup"}
    )
    assert (
        _completion_cleanup_authority_document("campaign-1", target, api_session=active)["status"]
        == "unresolved"
    )

    cleaned = CampaignApiSession(
        "campaign-1",
        "target-1",
        "sha256:adapter",
        "cleaned",
        {
            "__cleanup_exchange": {
                "id": "cleanup",
                "cleanup": True,
                "request": {"session": "sealed"},
                "response": {"closed": True},
                "outcome": "acknowledged",
            }
        },
        cleanup_disposition="cleanup_completed",
    )
    authority = _completion_cleanup_authority_document("campaign-1", target, api_session=cleaned)
    assert authority["status"] == "acknowledged"
    assert authority["digest"].startswith("sha256:")


def test_api_cleanup_authority_rejects_legacy_false_acknowledgement() -> None:
    target = SimpleNamespace(id="target-1", interface="http_api")
    legacy = CampaignApiSession(
        "campaign-1",
        "target-1",
        "sha256:adapter",
        "cleaned",
        {
            "__cleanup_exchange": {
                "cleanup": True,
                "response": {"closed": False},
                "outcome": "acknowledged",
            }
        },
    )

    authority = _completion_cleanup_authority_document("campaign-1", target, api_session=legacy)

    assert authority["status"] == "unresolved"


def test_api_cleanup_authority_is_not_applicable_without_owned_session() -> None:
    authority = _completion_cleanup_authority_document(
        "campaign-1",
        SimpleNamespace(id="target-1", interface="http_api"),
        api_sessions=(),
    )

    assert authority["status"] == "not_applicable"
    assert authority["sessions"] == ()


def test_api_cleanup_authority_accepts_cleaned_session_without_target_cleanup() -> None:
    target = SimpleNamespace(id="target-1", interface="http_api")
    cleaned = CampaignApiSession(
        "campaign-1",
        "target-1",
        "sha256:adapter",
        "cleaned",
        {"session_id": "session-1"},
        cleanup_disposition="no_cleanup_obligation",
    )

    authority = _completion_cleanup_authority_document(
        "campaign-1",
        target,
        api_session=cleaned,
        api_cleanup_lifecycle="not_required",
    )

    assert authority["status"] == "acknowledged"
    assert authority["target_cleanup_required"] is False
    assert "exchange" not in authority


def test_api_cleanup_authority_rejects_untyped_cleanup_free_session() -> None:
    target = SimpleNamespace(id="target-1", interface="http_api")
    legacy = CampaignApiSession(
        "campaign-1",
        "target-1",
        "sha256:adapter",
        "cleaned",
        {"session_id": "session-1"},
    )

    authority = _completion_cleanup_authority_document(
        "campaign-1", target, api_session=legacy, api_cleanup_lifecycle="not_required"
    )

    assert authority["status"] == "unresolved"


async def test_completion_reader_acknowledges_all_clean_scopes() -> None:
    sessions = (_cleaned_api_session("worker-a"), _cleaned_api_session("worker-b"))
    repository = SimpleNamespace(list_for_campaign=lambda campaign_id: sessions)

    loaded = await _campaign_api_sessions_for_completion(repository, "campaign-1")
    authority = _completion_cleanup_authority_document(
        "campaign-1", SimpleNamespace(id="target-1", interface="http_api"), api_sessions=loaded
    )

    assert authority["status"] == "acknowledged"
    assert len(authority["sessions"]) == 2


async def test_completion_reader_rejects_one_unresolved_scope() -> None:
    unresolved = CampaignApiSession(
        "campaign-1",
        "target-1",
        "sha256:adapter",
        "active",
        {},
        session_scope_id="worker-b",
    )
    sessions = (_cleaned_api_session("worker-a"), unresolved)
    repository = SimpleNamespace(list_for_campaign=lambda campaign_id: sessions)

    loaded = await _campaign_api_sessions_for_completion(repository, "campaign-1")
    authority = _completion_cleanup_authority_document(
        "campaign-1", SimpleNamespace(id="target-1", interface="http_api"), api_sessions=loaded
    )

    assert authority["status"] == "unresolved"


def test_api_cleanup_authority_accepts_typed_no_ownership_disposition() -> None:
    collision_loser = CampaignApiSession(
        "campaign-1",
        "target-1",
        "sha256:adapter",
        "cleaned",
        {},
        session_scope_id="worker-collision-loser",
        cleanup_disposition="cleanup_not_owned",
    )

    authority = _completion_cleanup_authority_document(
        "campaign-1",
        SimpleNamespace(id="target-1", interface="http_api"),
        api_sessions=(collision_loser,),
    )

    assert authority["status"] == "acknowledged"


def test_api_cleanup_authority_rejects_no_obligation_when_cleanup_is_required() -> None:
    invalid = CampaignApiSession(
        "campaign-1",
        "target-1",
        "sha256:adapter",
        "cleaned",
        {},
        session_scope_id="worker-invalid",
        cleanup_disposition="no_cleanup_obligation",
    )

    authority = _completion_cleanup_authority_document(
        "campaign-1",
        SimpleNamespace(id="target-1", interface="http_api"),
        api_sessions=(invalid,),
        api_cleanup_lifecycle="required",
    )

    assert authority["status"] == "unresolved"


def test_api_cleanup_authority_rejects_unknown_lifecycle_as_no_obligation() -> None:
    invalid = CampaignApiSession(
        "campaign-1",
        "target-1",
        "sha256:adapter",
        "cleaned",
        {},
        session_scope_id="worker-unknown",
        cleanup_disposition="no_cleanup_obligation",
    )

    authority = _completion_cleanup_authority_document(
        "campaign-1",
        SimpleNamespace(id="target-1", interface="http_api"),
        api_sessions=(invalid,),
        api_cleanup_lifecycle="unknown",
    )

    assert authority["status"] == "unresolved"
    assert authority["target_cleanup_lifecycle"] == "unknown"
    assert authority["target_cleanup_required"] is None


def test_api_cleanup_authority_acknowledges_unestablished_requirement_with_uncertainty() -> None:
    session = CampaignApiSession(
        "campaign-1",
        "target-1",
        "sha256:adapter",
        "ambiguous",
        {},
        session_scope_id="worker-unknown",
        cleanup_disposition="cleanup_requirement_unestablished",
    )

    authority = _completion_cleanup_authority_document(
        "campaign-1",
        SimpleNamespace(id="target-1", interface="http_api"),
        api_sessions=(session,),
        api_cleanup_lifecycle="unknown",
    )

    # The adapter can never establish an executable cleanup action, so the
    # quarantined physical-session claim is a recorded limitation, not a
    # completion blocker.
    assert authority["status"] == "acknowledged"
    assert authority["target_cleanup_lifecycle"] == "unknown"
    assert authority["target_cleanup_required"] is None
    assert authority["evaluation_uncertain"] is True
    assert authority["evaluation_uncertainties"] == (
        {
            "kind": "evaluation_uncertain",
            "source": "campaign_api_session.cleanup_disposition",
            "session_scope_id": "worker-unknown",
            "disposition": "cleanup_requirement_unestablished",
            "remote_cleanup_attempted": False,
            "reason": (
                "The reviewed adapter did not establish whether target cleanup was required; "
                "the physical target-session claim remains quarantined because no reviewed "
                "remote cleanup call was available."
            ),
        },
    )


async def test_completion_reader_filters_other_targets_without_losing_scopes() -> None:
    sessions = (
        _cleaned_api_session("worker-a"),
        _cleaned_api_session("worker-b"),
        CampaignApiSession(
            "campaign-1",
            "target-2",
            "sha256:other",
            "active",
            {},
            session_scope_id="worker-c",
        ),
    )
    repository = SimpleNamespace(list_for_campaign=lambda campaign_id: sessions)

    loaded = await _campaign_api_sessions_for_completion(repository, "campaign-1")
    authority = _completion_cleanup_authority_document(
        "campaign-1", SimpleNamespace(id="target-1", interface="http_api"), api_sessions=loaded
    )

    assert authority["status"] == "acknowledged"
    assert tuple(row[0] for row in authority["sessions"]) == ("worker-a", "worker-b")


async def test_completion_reader_preserves_legacy_repository_fallback() -> None:
    session = _cleaned_api_session("legacy:campaign")
    repository = SimpleNamespace(load=lambda campaign_id: session)

    assert await _campaign_api_sessions_for_completion(repository, "campaign-1") == (session,)


def test_browser_cleanup_authority_fails_closed_while_actor_is_owned() -> None:
    target = SimpleNamespace(id="target-1", interface="browser")
    now = datetime.now(UTC)
    context = BrowserMetadata(
        context_id="context-1",
        mode="persistent",
        target_id="target-1",
        owner=BrowserOwner(session_id="session-1", worker_id="operator", technique_id="recon"),
        allowed_origins=frozenset({"https://target.test"}),
        tabs=(),
        state="active",
        active_actor_id="campaign-1/worker",
        created_at=now,
        updated_at=now,
    )
    unresolved = _completion_cleanup_authority_document(
        "campaign-1", target, browser_contexts=(context,)
    )
    released = _completion_cleanup_authority_document(
        "campaign-1",
        target,
        browser_contexts=(context.model_copy(update={"active_actor_id": None}),),
    )

    assert unresolved["status"] == "unresolved"
    assert released["status"] == "acknowledged"
    assert unresolved["digest"] != released["digest"]


def test_browser_cleanup_authority_accepts_released_restartable_context() -> None:
    target = SimpleNamespace(id="target-1", interface="browser")
    now = datetime.now(UTC)
    context = BrowserMetadata(
        context_id="context-1",
        mode="persistent",
        target_id="target-1",
        owner=BrowserOwner(session_id="session-1", worker_id="operator", technique_id="recon"),
        allowed_origins=frozenset({"https://target.test"}),
        tabs=(),
        state="restart_required",
        active_actor_id=None,
        created_at=now,
        updated_at=now,
    )

    authority = _completion_cleanup_authority_document(
        "campaign-1", target, browser_contexts=(context,)
    )

    assert authority["status"] == "acknowledged"
    assert authority["contexts"] == (("context-1", "restart_required", ""),)
