import json
from types import SimpleNamespace

import pytest
from merlin_contracts import ChatMessage, ToolContext
from merlin_contracts.user_input import (
    ANSWER_PREFIX,
    InputRequest,
    InputResponse,
    answer_prompt,
    pending_input,
)
from merlin_tui.kernel_backend import KernelBackend
from merlin_tui.user_input_screen import UserInputScreen
from merlin_tui.user_input_tool import RequestUserInputTool
from rich.console import Console
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

QUESTIONS = [
    {
        "id": "auth",
        "question": "How do you want to proceed?",
        "options": [
            {"id": "email", "label": "Email magic code", "description": "Use an email you control"},
            {"id": "google", "label": "Google OAuth", "description": "Continue in your browser"},
            {
                "id": "existing",
                "label": "Existing session",
                "description": "Use a configured handle",
            },
            {"id": "rescope", "label": "Change target", "description": "Requires a new review"},
        ],
    }
]


async def receipt():
    return await RequestUserInputTool().invoke(
        ToolContext(session_id="s1", call_id="question-1", workspace_root="."),
        {"questions": QUESTIONS},
    )


@pytest.mark.asyncio
async def test_real_tool_schema_broker_and_durable_question():
    from merlin_tools import ToolBroker

    tool = RequestUserInputTool()
    broker = ToolBroker()
    broker.register(tool)
    result = await broker.invoke(
        "request_user_input",
        ToolContext(session_id="s1", call_id="question-1", workspace_root="."),
        {"questions": QUESTIONS},
    )
    assert not result.is_error
    message = ChatMessage(
        role="tool", name="request_user_input", tool_call_id="question-1", content=result.content
    )
    request = pending_input([message])
    assert request.request_id == "s1:question-1"
    response = InputResponse.model_validate(
        {"request_id": request.request_id, "answers": {"auth": {"option_ids": ["existing"]}}}
    )
    encoded = answer_prompt(request, response)
    assert pending_input([message, ChatMessage(role="user", content=encoded)]) is None
    # Restoring only the persisted request restores exactly the same IDs and options.
    assert pending_input([message]).model_dump() == request.model_dump()


@pytest.mark.asyncio
async def test_question_tool_rejects_worker_and_forged_identity():
    tool = RequestUserInputTool()
    worker = ToolContext(session_id="s1", call_id="q", agent_id="worker", workspace_root=".")
    assert (await tool.invoke(worker, {"questions": QUESTIONS})).is_error
    foreground = ToolContext(session_id="s1", call_id="q", workspace_root=".")
    assert (
        await tool.invoke(foreground, {"request_id": "forged", "questions": QUESTIONS})
    ).is_error


@pytest.mark.asyncio
async def test_campaign_execution_approval_cannot_replace_client_review():
    result = await RequestUserInputTool().invoke(
        ToolContext(session_id="s1", call_id="approval", workspace_root="."),
        {
            "questions": [
                {
                    "id": "approve_execute",
                    "question": "The prepared campaign manifest is ready. Execute it?",
                    "options": [
                        {
                            "id": "approve",
                            "label": "Approve & execute",
                            "description": "Begin the reviewed campaign.",
                        },
                        {
                            "id": "review_more",
                            "label": "Hold for review",
                            "description": "Keep the manifest prepared.",
                        },
                    ],
                    "default": ["approve"],
                }
            ]
        },
    )

    assert result.is_error
    assert "client's sealed-review control" in result.content
    assert "request_user_input" in result.content


@pytest.mark.parametrize(
    "answers",
    [
        {},
        {"auth": {"option_ids": ["unknown"]}},
        {"auth": {"option_ids": ["email", "google"]}},
        {"auth": {"option_ids": ["email", "email"]}},
        {"auth": {}},
    ],
)
def test_invalid_answers_rejected(answers):
    request = InputRequest(request_id="q", questions=QUESTIONS)
    with pytest.raises(ValueError):
        InputResponse(request_id="q", answers=answers).validate_for(request)


def test_multiple_free_text_defaults_and_duplicate_ids():
    values = {**QUESTIONS[0], "multiple": True, "default": ["email", "google"]}
    request = InputRequest(request_id="q", questions=[values])
    InputResponse(
        request_id="q",
        answers={"auth": {"option_ids": ["email", "google"], "text": "Use test account"}},
    ).validate_for(request)
    with pytest.raises(ValueError):
        InputRequest(request_id="q", questions=[QUESTIONS[0], QUESTIONS[0]])
    with pytest.raises(ValueError):
        InputRequest(request_id="q", questions=[{**QUESTIONS[0], "default": ["bogus"]}])


@pytest.mark.asyncio
async def test_backend_rejects_stale_response_before_kernel_submission():
    result = await receipt()
    messages = [
        ChatMessage(
            role="tool",
            name="request_user_input",
            content=result.content,
            tool_call_id="question-1",
        )
    ]

    async def load(_):
        return messages

    kernel = SimpleNamespace(session_id="s1", _store=SimpleNamespace(load_messages=load))
    backend = KernelBackend(kernel)
    response = ANSWER_PREFIX + json.dumps(
        {"request_id": "old-question", "answers": {"auth": {"option_ids": ["email"]}}}
    )
    with pytest.raises(ValueError, match="no longer pending"):
        _ = [event async for event in backend.stream(response)]


@pytest.mark.asyncio
async def test_terminal_question_selection_and_free_text():
    request = InputRequest(request_id="question-1", questions=QUESTIONS)
    result = []

    class QuestionApp(App):
        def on_mount(self):
            self.push_screen(UserInputScreen(request), result.append)

    app = QuestionApp()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.click("#answer-0-2")
        app.screen.query_one("#text-0", Input).value = "Use my saved test account"
        await pilot.click("#question-continue")
        await pilot.pause()
    response = InputResponse.model_validate_json(result[0][len(ANSWER_PREFIX) :])
    assert response.answers["auth"].option_ids == ["existing"]
    assert response.answers["auth"].text == "Use my saved test account"


@pytest.mark.asyncio
async def test_terminal_question_is_fully_keyboard_controllable():
    request = InputRequest(request_id="question-keyboard", questions=QUESTIONS)
    result = []

    class QuestionApp(App):
        def on_mount(self):
            self.push_screen(UserInputScreen(request), result.append)

    app = QuestionApp()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        assert app.screen.query_one("#options-0").has_focus
        await pilot.press("down", "space")
        await pilot.press("tab")
        assert app.screen.query_one("#text-0", Input).has_focus
        await pilot.press("ctrl+enter")
        await pilot.pause()

    response = InputResponse.model_validate_json(result[0][len(ANSWER_PREFIX) :])
    assert response.answers["auth"].option_ids == ["google"]


@pytest.mark.asyncio
async def test_terminal_question_continue_button_activates_with_enter():
    request = InputRequest(request_id="question-enter", questions=QUESTIONS)
    result = []

    class QuestionApp(App):
        def on_mount(self):
            self.push_screen(UserInputScreen(request), result.append)

    app = QuestionApp()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.press("tab", "tab")
        assert app.screen.query_one("#question-continue").has_focus
        await pilot.press("enter")
        await pilot.pause()

    assert len(result) == 1


@pytest.mark.asyncio
async def test_backend_resume_and_answer_resolve_the_same_durable_question():
    import asyncio

    result = await receipt()
    messages = [
        ChatMessage(
            role="tool",
            name="request_user_input",
            tool_call_id="question-1",
            content=result.content,
        )
    ]
    submissions = []

    async def load(_):
        return messages

    class Kernel:
        session_id = "s1"
        turn_active = False
        _store = SimpleNamespace(load_messages=load)

        async def submit(self, prompt):
            submissions.append(prompt)
            if not prompt.startswith("/"):
                messages.append(ChatMessage(role="user", content=prompt))

        async def next_event(self):
            await asyncio.sleep(1)

    backend = KernelBackend(Kernel())
    events = [event async for event in backend.stream("/resume s1")]
    restored = InputRequest.model_validate_json(
        next(event.text for event in events if event.kind == "user_input")
    )
    response = InputResponse(
        request_id=restored.request_id, answers={"auth": {"option_ids": ["google"]}}
    )
    prompt = answer_prompt(restored, response)
    events = [event async for event in backend.stream(prompt)]
    assert any(
        event.kind == "user_input_resolved" and event.text == restored.request_id
        for event in events
    )
    assert submissions == ["/resume s1", prompt]
    with pytest.raises(ValueError, match="no longer pending"):
        _ = [event async for event in backend.stream(prompt)]
    assert submissions == ["/resume s1", prompt]


@pytest.mark.asyncio
async def test_backend_accepts_the_exact_question_it_displayed_when_durable_order_lags():
    import asyncio

    request = InputRequest(request_id="displayed-question", questions=QUESTIONS)
    messages = []
    events = [
        SimpleNamespace(
            type="tool",
            payload={
                "name": "request_user_input",
                "state": "completed",
                "text": json.dumps(
                    {"status": "awaiting_input", "request": request.model_dump()}
                ),
            },
        )
    ]
    submissions = []

    async def load(_):
        return messages

    class Kernel:
        session_id = "s1"
        turn_active = False
        _store = SimpleNamespace(load_messages=load)

        async def submit(self, prompt):
            submissions.append(prompt)

        async def next_event(self):
            if events:
                return events.pop(0)
            await asyncio.sleep(1)

    backend = KernelBackend(Kernel())
    first = [event async for event in backend.stream("show the form")]
    displayed = InputRequest.model_validate_json(
        next(event.text for event in first if event.kind == "user_input")
    )
    answer = answer_prompt(
        displayed,
        InputResponse(
            request_id=displayed.request_id,
            answers={"auth": {"option_ids": ["existing"]}},
        ),
    )
    resolved = [event async for event in backend.stream(answer)]

    assert any(
        event.kind == "user_input_resolved" and event.text == displayed.request_id
        for event in resolved
    )
    assert submissions == ["show the form", answer]
    with pytest.raises(ValueError, match="no longer pending"):
        _ = [event async for event in backend.stream(answer)]
    assert submissions == ["show the form", answer]


@pytest.mark.asyncio
async def test_terminal_app_opens_structured_question_and_continues():
    from merlin_tui.app import MerlinApp
    from merlin_tui.backend import DemoBackend, StreamEvent

    request = InputRequest(request_id="question-1", questions=QUESTIONS)
    received = []

    class Backend(DemoBackend):
        async def stream(self, prompt):
            received.append(prompt)
            if len(received) == 1:
                yield StreamEvent("user_input", request.model_dump_json())
            else:
                yield StreamEvent("user_input_resolved", request.request_id)
                yield StreamEvent(
                    "content", "Continuing the retained reconnaissance with your saved session."
                )

    app = MerlinApp(Backend())
    async with app.run_test(size=(120, 45)) as pilot:
        app.query_one("#composer").text = "Inspect this target"
        await app.action_send()
        await pilot.pause()
        assert isinstance(app.screen, UserInputScreen)
        await pilot.click("#answer-0-2")
        await pilot.click("#question-continue")
        await pilot.pause()
        assert len(received) == 2
        answer = InputResponse.model_validate_json(received[1][len(ANSWER_PREFIX) :])
        assert answer.answers["auth"].option_ids == ["existing"]
        assert any("Continuing the retained" in entry.text for entry in app._transcript_entries)


@pytest.mark.asyncio
async def test_campaign_approval_modal_contains_scrollable_review_summary() -> None:
    from merlin_tui.app import MerlinApp
    from merlin_tui.backend import DemoBackend, StreamEvent

    request = InputRequest(
        request_id="campaign-approval",
        questions=[
            {
                "id": "approval",
                "question": "Approve execution of the prepared campaign manifest?",
                "options": [
                    {
                        "id": "approve",
                        "label": "Approve & execute",
                        "description": "Run the reviewed campaign",
                    },
                    {
                        "id": "revise",
                        "label": "Revise something",
                        "description": "Hold execution",
                    },
                ],
            }
        ],
    )
    review = {
        "campaign_id": "campaign/review-visible",
        "manifest_digest": "sha256:review-visible",
        "objective": "Assess the local game safely",
        "profile": "quickscan",
        "review": {
            "recovered_fields": {
                "Target": "http://localhost:3100",
                "Coverage": "20 active candidates; remaining catalogue dormant",
            }
        },
    }

    class Backend(DemoBackend):
        async def stream(self, prompt):
            del prompt
            yield StreamEvent("campaign_review", json.dumps(review))
            yield StreamEvent("user_input", request.model_dump_json())

    app = MerlinApp(Backend())
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#composer").text = "prepare campaign"
        await app.action_send()
        await pilot.pause()
        assert isinstance(app.screen, UserInputScreen)
        assert app.screen.query_one("#question-context", Static)
        console = Console(record=True, width=100)
        console.print(app.screen.context)
        rendered = console.export_text()
        assert "campaign/review-visible" in rendered
        assert "sha256:review-visible" in rendered
        assert "Assess the local game safely" in rendered
        assert "20 active candidates" in rendered
        assert isinstance(app.screen.query_one("#operator-question"), VerticalScroll)


def test_every_choice_has_default_and_free_text_stays_empty():
    from merlin_contracts.user_input import InputQuestion

    assert InputQuestion.model_validate(QUESTIONS[0]).default == ["email"]
    assert InputQuestion.model_validate({**QUESTIONS[0], "multiple": True}).default == ["email"]
    assert InputQuestion.model_validate({**QUESTIONS[0], "default": ["google"]}).default == [
        "google"
    ]
    assert InputQuestion(id="context", question="Any context?").default == []


@pytest.mark.asyncio
async def test_campaign_defaults_reach_tool_receipt_and_tui_without_submitting():
    questions = [
        {
            "id": "profile",
            "question": "Which profile?",
            "options": [
                {"id": "recommended", "label": "Recommended"},
                {"id": "quick", "label": "Quickscan"},
                {"id": "full", "label": "Exhaustive"},
                {"id": "explore", "label": "Exploration / learning"},
            ],
            "default": ["recommended"],
        },
        {
            "id": "team",
            "question": "Which composition?",
            "options": [
                {"id": "fixed", "label": "Opinionated (default)"},
                {"id": "auto", "label": "Autonomous"},
            ],
            "default": ["fixed"],
        },
    ]
    result = await RequestUserInputTool().invoke(
        ToolContext(session_id="s1", call_id="defaults", workspace_root="."),
        {"questions": questions},
    )
    assert not result.is_error
    request = InputRequest.model_validate(json.loads(result.content)["request"])
    assert [question.default for question in request.questions] == [["recommended"], ["auto"]]
    assert request.questions[1].options[0].label == "Opinionated"

    from textual.widgets import RadioButton

    class ChoiceApp(App):
        def on_mount(self):
            self.push_screen(UserInputScreen(request))

    app = ChoiceApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, UserInputScreen)
        assert app.screen.query_one("#answer-0-0", RadioButton).value
        assert app.screen.query_one("#answer-1-1", RadioButton).value
        assert not app.screen.query_one("#answer-0-1", RadioButton).value
        assert not app.screen.query_one("#answer-1-0", RadioButton).value
        # Merely opening the screen leaves it pending; Continue is required.
        assert isinstance(app.screen, UserInputScreen)


@pytest.mark.asyncio
async def test_campaign_profile_question_adds_missing_composition_choice():
    result = await RequestUserInputTool().invoke(
        ToolContext(session_id="s1", call_id="profile-only", workspace_root="."),
        {
            "questions": [
                {
                    "id": "profile",
                    "question": "Which Merlin scan profile should I use?",
                    "options": [
                        {"id": "quickscan", "label": "Quickscan"},
                        {"id": "recommended", "label": "Recommended"},
                        {"id": "exhaustive", "label": "Exhaustive"},
                        {"id": "exploration", "label": "Exploration / learning"},
                    ],
                }
            ]
        },
    )

    assert not result.is_error
    request = InputRequest.model_validate(json.loads(result.content)["request"])
    assert [question.id for question in request.questions] == [
        "profile",
        "specialist_composition",
    ]
    assert request.questions[1].default == ["autonomous"]
    assert [option.id for option in request.questions[1].options] == [
        "autonomous",
        "opinionated",
    ]
