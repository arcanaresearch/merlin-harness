# Representative campaign reports

These retained artifacts are generated from one deterministic, evidence-rich campaign revision:

- `representative-report.html` is the default standalone report with desktop and collapsible mobile
  contents navigation, outcome visualization, findings, evidence, exchanges, lineage, frontier,
  limitations, and explicit provisional state.
- `representative-report.md` projects the same revision for source-control and text workflows.
- `representative-report-csv.zip` contains relational summary, finding, technique, evidence,
  exchange, lineage, language, learning, and unsearched-frontier tables.
- `representative-report.json` is the least-preferred, machine-integration projection of the same
  immutable revision; HTML remains the default human report.

Regenerate them with:

```shell
uv run python scripts/generate_sample_reports.py
```

The report test suite byte-compares regenerated artifacts with these files. This includes stable ZIP
member timestamps and permissions, so renderer changes require an intentional sample refresh.
