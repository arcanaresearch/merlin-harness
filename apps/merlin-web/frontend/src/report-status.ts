type Report = Record<string, unknown>;

/** Execution termination and assessment completeness are different facts. */
export function reportStatus(report: Report) {
    if (["budget_exhausted", "capped"].includes(String(report.stop_reason))) {
        return {
            title: "Scan finished · limit reached",
            detail: "The configured scan limit was reached. Results cover the work completed within that limit.",
            assessment: `Coverage assessment: ${String(report.report_state || "not available")}`,
        };
    }
    return {
        title: String(report.report_state || "Scan finished"),
        detail: String(report.stop_reason || ""),
        assessment: "",
    };
}
