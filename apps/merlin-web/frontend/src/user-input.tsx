import { useState } from "react";
import { Button } from "./ui";

export const ANSWER_PREFIX = "MERLIN_USER_INPUT_RESPONSE\n";
export type InputAnswer = { option_ids: string[]; text: string };
export type InputRequest = {
    request_id: string;
    questions: {
        id: string;
        question: string;
        options: { id: string; label: string; description: string }[];
        default: string[];
        multiple: boolean;
        allow_free_text: boolean;
    }[];
};
export function decodeInputRequest(value: unknown): InputRequest | null {
    try {
        const request = typeof value === "string" ? JSON.parse(value) : value;
        if (
            !request ||
            typeof request.request_id !== "string" ||
            !Array.isArray(request.questions)
        )
            return null;
        if (
            !request.questions.length ||
            !request.questions.every(
                (q: InputRequest["questions"][number]) =>
                    typeof q.id === "string" &&
                    typeof q.question === "string" &&
                    Array.isArray(q.options) &&
                    Array.isArray(q.default),
            )
        )
            return null;
        return request;
    } catch {
        return null;
    }
}
export function userAnswerLabel(value: string): string {
    if (!value.startsWith(ANSWER_PREFIX)) return value;
    try {
        const response = JSON.parse(value.slice(ANSWER_PREFIX.length));
        return (
            "Selected: " +
            Object.entries(response.answers as Record<string, InputAnswer>)
                .map(
                    ([question, answer]) =>
                        `${question}: ${[...answer.option_ids, answer.text].filter(Boolean).join(", ")}`,
                )
                .join(" · ")
        );
    } catch {
        return value;
    }
}
export function inputAnswersFor(
    requestId: string,
    events: { kind: string; value: unknown }[],
): Record<string, InputAnswer> | undefined {
    for (const event of events) {
        if (
            event.kind !== "user" ||
            typeof event.value !== "string" ||
            !event.value.startsWith(ANSWER_PREFIX)
        )
            continue;
        try {
            const response = JSON.parse(
                event.value.slice(ANSWER_PREFIX.length),
            );
            if (response.request_id === requestId) return response.answers;
        } catch {
            /* Historical text may not be a structured answer. */
        }
    }
}
export function UserInputChoices({
    request,
    disabled,
    answered,
    response,
    onSubmit,
}: {
    request: InputRequest;
    disabled: boolean;
    answered: boolean;
    response?: Record<string, InputAnswer>;
    onSubmit: (prompt: string) => Promise<void>;
}) {
    const [answers, setAnswers] = useState<Record<string, InputAnswer>>(() =>
        Object.fromEntries(
            request.questions.map((q) => [
                q.id,
                { option_ids: q.default, text: "" },
            ]),
        ),
    );
    const [submitting, setSubmitting] = useState(false);
    const valid = request.questions.every(
        (q) => answers[q.id]?.option_ids.length || answers[q.id]?.text.trim(),
    );
    const update = (id: string, answer: InputAnswer) =>
        setAnswers((previous) => ({ ...previous, [id]: answer }));
    return (
        <section
            className="operator-questions"
            aria-label="Merlin needs your input"
        >
            {request.questions.map((q) => (
                <fieldset
                    key={q.id}
                    disabled={disabled || answered || submitting}
                >
                    <legend>{q.question}</legend>
                    <div className="operator-options">
                        {q.options.map((option) => (
                            <label className="operator-option" key={option.id}>
                                <input
                                    type={q.multiple ? "checkbox" : "radio"}
                                    name={`${request.request_id}-${q.id}`}
                                    value={option.id}
                                    checked={(answered
                                        ? response?.[q.id]?.option_ids || []
                                        : answers[q.id].option_ids
                                    ).includes(option.id)}
                                    onChange={(e) =>
                                        update(q.id, {
                                            ...answers[q.id],
                                            option_ids: q.multiple
                                                ? e.target.checked
                                                    ? [
                                                          ...answers[q.id]
                                                              .option_ids,
                                                          option.id,
                                                      ]
                                                    : answers[
                                                          q.id
                                                      ].option_ids.filter(
                                                          (id) =>
                                                              id !== option.id,
                                                      )
                                                : [option.id],
                                        })
                                    }
                                />
                                <span>
                                    <strong>{option.label}</strong>
                                    <small>{option.description}</small>
                                </span>
                            </label>
                        ))}
                    </div>
                    {q.allow_free_text && (
                        <label className="operator-free-text">
                            Your answer or additional context
                            <textarea
                                value={
                                    answered
                                        ? response?.[q.id]?.text || ""
                                        : answers[q.id].text
                                }
                                rows={2}
                                onChange={(e) =>
                                    update(q.id, {
                                        ...answers[q.id],
                                        text: e.target.value,
                                    })
                                }
                            />
                        </label>
                    )}
                </fieldset>
            ))}
            {answered ? (
                <small>Answered or superseded</small>
            ) : (
                <Button
                    disabled={disabled || submitting || !valid}
                    onClick={async () => {
                        setSubmitting(true);
                        try {
                            await onSubmit(
                                ANSWER_PREFIX +
                                    JSON.stringify({
                                        request_id: request.request_id,
                                        answers,
                                    }),
                            );
                        } finally {
                            setSubmitting(false);
                        }
                    }}
                >
                    {submitting ? "Sending answer…" : "Continue"}
                </Button>
            )}
        </section>
    );
}
