import {
    cleanup,
    fireEvent,
    render,
    screen,
    waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ANSWER_PREFIX, InputRequest, UserInputChoices } from "./user-input";

afterEach(cleanup);
const request: InputRequest = {
    request_id: "question-1",
    questions: [
        {
            id: "auth",
            question: "How do you want to proceed?",
            options: [
                {
                    id: "email",
                    label: "Email magic code",
                    description: "Use an email you control",
                },
                {
                    id: "google",
                    label: "Google OAuth",
                    description: "Continue in browser",
                },
                {
                    id: "existing",
                    label: "Existing session",
                    description: "Use a configured handle",
                },
                {
                    id: "rescope",
                    label: "Change target",
                    description: "Requires a new review",
                },
            ],
            multiple: false,
            default: [],
            allow_free_text: true,
        },
    ],
};
it("sends exact IDs and free text for arbitrary authentication choices", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
        <UserInputChoices
            request={request}
            disabled={false}
            answered={false}
            onSubmit={onSubmit}
        />,
    );
    expect(
        (screen.getByRole("button", { name: "Continue" }) as HTMLButtonElement)
            .disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("radio", { name: /Existing session/ }));
    fireEvent.change(screen.getByRole("textbox"), {
        target: { value: "Use my saved test account" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(
        JSON.parse(onSubmit.mock.calls[0][0].slice(ANSWER_PREFIX.length)),
    ).toEqual({
        request_id: "question-1",
        answers: {
            auth: {
                option_ids: ["existing"],
                text: "Use my saved test account",
            },
        },
    });
});
it("supports multiple selections and never auto-submits defaults", () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
        <UserInputChoices
            request={{
                ...request,
                questions: [
                    {
                        ...request.questions[0],
                        multiple: true,
                        default: ["email"],
                    },
                ],
            }}
            disabled={false}
            answered={false}
            onSubmit={onSubmit}
        />,
    );
    expect(
        (
            screen.getByRole("checkbox", {
                name: /Email magic/,
            }) as HTMLInputElement
        ).checked,
    ).toBe(true);
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox", { name: /Google OAuth/ }));
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(
        JSON.parse(onSubmit.mock.calls[0][0].slice(ANSWER_PREFIX.length))
            .answers.auth.option_ids,
    ).toEqual(["email", "google"]);
});
it("answered questions are read-only", () => {
    render(
        <UserInputChoices
            request={request}
            disabled={false}
            answered={true}
            onSubmit={vi.fn()}
        />,
    );
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
    expect(
        (
            screen.getByRole("radio", {
                name: /Existing session/,
            }) as HTMLInputElement
        ).closest("fieldset")?.disabled,
    ).toBe(true);
});

it("retains the exact answered selection after a browser refresh", () => {
    render(
        <UserInputChoices
            request={request}
            disabled={false}
            answered={true}
            response={{
                auth: { option_ids: ["existing"], text: "Saved test account" },
            }}
            onSubmit={vi.fn()}
        />,
    );
    expect(
        (
            screen.getByRole("radio", {
                name: /Existing session/,
            }) as HTMLInputElement
        ).checked,
    ).toBe(true);
    expect(
        (screen.getByRole("radio", { name: /Email magic/ }) as HTMLInputElement)
            .checked,
    ).toBe(false);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
        "Saved test account",
    );
});

it("preselects the shared campaign defaults without submitting", () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
        <UserInputChoices
            request={{
                request_id: "campaign-defaults",
                questions: [
                    {
                        id: "profile",
                        question: "Which profile?",
                        options: [
                            {
                                id: "recommended",
                                label: "Recommended",
                                description: "",
                            },
                            {
                                id: "quick",
                                label: "Quickscan",
                                description: "",
                            },
                        ],
                        default: ["quick"],
                        multiple: false,
                        allow_free_text: true,
                    },
                    {
                        id: "team",
                        question: "Which team?",
                        options: [
                            {
                                id: "fixed",
                                label: "Opinionated",
                                description: "",
                            },
                            {
                                id: "auto",
                                label: "Autonomous",
                                description: "",
                            },
                        ],
                        default: ["auto"],
                        multiple: false,
                        allow_free_text: true,
                    },
                ],
            }}
            disabled={false}
            answered={false}
            onSubmit={onSubmit}
        />,
    );
    expect(
        (screen.getByRole("radio", { name: "Quickscan" }) as HTMLInputElement)
            .checked,
    ).toBe(true);
    expect(
        (screen.getByRole("radio", { name: "Autonomous" }) as HTMLInputElement)
            .checked,
    ).toBe(true);
    expect(
        (screen.getByRole("radio", { name: "Recommended" }) as HTMLInputElement)
            .checked,
    ).toBe(false);
    expect(
        (screen.getByRole("radio", { name: "Opinionated" }) as HTMLInputElement)
            .checked,
    ).toBe(false);
    expect(onSubmit).not.toHaveBeenCalled();
});
