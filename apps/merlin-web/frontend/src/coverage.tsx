import { Button } from "./ui";
type Row = Record<string, unknown>;
const record = (v: unknown): Row =>
    v && typeof v === "object" && !Array.isArray(v) ? (v as Row) : {};
const array = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const stringify = (v: unknown) =>
    typeof v === "string" ? v : JSON.stringify(v, null, 2);
const colors: Record<string, string> = {
    confirmed: "#b24c56",
    negative: "#487d68",
    unclear: "#b88739",
    blocked: "#77818f",
    untested: "#b5bdc8",
    partial: "#688cb9",
};
export function CoverageChart({ value }: { value: unknown }) {
    const counts: Record<string, number> = Array.isArray(value)
        ? Object.fromEntries(value as [string, number][])
        : (record(value) as Record<string, number>);
    const outcomes = Object.entries(counts).filter(
        ([k, v]) => k in colors && Number.isFinite(v) && v > 0,
    );
    const total = outcomes.reduce((n, [, v]) => n + v, 0);
    let position = 0;
    const stops = outcomes.map(([k, v]) => {
        const start = position;
        position += (v / total) * 100;
        return `${colors[k]} ${start}% ${position}%`;
    });
    return (
        <div className="coverage-chart">
            <div
                className="donut"
                role="img"
                aria-label={
                    outcomes.map(([k, v]) => `${v} ${k}`).join(", ") ||
                    "No outcomes recorded"
                }
                style={{
                    background: total
                        ? `conic-gradient(${stops.join(",")})`
                        : "var(--ui-bg-tertiary)",
                }}
            >
                <div>
                    <strong>{total}</strong>
                    <small>outcomes</small>
                </div>
            </div>
            <div className="coverage-legend">
                {Object.entries(counts).map(([k, v]) => (
                    <div key={k}>
                        <span
                            style={{
                                background: colors[k] || "var(--ui-accent)",
                            }}
                        />
                        <span>{k}</span>
                        <strong>{v}</strong>
                    </div>
                ))}
            </div>
        </div>
    );
}
export function CampaignDetail({
    value,
    onDecision,
}: {
    value: unknown;
    onDecision: (decision: string) => void;
}) {
    const status = record(value);
    const completion = record(status.completion_document);
    const cells = array(completion.cells).filter(Array.isArray) as unknown[][];
    const counts: Record<string, number> = {};
    for (const c of cells) {
        const key = String(c[2]);
        counts[key] = (counts[key] || 0) + 1;
    }
    const decision = record(status.saturation_decision);
    return (
        <div>
            <CoverageChart value={counts} />
            {decision.state === "awaiting_operator" && (
                <section className="card">
                    <h2>Coverage decision required</h2>
                    <p>{String(decision.purpose || "Saturation review")}</p>
                    <Button onClick={() => onDecision("stop")}>
                        Stop with retained evidence
                    </Button>
                    <Button
                        variant="secondary"
                        onClick={() => onDecision("continue")}
                    >
                        Continue discovery
                    </Button>
                </section>
            )}
            <h2>Technique outcomes</h2>
            <div className="coverage-cells">
                {cells.map((c, i) => (
                    <div key={i}>
                        <span>{String(c[1])}</span>
                        <span className="badge">{String(c[2])}</span>
                    </div>
                ))}
            </div>
            <details>
                <summary>Hypotheses, discovery candidates, and lineage</summary>
                <pre className="detail">
                    {stringify(
                        completion.hypotheses ||
                            completion.hypothesis_evidence_graphs ||
                            {},
                    )}
                </pre>
            </details>
            <details>
                <summary>Budget and pacing</summary>
                <pre className="detail">
                    {stringify({ budget: status.budget, pace: status.pace })}
                </pre>
            </details>
            <details>
                <summary>Full campaign evidence</summary>
                <pre className="detail">{stringify(value)}</pre>
            </details>
        </div>
    );
}
