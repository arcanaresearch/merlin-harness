import { useRef, useState } from "react";
import { Button, Input } from "./ui";
type Row = Record<string, unknown>;
const object = (value: unknown): Row =>
    value && typeof value === "object" && !Array.isArray(value)
        ? (value as Row)
        : {};
const label = (value: string) =>
    value.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
const number = (value: unknown) =>
    typeof value === "number"
        ? value.toLocaleString(undefined, { maximumFractionDigits: 4 })
        : String(value ?? "Unknown");
function Technique({ id }: { id: string }) {
    const [base, ...parts] = id.split("@");
    return (
        <span className="review-technique">
            <strong>{base.split("/").at(-1)?.replaceAll("-", " ")}</strong>
            <small>{base.split("/").slice(0, -1).join(" / ")}</small>
            <span className="review-tags">
                {parts.map((part) => (
                    <span className="badge" key={part}>
                        {part
                            .replace("lang=", "Language: ")
                            .replace("variant=", "Variant: ")}
                    </span>
                ))}
            </span>
            <details>
                <summary>Full identifier</summary>
                <code>{id}</code>
            </details>
        </span>
    );
}
function ReviewList({ items }: { items: unknown[] }) {
    const [query, setQuery] = useState("");
    const [page, setPage] = useState(0);
    const matches = items.filter(
        (item) =>
            !query ||
            JSON.stringify(item).toLowerCase().includes(query.toLowerCase()),
    );
    const pages = Math.max(1, Math.ceil(matches.length / 25));
    const current = Math.min(page, pages - 1);
    return (
        <div className="review-list">
            {items.length > 25 && (
                <Input
                    aria-label="Filter review entries"
                    placeholder="Filter by technique, language, or reason…"
                    value={query}
                    onChange={(event) => {
                        setQuery(event.target.value);
                        setPage(0);
                    }}
                />
            )}
            <p className="field-muted">
                {matches.length.toLocaleString()} entries
                {matches.length > 25
                    ? ` · Page ${current + 1} of ${pages}`
                    : ""}
            </p>
            <ul>
                {matches
                    .slice(current * 25, current * 25 + 25)
                    .map((item, index) => (
                        <li key={`${current}-${index}`}>
                            <ReviewValue value={item} />
                        </li>
                    ))}
            </ul>
            {matches.length > 25 && (
                <div className="actions">
                    <Button
                        variant="secondary"
                        disabled={current === 0}
                        onClick={() => setPage(current - 1)}
                    >
                        Previous
                    </Button>
                    <Button
                        variant="secondary"
                        disabled={current === pages - 1}
                        onClick={() => setPage(current + 1)}
                    >
                        Next
                    </Button>
                </div>
            )}
        </div>
    );
}
function ReviewValue({ value }: { value: unknown }) {
    if (Array.isArray(value))
        return value.length ? (
            <ReviewList items={value} />
        ) : (
            <span className="field-muted">None</span>
        );
    const record = object(value);
    if (typeof record.technique_id === "string") {
        const extra = Object.fromEntries(
            Object.entries(record).filter(
                ([key, v]) =>
                    ![
                        "technique_id",
                        "reason",
                        "outcome",
                        "controller_id",
                    ].includes(key) &&
                    v != null &&
                    v !== "",
            ),
        );
        return (
            <div className="review-test-cell">
                <Technique id={record.technique_id} />
                <div className="review-tags">
                    {record.outcome != null && (
                        <span className="badge">{String(record.outcome)}</span>
                    )}
                    {record.controller_id != null && (
                        <span className="field-muted">
                            Controller: {String(record.controller_id)}
                        </span>
                    )}
                </div>
                {record.reason != null && <p>{String(record.reason)}</p>}
                {Object.keys(extra).length > 0 && (
                    <details>
                        <summary>Evidence and identifiers</summary>
                        <ReviewValue value={extra} />
                    </details>
                )}
            </div>
        );
    }
    if (value && typeof value === "object")
        return (
            <dl className="resource-fields">
                {Object.entries(object(value))
                    .filter(
                        ([, v]) =>
                            v !== null &&
                            v !== undefined &&
                            !(Array.isArray(v) && !v.length),
                    )
                    .map(([key, v]) => (
                        <div key={key}>
                            <dt>{label(key)}</dt>
                            <dd>
                                {key === "technique_id" &&
                                typeof v === "string" ? (
                                    <Technique id={v} />
                                ) : (
                                    <ReviewValue
                                        value={
                                            key === "baseline" && v === "und"
                                                ? "Unknown (no recorded default language)"
                                                : v
                                        }
                                    />
                                )}
                            </dd>
                        </div>
                    ))}
            </dl>
        );
    if (
        typeof value === "string" &&
        (value.includes("/cat/") || value.includes("@lang="))
    )
        return <Technique id={value} />;
    return (
        <span className="field-text">
            {typeof value === "boolean"
                ? value
                    ? "Yes"
                    : "No"
                : number(value)}
        </span>
    );
}
export function CampaignReview({
    review,
    onApprove,
    onCancel,
    onHybrid,
    onChanges,
}: {
    review: Row;
    onApprove: () => Promise<boolean>;
    onCancel: () => Promise<boolean>;
    onHybrid: () => Promise<boolean>;
    onChanges: () => void;
}) {
    const data = object(review.review),
        coverage = object(data.admitted_coverage),
        estimate = object(data.preflight_estimate),
        target = object(review.target),
        budget = object(data.budget);
    const [pending, setPending] = useState("");
    const [error, setError] = useState("");
    const lock = useRef(false);
    const perform = async (name: string, action: () => Promise<boolean>) => {
        if (lock.current) return;
        lock.current = true;
        setPending(name);
        setError("");
        try {
            if (!(await action()))
                setError(
                    "The request did not complete. See the error above and try again.",
                );
        } catch (cause) {
            setError(cause instanceof Error ? cause.message : String(cause));
        } finally {
            lock.current = false;
            setPending("");
        }
    };
    const primaryKeys = [
        "authentication",
        "known_limitations",
        "stopping_policy",
        "cleanup_obligations",
    ];
    return (
        <section className="card review" aria-busy={Boolean(pending)}>
            <div className="eyebrow">Plan ready · awaiting your approval</div>
            <h2>Campaign review</h2>
            <p>
                Review the target, coverage, and spend before starting workers.
            </p>
            <dl className="resource-fields">
                {review.objective != null && (
                    <div>
                        <dt>Objective</dt>
                        <dd>{String(review.objective)}</dd>
                    </div>
                )}
                <div>
                    <dt>Target</dt>
                    <dd>
                        {String(
                            target.endpoint ??
                                target.id ??
                                "See target contract below",
                        )}
                    </dd>
                </div>
                <div>
                    <dt>Profile</dt>
                    <dd>{label(String(review.profile ?? "Unknown"))}</dd>
                </div>
                <div>
                    <dt>Coverage</dt>
                    <dd>
                        {number(coverage.technique_count)} techniques ·{" "}
                        {number(coverage.variant_count)} test cells
                    </dd>
                </div>
                <div>
                    <dt>Estimated spend</dt>
                    <dd>
                        {estimate.estimated_cost_usd == null
                            ? "Unavailable"
                            : `$${Number(estimate.estimated_cost_usd).toFixed(4)}`}{" "}
                        · {number(estimate.estimated_tokens)} tokens
                        {estimate.conservative_cost_usd != null && (
                            <small className="field-muted">
                                Conservative estimate: $
                                {Number(estimate.conservative_cost_usd).toFixed(
                                    4,
                                )}
                                . Estimates are not spending limits.
                            </small>
                        )}
                    </dd>
                </div>
                <div>
                    <dt>Budget limits</dt>
                    <dd>
                        {Object.entries(budget).some(([, v]) => v != null) ? (
                            <ReviewValue value={budget} />
                        ) : (
                            "No explicit limits configured"
                        )}
                    </dd>
                </div>
                {primaryKeys
                    .filter(
                        (key) =>
                            data[key] != null &&
                            !(
                                Array.isArray(data[key]) &&
                                !(data[key] as unknown[]).length
                            ),
                    )
                    .map((key) => (
                        <div key={key}>
                            <dt>{label(key)}</dt>
                            <dd>
                                <ReviewValue value={data[key]} />
                            </dd>
                        </div>
                    ))}
            </dl>
            <div className="review-sections">
                {Object.entries(data)
                    .filter(
                        ([key, value]) =>
                            !["budget", ...primaryKeys].includes(key) &&
                            value != null &&
                            (typeof value !== "object" ||
                                Object.keys(value).length > 0),
                    )
                    .map(([key, value]) => (
                        <details key={key}>
                            <summary>
                                {label(key)}
                                {Array.isArray(value)
                                    ? ` (${value.length.toLocaleString()})`
                                    : ""}
                            </summary>
                            {key === "cross_campaign_learning" && (
                                <p className="field-muted">
                                    “New” means no matching prior test record;
                                    it does not mean a newly discovered
                                    vulnerability.
                                </p>
                            )}
                            {key === "technique_skips" && (
                                <p className="field-muted">
                                    These cells are excluded from this prepared
                                    plan. Inspect the outcome and reason before
                                    approving.
                                </p>
                            )}
                            <ReviewValue value={value} />
                        </details>
                    ))}
            </div>
            <div className="review-controls">
                <div className="actions">
                    <Button
                        disabled={Boolean(pending)}
                        onClick={() =>
                            void perform("Starting campaign…", onApprove)
                        }
                    >
                        {pending === "Starting campaign…"
                            ? pending
                            : "Approve and start"}
                    </Button>
                    {Array.isArray(
                        object(data.target_contract).execution_options,
                    ) &&
                        (
                            object(data.target_contract)
                                .execution_options as unknown[]
                        ).includes("hybrid") && (
                            <Button
                                variant="secondary"
                                disabled={Boolean(pending)}
                                onClick={() =>
                                    void perform(
                                        "Preparing hybrid review…",
                                        onHybrid,
                                    )
                                }
                            >
                                Use hybrid and review again
                            </Button>
                        )}
                    <Button
                        variant="secondary"
                        disabled={Boolean(pending)}
                        onClick={onChanges}
                    >
                        Request changes
                    </Button>
                    <Button
                        variant="ghost"
                        disabled={Boolean(pending)}
                        onClick={() =>
                            void perform("Cancelling preparation…", onCancel)
                        }
                    >
                        Cancel preparation
                    </Button>
                </div>
                {pending && (
                    <p role="status" className="review-pending">
                        <span className="review-spinner" />
                        {pending} Please wait for confirmation; you do not need
                        to click again.
                    </p>
                )}
                {error && <p role="alert">{error}</p>}
                <small>
                    No workers start before approval. Approval applies to this
                    exact manifest.
                </small>
            </div>
        </section>
    );
}
