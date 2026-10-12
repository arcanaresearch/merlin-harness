import { useState } from "react";
import { Button } from "./ui";
type Row = Record<string, unknown>;
const str = (v: unknown) => (typeof v === "string" ? v : "");
const state = (r: Row) => str(r.status) || str(r.latest_event) || "unknown";
const working = (r: Row) =>
    r.status_group === "working" ||
    ["working", "running", "started"].includes(state(r));
export function groupAgents(assignments: Row[]) {
    const groups = new Map<string, Row[]>();
    for (const row of assignments) {
        const identity =
            row.identity_provenance !== "legacy_inferred" &&
            row.specialist_instance_id
                ? str(row.specialist_instance_id)
                : str(row.owner) && row.owner !== "unassigned"
                  ? str(row.owner)
                  : str(row.assignment_id) || str(row.name);
        // An identity belongs to a campaign/session; never combine separate campaigns.
        const key = `${str(row.session_id)}:${str(row.campaign_id)}:${identity}`;
        groups.set(key, [...(groups.get(key) || []), row]);
    }
    return [...groups].map(([key, tasks]) => ({
        key,
        tasks,
        current:
            tasks.find(working) ||
            tasks.find((r) => ["prepared", "queued"].includes(state(r))) ||
            tasks[tasks.length - 1],
    }));
}
function taskLabel(row: Row) {
    const technique = str(row.technique)
        .split("@")[0]
        .split("/")
        .at(-1)
        ?.replaceAll("-", " ");
    return technique
        ? `${str(row.execution_stage) || "Test"} · ${technique}`
        : str(row.task) || str(row.name);
}
function AgentCard({
    tasks,
    current,
    onInspect,
    onMessage,
}: {
    tasks: Row[];
    current: Row;
    onInspect: (row: Row) => void;
    onMessage: (row: Row) => void;
}) {
    const [count, setCount] = useState(10);
    const activity = Array.isArray(current.activity)
        ? current.activity
              .filter(
                  (v) =>
                      typeof v === "string" &&
                      !v.toLowerCase().includes("heartbeat"),
              )
              .at(-1)
        : "";
    return (
        <article className="record agent-summary">
            <header>
                <h2>
                    {str(current.profile) ||
                        str(current.specialist_instance_id) ||
                        str(current.name) ||
                        "Worker pending assignment"}
                </h2>
                <span className="badge">
                    {working(current)
                        ? "Working"
                        : state(current).replaceAll("_", " ")}
                </span>
            </header>
            <p className="agent-meta">
                {str(current.provider) || "Model not recorded"} ·{" "}
                {str(current.specialist_instance_id) ||
                    str(current.owner) ||
                    "Identity not recorded"}
            </p>
            <div className="metrics">
                <span>{tasks.length} tasks</span>
                <span>{tasks.filter(working).length} running</span>
                <span>
                    {
                        tasks.filter((r) =>
                            ["queued", "prepared"].includes(state(r)),
                        ).length
                    }{" "}
                    queued
                </span>
                <span>
                    {
                        tasks.filter((r) =>
                            ["completed", "succeeded"].includes(state(r)),
                        ).length
                    }{" "}
                    completed
                </span>
                <span>
                    {tasks.filter((r) => state(r) === "failed").length} failed
                </span>
            </div>
            <h3>Current / latest task</h3>
            <p>{taskLabel(current)}</p>
            <p className="agent-activity">
                {str(activity) ||
                    (working(current)
                        ? "Running · waiting for recorded activity"
                        : ["queued", "prepared"].includes(state(current))
                          ? "Waiting for admission"
                          : "No recent activity recorded")}
            </p>
            {current.result ? <p>{str(current.result)}</p> : null}
            <div className="actions">
                <Button variant="secondary" onClick={() => onInspect(current)}>
                    View transcript
                </Button>
                <Button variant="ghost" onClick={() => onMessage(current)}>
                    Message agent
                </Button>
            </div>
            <details>
                <summary>Task history · {tasks.length} assignments</summary>
                <ul className="agent-task-history">
                    {tasks.slice(0, count).map((task, i) => (
                        <li key={str(task.assignment_id) || i}>
                            <div>
                                <strong>{taskLabel(task)}</strong>
                                <small>
                                    {str(task.requested_language)
                                        ? `Language: ${str(task.requested_language)} · `
                                        : ""}
                                    {state(task)}
                                </small>
                            </div>
                            <Button
                                variant="ghost"
                                onClick={() => onInspect(task)}
                            >
                                Transcript
                            </Button>
                        </li>
                    ))}
                </ul>
                {count < tasks.length && (
                    <Button
                        variant="secondary"
                        onClick={() => setCount((n) => n + 25)}
                    >
                        Load more tasks
                    </Button>
                )}
            </details>
        </article>
    );
}
export function AgentOverview({
    assignments,
    onInspect,
    onMessage,
}: {
    assignments: Row[];
    onInspect: (row: Row) => void;
    onMessage: (row: Row) => void;
}) {
    const groups = groupAgents(assignments);
    return (
        <>
            <div className="metrics">
                <span>{groups.length} recorded agent identities</span>
                <span>{assignments.length} task assignments</span>
            </div>
            <div className="records">
                {groups.map((group) => (
                    <AgentCard
                        key={group.key}
                        tasks={group.tasks}
                        current={group.current}
                        onInspect={onInspect}
                        onMessage={onMessage}
                    />
                ))}
            </div>
        </>
    );
}
