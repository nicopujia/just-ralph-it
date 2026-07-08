---
name: verify
description: Workflow to thoroughly verify your changes. Use when making large logic changes.
---
Spin a few very capable subagents to:
1. Reduce diff LOC additions (only logic-wise, not docs-wise) as much as possible while preserving behavior.
2. Review diff based on this Guidelines section point by point, and to refactor if it found any inconsistencies.
3. Manually test (via `tmux`) your changes as a real user would use JRI in production, sending real messages to real models, and judging the behavior and answers quality. For example:
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
