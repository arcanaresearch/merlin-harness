import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    cleanup,
    fireEvent,
    render,
    screen,
    waitFor,
} from "@testing-library/react";
import { App } from "./main";

const calls: string[] = [];
const submitted: string[] = [];
const rpcCalls: { method: string; args: unknown[] }[] = [];
let failMethod = "";
let resourceFixtures: Record<string, unknown> = {};
let failAuthentication = false;
const connectedTokens: string[] = [];
let permissionFixture: Record<string, unknown>[] = [];
let inputFixture: Record<string, unknown> | null = null;
const permissionDecisions: Record<string, unknown>[] = [];
let holdAgents = false;
let holdChat = false;
let busyFixture = false;
let failChat = false;
let savedSessions: Record<string, unknown>[] = [];
let contextMessages: Record<string, unknown>[] = [];
let chatRequests = 0;
let eventLog = [
    {
        cursor: 1,
        kind: "status",
        value: "Isolated component acceptance fixture",
    },
];
const record = {
    id: "fixture-id",
    name: "Fixture record",
    title: "Fixture record",
    summary: "Fixture record",
    status: "paused",
    objective: "Fixture objective",
    owner: "fixture-worker",
    assignment_id: "fixture-assignment",
    task: "Fixture task",
    run: "fixture-id",
    output: "/tmp/fixture.html",
    kind: "fixture",
    detail: "Retained fixture evidence",
    latest_event: "completed",
};

beforeEach(() => {
    calls.length = 0;
    submitted.length = 0;
    rpcCalls.length = 0;
    failMethod = "";
    resourceFixtures = {};
    failAuthentication = false;
    connectedTokens.length = 0;
    permissionFixture = [];
    permissionDecisions.length = 0;
    holdAgents = false;
    holdChat = false;
    inputFixture = null;
    busyFixture = false;
    failChat = false;
    savedSessions = [];
    contextMessages = [];
    chatRequests = 0;
    eventLog = [
        {
            cursor: 1,
            kind: "status",
            value: "Isolated component acceptance fixture",
        },
    ];
    history.replaceState(null, "", "/?view=Chat");
    localStorage.clear();
    vi.stubGlobal(
        "ResizeObserver",
        class {
            observe() {}
            disconnect() {}
            unobserve() {}
        },
    );
    window.matchMedia = vi.fn().mockReturnValue({
        matches: false,
        addEventListener() {},
        removeEventListener() {},
    });
    Element.prototype.scrollIntoView = vi.fn();
    vi.stubGlobal(
        "fetch",
        vi.fn(async (input: string, options?: RequestInit) => {
            const body = options?.body ? JSON.parse(String(options.body)) : {};
            if (input === "/api/connect") {
                connectedTokens.push(body.token);
                failAuthentication = false;
                return new Response(JSON.stringify({ connected: true }));
            }
            if (input.includes("history?") && failAuthentication)
                return new Response("Unauthorized", { status: 401 });
            if (input.includes("history?"))
                return new Response(
                    JSON.stringify({
                        messages: contextMessages,
                        events: eventLog,
                        cursor: eventLog.at(-1)?.cursor ?? 0,
                        session_id: "fixture",
                        has_more: false,
                    }),
                );
            if (input.includes("events?") && failAuthentication)
                return new Response("Unauthorized", { status: 401 });
            if (input.includes("events?"))
                return new Response(
                    JSON.stringify({
                        events: eventLog.filter(
                            (e) => e.cursor > Number(input.split("after=")[1]),
                        ),
                        busy: busyFixture,
                        permissions: permissionFixture,
                        user_input: inputFixture,
                        review: null,
                    }),
                );
            if (input === "/api/permission") {
                permissionDecisions.push(body);
                return new Response(JSON.stringify({ accepted: true }));
            }
            if (input === "/api/chat") {
                chatRequests++;
                submitted.push(body.prompt);
                if (failChat)
                    return new Response(
                        JSON.stringify({ error: "Resume rejected" }),
                        { status: 400 },
                    );
                if (holdChat) return new Promise(() => {});
                const nextCursor = (eventLog.at(-1)?.cursor ?? 0) + 1;
                eventLog.push(
                    { cursor: nextCursor, kind: "user", value: body.prompt },
                    {
                        cursor: nextCursor + 1,
                        kind: "content",
                        value: "Hello from the fixture provider",
                    },
                );
                return new Response(JSON.stringify({ accepted: true }));
            }
            calls.push(body.method);
            rpcCalls.push({ method: body.method, args: body.args || [] });
            if (body.method === failMethod)
                return new Response(
                    JSON.stringify({ error: "Fixture API failure" }),
                    { status: 400 },
                );
            if (body.method === "list_agents" && holdAgents)
                return new Promise(() => {});
            const values: Record<string, unknown> = {
                setup_status: {
                    provider: {
                        model: "fixture",
                        base_url: "http://localhost/v1",
                    },
                    providers: [
                        {
                            id: "custom",
                            label: "Custom endpoint",
                            base_url: "",
                        },
                        {
                            id: "openai",
                            label: "OpenAI",
                            base_url: "https://api.openai.com/v1",
                            needs_key: true,
                        },
                        {
                            id: "deepseek",
                            label: "DeepSeek",
                            base_url: "https://api.deepseek.com/v1",
                            needs_key: true,
                        },
                    ],
                    model_endpoints: [
                        {
                            model: "existing-model",
                            base_url: "https://api.deepseek.com/v1",
                            key_saved: true,
                        },
                    ],
                },
                command_catalog: ["/new", "/resume", "/agents"],
                conversation_context: {
                    session_id: "fixture-session",
                    messages: contextMessages,
                },
                list_resume_session_page: {
                    items: savedSessions,
                    total: savedSessions.length,
                },
                get_runtime_status: {
                    model: "fixture",
                    transport: "openai_compatible",
                },
                get_permission_mode: "prompt",
                list_permission_rules: [],
                list_lessons: "Fixture lessons",
                campaign_status: { ...record, coverage: [] },
            };
            return new Response(
                JSON.stringify({
                    value: resourceFixtures[body.method] ??
                        values[body.method] ?? [record],
                }),
            );
        }),
    );
});
afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
});

const pages = [
    "Campaigns",
    "Agents",
    "Tasks",
    "Coverage",
    "Findings",
    "Reports",
    "Sessions",
    "Targets",
    "Knowledge packs",
    "Skills",
    "MCP",
    "Memory",
    "Browser",
    "Permissions",
    "Promotions",
    "Diagnostics",
    "Logs",
    "History",
    "Context",
    "Lessons",
    "Models",
    "Settings",
];
describe("operator views with populated records", () => {
    it.each(pages)("loads %s independently", async (page) => {
        history.replaceState(null, "", `/?view=${encodeURIComponent(page)}`);
        render(<App />);
        await waitFor(() =>
            expect(screen.getByText("Merlin connected")).toBeTruthy(),
        );
        await waitFor(() =>
            expect(document.body.textContent).not.toContain(
                "Loading current view",
            ),
        );
        await waitFor(() =>
            expect(document.body.textContent).not.toContain(
                `Loading ${page.toLowerCase()}…`,
            ),
        );
        expect(screen.queryByRole("alert")).toBeNull();
        expect(
            document.querySelectorAll('[data-slot="sidebar-row"]'),
        ).toHaveLength(7);
    });
});
it("main and secondary navigation actually change the view", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Campaigns" }));
    await waitFor(() =>
        expect(
            screen.getByRole("navigation", { name: "Campaigns pages" }),
        ).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Agents" }));
    await waitFor(() => expect(location.search).toBe("?view=Agents"));
    await waitFor(() =>
        expect(document.body.textContent).toContain("Fixture task"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Resources" }));
    fireEvent.click(screen.getByRole("button", { name: "Knowledge packs" }));
    await waitFor(() => expect(location.search).toBe("?view=Knowledge+packs"));
    await waitFor(() =>
        expect(document.body.textContent).toContain("Fixture record"),
    );
});
it("a hanging agents request does not block chat sending", async () => {
    holdAgents = true;
    render(<App />);
    const input = await screen.findByRole("textbox", {
        name: "Message Merlin",
    });
    await waitFor(() =>
        expect(screen.getByText("Merlin connected")).toBeTruthy(),
    );
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(chatRequests).toBe(1));
    await waitFor(() => expect((input as HTMLTextAreaElement).value).toBe(""));
    await waitFor(
        () =>
            expect(document.body.textContent).toContain(
                "Hello from the fixture provider",
            ),
        { timeout: 2000 },
    );
});
it("reports an API failure and retry recovers the selected page", async () => {
    failMethod = "list_targets";
    history.replaceState(null, "", "/?view=Targets");
    render(<App />);
    await waitFor(() =>
        expect(screen.getByRole("alert").textContent).toContain(
            "Fixture API failure",
        ),
    );
    expect(document.body.textContent).not.toContain("Loading targets…");
    failMethod = "";
    fireEvent.click(screen.getByRole("button", { name: "Retry page" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(document.body.textContent).toContain("Fixture record");
});
it("keeps Chat usable when its session report lookup fails", async () => {
    failMethod = "list_reports";
    render(<App />);
    await screen.findByRole("textbox", { name: "Message Merlin" });
    await waitFor(() => expect(calls).toContain("list_reports"));
    const reportRequest = vi
        .mocked(fetch)
        .mock.calls.find(
            ([, options]) =>
                options?.body &&
                JSON.parse(String(options.body)).method === "list_reports",
        );
    expect(JSON.parse(String(reportRequest?.[1]?.body)).kwargs).toEqual({
        session_only: true,
    });
    expect(screen.queryByRole("alert")).toBeNull();
    const input = screen.getByRole("textbox", { name: "Message Merlin" });
    fireEvent.change(input, { target: { value: "hello after resume" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(submitted).toEqual(["hello after resume"]));
});
it("loads historical Reports once and exposes a retry for a report failure", async () => {
    failMethod = "list_reports";
    history.replaceState(null, "", "/?view=Reports");
    render(<App />);
    await waitFor(() =>
        expect(screen.getByRole("alert").textContent).toContain(
            "Could not refresh reports",
        ),
    );
    expect(calls.filter((method) => method === "list_reports")).toHaveLength(1);
    const reportRequest = vi
        .mocked(fetch)
        .mock.calls.find(
            ([, options]) =>
                options?.body &&
                JSON.parse(String(options.body)).method === "list_reports",
        );
    expect(JSON.parse(String(reportRequest?.[1]?.body)).kwargs).toEqual({});
    failMethod = "";
    fireEvent.click(screen.getByRole("button", { name: "Retry page" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
});
it("prevents duplicate submission while HTTP acceptance is pending", async () => {
    holdChat = true;
    render(<App />);
    await waitFor(() =>
        expect(screen.getByText("Merlin connected")).toBeTruthy(),
    );
    const input = await screen.findByRole("textbox", {
        name: "Message Merlin",
    });
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(chatRequests).toBe(1));
    expect(
        screen
            .getByRole("button", { name: "Sending…" })
            .hasAttribute("disabled"),
    ).toBe(true);
    expect((input as HTMLTextAreaElement).value).toBe("hello");
});

it.each([
    ["Campaigns", "Pause", "pause_campaign"],
    ["Campaigns", "Resume", "resume_campaign"],
    ["Browser", "Take over", "takeover_browser_context"],
])("%s action %s targets the selected record", async (page, button, method) => {
    history.replaceState(null, "", `/?view=${page}`);
    render(<App />);
    await waitFor(() =>
        expect(screen.getByRole("button", { name: button })).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: button }));
    await waitFor(() =>
        expect(rpcCalls).toContainEqual({ method, args: ["fixture-id"] }),
    );
});
it.each([
    ["Targets", "Select target", '/targets select "fixture-id"'],
    ["Skills", "Enable", '/skills enable "Fixture record"'],
    ["MCP", "check", '/mcp check "Fixture record"'],
    ["MCP", "restart", '/mcp restart "Fixture record"'],
    ["MCP", "disable", '/mcp disable "Fixture record"'],
    ["Browser", "Clean up", '/browser cleanup "fixture-id"'],
    ["Browser", "Resume agent", '/browser resume "fixture-id"'],
    ["Findings", "accept", '/findings accept "fixture-id"'],
    ["Findings", "reject", '/findings reject "fixture-id"'],
    ["Promotions", "accept", '/promotion accept "fixture-id"'],
    ["Promotions", "reject", '/promotion reject "fixture-id"'],
])(
    "%s action %s sends the explicit operator command",
    async (page, button, command) => {
        history.replaceState(null, "", `/?view=${page}`);
        render(<App />);
        await waitFor(() =>
            expect(screen.getByRole("button", { name: button })).toBeTruthy(),
        );
        fireEvent.click(screen.getByRole("button", { name: button }));
        await waitFor(() => expect(submitted).toEqual([command]));
    },
);
it("failed campaign cancellation retains the confirmation dialog and shows failure", async () => {
    failMethod = "cancel_campaign";
    history.replaceState(null, "", "/?view=Campaigns");
    render(<App />);
    await waitFor(() =>
        expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel campaign" }));
    await waitFor(() =>
        expect(document.body.textContent).toContain("Fixture API failure"),
    );
    expect(screen.getByRole("dialog")).toBeTruthy();
});
it("guide Lead preserves the entered guidance on failure", async () => {
    failMethod = "guide_campaign";
    history.replaceState(null, "", "/?view=Campaigns");
    render(<App />);
    await waitFor(() =>
        expect(screen.getByRole("button", { name: "Guide Lead" })).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Guide Lead" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Guidance" }), {
        target: { value: "Focus on authentication" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send guidance" }));
    await waitFor(() =>
        expect(document.body.textContent).toContain("Fixture API failure"),
    );
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(
        (
            screen.getByRole("textbox", {
                name: "Guidance",
            }) as HTMLTextAreaElement
        ).value,
    ).toBe("Focus on authentication");
});

it("new campaign preserves target, effort and formation in the preparation request", async () => {
    render(<App />);
    await waitFor(() =>
        expect(screen.getByText("Merlin connected")).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: "New campaign" }));
    fireEvent.change(screen.getByLabelText("Target URL or app"), {
        target: { value: "https://fixture.example" },
    });
    fireEvent.change(
        screen.getByLabelText("Objective, scope and constraints"),
        { target: { value: "Assess the authorized fixture only" } },
    );
    fireEvent.change(screen.getByLabelText("Scan effort"), {
        target: { value: "exploration" },
    });
    fireEvent.change(screen.getByLabelText("Agent formation"), {
        target: { value: "opinionated" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Prepare campaign" }));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]).toContain("https://fixture.example");
    expect(submitted[0]).toContain("campaign profile: exploration");
    expect(submitted[0]).toContain("specialist_composition='opinionated'");
});

it("resumes the selected saved session and closes both dialogs", async () => {
    savedSessions = [
        {
            id: "saved-session-123",
            title: "helo",
            updated_at: "2026-10-10T01:00:00Z",
        },
    ];
    history.replaceState(null, "", "/?view=Sessions");
    render(<App />);
    await screen.findByText("Merlin connected");
    fireEvent.click(
        await screen.findByRole("button", { name: "Resume session" }),
    );
    expect(await screen.findByText("Session title")).toBeTruthy();
    expect(screen.getAllByText("saved-session-123").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Resume session" }));
    await waitFor(() =>
        expect(submitted).toContain("/resume saved-session-123"),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

it("keeps the resume dialog and selected session on submission failure", async () => {
    failChat = true;
    savedSessions = [
        { id: "saved-session-123", title: "helo", updated_at: "yesterday" },
    ];
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "helo" }));
    fireEvent.click(screen.getByRole("button", { name: "Resume session" }));
    expect(await screen.findByText("Resume rejected")).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getAllByText("saved-session-123").length).toBeGreaterThan(0);
});

it("exposes multiple providers and saves an additional keyed model route", async () => {
    history.replaceState(null, "", "/?view=Settings");
    render(<App />);
    fireEvent.change(await screen.findByLabelText("Provider"), {
        target: { value: "openai" },
    });
    expect((screen.getByLabelText("Endpoint") as HTMLInputElement).value).toBe(
        "https://api.openai.com/v1",
    );
    fireEvent.change(screen.getByLabelText("Model"), {
        target: { value: "new-model" },
    });
    fireEvent.change(screen.getByLabelText("API key", { exact: true }), {
        target: { value: "fixture-key" },
    });
    fireEvent.click(
        screen.getByRole("button", { name: "Save additional provider model" }),
    );
    await waitFor(() =>
        expect(rpcCalls).toContainEqual({
            method: "configure_model_endpoint",
            args: ["new-model", "https://api.openai.com/v1", "fixture-key"],
        }),
    );
    expect(screen.getByText(/API key saved/)).toBeTruthy();
    expect(
        (screen.getByLabelText("API key", { exact: true }) as HTMLInputElement)
            .value,
    ).toBe("");
});

it("restores saved conversation after a daemon session-reset acknowledgment", async () => {
    render(<App />);
    await screen.findByText("Merlin connected");
    contextMessages = [
        { role: "user", content: "Restored saved question" },
        { role: "assistant", content: "Restored saved answer" },
    ];
    eventLog.push({ cursor: 10, kind: "session_reset", value: "" });
    expect(await screen.findByText("Restored saved question")).toBeTruthy();
    expect(await screen.findByText("Restored saved answer")).toBeTruthy();
});

it("keeps the current conversation visible while resume is pending", async () => {
    holdChat = true;
    savedSessions = [{ id: "saved-session-123", title: "helo" }];
    eventLog.push({
        cursor: 2,
        kind: "content",
        value: "Current conversation must stay",
    });
    render(<App />);
    await screen.findByText("Current conversation must stay");
    fireEvent.click(await screen.findByRole("button", { name: "helo" }));
    fireEvent.click(screen.getByRole("button", { name: "Resume session" }));
    await screen.findByRole("button", { name: "Resuming…" });
    expect(screen.getByText("Current conversation must stay")).toBeTruthy();
    expect(
        (screen.getByRole("button", { name: "Resuming…" }) as HTMLButtonElement)
            .disabled,
    ).toBe(true);
    expect(chatRequests).toBe(1);
});

it("does not present an empty working chat when browser authentication fails", async () => {
    failAuthentication = true;
    render(<App />);
    await screen.findByRole("heading", {
        name: "Connect this browser to Merlin",
    });
    expect(screen.queryByRole("heading", { name: "Conversation" })).toBeNull();
    expect(calls).not.toContain("list_campaigns");
});

it("accepts a fresh /web token in the same open tab without a reload", async () => {
    failAuthentication = true;
    render(<App />);
    await screen.findByRole("heading", {
        name: "Connect this browser to Merlin",
    });
    history.replaceState(null, "", "/?view=Chat#token=fresh-workspace-token");
    fireEvent(window, new HashChangeEvent("hashchange"));
    await screen.findByRole("heading", { name: "Conversation" });
    expect(connectedTokens).toEqual(["fresh-workspace-token"]);
    expect(location.hash).toBe("");
});

it("leaves the operational workspace when an established connection expires", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "Conversation" });
    failAuthentication = true;
    await screen.findByRole(
        "heading",
        { name: "Connect this browser to Merlin" },
        { timeout: 2000 },
    );
    expect(
        screen.queryByRole("textbox", { name: "Message Merlin" }),
    ).toBeNull();
    expect(screen.getByText("Merlin disconnected")).toBeTruthy();
});

it("replays a replacement workspace from cursor zero when /web reconnects", async () => {
    eventLog = [
        { cursor: 400, kind: "status", value: "Old workspace transcript" },
    ];
    render(<App />);
    await screen.findByText("Old workspace transcript");
    eventLog = [
        {
            cursor: 1,
            kind: "status",
            value: "Replacement workspace transcript",
        },
    ];
    history.replaceState(null, "", "/?view=Chat#token=replacement-token");
    fireEvent(window, new HashChangeEvent("hashchange"));
    await screen.findByText("Replacement workspace transcript");
    expect(screen.queryByText("Old workspace transcript")).toBeNull();
});

it("shows reviewed command details and sends the exact command session decision", async () => {
    permissionFixture = [
        {
            id: "permission-1",
            tool: { name: "shell_command" },
            effect: "subprocess",
            details: "Command: echo reviewed\nWorking directory: /fixture",
            allow_command_session: true,
        },
    ];
    render(<App />);
    await screen.findByText(/Command: echo reviewed/);
    fireEvent.click(
        await screen.findByRole("button", {
            name: "Allow this command + directory (session)",
        }),
    );
    await waitFor(() =>
        expect(permissionDecisions).toEqual([
            { id: "permission-1", decision: "allow_command_session" },
        ]),
    );
});

it("does not offer a reusable command allowance for other tools", async () => {
    permissionFixture = [
        {
            id: "permission-2",
            tool: { name: "target_request" },
            effect: "target_network",
            details: "Arguments (sensitive values redacted): {}",
            allow_command_session: false,
        },
    ];
    render(<App />);
    await screen.findByRole("heading", { name: "Tool permission requested" });
    expect(
        screen.queryByRole("button", {
            name: "Allow this command + directory (session)",
        }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Deny" }));
    await waitFor(() =>
        expect(permissionDecisions).toEqual([
            { id: "permission-2", decision: "deny" },
        ]),
    );
});

it.each(["close button", "Escape"])(
    "closing a recent-session resume with %s does not open empty details over Settings",
    async (dismiss) => {
        savedSessions = [
            {
                id: "saved-helo-session",
                title: "helo",
                updated_at: "2026-10-10",
            },
        ];
        history.replaceState(null, "", "/?view=Settings");
        render(<App />);
        await screen.findByRole("heading", { name: "Provider access" });
        fireEvent.click(await screen.findByRole("button", { name: "helo" }));
        await screen.findByRole("heading", { name: "Resume this session" });
        if (dismiss === "Escape")
            fireEvent.keyDown(document, { key: "Escape", code: "Escape" });
        else fireEvent.click(screen.getByRole("button", { name: "Close" }));
        await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
        expect(
            screen.queryByText(
                "Retained Merlin state and attributable evidence.",
            ),
        ).toBeNull();
        expect(
            screen.getByRole("heading", { name: "Provider access" }),
        ).toBeTruthy();
        expect(submitted).toEqual([]);
        // The cancelled selection remains available for a deliberate new attempt.
        fireEvent.click(screen.getByRole("button", { name: "helo" }));
        await screen.findByRole("heading", { name: "Resume this session" });
        fireEvent.click(screen.getByRole("button", { name: "Resume session" }));
        await waitFor(() =>
            expect(submitted).toEqual(["/resume saved-helo-session"]),
        );
    },
);

it("streams visible reasoning, shows tool activity, then collapses completed reasoning", async () => {
    busyFixture = true;
    eventLog = [
        { cursor: 1, kind: "user", value: "Assess my target" },
        { cursor: 2, kind: "thinking", value: "Checking the scope. " },
    ];
    render(<App />);
    const reasoning = await screen.findByText("Checking the scope.");
    await waitFor(() => expect(reasoning.closest("details")?.open).toBe(true));
    expect(
        screen
            .getAllByRole("status")
            .some((node) => node.textContent?.includes("Thinking…")),
    ).toBe(true);
    eventLog.push({
        cursor: 3,
        kind: "thinking",
        value: "Next, inspect the target.",
    });
    await screen.findByText("Checking the scope. Next, inspect the target.");
    eventLog.push({
        cursor: 4,
        kind: "tool",
        value: "assessment_prepare started",
    });
    await waitFor(() =>
        expect(screen.getByRole("status").textContent).toContain(
            "assessment_prepare started",
        ),
    );
    busyFixture = false;
    eventLog.push(
        { cursor: 5, kind: "content", value: "Your plan is ready." },
        { cursor: 6, kind: "turn_end", value: "" },
    );
    await screen.findByText("Your plan is ready.");
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
    expect(
        screen
            .getByText("Checking the scope. Next, inspect the target.")
            .closest("details")?.open,
    ).toBe(false);
    expect(screen.getByText("Reasoning")).toBeTruthy();
});

it("shows model waiting activity even when no reasoning is emitted", async () => {
    busyFixture = true;
    eventLog = [{ cursor: 1, kind: "user", value: "hello" }];
    render(<App />);
    await waitFor(() =>
        expect(screen.getByRole("status").textContent).toContain(
            "Waiting for model…",
        ),
    );
    expect(screen.queryByText("Thinking…")).toBeNull();
});

it("renders streamed profile and agent choices and submits named selections once", async () => {
    eventLog = [
        { cursor: 1, kind: "user", value: "Scan my target" },
        { cursor: 2, kind: "content", value: "**Please choose a pro" },
        {
            cursor: 3,
            kind: "content",
            value: "file:**\n1. Recommended [default]\n2. Quickscan\n3. Exhaustive\n4. Exploration / learning\n\nSeparately, agent composition:\n1. Autonomous [default]\n2. Opinionated",
        },
        { cursor: 4, kind: "turn_end", value: "" },
    ];
    render(<App />);
    await screen.findByRole("heading", { name: "Choose how Merlin works" });
    expect(
        (screen.getByRole("radio", { name: /Recommended/ }) as HTMLInputElement)
            .checked,
    ).toBe(true);
    expect(
        (screen.getByRole("radio", { name: /Autonomous/ }) as HTMLInputElement)
            .checked,
    ).toBe(true);
    expect(
        document.querySelector(".message-markdown strong")?.textContent,
    ).toBe("Please choose a profile:");
    fireEvent.click(screen.getByRole("radio", { name: /Exploration/ }));
    fireEvent.click(screen.getByRole("radio", { name: /Opinionated/ }));
    expect(submitted).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Continue →" }));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]).toContain("campaign profile: exploration");
    expect(submitted[0]).toContain("specialist_composition='opinionated'");
    await waitFor(() =>
        expect(
            screen.queryByRole("heading", { name: "Choose how Merlin works" }),
        ).toBeNull(),
    );
});

it("does not show campaign choices for an earlier turn after the operator replies", async () => {
    eventLog = [
        {
            cursor: 1,
            kind: "content",
            value: "Choose the campaign profile: Quickscan Recommended Exhaustive Exploration",
        },
        { cursor: 2, kind: "user", value: "quickscan" },
        { cursor: 3, kind: "content", value: "Starting reconnaissance." },
    ];
    render(<App />);
    await screen.findByText("Starting reconnaissance.");
    expect(
        screen.queryByRole("heading", { name: "Choose how Merlin works" }),
    ).toBeNull();
});

it("renders Markdown safely without enabling model-supplied HTML", async () => {
    eventLog = [
        {
            cursor: 1,
            kind: "content",
            value: "**Plan ready** with `quickscan`.\n\n<script>alert('unsafe')</script>\n\n[Unsafe](javascript:alert%281%29)",
        },
    ];
    render(<App />);
    await screen.findByText("Plan ready");
    expect(
        document.querySelector(".message-markdown strong")?.textContent,
    ).toBe("Plan ready");
    expect(document.querySelector(".message-markdown code")?.textContent).toBe(
        "quickscan",
    );
    expect(document.querySelector(".message-markdown script")).toBeNull();
    expect(screen.getByText("Unsafe").getAttribute("href")).not.toContain(
        "javascript:",
    );
});

it("renders usage in a response footer rather than a dedicated chat row", async () => {
    eventLog = [
        { cursor: 1, kind: "thinking", value: "Checking scope" },
        {
            cursor: 2,
            kind: "usage",
            value: "17,152 tokens (16,767 in · 385 out)",
        },
        { cursor: 3, kind: "content", value: "Your assessment is configured." },
        { cursor: 4, kind: "turn_end", value: "" },
    ];
    render(<App />);
    const usage = await screen.findByText(
        "17,152 tokens (16,767 in · 385 out)",
    );
    expect(usage.tagName).toBe("FOOTER");
    expect(usage.closest("article")?.textContent).toContain(
        "Your assessment is configured.",
    );
    expect(document.querySelector(".message.usage")).toBeNull();
});

it("restores generic pending choices from the snapshot and submits exact IDs", async () => {
    inputFixture = {
        request_id: "s1:auth",
        questions: [
            {
                id: "auth",
                question: "How do you want to proceed?",
                options: [
                    {
                        id: "saved",
                        label: "Existing authenticated session",
                        description: "Use a configured credential handle",
                    },
                    {
                        id: "browser",
                        label: "Google OAuth",
                        description: "Continue in browser",
                    },
                ],
                default: [],
                multiple: false,
                allow_free_text: true,
            },
        ],
    };
    render(<App />);
    fireEvent.click(
        await screen.findByRole("radio", {
            name: /Existing authenticated session/,
        }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(submitted.length).toBe(1));
    expect(
        JSON.parse(submitted[0].split("\n")[1]).answers.auth.option_ids,
    ).toEqual(["saved"]);
    inputFixture = null;
    eventLog.push({
        cursor: 100,
        kind: "user_input_resolved",
        value: "s1:auth",
    });
    await waitFor(() =>
        expect(screen.queryByRole("button", { name: "Continue" })).toBeNull(),
    );
});

it("renders actual diagnostic tuple and refreshes its route", async () => {
    resourceFixtures.run_doctor = [
        false,
        [{ name: "Storage", status: "failed", detail: "Database unavailable" }],
    ];
    history.replaceState(null, "", "/?view=Diagnostics");
    render(<App />);
    expect(
        await screen.findByRole("button", {
            name: "Storage Database unavailable failed",
        }),
    ).toBeTruthy();
    expect(screen.queryByText("Recorded entry")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() =>
        expect(
            calls.filter((method) => method === "run_doctor").length,
        ).toBeGreaterThan(1),
    );
});

it("bounds long inventories and searches beyond the rendered batch", async () => {
    resourceFixtures.list_logs = Array.from({ length: 61 }, (_, i) => ({
        summary: `Worker ${i} finished`,
        kind: "info",
        occurred_at: "2026-10-10",
    }));
    history.replaceState(null, "", "/?view=Logs");
    render(<App />);
    expect(await screen.findByText(/Showing 25 of 61/)).toBeTruthy();
    expect(document.querySelectorAll(".record")).toHaveLength(25);
    fireEvent.click(screen.getByRole("button", { name: "Show 25 more" }));
    expect(document.querySelectorAll(".record")).toHaveLength(50);
    fireEvent.change(screen.getByRole("textbox", { name: "Search Logs" }), {
        target: { value: "Worker 60 finished" },
    });
    expect(
        await screen.findByRole("button", { name: /Worker 60 finished/ }),
    ).toBeTruthy();
    expect(document.querySelectorAll(".record")).toHaveLength(1);
});

it("renders lesson envelopes and default provider access", async () => {
    resourceFixtures.list_lessons = {
        counts: { negative_result: 1 },
        lessons: [
            {
                id: "lesson/1",
                technique_id: "memory-isolation",
                content: "Validate the remembered identity",
                outcome: "negative_result",
            },
        ],
    };
    history.replaceState(null, "", "/?view=Lessons");
    render(<App />);
    expect(
        await screen.findByRole("button", {
            name: /Memory isolation Validate the remembered identity negative result/,
        }),
    ).toBeTruthy();
    fireEvent.click(
        screen.getAllByRole("button", { name: "Settings" }).at(-1)!,
    );
    expect(await screen.findByText("fixture · Default")).toBeTruthy();
    expect(screen.getByText("existing-model")).toBeTruthy();
});

it("starts chat when a pre-paging embedded server returns history 404", async () => {
    const original = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation((input, options) =>
        String(input).includes("history?")
            ? Promise.resolve(new Response("Not Found", { status: 404 }))
            : original(input, options),
    );
    render(<App />);
    await screen.findByText(
        /This running Merlin server predates chat history paging/,
    );
    expect(
        screen.getByRole("textbox", { name: "Message Merlin" }),
    ).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
});
it("loads the Agents inventory only once per refresh", async () => {
    history.replaceState(null, "", "/?view=Agents");
    render(<App />);
    await screen.findByText("1 recorded agent identities");
    expect(calls.filter((method) => method === "list_agents")).toHaveLength(1);
});
