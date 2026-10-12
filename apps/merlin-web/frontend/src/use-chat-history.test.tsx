import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useChatHistory } from "./use-chat-history";
afterEach(cleanup);
it("loads older pages once, deduplicates IDs, and preserves the visible message", async () => {
    let finish!: (value: object) => void;
    const load = vi.fn(
        () =>
            new Promise<object>((resolve) => {
                finish = resolve;
            }),
    );
    const { result } = renderHook(() => useChatHistory(load));
    const container = document.createElement("div"),
        node = document.createElement("article");
    node.dataset.historyMessage = "51";
    container.append(node);
    node.getBoundingClientRect = () => ({ top: 10, bottom: 60 }) as DOMRect;
    container.getBoundingClientRect = () => ({ top: 0 }) as DOMRect;
    result.current.element.current = container;
    act(() =>
        result.current.reset({
            session_id: "session",
            messages: [{ id: "51" }],
            before: 51,
            has_more: true,
        }),
    );
    let request!: Promise<void>;
    act(() => {
        request = result.current.older();
        void result.current.older();
    });
    expect(load).toHaveBeenCalledTimes(1);
    expect(load).toHaveBeenCalledWith("history?before=51&session_id=session");
    node.getBoundingClientRect = vi
        .fn()
        .mockReturnValueOnce({ top: 10, bottom: 60 })
        .mockReturnValueOnce({ top: 10 })
        .mockReturnValue({ top: 210 });
    await act(async () => {
        finish({
            messages: [{ id: "1" }, { id: "51" }],
            before: 1,
            has_more: false,
        });
        await request;
    });
    expect(result.current.messages.map((m) => m.id)).toEqual(["1", "51"]);
    expect(container.scrollTop).toBe(200);
    expect(result.current.hasMore).toBe(false);
});
it("discards an old session response and permits retry after failure", async () => {
    let finish!: (value: object) => void;
    const load = vi
        .fn()
        .mockImplementationOnce(
            () =>
                new Promise((resolve) => {
                    finish = resolve;
                }),
        )
        .mockRejectedValueOnce(new Error("Offline"))
        .mockResolvedValueOnce({
            messages: [{ id: "new-old" }],
            has_more: false,
        });
    const { result } = renderHook(() => useChatHistory(load));
    act(() =>
        result.current.reset({
            session_id: "old",
            messages: [{ id: "old" }],
            before: 50,
            has_more: true,
        }),
    );
    let pending!: Promise<void>;
    act(() => {
        pending = result.current.older();
    });
    act(() =>
        result.current.reset({
            session_id: "new",
            messages: [{ id: "new" }],
            before: 100,
            has_more: true,
        }),
    );
    await act(async () => {
        finish({ messages: [{ id: "wrong-session" }] });
        await pending;
    });
    expect(result.current.messages.map((m) => m.id)).toEqual(["new"]);
    await act(async () => {
        await result.current.older();
    });
    expect(result.current.error).toBe("Offline");
    await act(async () => {
        await result.current.older();
    });
    expect(result.current.error).toBe("");
    expect(result.current.messages.map((m) => m.id)).toEqual([
        "new-old",
        "new",
    ]);
});
