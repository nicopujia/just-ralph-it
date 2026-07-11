---
name: verify
description: Workflow to thoroughly verify your changes. Use when making large logic changes.
---

# Verification process

Once you implement anything into codebase, the work is not done yet. You must test and polish your changes as a human would do. For that, stick to the following workflow in the order specified, one step at a time:

## Stage 1: Thorough Testing

1. First, cheaply test your changes with `./scripts/check.py` and running one-off Python scripts that test the new code.
2. If you find issues, spin a subagent to address them and then go back to step 1 until you don't have more issues.
3. Spin subagent(s) to manually test (via `tmux`) your changes *as a real user would use JRI in production*, sending real messages to real models, and judging the behavior and answers quality. For example:
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
    Note that this is example is very bounded. The scope of the manual test depends on the scope of the change made. You have to judge whether it is enough to send just one message or if your changes should be tested with one, or even more, conversations, and which should be the length and complexity of those conversations. After making your judgement, prompt your testing subagents accordingly.
4. If you find issues, judge whether they make sense. If they do, spin a subagent to address them and then go back to step 3 until you don't have more meaningful issues. Otherwise, proceed to the following stage.

## Stage 2: Ruthless Refactoring

Reduce diff LOC additions (only logic-wise, not docs-wise) as much as possible while preserving behavior and spec intent. For that, perform various subagents rounds using the following prompt:
```md
Based on @AGENTS.md guidelines, how can the diff or diff-related code be simplified?
```
The subagent will suggest some simplification approaches. You have to judge which ones make sense and which ones are not aligned with the original intent. Tell the subagent to apply the ones that make sense.

Then, perform another round, spinning another subagent with the same prompt. Repeat that loop until either
- the subagent does not find any simplification approaches, or
- no simplification approach is aligned with the original spec.

## Stage 3: Final smoke test

After refactoring, do one more testing round, though less thoroughly than on stage 1, just to ensure you didn't break anything.

---

## Important considerations

- Only treat committed code as an example to follow; dirty changes are disposable.
- To judge whether behavior is correct or not, study the [project concept document](https://nicolaspujia.com/just-ralph-it.md) and think if they are aligned with it.
