# Contributing

Use Python 3.13 and `uv`. Prepare the workspace with `uv sync --all-packages --frozen`.

Run `make lint` and `make format-check` before submitting changes. `make format` applies
Ruff formatting. Python uses four spaces, double quotes, LF endings, and a 100-character
line target. Keep changes focused and run tests for the affected package.

Use `make wheels` to regenerate derived encrypted knowledge assets and build all workspace
wheels. The `.rpk.enc` files are generated and excluded from Git. Do not edit them directly.

No local credentials, browser profiles, target transcripts, internal plans, or runtime
databases belong in commits. Use synthetic fixtures for tests and representative reports.
See [licensing](LICENSING.md) before adding or redistributing bundled content.
