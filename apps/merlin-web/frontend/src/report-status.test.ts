import { expect, it } from "vitest";
import { reportStatus } from "./report-status";

it("separates reaching the configured limit from incomplete coverage", () => {
    expect(
        reportStatus({ stop_reason: "capped", report_state: "incomplete" }),
    ).toEqual({
        title: "Scan finished · limit reached",
        detail: "The configured scan limit was reached. Results cover the work completed within that limit.",
        assessment: "Coverage assessment: incomplete",
    });
});
it("renders the canonical budget terminal reason as a reached limit", () => {
    expect(
        reportStatus({ stop_reason: "budget_exhausted", report_state: "incomplete" }),
    ).toMatchObject({
        title: "Scan finished · limit reached",
        assessment: "Coverage assessment: incomplete",
    });
});
it("does not relabel failures as a normal capped scan", () => {
    expect(
        reportStatus({ stop_reason: "failed", report_state: "incomplete" }),
    ).toMatchObject({ title: "incomplete", detail: "failed" });
});
