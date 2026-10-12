import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button } from "./ui";

export function MessageMarkdown({ content }: { content: string }) {
    return (
        <div className="message-markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
                {content}
            </ReactMarkdown>
        </div>
    );
}

export function requestedCampaignChoices(content: string) {
    const plain = content.toLowerCase().replace(/[*_`]/g, "");
    const profile =
        /(?:choose|select|pick)[^\n.!?]{0,70}(?:profile|scan effort)/.test(
            plain,
        ) &&
        ["quickscan", "recommended", "exhaustive", "exploration"].every(
            (option) => plain.includes(option),
        );
    const formation =
        /(?:agent|specialist) (?:composition|formation)/.test(plain) &&
        plain.includes("autonomous") &&
        plain.includes("opinionated");
    return { profile, formation };
}

const profiles = [
    ["quickscan", "Quickscan", "Fast smoke pass with bounded investigation."],
    ["recommended", "Recommended", "Representative coverage for the target."],
    ["exhaustive", "Exhaustive", "Broader testing of applicable techniques."],
    [
        "exploration",
        "Exploration / learning",
        "Discovery-led hypotheses and adaptive testing.",
    ],
];
const formations = [
    [
        "autonomous",
        "Autonomous",
        "The Campaign Lead builds a team for your target.",
    ],
    ["opinionated", "Opinionated", "Use Merlin’s reviewed agent template."],
];

export function CampaignChoices({
    profile,
    formation,
    showProfile,
    showFormation,
    onProfile,
    onFormation,
    onContinue,
    disabled,
}: {
    profile: string;
    formation: string;
    showProfile: boolean;
    showFormation: boolean;
    onProfile: (value: string) => void;
    onFormation: (value: string) => void;
    onContinue: () => void;
    disabled: boolean;
}) {
    return (
        <form
            className="campaign-choices"
            onSubmit={(event) => {
                event.preventDefault();
                onContinue();
            }}
        >
            <div className="eyebrow">Campaign setup</div>
            <h2>Choose how Merlin works</h2>
            {[
                {
                    show: showProfile,
                    name: "Campaign profile",
                    value: profile,
                    options: profiles,
                    select: onProfile,
                    defaultValue: "quickscan",
                },
                {
                    show: showFormation,
                    name: "Agent composition",
                    value: formation,
                    options: formations,
                    select: onFormation,
                    defaultValue: "autonomous",
                },
            ]
                .filter((group) => group.show)
                .map((group) => (
                    <fieldset key={group.name} disabled={disabled}>
                        <legend>{group.name}</legend>
                        <div className="choice-grid">
                            {group.options.map(
                                ([value, label, description]) => (
                                    <label
                                        className={`choice-option ${group.value === value ? "selected" : ""}`}
                                        key={value}
                                    >
                                        <input
                                            type="radio"
                                            name={group.name}
                                            value={value}
                                            checked={group.value === value}
                                            onChange={() => group.select(value)}
                                        />
                                        <span>
                                            <span className="choice-title">
                                                {label}
                                                {value ===
                                                    group.defaultValue && (
                                                    <span className="choice-default">
                                                        Default
                                                    </span>
                                                )}
                                            </span>
                                            <span className="choice-description">
                                                {description}
                                            </span>
                                        </span>
                                    </label>
                                ),
                            )}
                        </div>
                    </fieldset>
                ))}
            <div className="choice-footer">
                <span>Your selections apply to this campaign.</span>
                <Button type="submit" disabled={disabled}>
                    Continue →
                </Button>
            </div>
        </form>
    );
}
