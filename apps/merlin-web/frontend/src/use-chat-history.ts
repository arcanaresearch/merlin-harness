import { useCallback, useLayoutEffect, useRef, useState } from "react";

type Message = Record<string, unknown>;
export type HistoryPage = {
    session_id?: string;
    messages?: Message[];
    before?: number | null;
    has_more?: boolean;
};

export function useChatHistory(load: (query: string) => Promise<HistoryPage>) {
    const [messages, setMessages] = useState<Message[]>([]);
    const [paging, setPaging] = useState<HistoryPage>({});
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState("");
    const element = useRef<HTMLDivElement | null>(null);
    const epoch = useRef(0);
    const pending = useRef(false);
    const anchor = useRef<{ node: Element; top: number } | null>(null);
    const reset = useCallback((page: HistoryPage = {}) => {
        epoch.current++;
        pending.current = false;
        anchor.current = null;
        setMessages(page.messages ?? []);
        setPaging(page);
        setLoading(false);
        setError("");
    }, []);
    const older = async () => {
        if (pending.current || !paging.has_more || !paging.before) return;
        const version = epoch.current;
        pending.current = true;
        setLoading(true);
        setError("");
        try {
            const query = new URLSearchParams({
                before: String(paging.before),
                session_id: paging.session_id ?? "",
            });
            const page = await load(`history?${query}`);
            if (version !== epoch.current) return;
            const container = element.current;
            const visible =
                container &&
                [...container.querySelectorAll("[data-history-message]")].find(
                    (node) =>
                        node.getBoundingClientRect().bottom >=
                        container.getBoundingClientRect().top,
                );
            if (visible)
                anchor.current = {
                    node: visible,
                    top: visible.getBoundingClientRect().top,
                };
            setMessages((previous) => {
                const ids = new Set(previous.map((message) => message.id));
                return [
                    ...(page.messages ?? []).filter(
                        (message) => !ids.has(message.id),
                    ),
                    ...previous,
                ];
            });
            setPaging(page);
        } catch (cause) {
            if (version === epoch.current)
                setError(
                    cause instanceof Error ? cause.message : String(cause),
                );
        } finally {
            if (version === epoch.current) {
                pending.current = false;
                setLoading(false);
            }
        }
    };
    useLayoutEffect(() => {
        if (anchor.current && element.current) {
            element.current.scrollTop +=
                anchor.current.node.getBoundingClientRect().top -
                anchor.current.top;
            anchor.current = null;
        }
    }, [messages]);
    return {
        messages,
        reset,
        older,
        element,
        loading,
        error,
        hasMore: Boolean(paging.has_more),
    };
}
