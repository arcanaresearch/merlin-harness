---
name: workspace-reader
description: Read a requested workspace text file before answering questions about it.
---

# Workspace Reader

When the user asks about a workspace file, call the `read_file` tool before answering.
Never guess file contents. Keep the answer concise and begin it with `SKILL_ACTIVE:`.
Referenced scripts, if any are added later, must not be executed automatically.
