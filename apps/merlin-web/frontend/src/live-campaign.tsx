import { useState } from "react";
import { groupAgents } from "./agent-overview";
import { Button } from "./ui";
type Row = Record<string, unknown>;
const str = (v: unknown) => (typeof v === "string" ? v : "");
const state = (r: Row) => str(r.status) || str(r.latest_event) || "unknown";
const running = (r: Row) =>
    r.status_group === "working" ||
    ["running", "working", "started"].includes(state(r));
const terminal = (r: Row) =>
    [
        "completed",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
        "quarantined",
    ].includes(state(r));
export function LiveCampaign({
    campaign,
    assignments,
    onInspect,
    onAgents,
    onPause,
    onGuide,
}: {
    campaign: Row;
    assignments: Row[];
    onInspect: (row: Row) => void;
    onAgents: () => void;
    onPause: () => void;
    onGuide: () => void;
}) {
    const [expanded, setExpanded] = useState(false);
    const tasks = assignments.filter((row) => row.campaign_id === campaign.id);
    const agents = groupAgents(tasks).sort(
        (a, b) => Number(running(b.current)) - Number(running(a.current)),
    );
    const finished = tasks.filter(terminal).length;
    const queued = tasks.filter((r) =>
        ["prepared", "queued"].includes(state(r)),
    ).length;
    const failed = tasks.filter((r) => state(r) === "failed").length;
    const paused = campaign.status === "paused";
    const reviewing = !paused && tasks.length > 0 && finished === tasks.length;
    return (
        <section
            className="card swarm live-campaign"
            aria-label="Live campaign agents"
        >
            <header>
                <div className="eyebrow">
                    {paused
                        ? "Paused"
                        : reviewing
                          ? "Reviewing wave results"
                          : "Campaign running"}
                    {campaign.wave_number != null
                        ? ` · Wave ${campaign.wave_number}`
                        : ""}
                </div>
                <span className="live-indicator">Updates automatically</span>
            </header>
            <h2>{str(campaign.objective) || "Active campaign"}</h2>
            <div className="metrics">
                <span>{agents.length} agent identities</span>
                <span>
                    {finished}/{tasks.length} recorded tasks finished
                </span>
                <span>{tasks.filter(running).length} running</span>
                <span>{queued} queued</span>
                {failed > 0 && <span>{failed} failed</span>}
            </div>
            {tasks.length > 0 && (
                <progress
                    aria-label="Recorded task progress"
                    value={finished}
                    max={tasks.length}
                />
            )}
            <p className="live-lead">
                <strong>Campaign Lead</strong>
                <span>
                    {str(campaign.lead_activity) ||
                        (paused
                            ? "Paused"
                            : reviewing
                              ? "Reviewing results before the next wave"
                              : "Waiting for recorded lead activity")}
                </span>
            </p>
            <div className="live-agent-rows">
                {agents
                    .slice(0, expanded ? agents.length : 4)
                    .map(({ key, current, tasks: history }) => {
                        const updates =
                            Array.isArray(current.output) &&
                            current.output.length
                                ? current.output
                                : Array.isArray(current.activity)
                                  ? current.activity
                                  : [];
                        const latest = updates
                            .filter(
                                (v) =>
                                    typeof v === "string" &&
                                    !v.toLowerCase().includes("heartbeat"),
                            )
                            .at(-1);
                        const technique = str(current.technique)
                            .split("@")[0]
                            .split("/")
                            .at(-1)
                            ?.replaceAll("-", " ");
                        return (
                            <button
                                key={key}
                                className="live-agent-row"
                                onClick={() => onInspect(current)}
                                aria-label={`Inspect ${str(current.profile) || str(current.specialist_instance_id) || str(current.name)}`}
                            >
                                <div>
                                    <strong>
                                        {str(current.profile) ||
                                            str(
                                                current.specialist_instance_id,
                                            ) ||
                                            str(current.name) ||
                                            "Worker pending assignment"}
                                    </strong>
                                    <span className="live-agent-task">
                                        {technique || str(current.task)}
                                        {str(current.requested_language)
                                            ? ` · ${str(current.requested_language)}`
                                            : ""}
                                    </span>
                                    <span className="live-agent-activity">
                                        {str(latest) ||
                                            str(current.result) ||
                                            (running(current)
                                                ? "Waiting for recorded activity"
                                                : [
                                                        "prepared",
                                                        "queued",
                                                    ].includes(state(current))
                                                  ? "Waiting for admission"
                                                  : "No recent activity recorded")}
                                    </span>
                                </div>
                                <div className="live-agent-state">
                                    <span className="badge">
                                        {state(current)}
                                    </span>
                                    <small>
                                        {history.filter(terminal).length}/
                                        {history.length} tasks finished
                                    </small>
                                    <small>Inspect transcript →</small>
                                </div>
                            </button>
                        );
                    })}
            </div>
            {!agents.length && (
                <p aria-live="polite">Waiting for recorded agent assignments…</p>
            )}
            {agents.length > 4 && (
                <Button variant="ghost" onClick={() => setExpanded((v) => !v)}>
                    {expanded
                        ? "Show fewer agents"
                        : `Show all ${agents.length} agents`}
                </Button>
            )}
            <div className="actions">
                <Button variant="secondary" onClick={onAgents}>
                    All agents and tasks
                </Button>
                <Button variant="ghost" onClick={onGuide}>
                    Guide campaign
                </Button>
                <Button variant="ghost" onClick={onPause}>
                    {paused ? "Resume" : "Pause"}
                </Button>
            </div>
            <small className="live-progress-note">
                Recorded task progress; more tasks may be added in later waves.
            </small>
        </section>
    );
}
