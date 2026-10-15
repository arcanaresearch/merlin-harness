import { reportStatus } from "./report-status";
import { LiveCampaign } from "./live-campaign";
import { AgentOverview } from "./agent-overview";
import { createChatHistoryLoader } from "./initial-chat";
import { CampaignReview } from "./campaign-review";
import {
    StrictMode,
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
} from "react";
import { createRoot } from "react-dom/client";
import { useChatHistory } from "./use-chat-history";
import { useFollowScroll } from "./use-follow-scroll";
import { AppShell } from "./shell/AppShell";
import {
    Button,
    RootTooltipProvider,
    PageHeader,
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogDescription,
    Input,
    Textarea,
} from "./ui";
import "./styles.css";
import "./workspace.css";
import { CoverageChart, CampaignDetail } from "./coverage";
import {
    RecordFacts,
    StructuredFields,
    viewRows,
    recordSummary,
    pageGuide,
    savedProviderModels,
} from "./resource-views";
import { displayTranscript } from "./transcript";
import {
    decodeInputRequest,
    inputAnswersFor,
    UserInputChoices,
    userAnswerLabel,
} from "./user-input";
import {
    CampaignChoices,
    MessageMarkdown,
    requestedCampaignChoices,
} from "./campaign-choices";

type Row = Record<string, unknown>;
type Event = { cursor: number; kind: string; value: unknown };
const text = (value: unknown) =>
    typeof value === "string"
        ? value
        : value == null
          ? ""
          : JSON.stringify(value, null, 2);
const rows = (value: unknown): Row[] =>
    Array.isArray(value)
        ? (value.filter((x) => x && typeof x === "object") as Row[])
        : [];
const obj = (value: unknown): Row =>
    value && typeof value === "object" && !Array.isArray(value)
        ? (value as Row)
        : {};
const methods: Record<string, string> = {
    Campaigns: "list_campaigns",
    Agents: "list_agents",
    Tasks: "list_schedules",
    Findings: "list_findings",
    Reports: "list_reports",
    Sessions: "list_resume_session_page",
    Targets: "list_targets",
    "Knowledge packs": "list_packs",
    Skills: "list_skills",
    MCP: "list_mcp_servers",
    Memory: "list_memory_records",
    Browser: "list_browser_contexts",
    Permissions: "list_permission_history",
    Promotions: "list_promotion_candidates",
    Diagnostics: "run_doctor",
    Logs: "list_logs",
    History: "list_history",
    Context: "conversation_context",
    Lessons: "list_lessons",
    Models: "get_runtime_status",
    Settings: "get_runtime_status",
};
async function request(path: string, payload?: unknown): Promise<Row> {
    const response = await fetch(
        `/api/${path}`,
        payload === undefined
            ? { signal: AbortSignal.timeout(20000) }
            : {
                  method: "POST",
                  signal: AbortSignal.timeout(20000),
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(payload),
              },
    ).catch((cause: unknown) => {
        if (
            cause instanceof DOMException &&
            ["TimeoutError", "AbortError"].includes(cause.name)
        )
            throw new Error(
                "Merlin did not respond within 20 seconds. Saved data remains visible. Retry the page; if this persists, inspect daemon diagnostics.",
            );
        throw cause;
    });
    const result = await response.json().catch(() => ({
        error:
            response.status === 401
                ? "Return to the Merlin terminal and enter /web to reconnect."
                : `Request failed (${response.status})`,
    }));
    if (!response.ok)
        throw Object.assign(
            new Error(result.error || `Request failed (${response.status})`),
            { status: response.status },
        );
    return result;
}
async function rpc(
    method: string,
    args: unknown[] = [],
    kwargs: Row = {},
): Promise<unknown> {
    return (await request("rpc", { method, args, kwargs })).value;
}
function Detail({ value }: { value: unknown }) {
    return <StructuredFields value={value} />;
}
function Badge({ value }: { value: unknown }) {
    return value ? (
        <span className="badge">{text(value).replaceAll("_", " ")}</span>
    ) : null;
}
function ReasoningTrace({ value, live }: { value: unknown; live: boolean }) {
    const [open, setOpen] = useState(live);
    const body = useFollowScroll(live, live);
    useEffect(() => setOpen(live), [live]);
    return (
        <details
            className={`trace reasoning-trace ${live ? "is-live" : ""}`}
            open={open}
            onToggle={(event) => setOpen(event.currentTarget.open)}
        >
            <summary>{live ? "Thinking…" : "Reasoning"}</summary>
            <div className="reasoning-text" ref={body}>
                {text(value)}
            </div>
        </details>
    );
}
function TurnActivity({ label }: { label: string }) {
    const [seconds, setSeconds] = useState(0);
    useEffect(() => {
        const started = Date.now();
        const timer = setInterval(
            () => setSeconds(Math.floor((Date.now() - started) / 1000)),
            1000,
        );
        return () => clearInterval(timer);
    }, []);
    return (
        <div className="turn-activity" role="status">
            <span className="activity-spinner" aria-hidden="true" />
            <span>{label}</span>
            <span className="activity-elapsed" aria-hidden="true">
                {seconds}s
            </span>
        </div>
    );
}
export function App() {
    const [page, setPage] = useState(
        () => new URLSearchParams(location.search).get("view") || "Chat",
    );
    const [connected, setConnected] = useState(false);
    const [ready, setReady] = useState(false);
    const [online, setOnline] = useState(false);
    const [sending, setSending] = useState(false);
    const [pageError, setPageError] = useState("");
    const [refreshing, setRefreshing] = useState(false);
    const refreshPending = useRef(new Set<string>());
    const [connectionAttempt, setConnectionAttempt] = useState(0);
    const pendingSend = useRef<{ prompt: string; id: string } | null>(null);
    const sendingRef = useRef(false);
    const [agentRows, setAgentRows] = useState<Row[]>([]);
    const [reports, setReports] = useState<Row[]>([]);
    const [pendingPermissions, setPendingPermissions] = useState<Row[]>([]);
    const [pendingInput, setPendingInput] =
        useState<ReturnType<typeof decodeInputRequest>>(null);
    const [focusCampaign, setFocusCampaign] = useState("");
    const [setup, setSetup] = useState<Row>({});
    const [roleModels, setRoleModels] = useState<Record<string, string>>({});
    const [roleTransports, setRoleTransports] = useState<
        Record<string, string>
    >({});
    const [commands, setCommands] = useState<string[]>([]);
    const [modelEfforts, setModelEfforts] = useState<Record<string, string>>(
        {},
    );
    const [permissionRules, setPermissionRules] = useState<unknown>([]);
    const [permissionMode, setPermissionMode] = useState("");
    const [remoteName, setRemoteName] = useState("");
    const [remoteUrl, setRemoteUrl] = useState("");
    const [remoteHandle, setRemoteHandle] = useState("");
    const [remoteSecret, setRemoteSecret] = useState("");
    const [error, setError] = useState("");
    const [notice, setNotice] = useState("");
    const [dataPage, setDataPage] = useState("");
    const [data, setData] = useState<unknown>([]);
    const [campaigns, setCampaigns] = useState<Row[]>([]);
    const [sessions, setSessions] = useState<Row[]>([]);
    const [events, setEvents] = useState<Event[]>([]);
    const [busy, setBusy] = useState(false);
    const [prompt, setPrompt] = useState("");
    const [dialog, setDialog] = useState("");
    const [selected, setSelected] = useState<Row | null>(null);
    const [detail, setDetail] = useState<unknown>(null);
    const [review, setReview] = useState<Row | null>(null);
    const [search, setSearch] = useState("");
    const [visibleCount, setVisibleCount] = useState(25);
    useEffect(() => setVisibleCount(25), [page, search]);
    const [formation, setFormation] = useState("autonomous");
    const [profile, setProfile] = useState("recommended");
    const [target, setTarget] = useState("");
    const [objective, setObjective] = useState("");
    const [endpoint, setEndpoint] = useState("http://127.0.0.1:11434/v1");
    const [model, setModel] = useState("");
    const [providerId, setProviderId] = useState("custom");
    const [key, setKey] = useState("");
    const [lead, setLead] = useState("");
    const [transport, setTransport] = useState("openai_compatible");
    const [jev, setJev] = useState("");
    const loadChatHistory = useMemo(() => createChatHistoryLoader(request), []);
    const chatHistory = useChatHistory(loadChatHistory);
    const { messages } = chatHistory;
    const [dark, setDark] = useState(
        localStorage.getItem("merlin.theme") === "dark",
    );
    const cursor = useRef(0);
    const thread = useFollowScroll(page);
    const bindThread = useCallback(
        (node: HTMLDivElement | null) => {
            thread(node);
            chatHistory.element.current = node;
        },
        [thread],
    );
    const startup = useRef(false);
    const currentPage = useRef(page);
    currentPage.current = page;
    const act = useCallback(async (action: () => Promise<unknown>) => {
        setError("");
        try {
            return await action();
        } catch (cause) {
            setError(String(cause instanceof Error ? cause.message : cause));
            return undefined;
        }
    }, []);
    const refresh = useCallback(async () => {
        const loadedPage = currentPage.current;
        if (refreshPending.current.has(loadedPage)) return;
        refreshPending.current.add(loadedPage);
        setRefreshing(true);
        // Render the selected view independently. A failed inventory must not
        // block chat, navigation, or another page's successful response.
        const load = async (
            method: string,
            apply: (value: unknown) => void,
            kwargs: Row = {},
            primary = false,
        ) => {
            try {
                apply(await rpc(method, [], kwargs));
            } catch (cause) {
                if (primary && currentPage.current === loadedPage)
                    setPageError(
                        `Could not refresh ${loadedPage.toLowerCase()}; showing saved data if available. ${cause instanceof Error ? cause.message : String(cause)}`,
                    );
            }
        };
        const jobs: Promise<void>[] = [];
        if (["Settings", "Models"].includes(loadedPage))
            jobs.push(load("setup_status", (v) => setSetup(obj(v))));
        if (loadedPage !== "Campaigns")
            jobs.push(load("list_campaigns", (v) => setCampaigns(rows(v))));
        if (["Chat", "Coverage"].includes(loadedPage))
            jobs.push(
                load("list_reports", (v) => setReports(rows(v)), {
                    session_only: true,
                }),
            );
        if (loadedPage === "Chat")
            jobs.push(load("list_agents", (v) => setAgentRows(rows(v))));
        const method = methods[loadedPage];
        if (method)
            jobs.push(
                load(
                    method,
                    (result) => {
                        if (currentPage.current === loadedPage) {
                            setData(
                                loadedPage === "Sessions"
                                    ? rows(obj(result).items)
                                    : result,
                            );
                            if (loadedPage === "Campaigns")
                                setCampaigns(rows(result));
                            if (loadedPage === "Reports")
                                setReports(rows(result));
                            if (loadedPage === "Agents")
                                setAgentRows(rows(result));
                            setDataPage(loadedPage);
                            setPageError("");
                        }
                    },
                    loadedPage === "Sessions" ? { limit: 50, offset: 0 } : {},
                    true,
                ),
            );
        if (loadedPage === "Permissions") {
            jobs.push(load("list_permission_rules", setPermissionRules));
            jobs.push(
                load("get_permission_mode", (v) => setPermissionMode(text(v))),
            );
        }
        await Promise.allSettled(jobs);
        refreshPending.current.delete(loadedPage);
        setRefreshing(refreshPending.current.has(currentPage.current));
    }, []);
    useEffect(() => {
        const navigate = () =>
            setPage(new URLSearchParams(location.search).get("view") || "Chat");
        const shortcuts = (event: KeyboardEvent) => {
            if (!event.ctrlKey || event.metaKey || dialog || selected) return;
            if (event.key.toLowerCase() === "k") {
                event.preventDefault();
                setDialog("Commands");
            }
            if (event.key.toLowerCase() === "n") {
                event.preventDefault();
                setDialog(event.shiftKey ? "New campaign" : "New session");
            }
        };
        window.addEventListener("popstate", navigate);
        window.addEventListener("keydown", shortcuts);
        return () => {
            window.removeEventListener("popstate", navigate);
            window.removeEventListener("keydown", shortcuts);
        };
    }, [dialog, selected]);
    useEffect(() => {
        const url = new URL(location.href);
        if ((url.searchParams.get("view") || "Chat") !== page) {
            url.searchParams.set("view", page);
            history.pushState(null, "", url);
        }
    }, [page]);
    useEffect(() => {
        document.documentElement.dataset.theme = dark
            ? "pirate-dark"
            : "pirate-light";
        localStorage.setItem("merlin.theme", dark ? "dark" : "light");
    }, [dark]);
    useEffect(() => {
        const reconnect = () => {
            if (!new URLSearchParams(location.hash.slice(1)).has("token"))
                return;
            startup.current = false;
            cursor.current = 0;
            setEvents([]);
            chatHistory.reset();
            setReview(null);
            setDialog("");
            setSelected(null);
            setConnected(false);
            setOnline(false);
            setConnectionAttempt((value) => value + 1);
        };
        window.addEventListener("hashchange", reconnect);
        return () => window.removeEventListener("hashchange", reconnect);
    }, []);
    useEffect(() => {
        if (startup.current) return;
        startup.current = true;
        void act(async () => {
            const token = new URLSearchParams(location.hash.slice(1)).get(
                "token",
            );
            if (token) {
                await request("connect", { token });
                history.replaceState(
                    null,
                    "",
                    location.pathname + location.search,
                );
            }
            const savedChat = await loadChatHistory("history?initial=1");
            chatHistory.reset(savedChat);
            if (savedChat.history_notice)
                setNotice(text(savedChat.history_notice));
            setEvents(rows(savedChat.events) as unknown as Event[]);
            cursor.current = Number(savedChat.cursor ?? 0);
            setConnected(true);
            setOnline(true);
            setReady(true);
            // Saved chat appears before daemon inventories finish loading.
            const settings = obj(await rpc("setup_status"));
            setSetup(settings);
            setCommands((await rpc("command_catalog")) as string[]);
            setModelEfforts(
                obj(obj(settings.provider).model_efforts) as Record<
                    string,
                    string
                >,
            );
            const configured = obj(settings.provider);
            if (configured.base_url) setEndpoint(text(configured.base_url));
            setProviderId(
                text(
                    rows(settings.providers).find(
                        (p) => p.base_url === configured.base_url,
                    )?.id,
                ) || "custom",
            );
            if (configured.model) setModel(text(configured.model));
            setRoleModels({
                ...obj(configured.role_models),
                worker: text(configured.model),
                lead: text(
                    obj(configured.role_models)["campaign-lead"] ||
                        obj(configured.role_models).lead,
                ),
                adversarial_explorer: text(
                    obj(configured.role_models)["adversarial-explorer"],
                ),
            } as Record<string, string>);
            setRoleTransports(
                obj(configured.model_transports) as Record<string, string>,
            );
            setConnected(true);
            setNotice("Connecting to the Merlin daemon…");
            if (!configured.model) {
                setPage("Settings");
                setNotice("Choose provider access and a model to start.");
            }
            const saved = obj(
                await rpc("list_resume_session_page", [], {
                    limit: 50,
                    offset: 0,
                }),
            );
            setSessions(rows(saved.items));
            if (
                rows(saved.items).length &&
                !rows(savedChat.messages).length &&
                !rows(savedChat.events).length
            )
                setDialog("Resume session");
            setNotice(text(savedChat.history_notice));
        });
    }, [act, refresh, connectionAttempt]);
    useEffect(() => {
        if (!connected) return;
        let stopped = false;
        let timer: ReturnType<typeof setTimeout>;
        async function poll() {
            try {
                const result = await request(`events?after=${cursor.current}`);
                if (stopped) return;
                setOnline(true);
                setError((previous) =>
                    previous.startsWith("Connection interrupted")
                        ? ""
                        : previous,
                );
                const incoming = rows(result.events) as unknown as Event[];
                if (incoming.length) {
                    cursor.current = incoming[incoming.length - 1].cursor;
                    setEvents((previous) => {
                        const reset = incoming.findLastIndex(
                            (e) => e.kind === "session_reset",
                        );
                        return [
                            ...(reset >= 0 ? [] : previous),
                            ...incoming.slice(reset >= 0 ? reset + 1 : 0),
                        ].slice(-5000);
                    });
                    for (const event of incoming) {
                        if (
                            [
                                "thinking",
                                "content",
                                "error",
                                "turn_end",
                            ].includes(event.kind) ||
                            event.kind.startsWith("tool")
                        )
                            setNotice("");
                        if (event.kind === "campaign_configure") {
                            setReview(null);
                            setFocusCampaign("");
                        }
                        if (event.kind === "session_reset") {
                            setReview(null);
                            chatHistory.reset();
                            setFocusCampaign("");
                            const savedChat =
                                await loadChatHistory("history?initial=1");
                            chatHistory.reset(savedChat);
                            if (savedChat.history_notice)
                                setNotice(text(savedChat.history_notice));
                            setEvents(
                                rows(savedChat.events) as unknown as Event[],
                            );
                            cursor.current = Number(
                                savedChat.cursor ?? cursor.current,
                            );
                        }
                        if (event.kind === "campaign_review") {
                            try {
                                const recovered = obj(
                                    JSON.parse(text(event.value)),
                                );
                                setReview(recovered);
                                setFocusCampaign(text(recovered.campaign_id));
                            } catch {
                                setError(
                                    "Campaign review could not be decoded. Approval is unavailable.",
                                );
                            }
                        }
                    }
                }
                // The snapshot is authoritative: replayed historical review events
                // must not resurrect an approval already executed in another tab.
                setReview(
                    result.review
                        ? obj(
                              typeof result.review === "string"
                                  ? JSON.parse(result.review)
                                  : result.review,
                          )
                        : null,
                );
                setPendingPermissions(rows(result.permissions));
                setPendingInput(decodeInputRequest(result.user_input));
                if (result.expired)
                    setNotice(
                        "Older live events are available in the retained session history.",
                    );
                setBusy(Boolean(result.busy));
            } catch (cause) {
                if (!stopped) {
                    setOnline(false);
                    if (
                        cause instanceof Error &&
                        "status" in cause &&
                        cause.status === 401
                    ) {
                        setConnected(false);
                        setDialog("");
                        setSelected(null);
                        setPageError("");
                    }
                    setError(
                        `Connection interrupted. Keep the Merlin terminal open. ${String(cause)}`,
                    );
                }
            }
            if (!stopped) timer = setTimeout(poll, 650);
        }
        void poll();
        return () => {
            stopped = true;
            clearTimeout(timer);
        };
    }, [connected]);
    useEffect(() => {
        if (!connected) return;
        let active = true;
        async function update() {
            try {
                await refresh();
            } catch (cause) {
                if (active)
                    setError(
                        `Refresh failed; showing retained state. ${String(cause)}`,
                    );
            }
        }
        let timer: ReturnType<typeof setTimeout>;
        async function loop() {
            await update();
            if (active) timer = setTimeout(loop, 3500);
        }
        void loop();
        return () => {
            active = false;
            clearTimeout(timer);
        };
    }, [connected, page, refresh]);
    const send = async (value: string, confirmed = false) => {
        if (sendingRef.current || !value.trim()) return;
        if (value.trim() === "/web") {
            setPage("Chat");
            setNotice("This session is already open in the browser.");
            setPrompt("");
            return;
        }
        if (value.trim() === "/setup endpoint") {
            setDialog("Model endpoint");
            setPrompt("");
            return;
        }
        if (value.trim() === "/mcp remote") {
            setDialog("Connect remote MCP");
            setPrompt("");
            return;
        }
        if (value.trim() === "/doctor config") {
            setDetail(await act(() => rpc("export_safe_config")));
            setSelected({ title: "Safe configuration" });
            setPrompt("");
            return;
        }
        const viewCommands: Record<string, string> = {
            "/setup": "Settings",
            "/setup endpoint": "Settings",
            "/setup jev": "Settings",
            "/model": "Models",
            "/agents": "Agents",
            "/tasks": "Tasks",
            "/report": "Reports",
            "/findings": "Findings",
            "/targets": "Targets",
            "/library": "Knowledge packs",
            "/skills": "Skills",
            "/mcp": "MCP",
            "/memory": "Memory",
            "/browser": "Browser",
            "/permissions": "Permissions",
            "/mode": "Permissions",
            "/promotion": "Promotions",
            "/doctor": "Diagnostics",
            "/logs": "Logs",
            "/history": "History",
            "/context": "Context",
            "/lessons": "Lessons",
            "/campaigns": "Campaigns",
        };
        if (viewCommands[value.trim()]) {
            setPage(viewCommands[value.trim()]);
            setPrompt("");
            setDialog("");
            return;
        }
        if (
            !confirmed &&
            ["/new", "/resume", "/sessions"].includes(value.trim())
        ) {
            setDialog(
                value.trim() === "/new" ? "New session" : "Resume session",
            );
            setPrompt("");
            return;
        }
        if (value.trim() === "/interrupt") {
            await run("interrupt");
            setPrompt("");
            return;
        }
        if (value.trim() === "/clear") {
            setEvents([]);
            chatHistory.reset();
            setPrompt("");
            return;
        }

        sendingRef.current = true;
        setSending(true);
        // Preserve the identity after an uncertain HTTP response. Retrying the
        // same draft must not create a second foreground turn.
        if (pendingSend.current?.prompt !== value)
            pendingSend.current = { prompt: value, id: crypto.randomUUID() };
        try {
            const result = await act(() =>
                request("chat", {
                    prompt: value,
                    request_id: pendingSend.current!.id,
                }),
            );
            if (result) {
                pendingSend.current = null;
                setPrompt((draft) => (draft === value ? "" : draft));
                setPage("Chat");
                setDialog("");
                setSelected(null);
                setNotice("Message accepted. Waiting for Merlin’s response…");
            }
            return Boolean(result);
        } finally {
            sendingRef.current = false;
            setSending(false);
        }
    };
    const newSession = async () => {
        await send("/new", true);
    };
    const run = async (
        method: string,
        args: unknown[] = [],
        kwargs: Row = {},
    ) => {
        const result = await act(() => rpc(method, args, kwargs));
        if (result !== undefined) {
            setNotice(typeof result === "string" ? result : "Saved.");
            await act(refresh);
        }
        return result;
    };
    useEffect(() => {
        setSelected(null);
        setSearch("");
        setPageError("");
    }, [page]);
    const inspect = async (item: Row) => {
        setSelected(item);
        setDetail(item);
        if (page === "Campaigns" || page === "Coverage")
            setDetail(await act(() => rpc("campaign_status", [item.id])));
        if (page === "Agents" || (page === "Chat" && item.assignment_id))
            setDetail(
                await act(() =>
                    rpc("get_agent_transcript", [item.assignment_id]),
                ),
            );
    };
    const active = campaigns.filter((c) =>
        ["executing", "paused"].includes(text(c.status)),
    );
    const permissions = pendingPermissions.map((value, i) => ({
        cursor: i,
        kind: "permission",
        value,
    }));
    const latestUserCursor =
        [...events].reverse().find((e) => e.kind === "user")?.cursor ?? -1;
    const latestReply = events
        .filter((e) => e.cursor > latestUserCursor && e.kind === "content")
        .map((e) => text(e.value))
        .join("");
    const requestedChoices = requestedCampaignChoices(latestReply);
    const lastTurnEnd =
        [...events].reverse().find((e) => e.kind === "turn_end")?.cursor ?? -1;
    const latestActivity = [...events]
        .reverse()
        .find(
            (e) =>
                e.cursor > lastTurnEnd &&
                (e.kind === "thinking" ||
                    e.kind === "content" ||
                    e.kind.startsWith("tool")),
        );
    const activityLabel = pendingPermissions.length
        ? "Waiting for your approval"
        : latestActivity?.kind === "thinking"
          ? "Thinking…"
          : latestActivity?.kind === "content"
            ? "Writing response…"
            : latestActivity?.kind.startsWith("tool")
              ? text(latestActivity.value) || "Using a tool…"
              : "Waiting for model…";
    const choosing =
        (requestedChoices.profile || requestedChoices.formation) &&
        !busy &&
        !pendingInput;
    const formationInstruction = `The operator explicitly selected specialist composition: ${formation} (orthogonal to the coverage profile); pass specialist_composition='${formation}' to assessment_prepare and do not ask again.`;
    const download = async (report: Row, format: string) => {
        if (format === "html" && text(report.output).endsWith(".html")) {
            window.open(
                `/api/file?path=${encodeURIComponent(text(report.output))}`,
                "_blank",
                "noopener",
            );
            return;
        }
        const result = obj(
            await act(() =>
                rpc("export_campaign_report", [
                    report.run,
                    report.output,
                    format,
                ]),
            ),
        );
        if (result.output) {
            const link = document.createElement("a");
            link.href = `/api/file?path=${encodeURIComponent(text(result.output))}`;
            link.download =
                text(result.output).split("/").pop() ||
                `merlin-report.${format}`;
            document.body.append(link);
            link.click();
            link.remove();
            setNotice(
                `${format.toUpperCase()} export prepared. The saved campaign is unchanged.`,
            );
        }
    };
    const groups = [
        { label: "Chat", pages: ["Chat"] },
        {
            label: "Campaigns",
            pages: ["Campaigns", "Agents", "Tasks", "Coverage"],
        },
        { label: "Results", pages: ["Findings", "Reports"] },
        {
            label: "Resources",
            pages: [
                "Targets",
                "Knowledge packs",
                "Skills",
                "MCP",
                "Memory",
                "Browser",
            ],
        },
        {
            label: "Sessions",
            pages: ["Sessions", "History", "Context", "Lessons"],
        },
        {
            label: "Settings",
            pages: [
                "Settings",
                "Models",
                "Permissions",
                "Promotions",
                "Diagnostics",
                "Logs",
            ],
        },
    ];
    const currentGroup =
        groups.find((group) => group.pages.includes(page)) || groups[0];
    const sections = [
        {
            id: "workspace",
            label: "Workspace",
            items: groups.map((group) => ({
                id: group.label,
                label: group.label,
                active: currentGroup === group,
                onClick: () => setPage(group.pages[0]),
            })),
        },
        {
            id: "saved",
            label: "Recent sessions",
            items: sessions.slice(0, 5).map((session) => ({
                id: text(session.id),
                label: text(session.title) || text(session.id),
                onClick: () => {
                    setSelected(session);
                    setDialog("Resume this session");
                },
            })),
        },
    ];
    const pageRows = (
        page === "Coverage"
            ? campaigns
            : dataPage === page
              ? viewRows(page, data)
              : []
    ).filter((item) => text(item).toLowerCase().includes(search.toLowerCase()));
    return (
        <RootTooltipProvider>
            <AppShell
                chordsEnabled={connected && !dialog && !selected}
                sidebar={{
                    sections,
                    primaryAction: {
                        label: "New session",
                        onClick: () => setDialog("New session"),
                        shortcut: ["⌘", "N"],
                    },
                }}
                onNewChat={() => setDialog("New session")}
                onNewScan={() => setDialog("New campaign")}
                titlebar={{
                    onTogglePalette: () => setDialog("Commands"),
                    onToggleSettings: () => setPage("Settings"),
                }}
                statusbar={{
                    connection: {
                        label: online
                            ? "Merlin connected"
                            : ready || error
                              ? "Merlin disconnected"
                              : "Connecting to Merlin",
                        tone: online ? "good" : "warn",
                    },
                    activity:
                        busy || active.length
                            ? {
                                  label: busy
                                      ? "Foreground working"
                                      : `${active.length} active campaigns`,
                                  onSelect: () => setPage("Agents"),
                              }
                            : undefined,
                }}
            >
                <div className="workspace">
                    {currentGroup.pages.length > 1 && (
                        <nav
                            className="section-navigation"
                            aria-label={`${currentGroup.label} pages`}
                        >
                            {currentGroup.pages.map((destination) => (
                                <Button
                                    key={destination}
                                    variant={
                                        page === destination
                                            ? "secondary"
                                            : "ghost"
                                    }
                                    aria-current={
                                        page === destination
                                            ? "page"
                                            : undefined
                                    }
                                    onClick={() => setPage(destination)}
                                >
                                    {destination === "Settings"
                                        ? "Providers"
                                        : destination}
                                </Button>
                            ))}
                        </nav>
                    )}
                    {pageError && (
                        <div className="banner error" role="alert">
                            {pageError}
                            <Button
                                variant="ghost"
                                onClick={() => void act(refresh)}
                            >
                                Retry page
                            </Button>
                        </div>
                    )}
                    {error && !dialog && !selected && (
                        <div className="banner error" role="alert">
                            {error}
                            <Button
                                variant="ghost"
                                onClick={() => {
                                    setError("");
                                    if (!connected) {
                                        startup.current = false;
                                        setConnectionAttempt((v) => v + 1);
                                    } else void act(refresh);
                                }}
                            >
                                Retry
                            </Button>
                        </div>
                    )}
                    {notice && (
                        <div className="banner" role="status">
                            {notice}
                            <Button
                                variant="ghost"
                                onClick={() => setNotice("")}
                            >
                                Dismiss
                            </Button>
                        </div>
                    )}
                    {!connected ? (
                        <div className="connection-panel" role="status">
                            <h1>
                                {error
                                    ? "Connect this browser to Merlin"
                                    : "Connecting to Merlin…"}
                            </h1>
                            <p>
                                In your running Merlin terminal, enter{" "}
                                <code>/web</code> and use the browser tab it
                                opens.
                            </p>
                            <p>
                                This page needs a connection before it can load
                                sessions or campaigns.
                            </p>
                        </div>
                    ) : page === "Chat" ? (
                        <>
                            <div className="chat-header">
                                <h1 className="conversation-title">
                                    Conversation
                                </h1>
                                <div>
                                    <Button
                                        variant="ghost"
                                        onClick={() =>
                                            setDialog("Resume session")
                                        }
                                    >
                                        Resume
                                    </Button>
                                    <Button
                                        onClick={() =>
                                            setDialog("New campaign")
                                        }
                                    >
                                        New campaign
                                    </Button>
                                </div>
                            </div>
                            <div
                                className="thread"
                                ref={bindThread}
                                onScroll={(event) => {
                                    if (event.currentTarget.scrollTop < 160)
                                        void chatHistory.older();
                                }}
                            >
                                {(chatHistory.hasMore ||
                                    chatHistory.loading ||
                                    chatHistory.error) && (
                                    <div
                                        className="history-loader"
                                        aria-live="polite"
                                    >
                                        {chatHistory.loading ? (
                                            "Loading older messages…"
                                        ) : (
                                            <button
                                                onClick={() =>
                                                    void chatHistory.older()
                                                }
                                            >
                                                {chatHistory.error
                                                    ? "Retry loading older messages"
                                                    : "Load older messages"}
                                            </button>
                                        )}
                                        {chatHistory.error && (
                                            <p role="alert">
                                                {chatHistory.error}
                                            </p>
                                        )}
                                    </div>
                                )}
                                {!events.length &&
                                    !messages.length &&
                                    !review && (
                                        <div className="intro">
                                            <img
                                                src="/merlin-mark.png"
                                                alt="Merlin wizard"
                                            />
                                            <h2>
                                                Your security team.
                                                <br />
                                                Your terminal. Now your browser.
                                            </h2>
                                            <p>
                                                Give Merlin a target and an
                                                objective. Review the plan,
                                                guide your agents, and keep the
                                                evidence.
                                            </p>
                                            <div className="suggestions">
                                                <button
                                                    onClick={() =>
                                                        setDialog(
                                                            "New campaign",
                                                        )
                                                    }
                                                >
                                                    Start a security assessment{" "}
                                                    <span>↗</span>
                                                </button>
                                                <button
                                                    onClick={() =>
                                                        setDialog(
                                                            "Resume session",
                                                        )
                                                    }
                                                >
                                                    Continue a saved session{" "}
                                                    <span>↗</span>
                                                </button>
                                                <button
                                                    onClick={() =>
                                                        setPage("Settings")
                                                    }
                                                >
                                                    Connect your models{" "}
                                                    <span>↗</span>
                                                </button>
                                            </div>
                                        </div>
                                    )}
                                {messages
                                    .filter(
                                        (m) =>
                                            !(
                                                m.role === "tool" &&
                                                m.name === "request_user_input"
                                            ),
                                    )
                                    .map((m, i) => (
                                        <article
                                            key={`saved-${m.id ?? i}`}
                                            data-history-message={text(
                                                m.id ?? i,
                                            )}
                                            className={`message ${text(m.role)}`}
                                        >
                                            <small>{text(m.role)}</small>
                                            {m.role === "thinking" ? (
                                                <ReasoningTrace
                                                    value={m.content}
                                                    live={false}
                                                />
                                            ) : text(m.role).startsWith(
                                                  "tool",
                                              ) ? (
                                                <details className="trace">
                                                    <summary>
                                                        {text(m.content)
                                                            .split("\n")[0]
                                                            .slice(0, 160) ||
                                                            "Tool activity"}
                                                    </summary>
                                                    <Detail value={m.content} />
                                                </details>
                                            ) : m.role === "assistant" ? (
                                                <MessageMarkdown
                                                    content={text(m.content)}
                                                />
                                            ) : (
                                                <div>
                                                    {m.role === "user"
                                                        ? userAnswerLabel(
                                                              text(m.content),
                                                          )
                                                        : text(m.content)}
                                                </div>
                                            )}
                                            {Boolean(m.usage) && (
                                                <footer className="response-usage">
                                                    {text(m.usage)}
                                                </footer>
                                            )}
                                        </article>
                                    ))}
                                {displayTranscript(events).map((e) =>
                                    e.kind === "campaign_review" ||
                                    e.kind.startsWith("permission") ||
                                    e.kind === "turn_end" ? null : e.kind ===
                                      "thinking" ? (
                                        <div
                                            className="response-block"
                                            key={e.cursor}
                                        >
                                            <ReasoningTrace
                                                value={e.value}
                                                live={
                                                    busy &&
                                                    e.cursor > lastTurnEnd
                                                }
                                            />
                                            {e.usage && (
                                                <footer className="response-usage">
                                                    {e.usage}
                                                </footer>
                                            )}
                                        </div>
                                    ) : e.kind === "user_input" ? (
                                        decodeInputRequest(e.value) && (
                                            <UserInputChoices
                                                key={e.cursor}
                                                request={decodeInputRequest(
                                                    e.value,
                                                )!}
                                                disabled={busy || sending}
                                                response={inputAnswersFor(
                                                    decodeInputRequest(e.value)!
                                                        .request_id,
                                                    events,
                                                )}
                                                answered={
                                                    decodeInputRequest(e.value)
                                                        ?.request_id !==
                                                    pendingInput?.request_id
                                                }
                                                onSubmit={async (value) => {
                                                    await send(value);
                                                }}
                                            />
                                        )
                                    ) : e.kind ===
                                      "user_input_resolved" ? null : e.kind.startsWith(
                                          "tool",
                                      ) ? (
                                        <details
                                            className="trace"
                                            key={e.cursor}
                                        >
                                            <summary>
                                                {text(e.value)
                                                    .split("\n")[0]
                                                    .slice(0, 160) ||
                                                    e.kind.replaceAll("_", " ")}
                                                {text(e.value).split("\n")[0]
                                                    .length > 160
                                                    ? "…"
                                                    : ""}
                                            </summary>
                                            <Detail value={e.value} />
                                        </details>
                                    ) : (
                                        <article
                                            className={`message ${e.kind}`}
                                            key={e.cursor}
                                        >
                                            <small>
                                                {e.kind === "user"
                                                    ? "You"
                                                    : e.kind === "content"
                                                      ? "Merlin"
                                                      : e.kind}
                                            </small>
                                            {e.kind === "content" ? (
                                                <MessageMarkdown
                                                    content={text(e.value)}
                                                />
                                            ) : (
                                                <div>
                                                    {e.kind === "user"
                                                        ? userAnswerLabel(
                                                              text(e.value),
                                                          )
                                                        : text(e.value)}
                                                </div>
                                            )}
                                            {e.usage && (
                                                <footer className="response-usage">
                                                    {e.usage}
                                                </footer>
                                            )}
                                        </article>
                                    ),
                                )}
                                {pendingInput &&
                                    !events.some(
                                        (e) =>
                                            e.kind === "user_input" &&
                                            decodeInputRequest(e.value)
                                                ?.request_id ===
                                                pendingInput.request_id,
                                    ) && (
                                        <UserInputChoices
                                            key={pendingInput.request_id}
                                            request={pendingInput}
                                            disabled={busy || sending}
                                            answered={false}
                                            onSubmit={async (value) => {
                                                await send(value);
                                            }}
                                        />
                                    )}
                                {(busy || sending) && (
                                    <TurnActivity
                                        label={
                                            sending
                                                ? "Sending message…"
                                                : activityLabel
                                        }
                                    />
                                )}
                                {choosing && (
                                    <CampaignChoices
                                        profile={profile}
                                        formation={formation}
                                        showProfile={requestedChoices.profile}
                                        showFormation={
                                            requestedChoices.formation
                                        }
                                        onProfile={setProfile}
                                        onFormation={setFormation}
                                        disabled={sending}
                                        onContinue={() =>
                                            void send(
                                                [
                                                    requestedChoices.profile
                                                        ? `The operator explicitly selected campaign profile: ${profile}; pass profile='${profile}' to campaign_configure and assessment_prepare. Do not ask for the profile again.`
                                                        : "",
                                                    requestedChoices.formation
                                                        ? formationInstruction
                                                        : "",
                                                ]
                                                    .filter(Boolean)
                                                    .join("\n"),
                                            )
                                        }
                                    />
                                )}
                                {!busy &&
                                    review &&
                                    !campaigns.some(
                                        (c) =>
                                            c.id === review.campaign_id &&
                                            text(c.status) !== "prepared",
                                    ) && (
                                        <CampaignReview
                                            key={text(review.manifest_digest)}
                                            review={review}
                                            onApprove={async () => {
                                                const result = await act(() =>
                                                    rpc("execute_campaign", [
                                                        review.campaign_id,
                                                        review.manifest_digest,
                                                    ]),
                                                );
                                                if (result === undefined)
                                                    return false;
                                                setReview(null);
                                                setNotice("Campaign started.");
                                                void act(refresh);
                                                return true;
                                            }}
                                            onCancel={async () => {
                                                const result = await act(() =>
                                                    rpc("cancel_campaign", [
                                                        review.campaign_id,
                                                    ]),
                                                );
                                                if (result === undefined)
                                                    return false;
                                                setReview(null);
                                                setNotice(
                                                    "Preparation cancelled.",
                                                );
                                                void act(refresh);
                                                return true;
                                            }}
                                            onHybrid={async () => {
                                                return Boolean(
                                                    await send(
                                                        "Use HYBRID execution mode and prepare the campaign again for review.",
                                                    ),
                                                );
                                            }}
                                            onChanges={() => {
                                                setPrompt(
                                                    "Change the prepared campaign: ",
                                                );
                                                setNotice(
                                                    "Describe your changes in the message box, then send them to Merlin.",
                                                );
                                            }}
                                        />
                                    )}
                                {permissions.map((e) => (
                                    <section className="card" key={e.cursor}>
                                        <h2>Tool permission requested</h2>
                                        <p>
                                            Tool:{" "}
                                            {text(obj(obj(e.value).tool).name)}{" "}
                                            · Effect:{" "}
                                            {text(obj(e.value).effect)}
                                        </p>
                                        <pre className="permission-details">
                                            {text(obj(e.value).details)}
                                        </pre>
                                        {obj(e.value).allow_command_session ===
                                            true && (
                                            <p>
                                                This session allowance covers
                                                this exact command, arguments,
                                                and directory. Changes require
                                                approval again.
                                            </p>
                                        )}
                                        <div className="actions">
                                            <Button
                                                onClick={() =>
                                                    void act(() =>
                                                        request("permission", {
                                                            id: obj(e.value).id,
                                                            decision:
                                                                "allow_once",
                                                        }),
                                                    )
                                                }
                                            >
                                                Allow once
                                            </Button>
                                            {obj(e.value)
                                                .allow_command_session ===
                                                true && (
                                                <Button
                                                    variant="secondary"
                                                    onClick={() =>
                                                        void act(() =>
                                                            request(
                                                                "permission",
                                                                {
                                                                    id: obj(
                                                                        e.value,
                                                                    ).id,
                                                                    decision:
                                                                        "allow_command_session",
                                                                },
                                                            ),
                                                        )
                                                    }
                                                >
                                                    Allow this command +
                                                    directory (session)
                                                </Button>
                                            )}
                                            <Button
                                                variant="secondary"
                                                onClick={() =>
                                                    void act(() =>
                                                        request("permission", {
                                                            id: obj(e.value).id,
                                                            decision: "deny",
                                                        }),
                                                    )
                                                }
                                            >
                                                Deny
                                            </Button>
                                        </div>
                                    </section>
                                ))}
                                {!busy &&
                                    !review &&
                                    reports
                                        .filter((r) => r.run === focusCampaign)
                                        .slice(-1)
                                        .map((r) => (
                                            <section
                                                className="card"
                                                key={text(r.output)}
                                            >
                                                <div className="eyebrow">
                                                    Campaign report ready
                                                </div>
                                                <h2>{reportStatus(r).title}</h2>
                                                <div className="metrics">
                                                    <span>
                                                        {text(r.finding_count)}{" "}
                                                        findings
                                                    </span>
                                                    <span>
                                                        {text(r.exchange_count)}{" "}
                                                        exchanges
                                                    </span>
                                                    <span>
                                                        Cleanup{" "}
                                                        {text(r.cleanup_status)}
                                                    </span>
                                                </div>
                                                <CoverageChart
                                                    value={r.coverage}
                                                />
                                                <p>{reportStatus(r).detail}</p>
                                                {reportStatus(r).assessment && (
                                                    <small>
                                                        {
                                                            reportStatus(r)
                                                                .assessment
                                                        }
                                                    </small>
                                                )}
                                                <div className="actions">
                                                    {[
                                                        "html",
                                                        "markdown",
                                                        "json",
                                                        "csv",
                                                    ].map((f) => (
                                                        <Button
                                                            key={f}
                                                            variant="secondary"
                                                            onClick={() =>
                                                                void download(
                                                                    r,
                                                                    f,
                                                                )
                                                            }
                                                        >
                                                            {f}
                                                        </Button>
                                                    ))}
                                                </div>
                                                <small>
                                                    Enter /new to start a new
                                                    session.
                                                </small>
                                            </section>
                                        ))}
                                {active.map((c) => (
                                    <LiveCampaign
                                        key={text(c.id)}
                                        campaign={c}
                                        assignments={agentRows}
                                        onInspect={(item) => void inspect(item)}
                                        onAgents={() => setPage("Agents")}
                                        onGuide={() => {
                                            setSelected(c);
                                            setDialog("Guide campaign");
                                        }}
                                        onPause={() =>
                                            void run(
                                                c.status === "paused"
                                                    ? "resume_campaign"
                                                    : "pause_campaign",
                                                [c.id],
                                            )
                                        }
                                    />
                                ))}
                            </div>
                            <form
                                className="composer"
                                onSubmit={(e) => {
                                    e.preventDefault();
                                    if (online && !sending && prompt.trim())
                                        void send(prompt);
                                }}
                            >
                                <Textarea
                                    aria-label="Message Merlin"
                                    placeholder={
                                        busy
                                            ? "Send guidance while Merlin works…"
                                            : "Ask Merlin or enter a /command…"
                                    }
                                    value={prompt}
                                    onChange={(e) => setPrompt(e.target.value)}
                                    onKeyDown={(e) => {
                                        if (e.key === "Enter" && !e.shiftKey) {
                                            e.preventDefault();
                                            if (
                                                online &&
                                                !sending &&
                                                prompt.trim()
                                            )
                                                void send(prompt);
                                        }
                                    }}
                                />
                                <div>
                                    <span>
                                        {busy
                                            ? "Working · your messages can guide this turn"
                                            : "Enter to send · Shift+Enter for a new line"}
                                    </span>
                                    {busy && (
                                        <Button
                                            type="button"
                                            variant="ghost"
                                            onClick={() =>
                                                void run("interrupt")
                                            }
                                        >
                                            Interrupt foreground
                                        </Button>
                                    )}
                                    <Button
                                        disabled={
                                            !online || sending || !prompt.trim()
                                        }
                                        type="submit"
                                    >
                                        {sending
                                            ? "Sending…"
                                            : busy
                                              ? "Send guidance ↑"
                                              : "Send ↑"}
                                    </Button>
                                </div>
                            </form>
                        </>
                    ) : (
                        <div className="page">
                            <PageHeader
                                title={page}
                                description={
                                    page === "Agents"
                                        ? "Live agent identities, task assignments, retained activity, and transcripts."
                                        : page === "Coverage"
                                          ? "Recorded coverage, discovery progress, and unresolved limitations."
                                          : pageGuide(page)
                                }
                                actions={
                                    <Button
                                        variant="secondary"
                                        disabled={refreshing}
                                        aria-busy={refreshing}
                                        onClick={() => void act(refresh)}
                                    >
                                        {refreshing ? "Refreshing…" : "Refresh"}
                                    </Button>
                                }
                            />
                            {page === "Settings" || page === "Models" ? (
                                <>
                                    <section className="card">
                                        <h2>Provider access</h2>
                                        <label>
                                            Provider
                                            <select
                                                value={providerId}
                                                onChange={(e) => {
                                                    setProviderId(
                                                        e.target.value,
                                                    );
                                                    setKey("");
                                                    const p = rows(
                                                        setup.providers,
                                                    ).find(
                                                        (x) =>
                                                            x.id ===
                                                            e.target.value,
                                                    );
                                                    if (p)
                                                        setEndpoint(
                                                            text(p.base_url),
                                                        );
                                                }}
                                            >
                                                {rows(setup.providers).map(
                                                    (p) => (
                                                        <option
                                                            key={text(p.id)}
                                                            value={text(p.id)}
                                                        >
                                                            {text(p.label)}
                                                        </option>
                                                    ),
                                                )}
                                            </select>
                                        </label>
                                        <p>
                                            Save additional provider models with
                                            their own endpoint and API key. Role
                                            routing selects which model each
                                            agent uses. Keys are stored
                                            privately on this Mac.
                                        </p>
                                        <label>
                                            Endpoint
                                            <Input
                                                value={endpoint}
                                                onChange={(e) =>
                                                    setEndpoint(e.target.value)
                                                }
                                            />
                                        </label>
                                        <label>
                                            Model
                                            <Input
                                                value={model}
                                                onChange={(e) =>
                                                    setModel(e.target.value)
                                                }
                                            />
                                        </label>
                                        <label>
                                            API key
                                            <Input
                                                type="password"
                                                autoComplete="off"
                                                value={key}
                                                onChange={(e) =>
                                                    setKey(e.target.value)
                                                }
                                            />
                                        </label>
                                        <Button
                                            onClick={() =>
                                                void run("configure_provider", [
                                                    endpoint,
                                                    model,
                                                    key || null,
                                                ]).then(async (v) => {
                                                    if (v !== undefined) {
                                                        setKey("");
                                                        setSetup(
                                                            obj(
                                                                await rpc(
                                                                    "setup_status",
                                                                ),
                                                            ),
                                                        );
                                                    }
                                                })
                                            }
                                        >
                                            Save default provider
                                        </Button>
                                        <Button
                                            disabled={
                                                !model.trim() ||
                                                !endpoint.trim() ||
                                                sending
                                            }
                                            variant="secondary"
                                            onClick={() =>
                                                void run(
                                                    "configure_model_endpoint",
                                                    [
                                                        model,
                                                        endpoint,
                                                        key || null,
                                                    ],
                                                    {
                                                        output_token_parameter:
                                                            rows(
                                                                setup.providers,
                                                            ).find(
                                                                (p) =>
                                                                    p.id ===
                                                                    providerId,
                                                            )
                                                                ?.output_token_parameter,
                                                        reasoning_parameter:
                                                            rows(
                                                                setup.providers,
                                                            ).find(
                                                                (p) =>
                                                                    p.id ===
                                                                    providerId,
                                                            )
                                                                ?.reasoning_parameter,
                                                    },
                                                ).then(async (v) => {
                                                    if (v !== undefined) {
                                                        setKey("");
                                                        setSetup(
                                                            obj(
                                                                await rpc(
                                                                    "setup_status",
                                                                ),
                                                            ),
                                                        );
                                                    }
                                                })
                                            }
                                        >
                                            Save additional provider model
                                        </Button>
                                        <p>
                                            A blank key keeps the saved key for
                                            the same endpoint.
                                        </p>
                                        <h3>Saved provider models</h3>
                                        {savedProviderModels(setup).map(
                                            (route) => (
                                                <article
                                                    className="record"
                                                    key={text(route.model)}
                                                >
                                                    <strong>
                                                        {text(route.model)}
                                                        {route.is_default
                                                            ? " · Default"
                                                            : ""}
                                                    </strong>
                                                    <p>
                                                        {text(route.base_url)} ·{" "}
                                                        {route.key_saved
                                                            ? "API key saved"
                                                            : "No dedicated API key"}
                                                    </p>
                                                    <Button
                                                        variant="secondary"
                                                        onClick={() => {
                                                            setModel(
                                                                text(
                                                                    route.model,
                                                                ),
                                                            );
                                                            setEndpoint(
                                                                text(
                                                                    route.base_url,
                                                                ),
                                                            );
                                                            setKey("");
                                                            setProviderId(
                                                                text(
                                                                    rows(
                                                                        setup.providers,
                                                                    ).find(
                                                                        (p) =>
                                                                            p.base_url ===
                                                                            route.base_url,
                                                                    )?.id,
                                                                ) || "custom",
                                                            );
                                                        }}
                                                    >
                                                        Edit endpoint
                                                    </Button>
                                                </article>
                                            ),
                                        )}
                                        {!savedProviderModels(setup).length && (
                                            <p>No provider models saved.</p>
                                        )}
                                    </section>
                                    <section className="card">
                                        <h2>Role routing</h2>
                                        <Button
                                            variant="secondary"
                                            onClick={() =>
                                                setDialog("Model endpoint")
                                            }
                                        >
                                            Add model endpoint
                                        </Button>
                                        <p>
                                            Choose a model and transport for
                                            each campaign role. Blank roles
                                            inherit the worker model.
                                        </p>
                                        {[
                                            "worker",
                                            "lead",
                                            "recon",
                                            "validator",
                                            "adversarial_explorer",
                                        ].map((role) => (
                                            <div
                                                className="role-row"
                                                key={role}
                                            >
                                                <label>
                                                    {role.replaceAll("_", " ")}
                                                    <Input
                                                        value={
                                                            roleModels[role] ||
                                                            ""
                                                        }
                                                        onChange={(e) =>
                                                            setRoleModels({
                                                                ...roleModels,
                                                                [role]: e.target
                                                                    .value,
                                                            })
                                                        }
                                                        placeholder={
                                                            model || "Model ID"
                                                        }
                                                    />
                                                </label>
                                                <label>
                                                    Reasoning effort
                                                    <select
                                                        value={
                                                            modelEfforts[
                                                                roleModels[
                                                                    role
                                                                ] || model
                                                            ] || "default"
                                                        }
                                                        onChange={(e) =>
                                                            setModelEfforts({
                                                                ...modelEfforts,
                                                                [roleModels[
                                                                    role
                                                                ] || model]:
                                                                    e.target
                                                                        .value,
                                                            })
                                                        }
                                                    >
                                                        {[
                                                            "default",
                                                            "low",
                                                            "medium",
                                                            "high",
                                                            "xhigh",
                                                        ].map((effort) => (
                                                            <option
                                                                key={effort}
                                                                value={effort}
                                                            >
                                                                {effort}
                                                            </option>
                                                        ))}
                                                    </select>
                                                </label>
                                                <label>
                                                    Transport
                                                    <select
                                                        value={
                                                            roleTransports[
                                                                roleModels[role]
                                                            ] ||
                                                            "openai_compatible"
                                                        }
                                                        onChange={(e) =>
                                                            setRoleTransports({
                                                                ...roleTransports,
                                                                [roleModels[
                                                                    role
                                                                ]]:
                                                                    e.target
                                                                        .value,
                                                            })
                                                        }
                                                    >
                                                        {[
                                                            "openai_compatible",
                                                            "openai_responses",
                                                            "anthropic_messages",
                                                            "gemini",
                                                            "codex_app_server",
                                                            "claude_cli",
                                                            "kimi_acp",
                                                        ].map((t) => (
                                                            <option key={t}>
                                                                {t}
                                                            </option>
                                                        ))}
                                                    </select>
                                                </label>
                                            </div>
                                        ))}
                                        <Button
                                            onClick={() =>
                                                void run(
                                                    "set_model_routing",
                                                    [],
                                                    {
                                                        worker_model:
                                                            roleModels.worker ||
                                                            model,
                                                        worker_transport:
                                                            roleTransports[
                                                                roleModels.worker ||
                                                                    model
                                                            ] ||
                                                            "openai_compatible",
                                                        heterogeneous: true,
                                                        lead_model:
                                                            roleModels.lead ||
                                                            "",
                                                        lead_transport:
                                                            roleTransports[
                                                                roleModels.lead
                                                            ] ||
                                                            "openai_compatible",
                                                        recon_model:
                                                            roleModels.recon ||
                                                            "",
                                                        recon_transport:
                                                            roleTransports[
                                                                roleModels.recon
                                                            ] ||
                                                            "openai_compatible",
                                                        validator_model:
                                                            roleModels.validator ||
                                                            "",
                                                        validator_transport:
                                                            roleTransports[
                                                                roleModels
                                                                    .validator
                                                            ] ||
                                                            "openai_compatible",
                                                        adversarial_explorer_model:
                                                            roleModels.adversarial_explorer ||
                                                            "",
                                                        adversarial_explorer_transport:
                                                            roleTransports[
                                                                roleModels
                                                                    .adversarial_explorer
                                                            ] ||
                                                            "openai_compatible",
                                                        role_efforts:
                                                            Object.fromEntries(
                                                                [
                                                                    "worker",
                                                                    "lead",
                                                                    "recon",
                                                                    "validator",
                                                                    "adversarial_explorer",
                                                                ].map(
                                                                    (role) => [
                                                                        role ===
                                                                        "worker"
                                                                            ? "workers"
                                                                            : role.replaceAll(
                                                                                  "_",
                                                                                  "-",
                                                                              ),
                                                                        modelEfforts[
                                                                            roleModels[
                                                                                role
                                                                            ] ||
                                                                                model
                                                                        ] ||
                                                                            "default",
                                                                    ],
                                                                ),
                                                            ),
                                                    },
                                                )
                                            }
                                        >
                                            Save roles
                                        </Button>
                                        <Button
                                            variant="secondary"
                                            onClick={() =>
                                                void run(
                                                    "apply_saved_model_routing",
                                                )
                                            }
                                        >
                                            Reconnect with saved routes
                                        </Button>
                                    </section>
                                    <section className="card">
                                        <h2>
                                            JEV judgments{" "}
                                            {setup.jev_configured
                                                ? "✓ key saved"
                                                : ""}
                                        </h2>
                                        <label>
                                            JEV API key
                                            <Input
                                                type="password"
                                                autoComplete="off"
                                                value={jev}
                                                onChange={(e) =>
                                                    setJev(e.target.value)
                                                }
                                            />
                                        </label>
                                        <Button
                                            onClick={() =>
                                                void run("save_jev", [
                                                    jev,
                                                ]).then(async (v) => {
                                                    if (v !== undefined) {
                                                        setJev("");
                                                        setSetup(
                                                            (await rpc(
                                                                "setup_status",
                                                            )) as Row,
                                                        );
                                                    }
                                                })
                                            }
                                        >
                                            Save JEV key
                                        </Button>
                                    </section>
                                    <section className="card">
                                        <h2>Appearance</h2>
                                        <Button
                                            variant="secondary"
                                            onClick={() => setDark(!dark)}
                                        >
                                            {dark
                                                ? "Use light theme"
                                                : "Use dark theme"}
                                        </Button>
                                    </section>
                                    <details>
                                        <summary>
                                            Current runtime and role
                                            configuration
                                        </summary>
                                        <Detail value={data} />
                                    </details>
                                </>
                            ) : (
                                <>
                                    <Input
                                        aria-label={`Search ${page}`}
                                        placeholder={`Search ${page.toLowerCase()}…`}
                                        value={search}
                                        onChange={(e) =>
                                            setSearch(e.target.value)
                                        }
                                    />
                                    {page === "Coverage" &&
                                        reports.map((report, i) => (
                                            <section className="card" key={i}>
                                                <h2>{text(report.run)}</h2>
                                                <CoverageChart
                                                    value={report.coverage}
                                                />
                                                <p>
                                                    {text(report.limitations)}
                                                </p>
                                            </section>
                                        ))}
                                    {page === "Context" &&
                                        dataPage === page && (
                                            <section className="card compact">
                                                <h2>Session context</h2>
                                                <StructuredFields
                                                    value={Object.fromEntries(
                                                        Object.entries(
                                                            obj(data),
                                                        ).filter(
                                                            ([key]) =>
                                                                ![
                                                                    "messages",
                                                                    "compactions",
                                                                ].includes(key),
                                                        ),
                                                    )}
                                                />
                                            </section>
                                        )}
                                    {page === "Agents" && dataPage === page && (
                                        <AgentOverview
                                            assignments={pageRows}
                                            onInspect={(item) =>
                                                void inspect(item)
                                            }
                                            onMessage={(item) => {
                                                setSelected(item);
                                                setDialog("Message agent");
                                            }}
                                        />
                                    )}
                                    {page === "Permissions" && (
                                        <section className="card">
                                            <h2>
                                                Permission mode ·{" "}
                                                {permissionMode}
                                            </h2>
                                            <details>
                                                <summary>
                                                    Saved session permission
                                                    rules
                                                </summary>
                                                <Detail
                                                    value={permissionRules}
                                                />
                                            </details>
                                            <p>
                                                Interrupt ends the foreground
                                                turn. Pause retains campaign
                                                state; cancel stops remaining
                                                campaign work and runs cleanup.
                                            </p>
                                            {["prompt", "auto", "yolo"].map(
                                                (mode) => (
                                                    <Button
                                                        key={mode}
                                                        variant="secondary"
                                                        onClick={() =>
                                                            void run(
                                                                "set_permission_mode",
                                                                [mode],
                                                            )
                                                        }
                                                    >
                                                        {mode}
                                                    </Button>
                                                ),
                                            )}
                                        </section>
                                    )}
                                    {page === "MCP" && (
                                        <Button
                                            onClick={() =>
                                                setDialog("Connect remote MCP")
                                            }
                                        >
                                            Connect remote MCP
                                        </Button>
                                    )}
                                    {dataPage !== page &&
                                        page !== "Coverage" && (
                                            <p role="status">
                                                {pageError
                                                    ? "This page could not load. Use Retry page above."
                                                    : `Loading ${page.toLowerCase()}…`}
                                            </p>
                                        )}
                                    {dataPage === page &&
                                        !pageRows.length &&
                                        (Array.isArray(data) ||
                                            Object.keys(obj(data)).length ===
                                                0) && (
                                            <div className="empty">
                                                <h2>
                                                    No {page.toLowerCase()} to
                                                    show
                                                </h2>
                                                <p>{pageGuide(page)}</p>
                                            </div>
                                        )}
                                    <div className="records">
                                        {(page === "Agents" ? [] : pageRows)
                                            .slice(0, visibleCount)
                                            .map((item, i) => (
                                                <article
                                                    className="record"
                                                    key={
                                                        text(
                                                            item.id ||
                                                                item.owner ||
                                                                item.run,
                                                        ) || i
                                                    }
                                                >
                                                    <button
                                                        className="record-main"
                                                        onClick={() =>
                                                            void inspect(item)
                                                        }
                                                    >
                                                        <div>
                                                            <strong>
                                                                {
                                                                    recordSummary(
                                                                        page,
                                                                        item,
                                                                        i,
                                                                    ).title
                                                                }
                                                            </strong>
                                                            <p>
                                                                {
                                                                    recordSummary(
                                                                        page,
                                                                        item,
                                                                        i,
                                                                    )
                                                                        .description
                                                                }
                                                            </p>
                                                        </div>
                                                        <Badge
                                                            value={
                                                                item.status ||
                                                                item.severity ||
                                                                item.state ||
                                                                item.evaluation_state ||
                                                                item.verdict ||
                                                                item.decision ||
                                                                item.outcome ||
                                                                item.kind ||
                                                                (typeof item.enabled ===
                                                                "boolean"
                                                                    ? item.enabled
                                                                        ? "Enabled"
                                                                        : "Disabled"
                                                                    : undefined)
                                                            }
                                                        />
                                                    </button>
                                                    <RecordFacts
                                                        page={page}
                                                        value={item}
                                                    />
                                                    {page === "Campaigns" && (
                                                        <div className="actions">
                                                            <Button
                                                                variant="ghost"
                                                                disabled={[
                                                                    "completed",
                                                                    "cancelled",
                                                                    "canceled",
                                                                ].includes(
                                                                    text(
                                                                        item.status,
                                                                    ),
                                                                )}
                                                                onClick={() =>
                                                                    void run(
                                                                        "pause_campaign",
                                                                        [
                                                                            item.id,
                                                                        ],
                                                                    )
                                                                }
                                                            >
                                                                Pause
                                                            </Button>
                                                            <Button
                                                                variant="ghost"
                                                                disabled={[
                                                                    "completed",
                                                                    "cancelled",
                                                                    "canceled",
                                                                ].includes(
                                                                    text(
                                                                        item.status,
                                                                    ),
                                                                )}
                                                                onClick={() =>
                                                                    void run(
                                                                        "resume_campaign",
                                                                        [
                                                                            item.id,
                                                                        ],
                                                                    )
                                                                }
                                                            >
                                                                Resume
                                                            </Button>
                                                            <Button
                                                                variant="ghost"
                                                                disabled={[
                                                                    "completed",
                                                                    "cancelled",
                                                                    "canceled",
                                                                ].includes(
                                                                    text(
                                                                        item.status,
                                                                    ),
                                                                )}
                                                                onClick={() => {
                                                                    setSelected(
                                                                        item,
                                                                    );
                                                                    setDialog(
                                                                        "Cancel campaign",
                                                                    );
                                                                }}
                                                            >
                                                                Cancel
                                                            </Button>
                                                            <Button
                                                                variant="ghost"
                                                                disabled={[
                                                                    "completed",
                                                                    "cancelled",
                                                                    "canceled",
                                                                ].includes(
                                                                    text(
                                                                        item.status,
                                                                    ),
                                                                )}
                                                                onClick={() => {
                                                                    setSelected(
                                                                        item,
                                                                    );
                                                                    setDialog(
                                                                        "Guide campaign",
                                                                    );
                                                                }}
                                                            >
                                                                Guide Lead
                                                            </Button>
                                                        </div>
                                                    )}
                                                    {page === "Sessions" && (
                                                        <Button
                                                            variant="ghost"
                                                            onClick={() => {
                                                                setSelected(
                                                                    item,
                                                                );
                                                                setDialog(
                                                                    "Resume this session",
                                                                );
                                                            }}
                                                        >
                                                            Resume session
                                                        </Button>
                                                    )}
                                                    {page === "Reports" && (
                                                        <>
                                                            <div className="metrics">
                                                                <span>
                                                                    {text(
                                                                        item.finding_count,
                                                                    )}{" "}
                                                                    findings
                                                                </span>
                                                                <span>
                                                                    {text(
                                                                        item.exchange_count,
                                                                    )}{" "}
                                                                    target
                                                                    exchanges
                                                                </span>
                                                                <span>
                                                                    Cleanup{" "}
                                                                    {text(
                                                                        item.cleanup_status,
                                                                    )}
                                                                </span>
                                                            </div>
                                                            <div className="actions">
                                                                {[
                                                                    "html",
                                                                    "markdown",
                                                                    "json",
                                                                    "csv",
                                                                ].map(
                                                                    (
                                                                        format,
                                                                    ) => (
                                                                        <Button
                                                                            variant="secondary"
                                                                            key={
                                                                                format
                                                                            }
                                                                            disabled={
                                                                                format ===
                                                                                    "html" &&
                                                                                !text(
                                                                                    item.output,
                                                                                ).endsWith(
                                                                                    ".html",
                                                                                )
                                                                            }
                                                                            onClick={() =>
                                                                                void download(
                                                                                    item,
                                                                                    format,
                                                                                )
                                                                            }
                                                                        >
                                                                            {format ===
                                                                            "html"
                                                                                ? "Open HTML"
                                                                                : format.toUpperCase()}
                                                                        </Button>
                                                                    ),
                                                                )}
                                                            </div>
                                                        </>
                                                    )}
                                                    {page === "Agents" && (
                                                        <div className="actions">
                                                            <span>
                                                                {text(
                                                                    item.provider,
                                                                )}{" "}
                                                                ·{" "}
                                                                {text(
                                                                    item.owner,
                                                                )}
                                                            </span>
                                                            <Button
                                                                variant="ghost"
                                                                onClick={() => {
                                                                    setSelected(
                                                                        item,
                                                                    );
                                                                    setDialog(
                                                                        "Message agent",
                                                                    );
                                                                }}
                                                            >
                                                                Message
                                                            </Button>
                                                        </div>
                                                    )}
                                                    {page === "Skills" && (
                                                        <Button
                                                            variant="ghost"
                                                            onClick={() =>
                                                                void send(
                                                                    `/skills ${item.enabled ? "disable" : "enable"} ${JSON.stringify(text(item.name))}`,
                                                                )
                                                            }
                                                        >
                                                            {item.enabled
                                                                ? "Disable"
                                                                : "Enable"}
                                                        </Button>
                                                    )}
                                                    {page === "Targets" && (
                                                        <Button
                                                            variant="ghost"
                                                            onClick={() =>
                                                                void send(
                                                                    `/targets select ${JSON.stringify(text(item.id))}`,
                                                                )
                                                            }
                                                        >
                                                            Select target
                                                        </Button>
                                                    )}
                                                    {page === "MCP" && (
                                                        <div className="actions">
                                                            {[
                                                                "check",
                                                                "restart",
                                                                "disable",
                                                            ].map((action) => (
                                                                <Button
                                                                    variant="ghost"
                                                                    key={action}
                                                                    onClick={() =>
                                                                        void send(
                                                                            `/mcp ${action} ${JSON.stringify(text(item.name))}`,
                                                                        )
                                                                    }
                                                                >
                                                                    {action}
                                                                </Button>
                                                            ))}
                                                        </div>
                                                    )}
                                                    {page === "Tasks" &&
                                                        rows(item.nodes).map(
                                                            (node) => (
                                                                <div
                                                                    className="task-row"
                                                                    key={text(
                                                                        node.id,
                                                                    )}
                                                                >
                                                                    <span>
                                                                        {text(
                                                                            node.id,
                                                                        )}
                                                                    </span>
                                                                    <Badge
                                                                        value={
                                                                            node.status
                                                                        }
                                                                    />
                                                                    <Button
                                                                        variant="ghost"
                                                                        onClick={() =>
                                                                            void run(
                                                                                "cancel_task",
                                                                                [
                                                                                    node.id,
                                                                                ],
                                                                            )
                                                                        }
                                                                    >
                                                                        Cancel
                                                                        task
                                                                    </Button>
                                                                </div>
                                                            ),
                                                        )}
                                                    {page === "Browser" && (
                                                        <div className="actions">
                                                            <Button
                                                                variant="ghost"
                                                                onClick={() =>
                                                                    void run(
                                                                        "takeover_browser_context",
                                                                        [
                                                                            item.id,
                                                                        ],
                                                                    )
                                                                }
                                                            >
                                                                Take over
                                                            </Button>
                                                            <Button
                                                                variant="ghost"
                                                                onClick={() =>
                                                                    void send(
                                                                        `/browser cleanup ${JSON.stringify(text(item.id))}`,
                                                                    )
                                                                }
                                                            >
                                                                Clean up
                                                            </Button>
                                                            <Button
                                                                variant="ghost"
                                                                onClick={() =>
                                                                    void send(
                                                                        `/browser resume ${JSON.stringify(text(item.id))}`,
                                                                    )
                                                                }
                                                            >
                                                                Resume agent
                                                            </Button>
                                                        </div>
                                                    )}
                                                    {page === "Findings" && (
                                                        <div className="actions">
                                                            {[
                                                                "accept",
                                                                "reject",
                                                            ].map((action) => (
                                                                <Button
                                                                    key={action}
                                                                    variant="ghost"
                                                                    onClick={() =>
                                                                        void send(
                                                                            `/findings ${action} ${JSON.stringify(text(item.id))}`,
                                                                        )
                                                                    }
                                                                >
                                                                    {action}
                                                                </Button>
                                                            ))}
                                                        </div>
                                                    )}
                                                    {page === "Promotions" && (
                                                        <div className="actions">
                                                            {[
                                                                "accept",
                                                                "reject",
                                                                "activate",
                                                            ].map((action) => (
                                                                <Button
                                                                    key={action}
                                                                    variant="ghost"
                                                                    onClick={() =>
                                                                        void send(
                                                                            `/promotion ${action} ${JSON.stringify(text(item.id))}`,
                                                                        )
                                                                    }
                                                                >
                                                                    {action}
                                                                </Button>
                                                            ))}
                                                        </div>
                                                    )}
                                                    <details>
                                                        <summary>
                                                            All fields
                                                        </summary>
                                                        <Detail value={item} />
                                                    </details>
                                                </article>
                                            ))}
                                    </div>
                                    {page !== "Agents" &&
                                        pageRows.length > 0 && (
                                            <div
                                                className="inventory-pagination"
                                                aria-live="polite"
                                            >
                                                <span>
                                                    Showing{" "}
                                                    {Math.min(
                                                        visibleCount,
                                                        pageRows.length,
                                                    )}{" "}
                                                    of {pageRows.length} loaded{" "}
                                                    {page.toLowerCase()} entries
                                                    {[
                                                        "Logs",
                                                        "History",
                                                    ].includes(page)
                                                        ? " · Latest 100 from the daemon"
                                                        : ""}
                                                </span>
                                                {visibleCount <
                                                    pageRows.length && (
                                                    <Button
                                                        variant="secondary"
                                                        onClick={() =>
                                                            setVisibleCount(
                                                                (count) =>
                                                                    count + 25,
                                                            )
                                                        }
                                                    >
                                                        Show 25 more
                                                    </Button>
                                                )}
                                            </div>
                                        )}
                                </>
                            )}
                        </div>
                    )}
                </div>
                <Dialog
                    open={Boolean(dialog)}
                    onOpenChange={(open) => {
                        if (!open) {
                            setDialog("");
                            setSelected(null);
                            setDetail(null);
                        }
                    }}
                >
                    <DialogContent>
                        {error && (
                            <div className="banner error" role="alert">
                                {error}
                            </div>
                        )}
                        <DialogHeader>
                            <DialogTitle>{dialog}</DialogTitle>
                            <DialogDescription>
                                {dialog.includes("Resume")
                                    ? "Restore saved conversation and campaign state. Prepared campaigns still need your approval."
                                    : dialog === "New campaign"
                                      ? "Target → effort → agent formation → recon → review → approval."
                                      : "Choose your next action."}
                            </DialogDescription>
                        </DialogHeader>
                        {dialog === "New session" && (
                            <>
                                <p>
                                    The current session remains saved. Running
                                    campaigns keep their durable state.
                                </p>
                                <Button
                                    disabled={busy}
                                    onClick={() => void newSession()}
                                >
                                    Create new session
                                </Button>
                            </>
                        )}
                        {dialog === "Resume session" && (
                            <div className="resume-list">
                                <Input
                                    aria-label="Search saved sessions"
                                    placeholder="Search saved sessions"
                                    onChange={(e) =>
                                        void act(async () => {
                                            const v = obj(
                                                await rpc(
                                                    "list_resume_session_page",
                                                    [],
                                                    {
                                                        query: e.target.value,
                                                        limit: 50,
                                                        offset: 0,
                                                    },
                                                ),
                                            );
                                            setSessions(rows(v.items));
                                        })
                                    }
                                />
                                {sessions.map((s) => (
                                    <button
                                        key={text(s.id)}
                                        onClick={() => {
                                            void send(`/resume ${text(s.id)}`);
                                        }}
                                    >
                                        <strong>
                                            {text(s.title) || text(s.id)}
                                        </strong>
                                        <small>{text(s.updated_at)}</small>
                                    </button>
                                ))}
                                {!sessions.length && (
                                    <p>No saved sessions found.</p>
                                )}
                                <Button
                                    variant="secondary"
                                    onClick={() => setDialog("New session")}
                                >
                                    Start a new session
                                </Button>
                            </div>
                        )}
                        {dialog === "Resume this session" && (
                            <>
                                <dl>
                                    <dt>Session title</dt>
                                    <dd>
                                        {text(selected?.title) ||
                                            "Untitled session"}
                                    </dd>
                                    <dt>Last updated</dt>
                                    <dd>
                                        {text(selected?.updated_at) ||
                                            "Unknown"}
                                    </dd>
                                    <dt>Session ID</dt>
                                    <dd>{text(selected?.id)}</dd>
                                </dl>
                                <Button
                                    disabled={sending || busy || !selected?.id}
                                    onClick={() => {
                                        void send(
                                            `/resume ${text(selected?.id)}`,
                                        );
                                    }}
                                >
                                    {sending ? "Resuming…" : "Resume session"}
                                </Button>
                            </>
                        )}
                        {dialog === "New campaign" && (
                            <>
                                <label>
                                    Target URL or app
                                    <Input
                                        value={target}
                                        onChange={(e) =>
                                            setTarget(e.target.value)
                                        }
                                        placeholder="https://your-app.example"
                                    />
                                </label>
                                <label>
                                    Objective, scope and constraints
                                    <Textarea
                                        value={objective}
                                        onChange={(e) =>
                                            setObjective(e.target.value)
                                        }
                                        placeholder="Assess prompt injection and data exposure within this authorized scope…"
                                    />
                                </label>
                                <label>
                                    Scan effort
                                    <select
                                        value={profile}
                                        onChange={(e) =>
                                            setProfile(e.target.value)
                                        }
                                    >
                                        <option value="recommended">
                                            Recommended — default
                                        </option>
                                        <option value="quickscan">
                                            Quickscan
                                        </option>
                                        <option value="exhaustive">
                                            Exhaustive
                                        </option>
                                        <option value="exploration">
                                            Exploration / learning
                                        </option>
                                    </select>
                                </label>
                                <label>
                                    Agent formation
                                    <select
                                        value={formation}
                                        onChange={(e) =>
                                            setFormation(e.target.value)
                                        }
                                    >
                                        <option value="autonomous">
                                            Autonomous — default
                                        </option>
                                        <option value="opinionated">
                                            Opinionated
                                        </option>
                                    </select>
                                </label>
                                <Button
                                    disabled={
                                        !target.trim() ||
                                        !objective.trim() ||
                                        busy
                                    }
                                    onClick={() => {
                                        setReview(null);
                                        void send(
                                            `Run a security assessment on ${target}.\n${objective}\nThe operator explicitly selected campaign profile: ${profile}; do not ask for the profile again.\n${formationInstruction}`,
                                        );
                                    }}
                                >
                                    Prepare campaign
                                </Button>
                            </>
                        )}
                        {["Guide campaign", "Message agent"].includes(
                            dialog,
                        ) && (
                            <>
                                <Textarea
                                    aria-label="Guidance"
                                    value={prompt}
                                    onChange={(e) => setPrompt(e.target.value)}
                                />
                                <Button
                                    disabled={!prompt.trim()}
                                    onClick={() => {
                                        if (dialog === "Guide campaign")
                                            void run("guide_campaign", [
                                                selected?.id,
                                                prompt,
                                            ]).then((value) => {
                                                if (value !== undefined)
                                                    setDialog("");
                                            });
                                        else
                                            void send(
                                                `/agent message ${text(selected?.owner)} ${prompt}`,
                                            );
                                    }}
                                >
                                    Send guidance
                                </Button>
                            </>
                        )}
                        {dialog === "Cancel campaign" && (
                            <>
                                <p>
                                    Stop this campaign’s remaining work and run
                                    its cleanup. Saved evidence remains
                                    available.
                                </p>
                                <Button
                                    onClick={() =>
                                        void run("cancel_campaign", [
                                            selected?.id,
                                        ]).then((value) => {
                                            if (value !== undefined)
                                                setDialog("");
                                        })
                                    }
                                >
                                    Cancel campaign
                                </Button>
                            </>
                        )}
                        {dialog === "Model endpoint" && (
                            <>
                                <p>
                                    Configure an endpoint for one model. Role
                                    routing can select that model after saving.
                                </p>
                                <label>
                                    Model ID
                                    <Input
                                        value={model}
                                        onChange={(e) =>
                                            setModel(e.target.value)
                                        }
                                    />
                                </label>
                                <label>
                                    Base URL
                                    <Input
                                        value={endpoint}
                                        onChange={(e) =>
                                            setEndpoint(e.target.value)
                                        }
                                    />
                                </label>
                                <label>
                                    API key
                                    <Input
                                        type="password"
                                        autoComplete="off"
                                        value={key}
                                        onChange={(e) => setKey(e.target.value)}
                                    />
                                </label>
                                <Button
                                    disabled={!model || !endpoint}
                                    onClick={() =>
                                        void run("configure_model_endpoint", [
                                            model,
                                            endpoint,
                                            key || null,
                                        ]).then((v) => {
                                            if (v !== undefined) {
                                                setKey("");
                                                setDialog("");
                                            }
                                        })
                                    }
                                >
                                    Save model endpoint
                                </Button>
                            </>
                        )}
                        {dialog === "Connect remote MCP" && (
                            <>
                                <label>
                                    Server name
                                    <Input
                                        value={remoteName}
                                        onChange={(e) =>
                                            setRemoteName(e.target.value)
                                        }
                                    />
                                </label>
                                <label>
                                    HTTPS MCP endpoint
                                    <Input
                                        value={remoteUrl}
                                        onChange={(e) =>
                                            setRemoteUrl(e.target.value)
                                        }
                                    />
                                </label>
                                <label>
                                    Credential handle
                                    <Input
                                        value={remoteHandle}
                                        onChange={(e) =>
                                            setRemoteHandle(e.target.value)
                                        }
                                    />
                                </label>
                                <label>
                                    Bearer token
                                    <Input
                                        type="password"
                                        autoComplete="off"
                                        value={remoteSecret}
                                        onChange={(e) =>
                                            setRemoteSecret(e.target.value)
                                        }
                                    />
                                </label>
                                <Button
                                    disabled={
                                        !remoteName ||
                                        !remoteUrl ||
                                        !remoteHandle ||
                                        !remoteSecret
                                    }
                                    onClick={() =>
                                        void run("configure_remote_mcp", [
                                            remoteName,
                                            remoteUrl,
                                            remoteHandle,
                                            remoteSecret,
                                        ]).then((v) => {
                                            if (v !== undefined) {
                                                setRemoteSecret("");
                                                setDialog("");
                                            }
                                        })
                                    }
                                >
                                    Save connection
                                </Button>
                            </>
                        )}
                        {dialog === "Commands" && (
                            <>
                                <p>
                                    All Merlin commands remain available through
                                    the conversation. Common views open
                                    directly.
                                </p>
                                <label>
                                    Command
                                    <Input
                                        placeholder="/help"
                                        value={prompt}
                                        onChange={(e) =>
                                            setPrompt(e.target.value)
                                        }
                                    />
                                </label>
                                <Button
                                    disabled={!prompt.trim()}
                                    onClick={() => void send(prompt)}
                                >
                                    Run command
                                </Button>
                                <details>
                                    <summary>
                                        Full TUI command reference
                                    </summary>
                                    <div className="command-grid">
                                        {commands.map((command) => (
                                            <button
                                                key={command}
                                                onClick={() =>
                                                    setPrompt(command)
                                                }
                                            >
                                                {command}
                                            </button>
                                        ))}
                                    </div>
                                </details>
                                <div className="command-grid">
                                    {Object.keys(methods).map((p) => (
                                        <button
                                            key={p}
                                            onClick={() => {
                                                setPage(p);
                                                setDialog("");
                                            }}
                                        >
                                            {p}
                                        </button>
                                    ))}
                                </div>
                                <label>
                                    Command or instruction
                                    <Input
                                        value={prompt}
                                        onChange={(e) =>
                                            setPrompt(e.target.value)
                                        }
                                        placeholder="/agent message OWNER …"
                                    />
                                </label>
                                <Button
                                    disabled={!prompt.trim()}
                                    onClick={() => void send(prompt)}
                                >
                                    Run command
                                </Button>
                            </>
                        )}
                    </DialogContent>
                </Dialog>
                <Dialog
                    open={selected !== null && !dialog}
                    onOpenChange={(open) => {
                        if (!open) setSelected(null);
                    }}
                >
                    <DialogContent>
                        {error && (
                            <div className="banner error" role="alert">
                                {error}
                            </div>
                        )}
                        <DialogHeader>
                            <DialogTitle>
                                {text(
                                    selected?.title ||
                                        selected?.name ||
                                        selected?.objective ||
                                        selected?.id,
                                ) ||
                                    (selected
                                        ? recordSummary(page, selected, 0).title
                                        : "Details")}
                            </DialogTitle>
                            <DialogDescription>
                                Retained Merlin state and attributable evidence.
                            </DialogDescription>
                        </DialogHeader>
                        {page === "Coverage" || page === "Campaigns" ? (
                            <CampaignDetail
                                value={detail}
                                onDecision={(decision) =>
                                    void run("decide_campaign_saturation", [
                                        selected?.id,
                                        decision,
                                    ])
                                }
                            />
                        ) : (
                            <Detail value={detail} />
                        )}
                        {page === "Coverage" && (
                            <p>
                                Coverage states and uncertainty are shown as
                                recorded; incomplete tests remain visible.
                            </p>
                        )}
                    </DialogContent>
                </Dialog>
            </AppShell>
        </RootTooltipProvider>
    );
}
const rootElement = document.getElementById("root");
if (rootElement)
    createRoot(rootElement).render(
        <StrictMode>
            <App />
        </StrictMode>,
    );
