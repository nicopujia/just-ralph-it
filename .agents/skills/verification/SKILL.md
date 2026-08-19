---
name: verification
description: Verify changes thoroughly. Use when making substantial changes.
---
# Verification
Implementation ≠ done. Test and polish as a human would, one step at a time:

## Stage 1: Thorough testing
1. Cheap first: `./scripts/check.py` + one-off Python scripts against the new code.
2. Issues → subagent fixes → back to 1. Repeat until clean.
3. `./scripts/mutate.py` asks whether the tests assert on the lines the change wrote, which coverage cannot say. It reads commits, not the working tree, so commit first. Kill every survivor with an assertion, or say why the mutant makes no difference. Run it only once the suite is green: a red suite kills every mutant and reports nothing.
4. Subagent(s) manually test via `tmux` *as a real user would use JRI in production*: real messages, real models, judging behavior and answer quality. Example:
    ```bash
    smoke_dir="$(mktemp -d)"
    cp .env "$smoke_dir/.env"
    tmux new-window -t jri -n smoke "jri"
    sleep 4
    tmux send-keys -t jri:smoke "I want to build a small app for tracking books I read." Enter
    sleep 4
    tmux capture-pane -pt jri:smoke
    tmux kill-window -t jri:smoke
    rm -rf "$smoke_dir"
    ```
    Very bounded example. Scope scales with the change: judge how many conversations, how long, how complex — then prompt testing subagents accordingly.
5. Issues → judge whether they're real. Real → subagent fixes → back to 4. Else → stage 2.

## Stage 2: Ruthless refactoring
Minimize diff LOC additions (logic + prompts, not docs) preserving behavior and style.

Each round, a fresh subagent audits the diff and diff-adjacent code across `src/` and `tests/` against @AGENTS.md, hunting over-engineering and style violations. Require every finding in one pass — each round costs a full re-analysis, so a partial list is a defect even when every item in it is real.

Judge each finding against original intent; have the subagent apply the aligned ones. New subagent, repeat until:
- no findings, or
- no finding aligns with original spec.

Fresh subagent per round: one that already argued for its own suggestions won't attack them.

## Stage 3: Final smoke test
One more round post-refactor, lighter than stage 1 — just confirm nothing broke.

## Notes
- Only committed code is exemplary; dirty changes are disposable.
- Judge correctness against the [project concept document](https://nicolaspujia.com/just-ralph-it.md).
