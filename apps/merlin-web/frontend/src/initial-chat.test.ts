import { expect, it, vi } from "vitest";
import { createChatHistoryLoader, loadInitialChat } from "./initial-chat";

it("loads the newest saved page in one request and delegates older pages", async () => {
    const recent = {
        messages: [{ id: "newest" }],
        cursor: 2345,
        before: 100,
        has_more: true,
    };
    const request = vi
        .fn()
        .mockResolvedValueOnce(recent)
        .mockResolvedValueOnce({ messages: [{ id: "older" }] });
    const load = createChatHistoryLoader(request);
    expect(await load("history?initial=1")).toBe(recent);
    expect(request).toHaveBeenCalledTimes(1);
    await load("history?before=100&session_id=saved");
    expect(request).toHaveBeenLastCalledWith(
        "history?before=100&session_id=saved",
    );
});

it("drains legacy backlog before first render, then pages older messages without polling replay", async () => {
    const backlog = Array.from({ length: 1234 }, (_, i) => ({
        cursor: i + 1,
        kind: i % 2 ? "content" : "user",
        value: `Message ${i + 1}`,
    }));
    const request = vi.fn(async (path: string) => {
        if (path.startsWith("history"))
            throw Object.assign(new Error("404"), { status: 404 });
        const after = Number(path.split("=")[1]);
        return {
            events: backlog.filter((e) => e.cursor > after).slice(0, 500),
            busy: false,
        };
    });
    const load = createChatHistoryLoader(request);
    const first = await load("history?initial=1");
    expect(first.cursor).toBe(1234);
    expect(first.events).toEqual([]);
    expect(first.has_more).toBe(true);
    expect((first.messages as any[]).map((m) => m.sequence)).toEqual(
        Array.from({ length: 50 }, (_, i) => 1185 + i),
    );
    expect(request.mock.calls.map(([path]) => path)).toEqual([
        "history?initial=1",
        "events?after=0",
        "events?after=500",
        "events?after=1000",
    ]);
    let before = first.before;
    const all = [...(first.messages as any[])];
    for (;;) {
        const older = await load(`history?before=${before}&session_id=`);
        all.unshift(...(older.messages as any[]));
        before = older.before;
        if (!older.has_more) break;
    }
    expect(all.map((m) => m.sequence)).toEqual(backlog.map((e) => e.cursor));
    expect(request).toHaveBeenCalledTimes(4);
});

it("keeps a running turn live and coalesces saved chunks across batch boundaries", async () => {
    const backlog = [
        { cursor: 1, kind: "user", value: "Previous prompt" },
        ...Array.from({ length: 1001 }, (_, i) => ({
            cursor: i + 2,
            kind: "content",
            value: "x",
        })),
        { cursor: 1003, kind: "user", value: "Current prompt" },
        { cursor: 1004, kind: "thinking", value: "Working" },
    ];
    const request = vi.fn(async (path: string) => {
        if (path.startsWith("history"))
            throw Object.assign(new Error("404"), { status: 404 });
        return {
            busy: true,
            events: backlog
                .filter((e) => e.cursor > Number(path.split("=")[1]))
                .slice(0, 500),
        };
    });
    const result = await loadInitialChat(request);
    expect(result.cursor).toBe(1004);
    expect(result.events).toEqual(backlog.slice(-2));
    expect((result.messages as any[])[1].content).toBe("x".repeat(1001));
});

it("never masks auth/storage failures with a history fallback", async () => {
    for (const status of [401, 500]) {
        const error = Object.assign(new Error("failed"), { status });
        const request = vi.fn().mockRejectedValue(error);
        await expect(loadInitialChat(request)).rejects.toBe(error);
        expect(request).toHaveBeenCalledTimes(1);
    }
});

it("rejects a broken legacy cursor instead of waiting forever", async () => {
    const request = vi
        .fn()
        .mockRejectedValueOnce(Object.assign(new Error("404"), { status: 404 }))
        .mockResolvedValue({
            events: [{ cursor: 0, kind: "content", value: "bad" }],
        });
    await expect(loadInitialChat(request)).rejects.toThrow(
        "cursor did not advance",
    );
});
