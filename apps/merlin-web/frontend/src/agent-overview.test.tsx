import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AgentOverview, groupAgents } from "./agent-overview";
afterEach(cleanup);
const tasks = Array.from({ length: 21 }, (_, i) => ({
    session_id: "session",
    campaign_id: "campaign",
    specialist_instance_id: "explorer-1",
    identity_provenance: "recorded",
    profile: "Adversarial explorer",
    assignment_id: `task-${i}`,
    status: i === 10 ? "working" : "queued",
    technique: "taxonomy/authority-relay",
    execution_stage: "execute",
    provider: "fixture-model",
}));
it("groups recorded identity, chooses working task and sends transcript/message to it", () => {
    const inspect = vi.fn(),
        message = vi.fn();
    render(
        <AgentOverview
            assignments={tasks}
            onInspect={inspect}
            onMessage={message}
        />,
    );
    expect(screen.getAllByText("Adversarial explorer")).toHaveLength(1);
    expect(screen.getByText("1 recorded agent identities")).toBeTruthy();
    expect(screen.getAllByText("Transcript")).toHaveLength(10);
    fireEvent.click(screen.getByText("View transcript"));
    expect(inspect).toHaveBeenCalledWith(tasks[10]);
    fireEvent.click(screen.getByText("Message agent"));
    expect(message).toHaveBeenCalledWith(tasks[10]);
    fireEvent.click(screen.getByText("Load more tasks"));
    expect(screen.getAllByText("Transcript")).toHaveLength(21);
});
it("isolates identities by campaign and does not merge unknown legacy assignments", () => {
    expect(
        groupAgents([...tasks, { ...tasks[0], campaign_id: "other" }]),
    ).toHaveLength(2);
    expect(
        groupAgents(
            tasks
                .slice(0, 2)
                .map((row) => ({
                    ...row,
                    identity_provenance: "legacy_inferred",
                    owner: "unassigned",
                })),
        ),
    ).toHaveLength(2);
});
