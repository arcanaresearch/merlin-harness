import { useState } from "react";

type Row = Record<string, unknown>;
const object = (value: unknown): Row =>
    value && typeof value === "object" && !Array.isArray(value)
        ? (value as Row)
        : {};
const list = (value: unknown): Row[] =>
    Array.isArray(value)
        ? value.filter(
              (item) =>
                  item && typeof item === "object" && !Array.isArray(item),
          )
        : [];
const human = (key: string) =>
    key.replace(/[_-]/g, " ").replace(/^./, (c) => c.toUpperCase());
const first = (row: Row, keys: string[], fallback = "") =>
    (keys
        .map((key) => row[key])
        .find(
            (value) => typeof value === "string" && value.trim(),
        ) as string) || fallback;

const guides: Record<string, string> = {
    Settings:
        "Connect providers and credentials. Set the default model used by Merlin.",
    Models: "Saved provider access and model routing for campaign roles. Blank roles inherit the worker model.",
    Permissions:
        "Review tool approval decisions and session rules. Permission mode controls when Merlin asks before executing tools.",
    Promotions:
        "Review proposed reusable improvements, their evaluation gates, and exact changes before accepting or activating them.",
    Diagnostics:
        "Health checks for configuration, storage, and runtime dependencies. Refresh runs the checks again.",
    Logs: "Recent runtime events from the daemon. Search by message, type, or time; open an event for its full details.",
    History:
        "Recent actions retained for the current session, with timestamps and outcomes.",
    Context:
        "Conversation messages and compaction records retained for the current session. This is an inspection view; use Chat to continue the conversation.",
    Lessons:
        "Lessons learned in this session. Review the supporting evidence and applicability before reusing a lesson.",
    Targets:
        "Configured assessment targets. Select a target to use it in the current session; starting a scan still requires a reviewed campaign plan.",
    "Knowledge packs":
        "Installed testing knowledge and attack cases. Open a pack to read its instructions, cases, and objectives.",
    Skills: "Installed agent skills and their availability. Enable or disable a skill using its row controls.",
    MCP: "Connected tool servers, available tools, and connection health. Connect a server or inspect its recent calls and errors.",
    Memory: "Retained memory records with their source, review state, expiry, and applicability. These are reusable records, separate from chat history.",
    Browser:
        "Browser contexts owned by Merlin, their tabs, target, blockers, and cleanup state. Inspect a context before requesting takeover.",
    Tasks: "Scheduled work and its execution state. Inspect a task before cancelling it.",
    Campaigns:
        "Prepared, running, and completed campaigns in this session. Inspect a campaign to review its plan and execution state.",
    Reports:
        "Saved assessment reports and export formats. Open a report or download its retained evidence.",
    Findings:
        "Assessment findings with severity, validation status, and evidence.",
    Sessions:
        "Saved conversations and campaign state. Resume a session to continue its work.",
};
export function pageGuide(page: string): string {
    return (
        guides[page] || "Inspect retained session data and available actions."
    );
}

export function viewRows(page: string, value: unknown): Row[] {
    if (
        page === "Diagnostics" &&
        Array.isArray(value) &&
        typeof value[0] === "boolean"
    )
        return list(value[1]);
    if (page === "Lessons") return list(object(value).lessons);
    if (page === "Context") {
        const context = object(value);
        return [
            ...list(context.messages).map((row) => ({
                ...row,
                entry_type: "message",
            })),
            ...list(context.compactions).map((row) => ({
                ...row,
                entry_type: "compaction",
            })),
        ];
    }
    return list(value);
}

export function savedProviderModels(setup: Row): Row[] {
    const provider = object(setup.provider);
    const additional = list(setup.model_endpoints);
    if (!provider.model) return additional;
    const match = additional.find(
        (row) =>
            row.model === provider.model && row.base_url === provider.base_url,
    );
    return [
        {
            ...match,
            model: provider.model,
            base_url: provider.base_url,
            key_saved: provider.key_saved ?? match?.key_saved,
            is_default: true,
        },
        ...additional.filter((row) => row !== match),
    ];
}

export function recordSummary(
    page: string,
    row: Row,
    index: number,
): { title: string; description: string } {
    const titleFields: Record<string, string[]> = {
        Permissions: ["tool"],
        Memory: ["record_id", "bundle_id"],
        Browser: ["target", "id"],
        Promotions: ["kind", "id"],
        Lessons: [
            "title",
            "summary",
            "hypothesis",
            "lesson",
            "technique_id",
            "id",
        ],
        Context: ["role", "name", "entry_type"],
        Diagnostics: ["name"],
        Logs: ["summary", "kind"],
        History: ["summary", "kind"],
    };
    const title = first(
        row,
        titleFields[page] || [
            "title",
            "objective",
            "name",
            "summary",
            "id",
            "run",
            "kind",
        ],
        `${page} item ${index + 1}`,
    );
    const descriptionFields: Record<string, string[]> = {
        Permissions: ["reason", "scope", "effect"],
        Memory: ["applicability", "source"],
        Browser: ["latest_action", "latest_observation", "owner"],
        Promotions: ["evaluation_failure", "version", "proposed_by"],
        Context: ["content", "text", "summary"],
        Lessons: [
            "description",
            "rationale",
            "content",
            "evidence_summary",
            "status",
        ],
    };
    const description = first(
        row,
        descriptionFields[page] || [
            "task",
            "description",
            "latest_event",
            "detail",
            "instructions",
            "model",
            "status",
        ],
    );
    return {
        title: human(title),
        description: [first(row, ["occurred_at", "updated_at"]), description]
            .filter(Boolean)
            .join(" · "),
    };
}

function FieldValue({ value, depth }: { value: unknown; depth: number }) {
    const [expanded, setExpanded] = useState(false);
    if (value == null || value === "")
        return <span className="field-muted">Not provided</span>;
    if (typeof value === "boolean") return <span>{value ? "Yes" : "No"}</span>;
    if (typeof value !== "object") {
        const content = String(value);
        return (
            <span className="field-text">
                {!expanded && content.length > 600
                    ? content.slice(0, 600) + "…"
                    : content}
                {content.length > 600 && (
                    <button
                        className="field-expand"
                        onClick={() => setExpanded(!expanded)}
                    >
                        {expanded ? "Show less" : "Read full text"}
                    </button>
                )}
            </span>
        );
    }
    if (depth >= 4)
        return (
            <details>
                <summary>Nested details</summary>
                <StructuredFields value={value} />
            </details>
        );
    if (Array.isArray(value)) {
        if (!value.length) return <span className="field-muted">None</span>;
        return (
            <>
                <ul className="field-list">
                    {value
                        .slice(0, expanded ? value.length : 10)
                        .map((item, i) => (
                            <li key={i}>
                                <FieldValue value={item} depth={depth + 1} />
                            </li>
                        ))}
                </ul>
                {value.length > 10 && (
                    <button
                        className="field-expand"
                        onClick={() => setExpanded(!expanded)}
                    >
                        {expanded ? "Show fewer" : `Show all ${value.length}`}
                    </button>
                )}
            </>
        );
    }
    return <FieldMap value={object(value)} depth={depth + 1} />;
}
function FieldMap({ value, depth }: { value: Row; depth: number }) {
    return (
        <dl className="resource-fields">
            {Object.entries(value).map(([key, field]) => (
                <div key={key}>
                    <dt>{human(key)}</dt>
                    <dd>
                        <FieldValue value={field} depth={depth} />
                    </dd>
                </div>
            ))}
        </dl>
    );
}
export function StructuredFields({ value }: { value: unknown }) {
    if (value == null) return <p role="status">No details available.</p>;
    return (
        <div className="structured-details">
            {Array.isArray(value) || typeof value !== "object" ? (
                <FieldValue value={value} depth={0} />
            ) : (
                <FieldMap value={object(value)} depth={0} />
            )}
        </div>
    );
}

export function RecordFacts({ page, value }: { page: string; value: Row }) {
    const keys: Record<string, string[]> = {
        Permissions: ["decision", "effect", "mode", "scope"],
        Promotions: [
            "version",
            "baseline_version",
            "proposed_by",
            "evaluation_failure",
            "gates",
        ],
        Memory: ["kind", "source", "reviewed_by", "expires_at"],
        Browser: ["isolation", "owner", "cleanup", "blockers", "tabs"],
        MCP: ["transport", "health", "admission", "tools", "stderr"],
        "Knowledge packs": ["enabled", "cases", "recent_runs"],
        Targets: ["model", "selected"],
        Skills: ["enabled", "error"],
        Lessons: [
            "outcome",
            "profile_id",
            "target_id",
            "technique_id",
            "confidence",
        ],
    };
    const fields = Object.fromEntries(
        (keys[page] || [])
            .filter((key) => value[key] !== undefined && value[key] !== "")
            .map((key) => [
                key,
                Array.isArray(value[key]) &&
                ["cases", "tools", "tabs"].includes(key)
                    ? `${(value[key] as unknown[]).length} ${key}`
                    : value[key],
            ]),
    );
    if (!Object.keys(fields).length) return null;
    return (
        <div className="record-facts">
            <StructuredFields value={fields} />
        </div>
    );
}
