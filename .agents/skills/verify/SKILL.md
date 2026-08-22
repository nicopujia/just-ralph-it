---
name: verify
description: Verify changes made to JRI source. Use when making non-trivial logic changes, such as new features, bug fixes, and refactors. Skip for documentation or copy changes.
---
# Verification
Implemented ≠ done. Test and polish as a human would, one step at a time.

Run this in a thread that did not write the change — fresh eyes are what make it worth running. Report the verdict and the issues that survived; the transcripts and the file reads stay here.

## Stage 1: Thorough testing
1. Cheap first: `./scripts/check.py` + one-off Python scripts against the new code.
2. Issues → subagent fixes → back to 1. Repeat until clean.
3. `./scripts/mutate.py` asks whether the tests assert on the lines changed, which coverage cannot say. It reads commits, not the working tree, so commit first. Kill every survivor with an assertion, or say why the mutant makes no difference. Run it only once the suite is green—a red suite kills every mutant and reports nothing.
4. Subagent(s) manually test *as a real user would use JRI in production*—real messages, real models, judging behavior and answer quality. Example using `tmux`:
    ```bash
    project="$PWD"
    smoke_dir="$(mktemp -d)"
    cp .env "$smoke_dir/.env"
    (cd "$smoke_dir" && uv run --project "$project" jri init)
    sed -i s@xai/grok-4.6@openai/gpt-5.6-sol@ "$smoke_dir/.jri/settings.yaml"
    smoke="jri-smoke-$$"
    tmux new-session -d -s "$smoke" -x 120 -y 40 -c "$smoke_dir" "uv run --project $project jri chat"
    sleep 4
    tmux send-keys -t "$smoke" -l "I want to build a small app for tracking books I read."
    sleep 3
    tmux capture-pane -pt "$smoke" | grep -F "books I read."
    tmux send-keys -t "$smoke" Enter
    sleep 5
    until ! tmux capture-pane -pt "$smoke" | grep -q Thinking; do sleep 5; done
    tmux capture-pane -pt "$smoke"
    tmux kill-session -t "$smoke"
    rm -rf "$smoke_dir"
    ```
    Open a session of your own and end that one. `$$` makes the name unique, so it can never be a session someone else is working in. Never run `tmux kill-server`, and never kill a session or window you did not create in this run: the developer may be working in tmux too, killing the last session takes the server and every pane with it, and a smoke test is not worth their day. A crashed run leaves its session behind under the same `jri-smoke-*` name it opened, and those are the only ones to clear.

    Send a message in three steps. `-l` sends the text and no key, the pane shows what arrived, and a third call sends `Enter`. One call with both truncates the message. The input still reads keys when `Enter` arrives, and JRI keeps only a part of the line. So find the last words on the pane first, and send the rest again when they are absent. The interviewer answers in 20 to 60 seconds: poll the pane, and never wait a fixed time. `Escape Escape` stops a turn that runs, and `C-q C-q` quits.

    `init` sets the workspace up and returns; `chat` is the window that reads keys. Always start JRI with `uv run --project <worktree>`—a bare `jri` runs the globally installed version, not the changed source. `init` writes the default models, and this project smoke-tests with other ones, so edit the settings file it wrote. Very bounded example. Scope scales with the change: judge how many conversations, how long, how complex—then prompt testing subagents accordingly.
5. Issues → judge whether they're real. Real → subagent fixes → back to 4. Else → stage 2.

A failure that survives three rounds of fixes ends the stage — report what was tried and wait.

## Stage 2: Ruthless refactoring
Minimize diff LOC additions (logic and prompts, not comments or docs) preserving behavior and style.

Each round, a fresh subagent audits the diff and diff-adjacent code against @AGENTS.md, hunting over-engineering and style violations. Require every finding in one pass — each round costs a full re-analysis, so a partial list is a defect even when every item in it is real.

Judge each finding against original intent; have the subagent apply the aligned ones. New subagent, repeat until:
- no findings, or
- no finding aligns with original spec.

Use fresh subagent per round because one that already argued for its own suggestions won't attack them.

## Stage 3: Final smoke test
One more round post-refactor, lighter than stage 1—just confirm nothing broke.

## Notes
- Only committed code in `main` is exemplary; dirty changes are disposable.
- Judge behavioral correctness against the [project concept document](https://nicolaspujia.com/just-ralph-it.md).
