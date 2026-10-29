from merlin_contracts.campaign_coordination import (
    has_url_candidate,
    is_explicit_campaign_launch,
    is_recon_handback_coordination,
)


def test_recognizes_explicit_campaign_launch_variants() -> None:
    for content in (
        "Assess https://a.test while logged in and continue testing.",
        "Run Merlin against https://a.test.",
        "Continue with a new campaign against https://a.test.",
        "Retry a fresh scan against https://a.test.",
        "Please resume with another assessment of https://a.test.",
        "Please continue with a fresh security evaluation of https://a.test.",
        "Start a new campaign against https://b.test.",
        "Launch a fresh scan of https://b.test.",
    ):
        assert is_explicit_campaign_launch(content)


def test_rejects_ordinary_campaign_continuations_as_new_launches() -> None:
    for content in (
        "Continue the assessment against https://a.test.",
        "Resume the Merlin scan on https://a.test.",
        "Retry the security assessment for target A.",
        "/status for the new campaign against https://a.test.",
        "Do not start a new campaign against https://a.test; continue the current assessment.",
        "Continue after a new login; I'm logged in for the current security assessment.",
        "Continue after another MFA challenge for the current security assessment.",
    ):
        assert not is_explicit_campaign_launch(content)


def test_url_candidate_detection_includes_malformed_and_prefixed_tokens() -> None:
    for content in (
        "Continue with ftp://(broken)",
        "Continue _ftp://b.test",
        "Continue éftp://b.test",
        "Continue https://(a.test)",
    ):
        assert has_url_candidate(content)
    assert not has_url_candidate("Continue the current target")


def test_recognizes_authenticated_browser_handback_variants() -> None:
    for content in (
        "Signed in. Hand the browser back and continue reconnaissance.",
        "Authentication for the selected operator account is complete; continue.",
        "MFA in the foreground browser has now been completed; proceed.",
        "Authentication " + ("x" * 30) + " complete; proceed.",
        "Hand " + ("x" * 35) + " browser back and continue.",
        "Return the foreground browser to the campaign lead and then "
        "hand the browser back; resume.",
    ):
        assert is_recon_handback_coordination(content)


def test_rejects_incomplete_or_unrelated_coordination() -> None:
    for content in (
        "Authentication is complete.",
        "Continue the security assessment.",
        "Hand the browser back when login is complete.",
        "Explain the current runtime status.",
        "I am not logged in; continue with a public-only explanation.",
        "I couldn't get logged in; continue by explaining the error.",
        "Authentication is complete, but do not continue; cancel this campaign.",
        "Authentication is complete; stop this campaign and continue with an explanation.",
        "MFA failed; proceed by reporting the failure.",
        "Authentication is not completed; continue with an explanation.",
        "MFA was never completed; proceed.",
        "Login is not done; continue.",
        "I am unsigned in; continue.",
        "The task was assigned in chat; continue.",
    ):
        assert not is_recon_handback_coordination(content)
