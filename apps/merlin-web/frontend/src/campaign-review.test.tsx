import {
    act,
    cleanup,
    fireEvent,
    render,
    screen,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CampaignReview } from "./campaign-review";
afterEach(cleanup);
const fixture = {
    campaign_id: "campaign",
    manifest_digest: "digest",
    profile: "quickscan",
    target: { endpoint: "https://fixture.test/api" },
    review: {
        budget: { turns: null, tokens: null },
        admitted_coverage: { technique_count: 69, variant_count: 2553 },
        preflight_estimate: {
            estimated_tokens: 175256,
            estimated_cost_usd: 0.127281,
        },
        technique_skips: Array.from({ length: 10212 }, (_, i) => ({
            technique_id: `pack/cat/direct/technique-${i}@lang=am@variant=mixed`,
            outcome: "deferred",
            reason: "Legacy language deferral",
        })),
        target_contract: { execution_options: ["api", "hybrid"] },
        cross_campaign_learning: { new: 12765 },
    },
};
it("keeps huge reviews collapsed and pages separated technique names and languages", () => {
    render(
        <CampaignReview
            review={fixture}
            onApprove={vi.fn()}
            onCancel={vi.fn()}
            onHybrid={vi.fn()}
            onChanges={vi.fn()}
        />,
    );
    expect(screen.getByText("No explicit limits configured")).toBeTruthy();
    expect(screen.queryByText("Show all 10212")).toBeNull();
    const disclosure = screen.getByText("Technique skips (10,212)");
    expect(disclosure.closest("details")?.open).toBe(false);
    fireEvent.click(disclosure);
    // Each collection mounts only a page, even when thousands of cells exist.
    expect(screen.getAllByText("Language: am")).toHaveLength(25);
    fireEvent.change(screen.getByLabelText("Filter review entries"), {
        target: { value: "technique-10211@" },
    });
    expect(screen.getAllByText("Language: am")).toHaveLength(1);
    expect(screen.getByText("technique 10211")).toBeTruthy();
});
it("shows immediate progress, prevents repeat approval, and allows retry on error", async () => {
    let finish!: (ok: boolean) => void;
    const approve = vi.fn(
        () =>
            new Promise<boolean>((resolve) => {
                finish = resolve;
            }),
    );
    render(
        <CampaignReview
            review={fixture}
            onApprove={approve}
            onCancel={vi.fn()}
            onHybrid={vi.fn()}
            onChanges={vi.fn()}
        />,
    );
    const button = screen.getByRole("button", { name: "Approve and start" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(approve).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status").textContent).toContain(
        "Starting campaign",
    );
    expect(
        screen
            .getByRole("button", { name: "Cancel preparation" })
            .hasAttribute("disabled"),
    ).toBe(true);
    await act(async () => finish(false));
    expect(screen.getByRole("alert").textContent).toContain("did not complete");
    fireEvent.click(screen.getByRole("button", { name: "Approve and start" }));
    expect(approve).toHaveBeenCalledTimes(2);
    await act(async () => finish(true));
});
it("runs cancellation, hybrid preparation, and change-request actions", async () => {
    const cancel = vi.fn().mockResolvedValue(true),
        hybrid = vi.fn().mockResolvedValue(true),
        changes = vi.fn();
    render(
        <CampaignReview
            review={fixture}
            onApprove={vi.fn()}
            onCancel={cancel}
            onHybrid={hybrid}
            onChanges={changes}
        />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Request changes" }));
    expect(changes).toHaveBeenCalledOnce();
    await act(async () =>
        fireEvent.click(
            screen.getByRole("button", { name: "Use hybrid and review again" }),
        ),
    );
    expect(hybrid).toHaveBeenCalledOnce();
    await act(async () =>
        fireEvent.click(
            screen.getByRole("button", { name: "Cancel preparation" }),
        ),
    );
    expect(cancel).toHaveBeenCalledOnce();
});
