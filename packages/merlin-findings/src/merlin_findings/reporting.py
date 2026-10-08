# Standalone HTML is intentionally kept as one self-contained document.
# ruff: noqa: E501

import csv
import html
import io
import json
import re
import zipfile
from collections.abc import Iterable

from merlin_contracts import Finding, ReportAudience, ReportFormat


def accepted_findings(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    return tuple(finding for finding in findings if finding.status == "accepted")


def export_json(findings: Iterable[Finding]) -> str:
    accepted = accepted_findings(findings)
    payload = {"findings": [finding.model_dump(mode="json") for finding in accepted]}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _markdown_text(value: object) -> str:
    lines: list[str] = []
    for line in str(value).splitlines() or ("",):
        escaped = html.escape(line, quote=False)
        escaped = re.sub(r"([\\`*_{}\[\]()#!>|])", r"\\\1", escaped)
        escaped = re.sub(r"^(\s*)([-+])(?=\s)", r"\1\\\2", escaped)
        escaped = re.sub(r"^(\s*\d+)(\.)(?=\s)", r"\1\\\2", escaped)
        lines.append(escaped)
    return "<br>".join(lines)


def _markdown_code(value: object) -> str:
    return html.escape(str(value), quote=False).replace("`", "&#96;").replace("\n", " ")


def export_markdown(findings: Iterable[Finding]) -> str:
    accepted = accepted_findings(findings)
    lines = [
        "# Merlin Findings Report",
        "",
        "## Contents",
        "",
        "- [Executive summary](#executive-summary)",
        "- [Findings](#findings)",
        "",
        "## Executive summary",
        "",
        f"Accepted findings: {len(accepted)}",
        "",
        "## Findings",
        "",
    ]
    if not accepted:
        return "\n".join([*lines, "No accepted findings.", ""])
    for finding in accepted:
        lines.extend(
            [
                f"## {_markdown_text(finding.title)}",
                "",
                f"- **ID:** `{_markdown_code(finding.id)}`",
                f"- **Status:** {_markdown_text(finding.status)}",
                f"- **Severity:** {_markdown_text(finding.severity)}",
                f"- **Creator:** {_markdown_text(finding.creator)}",
                f"- **Identity:** {_markdown_text(finding.identity or 'not assigned')}",
                f"- **Target:** {_markdown_text(finding.target_id)}",
                f"- **Technique:** {_markdown_text(finding.technique_id)}",
                f"- **Validation:** {_markdown_text(finding.validation)}",
                "",
                "### Summary",
                "",
                _markdown_text(finding.summary),
                "",
                "### Reproduction",
                "",
                _markdown_text(finding.reproduction),
                "",
                "### Remediation",
                "",
                _markdown_text(finding.remediation),
                "",
                "### Evidence",
                "",
            ]
        )
        lines.extend(f"- {_markdown_text(reference)}" for reference in finding.evidence_references)
        if not finding.evidence_references:
            lines.append("No evidence references.")
        if finding.validation_summary:
            lines.extend(["", "### Validation", "", _markdown_text(finding.validation_summary)])
        if finding.partial_signal is not None:
            lines.extend(
                [
                    "",
                    "### Partial compliance (not a verdict)",
                    "",
                    f"- **Observed:** {_markdown_text(finding.partial_signal.observed)}",
                    f"- **Refused:** {_markdown_text(finding.partial_signal.refused)}",
                ]
            )
            if finding.partial_signal.graded_by:
                lines.append(f"- **Graded by:** {_markdown_text(finding.partial_signal.graded_by)}")
        if finding.witnesses:
            lines.extend(["", "### Frozen witnesses", ""])
            lines.extend(
                f"- {_markdown_text(witness.reference)} "
                f"({_markdown_text(witness.digest)}, {_markdown_text(witness.source)})"
                for witness in finding.witnesses
            )
        if finding.attack_path:
            lines.extend(["", "### Attack path", ""])
            lines.extend(
                f"{step.ordinal + 1}. {_markdown_text(step.action)} — "
                f"{_markdown_text(step.observation)}"
                for step in finding.attack_path
            )
        lines.append("")
    return "\n".join(lines)


def export_html(findings: Iterable[Finding]) -> str:
    accepted = accepted_findings(findings)
    cards = (
        "".join(
            f"<article><h3>{html.escape(finding.title)}</h3><p>{html.escape(finding.summary)}</p>"
            f'<p class="meta">{html.escape(finding.severity)} · {html.escape(finding.validation)}</p></article>'
            for finding in accepted
        )
        or "<p>No accepted findings.</p>"
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Merlin findings report</title><style>body{{margin:0;color:#20272d;font:15px/1.55 system-ui,sans-serif}}.layout{{display:grid;grid-template-columns:220px minmax(0,900px);gap:48px;max-width:1240px;margin:auto;padding:48px 28px}}nav{{position:sticky;top:28px;align-self:start;border-right:1px solid #dde2e6}}nav a{{display:block;padding:5px 18px 5px 0;color:#285bb5}}main{{min-width:0}}h1,h2{{font-family:Georgia,serif}}h1{{font-size:48px}}h2{{font-size:28px;margin-top:56px;border-top:1px solid #dde2e6;padding-top:16px}}article{{padding:18px 20px;margin:12px 0;background:#f5f7f8;border-left:4px solid #b52836}}.meta{{color:#68737d}}details{{display:none}}@media(max-width:760px){{.layout{{display:block;padding:24px 18px}}nav{{display:none}}details{{display:block;border:1px solid #dde2e6;padding:12px}}details a{{display:block}}}}</style></head><body><div class="layout"><nav aria-label="Report contents"><strong>Contents</strong><a href="#summary">Executive summary</a><a href="#findings">Findings</a></nav><main><header id="summary"><p>Merlin assessment</p><h1>Findings report</h1><p>{len(accepted)} accepted findings.</p></header><details><summary>Contents</summary><a href="#summary">Executive summary</a><a href="#findings">Findings</a></details><section id="findings"><h2>Findings</h2>{cards}</section></main></div></body></html>"""


def export_csv(findings: Iterable[Finding]) -> bytes:
    rows = [finding.model_dump(mode="json") for finding in accepted_findings(findings)]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        buffer = io.StringIO(newline="")
        keys = sorted({key for row in rows for key in row})
        writer = csv.DictWriter(buffer, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value) if isinstance(value, (dict, list)) else value
                    for key, value in row.items()
                }
            )
        archive.writestr("findings.csv", buffer.getvalue())
    return output.getvalue()


def export_report(
    findings: Iterable[Finding], format: ReportFormat, audience: ReportAudience = "internal"
) -> str | bytes:
    findings = tuple(findings)
    if audience == "customer":
        findings = tuple(
            finding.model_copy(
                update={
                    "creator": "[redacted]",
                    "reproduction": "Reproduction details are available in the internal report.",
                    "evidence_references": [],
                    "witnesses": [],
                    "attack_path": [],
                    "partial_signal": None,
                }
            )
            for finding in findings
        )
    if format == "html":
        return export_html(findings)
    if format == "markdown":
        return export_markdown(findings)
    if format == "json":
        return export_json(findings)
    if format == "csv":
        return export_csv(findings)
    raise ValueError(f"unsupported report format {format!r}")
