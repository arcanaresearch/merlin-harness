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


def test_later_barrier_or_retraction_revokes_continuation_and_launch() -> None:
    for content in (
        "Continue. Actually cancel.",
        "Continue, but stop the current campaign.",
        "Continue? No, cancel.",
        "Continue. Explain status instead.",
        "Assess https://a.test. Do not proceed.",
        "Assess https://a.test. Never mind.",
        "Assess https://a.test. Don't continue.",
        "Assess https://a.test. Forget it.",
        "Assess https://a.test. Scratch that.",
        "Assess https://a.test. Disregard that.",
        "Assess https://a.test. Explain status instead.",
    ):
        assert campaign_coordination_intent(content).kind == "barrier", content


def test_later_continuation_or_handback_does_not_replace_launch() -> None:
    for content in (
        "Assess https://a.test for SSRF. Continue.",
        "Start a new campaign against https://a.test. Proceed.",
        "Assess https://a.test. Authentication complete; continue.",
    ):
        intent = campaign_coordination_intent(content)
        assert intent.kind == "launch", content
        assert intent.clause.startswith(("Assess ", "Start "))

    assert (
        campaign_coordination_intent(
            "Assess https://a.test. Never mind. Assess https://b.test instead."
        ).kind
        == "launch"
    )


def test_monotonic_cancellation_and_object_aware_wait_commands() -> None:
    for content in (
        "Cancel campaign. I am logged in, continue",
        "Stop campaign. Login succeeded, proceed",
        "Cancel A. Continue without new assessment of same A",
        "Cancel current campaign and inspect abort handling",
    ):
        assert campaign_coordination_intent(content).kind == "barrier", content
    for content in (
        "Don't cancel; continue",
        "Do not stop; resume",
    ):
        assert campaign_coordination_intent(content).kind == "continuation", content
    for content in (
        "Auth complete; stop waiting and continue",
        "Auth complete; end the wait and proceed",
        "Auth complete; stop the delay and resume",
    ):
        assert campaign_coordination_intent(content).kind == "handback", content


def test_launch_target_and_command_evidence_are_clause_local() -> None:
    for content in (
        "Do not assess target A. Start a new campaign.",
        "Do not assess https://a.test. Start a new campaign.",
        "Continue reading the new campaign report",
        "Resume fresh scan documentation",
        "Retry parsing another assessment result",
    ):
        assert campaign_coordination_intent(content).kind == "barrier", content
    for content in (
        "Continue with a new assessment of https://a.test",
        "Continue with another assessment of https://a.test",
        "Retry a fresh scan of target A",
        "When ready, please assess https://a.test",
    ):
        assert campaign_coordination_intent(content).kind == "launch", content


def test_narrative_campaign_evidence_never_launches() -> None:
    for content in (
        "Security assessment report for https://a.test",
        "Security assessment results for https://a.test",
        "Security assessment summary for https://a.test",
        "Security evaluation status for https://a.test",
        "Security evaluation report for https://a.test",
        "Merlin scan status for https://a.test",
        "Merlin scan results for https://a.test",
        "Red-team scan report for https://a.test",
        "Red-team assessment status for https://a.test",
        "Security assessment was cancelled for https://a.test",
        "Security evaluation not needed for https://a.test",
        "Merlin scan should not run against https://a.test",
    ):
        assert campaign_coordination_intent(content).kind == "barrier", content


def test_auth_requires_operator_state_and_command_continuation() -> None:
    for content in (
        "Logged in users continue to see errors",
        "Login succeeded while errors continue",
        "Login succeeded and tests continue",
        "MFA passed so checks continue",
        "Authentication successful and users proceed",
        "Login succeeded maybe",
        "MFA passed in this example",
        "Login isn't complete; continue.",
        "Authentication wasn't successful; continue.",
        "MFA isn't done; proceed.",
        "CAPTCHA wasn't solved; resume.",
        "Authentication isn’t finished; continue.",
        "I am not quite yet logged in; continue",
    ):
        assert campaign_coordination_intent(content).kind == "barrier", content
    for content in (
        "I logged in; continue",
        "We logged in; continue",
        "Okay, I'm signed in; continue",
        "Thanks, I'm signed in; continue",
        "I authenticated successfully; continue",
        "Done with MFA; proceed",
        "The browser is yours again; continue",
        "Signed in at https://b.test. Hand the browser back and continue reconnaissance.",
        "Login failed earlier and now logged in; continue",
        "Authentication complete and continue",
        "Authentication complete and proceed",
    ):
        assert campaign_coordination_intent(content).kind == "handback", content
    signed_target = campaign_coordination_intent(
        "Signed in at https://b.test. Hand the browser back and continue reconnaissance."
    )
    assert signed_target.target_clause == "Signed in at https://b.test."


def test_quotes_and_ordered_target_evidence_are_authoritative() -> None:
    quoted = (
        'The documentation says: "Do not assess https://a.test. '
        'Assess https://b.test instead." Explain what it means.'
    )
    assert campaign_coordination_intent(quoted).kind == "barrier"
    unquoted = 'Docs say "Assess https://b.test". Now assess https://c.test.'
    assert campaign_coordination_intent(unquoted).kind == "launch"
    assert campaign_coordination_intent(unquoted).target_clause == "Now assess https://c.test."

    ordered = campaign_coordination_intent(
        "Continue at https://a.test, then continue at https://b.test"
    )
    assert ordered.kind == "continuation"
    assert ordered.target_clause == "continue at https://b.test"
    denied = campaign_coordination_intent(
        "Continue https://a.test, then continue without starting a new campaign "
        "against https://b.test"
    )
    assert denied.kind == "targeted_barrier"
    assert denied.target_clause is not None and "https://b.test" in denied.target_clause

    auth_target = campaign_coordination_intent(
        "I logged in at https://a.test; continue at https://b.test"
    )
    assert auth_target.kind == "handback"
    assert auth_target.target_clause == "continue at https://b.test"
    auth_fallback = campaign_coordination_intent("I logged in at https://a.test; continue")
    assert auth_fallback.target_clause == "I logged in at https://a.test"


def test_conditional_auth_pronoun_cancellation_and_same_sentence_relaunch() -> None:
    for content in (
        "Authentication is complete if MFA passes; continue",
        "Authentication is complete unless the session expires; continue",
        "Authentication is complete when MFA passes; continue",
        "Cancel it; authentication complete; continue",
        "Stop it; authentication complete; continue",
        "End it; authentication complete; continue",
        "Cancel the new campaign; authentication complete; continue",
        "Stop the fresh scan; logged in; continue",
        "Abort this new assessment; MFA passed; proceed",
    ):
        assert campaign_coordination_intent(content).kind == "barrier", content
    relaunched = campaign_coordination_intent(
        "Cancel the campaign and start a new campaign against https://b.test"
    )
    assert relaunched.kind == "launch"
    assert relaunched.target_clause == "start a new campaign against https://b.test"


def test_post_launch_target_denial_and_restored_run_directives() -> None:
    for verb in ("assess", "scan", "evaluate", "run"):
        assert (
            campaign_coordination_intent(f"Assess https://a.test. Do not {verb} this target.").kind
            == "barrier"
        )
    for content in (
        "Run a security assessment of https://a.test",
        "Please run the security assessment against https://a.test",
        "Run a security assessment of target A",
        "Please run the security assessment against target A",
        "Run a new campaign against https://a.test",
        "Please run another scan against https://a.test",
        "Run a fresh assessment of https://a.test",
        "Run another security evaluation of https://a.test",
    ):
        assert campaign_coordination_intent(content).kind == "launch", content


def test_launch_reference_retractions_and_latest_continuation_target() -> None:
    for retraction in (
        "Cancel that launch",
        "Stop it",
        "End the launch",
        "Do not assess it",
        "This was only an example",
        "Please explain status instead",
        "Actually, do not run it",
    ):
        content = f"Assess https://a.test. {retraction}"
        assert campaign_coordination_intent(content).kind == "barrier", content
    intent = campaign_coordination_intent(
        "Continue https://a.test:bad, then continue https://b.test."
    )
    assert intent.kind == "continuation"
    assert intent.clause == "continue https://b.test."
    assert intent.target_clause == "continue https://b.test."


def test_polite_targeted_browser_handback_preserves_auth_provenance() -> None:
    intent = campaign_coordination_intent(
        "I signed in at https://b.test. Please hand the browser back and continue."
    )
    assert intent.kind == "handback"
    assert intent.target_clause == "I signed in at https://b.test."


def test_first_person_perfect_auth_and_quoted_documentation_example() -> None:
    for content in (
        "I've completed login; continue",
        "We've completed authentication; continue",
        "I've passed MFA; proceed",
    ):
        assert campaign_coordination_intent(content).kind == "handback", content
    quoted = (
        'Continue reading the documentation example: "Start a new campaign against https://b.test"'
    )
    assert campaign_coordination_intent(quoted).kind == "barrier"


def test_url_punctuation_is_not_a_clause_boundary() -> None:
    for objective in (
        "Assess https://a.test/chat;jsessionid=ABC123",
        "Assess https://a.test/chat?next=/a;b&mode=full",
    ):
        intent = campaign_coordination_intent(objective)
        assert intent.kind == "launch"
        assert intent.clause == objective
        assert intent.target_clause == objective
    assert campaign_coordination_intent("Assess https://a.test; cancel campaign").kind == "barrier"
