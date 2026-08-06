---
name: prompt-engineering
description: Write, compress, audit LLM agent system prompts—system prompts, AGENTS.md, CLAUDE.md, SKILL.md. Also when an agent ignores instructions, over-searches, stops early, or drifts scope.
---
# Agent prompts
Aim: the smallest set of high-signal lines that reliably produces the behavior. Attention is finite — each token added dilutes the rest, and recall degrades as context grows.

## Loop
Start minimal on the strongest model available. Run real tasks. Add a line only to fix a failure you observed; cut a line and re-run to prove it earns its place. Output varies — several runs per change, not one.

Keep a case per rule. Without cases you can neither cut safely nor tell a fix from a coincidence.

## Altitude
Too low: hardcoded if/else procedure — brittle, stale as soon as a file moves.
Too high: vague guidance assuming shared context.
Right: goal, hard limits, definition of done, heuristics for the judgment calls. Then let it pick the route.

## Contents
Include:
- Task and success criteria, stated so someone can test them.
- Hard limits and safety lines.
- Which tool when, and which source wins on conflict. Schemas already say what each tool does.
- Output shape and length.
- Stopping rules: how far to explore, when to act under uncertainty, when to stop and ask. Without these, agents over-search or over-ask.
- 2–4 diverse examples where showing beats stating. Not an edge-case dump.

Leave out: system background, restated schemas, anything readable at runtime. Point to a file instead of pasting it.

## Wording
- Say what to do. A "don't" spends attention on the behavior you don't want. Keep one only where no "do" covers the ground: a safety line, or a failure that leaves no trace.
- Name the choice, not the feeling. "Be thorough" is a mood; "lead with the conclusion" is checkable.
- Give the reason when it generalizes — the rule then covers cases you didn't list.
- One rule, one place. Two wordings read as two rules; a caution repeated fires where it shouldn't.
- Hunt contradictions. They cost more than any other defect: the agent burns reasoning reconciling them, then picks unpredictably.
- Compress hard — fragments, arrows, no articles. Keep every if/unless/threshold; compression eats those first. Per cut, ask what the line now permits.

## Known failure modes
- **Leaked identity.** A name in the prompt becomes the name of the user's product. Drop it; mark unavoidable identifiers as not-the-answer.
- **Drip-fed findings.** Models optimize for being right about what they report, not for reporting everything. Price it: an incomplete list is a defect even when every item on it is real.
- **Scope drift.** Extra features, extra styling, extra sections. Say: exactly and only what was asked; ambiguity → simplest valid reading.
- **Long runs.** Context rots. Notes to a file, compaction at milestones, subagents for wide search returning conclusions only.
