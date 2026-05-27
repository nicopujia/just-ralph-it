#!/bin/bash
# Usage: ./loop.sh
# Runs one planning iteration, then one more after every 10 build iterations.

BUILD_ITERATION=0
CURRENT_BRANCH=$(git branch --show-current)

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Branch: $CURRENT_BRANCH"
echo "Flow:   plan, then build indefinitely; re-plan every 10 builds"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

run_iteration() {
    local mode="$1"
    local prompt_file="$2"

    if [ ! -f "$prompt_file" ]; then
        echo "Error: $prompt_file not found"
        exit 1
    fi

    echo "⏳ Running Codex ($mode)..."
    echo ""

    codex exec \
        --dangerously-bypass-approvals-and-sandbox \
        --ignore-user-config \
        "$(cat "$prompt_file")"

    echo ""
    echo "✅ Codex $mode iteration complete"
}

run_iteration "plan" "PROMPT_plan.md"

while true; do
    run_iteration "build" "PROMPT_build.md"

    BUILD_ITERATION=$((BUILD_ITERATION + 1))

    if [ $((BUILD_ITERATION % 10)) -eq 0 ]; then
        echo -e "\n\n======================== PLAN AFTER BUILD $BUILD_ITERATION ========================\n"
        run_iteration "plan" "PROMPT_plan.md"
    fi

    echo -e "\n\n======================== BUILD LOOP $BUILD_ITERATION ========================\n"
done
