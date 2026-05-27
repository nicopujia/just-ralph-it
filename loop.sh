#!/bin/bash
# Usage: ./loop.sh
# Configure it with env vars below

MODEL=${MODEL:-gpt-5.4}
REASONING_EFFORT=${REASONING_EFFORT:-high}
PLAN_EVERY_N_ITERATIONS=${PLAN_EVERY_N_ITERATIONS:-10}
PLAN_FIRST=${PLAN_FIRST:-0}

BUILD_ITERATION=0
CURRENT_BRANCH=$(git branch --show-current)
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
STOP_FILE="$SCRIPT_DIR/.jri/stop"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Branch: $CURRENT_BRANCH"
if [ "$PLAN_FIRST" = "1" ] || [ "$PLAN_FIRST" = "true" ]; then
    echo "Flow: plan once, then build indefinitely; re-plan every $PLAN_EVERY_N_ITERATIONS builds"
else
    echo "Flow: build indefinitely; re-plan every $PLAN_EVERY_N_ITERATIONS builds"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

run_iteration() {
    local mode="$1"
    local prompt_file="$2"

    if [ -f "$STOP_FILE" ]; then
        echo "Stop file found at $STOP_FILE; exiting before $mode prompt."
        exit 0
    fi

    if [ ! -f "$prompt_file" ]; then
        echo "Error: $prompt_file not found"
        exit 1
    fi

    echo "⏳ Running Codex ($mode)..."
    echo ""

    codex exec \
        --dangerously-bypass-approvals-and-sandbox \
        --ignore-user-config \
        -m "$MODEL" \
        -c "model_reasoning_effort=\"$REASONING_EFFORT\"" \
        "$(cat "$prompt_file")"

    echo ""
    echo "✅ Codex $mode iteration complete"
}

if [ "$PLAN_FIRST" = "1" ] || [ "$PLAN_FIRST" = "true" ]; then
    run_iteration "plan" "PROMPT_plan.md"
fi

while true; do
    run_iteration "build" "PROMPT_build.md"

    BUILD_ITERATION=$((BUILD_ITERATION + 1))

    if [ $((BUILD_ITERATION % $PLAN_EVERY_N_ITERATIONS)) -eq 0 ]; then
        echo -e "\n\n======================== PLAN AFTER BUILD $BUILD_ITERATION ========================\n"
        run_iteration "plan" "PROMPT_plan.md"
    fi

    echo -e "\n\n======================== BUILD LOOP $BUILD_ITERATION ========================\n"
done
