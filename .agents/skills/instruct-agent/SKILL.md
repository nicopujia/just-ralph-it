---
name: instruct-agent
description: Write, compress, audit LLM inputs—system prompts, AGENTS.md, SKILL.md. Load before editing any text a model reads, even where the change is mostly code. Use also when an agent ignores instructions, over-searches, stops early, or drifts scope.
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
- Which tool when, and which source wins on conflict.
- Output shape and length.
- Stopping rules: how far to explore, when to act under uncertainty, when to stop and ask. Without these, agents over-search or over-ask.
- 2–4 diverse examples where showing beats stating. Not an edge-case dump.

Leave out: system background, restated schemas, anything a one-command lookup answers, behavior the model already defaults to ("a report at the length its findings need"). Point to a file instead of pasting it.

Restating the environment is a cache — it earns its load only when the lookup is expensive: the unwritten convention, the reason behind a choice, the gotcha no config confesses.

## Pointers
A pointer names material out of context and the branches that reach it: a skill `description`, an AGENTS.md line naming a doc. Its wording, not its target, decides whether the agent gets there — must-have material behind a weak pointer reads as a missing rule. Sharpen the words before you inline the material.
- Lead with the trigger word.
- One trigger per branch. Synonyms for one branch are one branch written twice.
- Cut the identity the body already carries.

A pointer spends tokens every turn, fired or not, so prune it harder than the body. Something invoked only by hand needs none: strip the description and pay nothing in context — the human becomes the index instead, which is the right trade wherever their judgment is the point.

## Done
Every step ends on a bound the agent can check. A fuzzy bound — "understanding reached" — lets it call itself done early, pulled on by the steps still in view. The bound also sets how much work happens: "every changed model accounted for" digs where "produce a change list" does not, and it holds with no steps at all.

Sharpen the bound before you split the sequence, and split only across a real context boundary — a hand-off, a subagent — because an inline call leaves the later steps in view.

## Wording
- Say what to do. A "don't" spends attention on the behavior you don't want. Keep one only where no "do" covers the ground: a safety line, or a failure that leaves no trace.
- Name the choice, not the feeling. "Be thorough" is a mood; "lead with the conclusion" is checkable.
- Give the reason when it generalizes — the rule then covers cases you didn't list.
- One rule, one place. Two wordings read as two rules; a caution repeated fires where it shouldn't.
- Hunt contradictions. They cost more than any other defect: the agent burns reasoning reconciling them, then picks unpredictably.
- Compress hard — fragments, arrows, no articles. Keep every if/unless/threshold; compression eats those first. Per cut, ask what the line now permits.
- Lean on a compact word the model already holds — *frontier*, *gate*, *stack*, *tight* — as a token, never a sentence: it anchors a region of behavior for one token because it recruits priors instead of buying a definition. Hunt the passages one retires ("fast, deterministic, low-overhead" → *tight*), and reuse it in the pointer. Coin nothing new; an invented word recruits nothing and you pay its meaning in tokens. A word too weak to beat the default (*careful*) is a no-op — the fix is a stronger word, not another rule.

## Audit
Reading a prompt you cannot run: check it against this file, in this order.
1. Contradictions — two lines that disagree. Costliest, so first.
2. Leaked identity — the system named where the output belongs to the user. Silent: nothing downstream catches it.
3. Misplaced rules — a rule about X stated only in something that calls X.
4. Weak pointers — must-have material behind wording that will not fire.
5. Missing stopping rules — loops with no exit, "repeat until" with no bound, criteria the agent cannot check.
6. Unbacked claims — every path, flag, and command verified against the repo.
7. Wording.

Out: findings worst first, each as what the lines say → why it fails → the fix in one line. Then what works, in two or three lines. Rewrite only when asked.

## Known failure modes
- **Leaked identity.** A name in the prompt becomes the name of what the agent produces. A role that speaks for the system may name it; wherever the output belongs to the user, the name offers itself as an answer. Drop it—the passive voice costs nothing—and mark unavoidable identifiers as not-the-answer. Worst in text injected per item, which repeats the name as the output grows.
- **Drip-fed findings.** Models optimize for being right about what they report, not for reporting everything. Price it: an incomplete list is a defect even when every item on it is real.
- **Scope drift.** Extra features, extra styling, extra sections. Say: exactly and only what was asked; ambiguity → simplest valid reading.
- **Long runs.** Context rots. Notes to a file, compaction at milestones, subagents for wide search returning conclusions only.
