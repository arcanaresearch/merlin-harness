type Report = Record<string, unknown>;

/** Execution termination and assessment completeness are different facts. */
export function reportStatus(report: Report) {
    if (report.stop_reason === "capped") {
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
