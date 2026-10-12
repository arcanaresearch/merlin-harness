import { displayTranscript, type TranscriptEvent } from "./transcript";

type Row = Record<string, unknown>;
type Request = (path: string) => Promise<Row>;
const PAGE_SIZE = 50;

/** Saved history and live polling have separate cursors and loading paths. */
export function createChatHistoryLoader(request: Request): Request {
    let legacy: Row[] | null = null;
    const page = (before = Infinity): Row => {
        const eligible = (legacy ?? []).filter(
            (m) => Number(m.sequence) < before,
        );
        const messages = eligible.slice(-PAGE_SIZE);
        return {
            messages,
            before: messages[0]?.sequence ?? null,
            has_more: eligible.length > messages.length,
        };
    };
    return async (path) => {
        const initial = path === "history?initial=1";
        if (!initial && legacy !== null) {
            const before = new URLSearchParams(path.split("?")[1]).get(
                "before",
            );
            return page(before ? Number(before) : Infinity);
        }
        try {
            const result = await request(path);
            if (initial) legacy = null;
            return result;
        } catch (error) {
            if (!initial || (error as { status?: number }).status !== 404)
                throw error;
        }
        // Old in-memory servers may serve rebuilt UI without the history route.
        // Drain retained events before rendering, never through the polling timer.
        let cursor = 0;
        let snapshot: Row = {};
        const retained: TranscriptEvent[] = [];
        for (;;) {
            snapshot = await request(`events?after=${cursor}`);
            const batch = (
                Array.isArray(snapshot.events) ? snapshot.events : []
            ) as TranscriptEvent[];
            const fresh = batch.filter((event) => event.cursor > cursor);
            if (batch.length && !fresh.length)
                throw new Error(
                    "Chat history cursor did not advance. Reopen /web from Merlin.",
                );
            retained.push(...fresh);
            cursor = fresh.at(-1)?.cursor ?? cursor;
            if (batch.length < 500) break;
        }
        let lastReset = -1;
        let lastUser = -1;
        retained.forEach((event, index) => {
            if (event.kind === "session_reset") lastReset = index;
        });
        const current = retained.slice(lastReset + 1);
        current.forEach((event, index) => {
            if (event.kind === "user") lastUser = index;
        });
        const activeTurn = snapshot.busy === true && lastUser >= 0;
        const saved = activeTurn ? current.slice(0, lastUser) : current;
        legacy = displayTranscript(saved)
            .filter(
                (event) =>
                    ![
                        "campaign_review",
                        "turn_end",
                        "user_input",
                        "user_input_resolved",
                    ].includes(event.kind) &&
                    !event.kind.startsWith("permission"),
            )
            .map((event) => ({
                id: `legacy-${event.cursor}`,
                sequence: event.cursor,
                role: event.kind === "content" ? "assistant" : event.kind,
                content:
                    typeof event.value === "string"
                        ? event.value
                        : JSON.stringify(event.value, null, 2),
                usage: event.usage,
            }));
        return {
            ...snapshot,
            ...page(),
            events: activeTurn ? current.slice(lastUser) : [],
            cursor,
            history_notice:
                "This running Merlin server predates chat history paging. Retained history is available; restart Merlin and reopen /web to access the full saved archive.",
        };
    };
}

export async function loadInitialChat(request: Request): Promise<Row> {
    return createChatHistoryLoader(request)("history?initial=1");
}
