---
title: Prettify Ralph output with ANSI formatting
priority: 2
assignee: Ralph
depends_on: []
acceptance_criteria:
  - "Each iteration prints a visible header before Ralph starts (e.g. `─── iteration N: <task-slug> ───` in bold/color)."
  - "Ralph streaming text output is preserved as-is (no reformatting of the LLM's own text)."
  - "Large tool outputs (file reads, search results) are trimmed: show file path + line count instead of full contents. The full output is still written to the log file unmodified."
  - "Iteration completion/failure is indicated with a colored status line (e.g. ✓ completed, ✗ failed, ⚠ needs human)."
  - "All formatting uses raw ANSI escape codes — no new dependencies."
  - "`make check` passes."
---

Currently Ralph output is bare `sys.stdout.write()` — raw text, no colors, no iteration counter, no task name. The user sees LLM tool outputs verbatim, including full file contents from reads.

Two changes needed:

### 1. Wrap streaming output with context

Add ANSI-colored markers around each iteration:
- **Iteration header**: printed before `_run_iteration` starts the OpenCode subprocess. Shows iteration number and task slug.
- **Status footer**: printed after the iteration completes. Shows outcome (completed/failed/needs human) with a colored symbol.
- **Step separators**: keep the existing newline insertion between `step_finish` events.

Use raw ANSI escape codes (no `rich` or other library). Define a small helper module (e.g. `src/jri/core/ui.py`) with constants for colors and a few formatting functions.

### 2. Trim verbose tool outputs in the event parser

In `opencode.py`, the `_tool_use_text` function currently returns the full tool output string. When the output exceeds a threshold (e.g. > 20 lines or > 2000 chars), trim it to a summary showing:
- For file reads: path + line count (e.g. `📄 src/jri/core/service.py (1481 lines)`)
- For other tools: first few lines + "… (N lines trimmed)"

The trimmed version goes to `sys.stdout`. The **full unmodified output still goes to the log file** — trimming is display-only.

Implementation notes:
- The streaming loop is in `opencode.py`, lines 179-209.
- Iteration headers/footers should be emitted from `service.py`'s `_run_iteration` method, not from `opencode.py`.
- The `_tool_use_text` function (lines 37-50) extracts tool output from JSON events — this is where trimming logic belongs.
- Keep `_text_event_text` (LLM's own text) unmodified — only trim tool outputs.
- The log file (`log_file.write(line)`) must receive the raw unmodified JSON line, not the trimmed version.
