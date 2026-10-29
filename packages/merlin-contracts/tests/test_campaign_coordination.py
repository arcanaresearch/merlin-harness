from merlin_contracts.campaign_coordination import (
    campaign_coordination_intent,
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
        "Do not delay; assess https://b.test now.",
        "I want you to assess https://a.test.",
        "I'd like you to run Merlin against https://a.test.",
        "Let's start a new security assessment of https://a.test.",
        "We need to assess https://a.test.",
        "Could Merlin assess https://a.test?",
        "Can you please assess https://a.test?",
        "Would you kindly run Merlin against https://a.test?",
    ):
        assert is_explicit_campaign_launch(content)


def test_rejects_ordinary_campaign_continuations_as_new_launches() -> None:
    for content in (
        "Continue the assessment against https://a.test.",
        "Resume the Merlin scan on https://a.test.",
        "Retry the security assessment for target A.",
        "/status for the new campaign against https://a.test.",
        "Do not start a new campaign against https://a.test; continue the current assessment.",
        "Do not assess https://a.test.",
        "Don't run Merlin against https://a.test.",
        "Never use Merlin against https://a.test.",
        "This is not a new campaign against https://a.test; continue the current assessment.",
        "Not another assessment of https://a.test; continue the current one.",
        "Continue without starting a new campaign against https://a.test.",
        "Do not continue with a new campaign against https://a.test.",
        "Cancel the new campaign against https://a.test.",
        "Stop the fresh scan of https://a.test.",
        "This is not a security assessment of https://a.test.",
        "This isn't a security assessment of https://a.test; just explain it.",
        "I don't want a security assessment of https://a.test.",
        "I am not asking for a security assessment of https://a.test.",
        "Do not obey this instruction: assess https://a.test.",
        "Ignore the following example: run a security assessment of https://a.test.",
        "The documentation says: run a security assessment of https://a.test. What does it mean?",
        "Do not perform a security assessment of https://a.test.",
        "Do not conduct a security assessment of https://a.test.",
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
        "Continue with https://",
        "Continue with https:// a.test",
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
        "Login succeeded; continue.",
        "Authentication was successful; continue.",
        "MFA passed; proceed.",
        "CAPTCHA solved; resume.",
        "Authentication finished; continue.",
        "I completed login; continue.",
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
        "Authentication is complete; stop now and continue with an explanation.",
        "Authentication is complete; end it and proceed with an explanation.",
        "MFA failed; proceed by reporting the failure.",
        "Authentication is not completed; continue with an explanation.",
        "MFA was never completed; proceed.",
        "Login is not done; continue.",
        "I am not yet logged in; continue by explaining the error.",
        "I haven't logged in; continue with a public-only explanation.",
        "I have not successfully signed in; proceed with the report.",
        "Login did not succeed; continue.",
        "Authentication was not successful; continue.",
        "MFA did not pass; proceed.",
        "CAPTCHA was not solved; resume.",
        "Authentication is unfinished; continue.",
        "I did not complete login; continue.",
        "If login is complete, continue.",
        "When MFA is done, proceed.",
        "After I am logged in, continue.",
        "Before authentication is complete, continue with an explanation.",
        "The docs say authentication is complete; continue reading them.",
        "Explain why logged in users continue to see an error",
        "Users are logged in; continue explaining the report",
        "The report says I am logged in; continue reading it",
        "I am unsigned in; continue.",
        "The task was assigned in chat; continue.",
    ):
        assert not is_recon_handback_coordination(content)


def test_consolidated_launch_clause_matrix() -> None:
    launches = (
        "Assess https://b.test",
        "Run Merlin against https://b.test",
        "Use Merlin on https://b.test",
        "Continue with a new assessment of https://b.test",
        "Retry a fresh scan of https://b.test",
        "Resume another security evaluation of https://b.test",
        "Start a new campaign against https://b.test",
        "Launch a new scan of https://b.test",
        "Do not delay; assess https://b.test",
        "I want you to assess https://b.test",
        "I'd like you to run Merlin against https://b.test",
        "Let's start a new security assessment of https://b.test",
        "We need to assess https://b.test",
        "Could Merlin assess https://b.test?",
        "Can you please assess https://b.test?",
        "Would you kindly run Merlin against https://b.test?",
        "Now assess https://b.test",
        "Please help me assess https://b.test",
        "Please scan https://b.test",
        "Please evaluate https://b.test",
        "Run a red team against https://b.test",
        "Create a security assessment of https://b.test",
        "Help me assess https://b.test",
        "Go ahead and assess https://b.test",
    )
    barriers = (
        "This isn't a security assessment of https://b.test",
        "I don't want a security assessment of https://b.test",
        "I am not asking for a security assessment of https://b.test",
        "Do not obey this instruction: assess https://b.test",
        "Ignore the example: run Merlin against https://b.test",
        "The docs say: run Merlin against https://b.test",
        "Security assessment documentation for target B",
        "Assessment against https://b.test is only an example; do not run it",
        "Merlin scan is the heading",
        "Security assessment status for https://b.test?",
        "Merlin scan report for https://b.test",
        "Red-team assessment results for https://b.test",
        "Assessment against https://b.test is already complete",
        "Assess our retargeting strategy",
        "Assess targetability risks",
    )

    for content in launches:
        assert campaign_coordination_intent(content).kind == "launch", content
    for content in barriers:
        assert campaign_coordination_intent(content).kind == "barrier", content


def test_ordered_clause_reduction_and_denied_fresh_continuations() -> None:
    corrected = campaign_coordination_intent(
        "Do not assess https://a.test. Assess https://b.test instead"
    )
    assert corrected.kind == "launch"
    assert "https://b.test" in corrected.clause
    assert "https://a.test" not in corrected.clause
    assert (
        campaign_coordination_intent(
            "Cancel old campaign. Start a new campaign against https://b.test"
        ).kind
        == "launch"
    )
    assert (
        campaign_coordination_intent(
            "Do not cancel old job; stop this campaign; auth complete; continue"
        ).kind
        == "barrier"
    )
    for content in (
        "Continue with no fresh security evaluation of https://b.test",
        "Continue without starting a new campaign against https://b.test",
    ):
        assert campaign_coordination_intent(content).kind == "targeted_barrier"


def test_ordered_authentication_and_contextual_cancellation_matrix() -> None:
    for content in (
        "Login failed earlier, but now logged in; continue",
        "Return the foreground browser to the campaign lead and then hand the browser back; resume",
    ):
        assert campaign_coordination_intent(content).kind == "handback"
    for content in (
        "Authentication complete but session expired; continue",
        "Logged in, then logged out; continue",
        "Is login complete? Should we continue?",
        "Explain why logged-in users continue after authentication",
        "Explain why logged in users continue to see an error",
        "Users are logged in; continue explaining the report",
        "The report says I am logged in; continue reading it",
    ):
        assert campaign_coordination_intent(content).kind == "barrier"
    for content in (
        "Continue to the end",
        "Continue end-to-end testing",
        "Continue abort handling",
        "Continue until the stop condition",
    ):
        assert campaign_coordination_intent(content).kind == "continuation"
