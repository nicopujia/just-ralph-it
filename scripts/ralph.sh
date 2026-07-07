#!/usr/bin/env bash
set -euo pipefail

if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="${1:-}"
MAX_ITERATIONS="${2:-0}"
CODEX_BIN="${CODEX_BIN:-codex}"
CODEX_MODEL="${CODEX_MODEL:-gpt-5.5}"
CODEX_REASONING_EFFORT="${CODEX_REASONING_EFFORT:-xhigh}"

if ! [[ "$MAX_ITERATIONS" =~ ^[0-9]+$ ]]; then
    echo "max_iterations must be a non-negative integer"
    exit 2
fi

if ! command -v "$CODEX_BIN" >/dev/null 2>&1; then
    echo "Codex command not found: $CODEX_BIN"
    exit 127
fi

case "$MODE" in
    plan)
        ;;
    build)
        ;;
    *)
        echo "Usage: ./scripts/ralph.sh <plan|build> [max_iterations]"
        exit 2
        ;;
esac

prompt() {
    case "$MODE" in
        plan)
            cat <<'PROMPT'
0a. Study `.jri/specs/*` with up to 250 parallel mini subagents to learn the application specifications.
0b. Study @IMPLEMENTATION_PLAN.md (if present) to understand the plan so far.

1. Study @IMPLEMENTATION_PLAN.md (if present; it may be incorrect) and use up to 500 mini subagents to study existing source code in `src/*` and compare it against `.jri/specs/*`. Use a 5.5 xhigh subagent to analyze findings, prioritize tasks, and create/update @IMPLEMENTATION_PLAN.md as a bullet point list sorted in priority of items yet to be implemented. Ultrathink. Consider searching for TODO, minimal implementations, placeholders, skipped/flaky tests, and inconsistent patterns. Study @IMPLEMENTATION_PLAN.md to determine starting point for research and keep it up to date with items considered complete/incomplete using subagents.

IMPORTANT: Plan only. Do NOT implement anything. Do NOT assume functionality is missing; confirm with code search first.
PROMPT
            ;;
        build)
            cat <<'PROMPT'
0a. Study `.jri/specs/*` with up to 500 parallel mini subagents to learn the application specifications.
0b. Study @IMPLEMENTATION_PLAN.md.
0c. For reference, the application source code is in `src/*`.

1. Your task is to implement functionality per the specifications using parallel subagents. Follow @IMPLEMENTATION_PLAN.md and choose the most important item to address. Before making changes, search the codebase (don't assume not implemented) using mini subagents. You may use up to 500 parallel mini subagents for searches/reads and only 1 5.4 subagent for build/tests. Use 5.5 xhigh subagents when complex reasoning is needed (debugging, architectural decisions).
2. After implementing functionality or resolving problems, run the tests for that unit of code that was improved. If functionality is missing then it's your job to add it as per the application specifications. Ultrathink.
3. When you discover issues, immediately update @IMPLEMENTATION_PLAN.md with your findings using a subagent. When resolved, update and remove the item.
4. When the tests pass, update @IMPLEMENTATION_PLAN.md, then `git add -A` then `git commit` with a message describing the changes.

99999. Important: When authoring documentation, capture the why — tests and implementation importance.
999999. Important: Single sources of truth, no migrations/adapters. If tests unrelated to your work fail, resolve them as part of the increment.
99999999. You may add extra logging if required to debug issues.
999999999. Keep @IMPLEMENTATION_PLAN.md current with learnings using a subagent — future work depends on this to avoid duplicating efforts. Update especially after finishing your turn.
9999999999. When you learn something new about how to run the application, update @AGENTS.md using a subagent but keep it brief. For example if you run commands multiple times before learning the correct command then that file should be updated.
99999999999. For any bugs you notice, resolve them or document them in @IMPLEMENTATION_PLAN.md using a subagent even if it is unrelated to the current piece of work.
999999999999. Implement functionality completely. Placeholders and stubs waste efforts and time redoing the same work.
9999999999999. When @IMPLEMENTATION_PLAN.md becomes large periodically clean out the items that are completed from the file using a subagent.
99999999999999. If you find inconsistencies in the .jri/specs/* then use an 5.5 xhigh subagent with 'ultrathink' requested to update the specs.
999999999999999. IMPORTANT: Keep @AGENTS.md operational only — status updates and progress notes belong in `IMPLEMENTATION_PLAN.md`. A bloated AGENTS.md pollutes every future loop's context.
PROMPT
            ;;
    esac
}

if [ -z "${TMUX:-}" ]; then
    echo "Tip: this is a long-running autonomous loop. Prefer running it inside tmux."
fi

BRANCH="$(git branch --show-current)"
if [ "$MODE" = "build" ] && [ -z "$BRANCH" ]; then
    echo "Build mode requires a named git branch so ralph.sh can push after each iteration."
    exit 1
fi
ITERATION=0

echo "Mode: $MODE"
echo "Prompt: embedded $MODE"
echo "Model: $CODEX_MODEL"
echo "Reasoning effort: $CODEX_REASONING_EFFORT"
echo "Branch: $BRANCH"
if [ "$MAX_ITERATIONS" -gt 0 ]; then
    echo "Max iterations: $MAX_ITERATIONS"
fi

while [ "$MAX_ITERATIONS" -eq 0 ] || [ "$ITERATION" -lt "$MAX_ITERATIONS" ]; do
    ITERATION=$((ITERATION + 1))
    echo
    echo "======================== LOOP $ITERATION ========================"
    echo

    prompt | "$CODEX_BIN" exec \
        -C "$ROOT" \
        -m "$CODEX_MODEL" \
        -c "model_reasoning_effort=\"$CODEX_REASONING_EFFORT\"" \
        -s danger-full-access \
        --dangerously-bypass-approvals-and-sandbox \
        -

    if [ "$MODE" = "build" ]; then
        git push origin "$BRANCH" || git push -u origin "$BRANCH"
    fi
done

if [ "$MAX_ITERATIONS" -gt 0 ]; then
    echo "Reached max iterations: $MAX_ITERATIONS"
fi
