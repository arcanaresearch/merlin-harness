import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LiveCampaign } from "./live-campaign";
afterEach(cleanup);
const campaign = {
    id: "active",
    status: "executing",
    objective: "Assess refund authorization",
    wave_number: 2,
    lead_activity: "Reviewing retained evidence",
};
const assignments = Array.from({ length: 21 }, (_, i) => ({
    campaign_id: "active",
    specialist_instance_id: "explorer",
    identity_provenance: "recorded",
    profile: "Explorer",
    assignment_id: `task-${i}`,
    status: i === 0 ? "working" : i < 11 ? "completed" : "queued",
    activity: i === 0 ? ["Sending a bounded target request"] : [],
    technique: "taxonomy/authority-relay",
}));
const callbacks = {
    onInspect: vi.fn(),
    onAgents: vi.fn(),
    onGuide: vi.fn(),
    onPause: vi.fn(),
};
it("chat summary groups assignments and prioritizes the running worker even before later queued rows", () => {
    render(
        <LiveCampaign
            campaign={campaign}
            assignments={[
                ...assignments,
                { campaign_id: "other", profile: "Prior campaign" },
            ]}
            {...callbacks}
        />,
    );
    expect(screen.getByText("1 agent identities")).toBeTruthy();
    expect(screen.getByText("10/21 recorded tasks finished")).toBeTruthy();
    expect(screen.getByText("Sending a bounded target request")).toBeTruthy();
    expect(screen.queryByText("Prior campaign")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Inspect Explorer" }));
    expect(callbacks.onInspect).toHaveBeenCalledWith(assignments[0]);
    fireEvent.click(screen.getByText("Guide campaign"));
    expect(callbacks.onGuide).toHaveBeenCalled();
});
it("updates in place from new snapshots and distinguishes paused from reviewing", () => {
    const { rerender } = render(
        <LiveCampaign
            campaign={campaign}
            assignments={assignments}
            {...callbacks}
        />,
    );
    rerender(
        <LiveCampaign
            campaign={{ ...campaign, status: "paused" }}
            assignments={assignments}
            {...callbacks}
        />,
    );
    expect(screen.getByText(/Paused/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Resume" })).toBeTruthy();
    rerender(
        <LiveCampaign
            campaign={campaign}
            assignments={assignments.map((a) => ({
                ...a,
                status: "completed",
            }))}
            {...callbacks}
        />,
    );
    expect(screen.getByText(/Reviewing wave results/)).toBeTruthy();
    expect(screen.getByText("21/21 recorded tasks finished")).toBeTruthy();
});
it("limits the compact chat to four identities and can expand the rest", () => {
    render(
        <LiveCampaign
            campaign={campaign}
            assignments={assignments
                .slice(0, 6)
                .map((a, i) => ({
                    ...a,
                    specialist_instance_id: `worker-${i}`,
                    profile: `Worker ${i}`,
                }))}
            {...callbacks}
        />,
    );
    expect(
        screen.getAllByRole("button", { name: /^Inspect Worker/ }),
    ).toHaveLength(4);
    fireEvent.click(screen.getByText("Show all 6 agents"));
    expect(
        screen.getAllByRole("button", { name: /^Inspect Worker/ }),
    ).toHaveLength(6);
});
