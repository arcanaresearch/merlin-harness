export type TranscriptEvent = {
    cursor: number;
    kind: string;
    value: unknown;
    usage?: string;
};

/** Attach provider usage to its response, retaining tool and operator boundaries. */
export function displayTranscript(
    events: TranscriptEvent[],
): TranscriptEvent[] {
    const output: TranscriptEvent[] = [];
    let pendingUsage = "";
    for (const event of events) {
        const previous = output.at(-1);
        if (event.kind === "usage") {
            const usage = String(event.value ?? "");
            if (previous && ["content", "thinking"].includes(previous.kind))
                previous.usage = usage;
            else pendingUsage = usage;
            continue;
        }
        const next = { ...event };
        if (["content", "thinking"].includes(next.kind)) {
            if (pendingUsage) {
                next.usage = pendingUsage;
                pendingUsage = "";
            }
            if (
                next.kind === "content" &&
                previous?.kind === "thinking" &&
                previous.usage
            ) {
                next.usage = previous.usage;
                delete previous.usage;
            }
            if (previous?.kind === next.kind) {
                previous.value =
                    String(previous.value ?? "") + String(next.value ?? "");
                if (next.usage) previous.usage = next.usage;
                continue;
            }
        } else if (!["turn_end"].includes(next.kind)) pendingUsage = "";
        output.push(next);
    }
    return output;
}
