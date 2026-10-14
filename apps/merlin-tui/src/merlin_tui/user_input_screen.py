"""Terminal rendering for the shared structured-question contract."""

from merlin_contracts.user_input import InputAnswer, InputRequest, InputResponse, answer_prompt
from rich.console import RenderableType
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, RadioButton, RadioSet, Static


class UserInputScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss_question", "Answer in chat")]
    DEFAULT_CSS = """
    UserInputScreen { align: center middle; background: $background 70%; }
    #operator-question { width: 90; max-width: 94%; height: auto; max-height: 90%;
        border: round $accent; padding: 1 2; background: $surface; }
    #operator-question Label { margin-top: 1; text-style: bold; }
    #operator-question RadioSet { width: 100%; height: auto; }
    #operator-question RadioButton, #operator-question Checkbox { width: 100%; height: auto; }
    #operator-question Input { margin-top: 1; }
    #operator-question Button { margin-top: 1; }
    #question-context { height: auto; margin-bottom: 1; }
    #question-error { color: $error; height: auto; }
    """

    def __init__(self, request: InputRequest, *, context: RenderableType | None = None) -> None:
        super().__init__()
        self.request = request
        self.context = context

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="operator-question"):
            if self.context is not None:
                yield Static(self.context, id="question-context")
            for index, question in enumerate(self.request.questions):
                yield Label(Text(question.question))
                if question.multiple:
                    for option_index, option in enumerate(question.options):
                        yield Checkbox(
                            Text(
                                option.label
                                + (" — " + option.description if option.description else "")
                            ),
                            value=option.id in question.default,
                            id=f"answer-{index}-{option_index}",
                        )
                elif question.options:
                    with RadioSet(id=f"options-{index}"):
                        for option_index, option in enumerate(question.options):
                            yield RadioButton(
                                Text(
                                    option.label
                                    + (" — " + option.description if option.description else "")
                                ),
                                value=option.id in question.default,
                                id=f"answer-{index}-{option_index}",
                            )
                if question.allow_free_text:
                    yield Input(placeholder="Your answer or additional context", id=f"text-{index}")
            yield Static("", id="question-error")
            yield Button("Continue", variant="primary", id="question-continue")
            yield Button("Answer in chat", id="question-close")

    @on(Button.Pressed, "#question-continue")
    def submit_answers(self) -> None:
        answers = {}
        for index, question in enumerate(self.request.questions):
            ids = [
                option.id
                for option_index, option in enumerate(question.options)
                if self.query_one(f"#answer-{index}-{option_index}").value
            ]
            answers[question.id] = InputAnswer(
                option_ids=ids,
                text=self.query_one(f"#text-{index}", Input).value
                if question.allow_free_text
                else "",
            )
        try:
            self.dismiss(
                answer_prompt(
                    self.request, InputResponse(request_id=self.request.request_id, answers=answers)
                )
            )
        except ValueError as error:
            self.query_one("#question-error", Static).update(Text(str(error)))

    @on(Button.Pressed, "#question-close")
    def action_dismiss_question(self) -> None:
        self.dismiss(None)
