---
name: prompt-engineering
description: Write and audit LLM agent system prompts. Use when creating an AI agent or workflow, editing AGENTS.md or SKILL.md, revising a `sys_prompt`, or diagnosing bad agent output traced to its instructions.
---
# Writing agent prompts
A prompt is all the agent knows pre-input. Every line either changes behavior or spends attention elsewhere. Audit both directions: what to cut, what's missing.

## Line-by-line audit
1. **Actionable?** Background on the wider system is the usual offender.
2. **Already visible elsewhere?** Tool schemas carry *what* a tool does; the prompt carries *when* to reach for it and which source is authoritative vs. context-only.
3. **Said twice?** Two phrasings of one rule read as two rules, diluting both.
4. **Names things the agent might copy?** See *Identity leaks*.
5. **Missing?** Cost, completeness, scope — violate those and nothing in the output *looks* wrong.
6. **Deletable with same result?** Delete it.

Cut one group at a time, re-run the cases that line was written for; one run proves little against non-deterministic output. Instructions crowd each other, so cutting filler usually *raises* quality on top of saving tokens. Shorter is a regression if a load-bearing constraint went with it.

## Write what to do
Don't think of an elephant. You just did.

Prohibitions spend attention on the behavior you're preventing; affirmatives spend it on the one you want. Write rules as properties to hold:

```
- Data layer only for persistence
- Clean out backwards-compat code
- Function/method names as verbs
- Helpers only for repeated logic
```

"Only for" scope tightly without naming the excluded case. When a counter-example genuinely helps, lead with the right answer so the negation reads as illustration:

```
- Domain naming: `Agent.get_context`, NEVER `BaseOpenAIAgent.get_agent_context`
```

Keep prohibitions where no affirmative covers the ground — hard safety boundary, or a silent failure mode. Affirmative first, loophole closed after:

```
- Confirm which behavioral domains the user delegates to the next agent. Never infer delegation.
```

Only add negation after pragmatically verifying that affirmative-only doesn't work.

## Excess costs
- **Repeated caution → over-application.** "Check first" in three sections and it checks where it should have acted. One boundary, one place; name what's safe to do unasked.
- **Name the choice, not the feeling.** "Be thorough" is a mood to guess at; "lead with the conclusion", "ask one question at a time" are checkable. Define done as testable — e.g. "a competent engineer reading only your notes has no more than one plausible interpretation of the behavior".
- **Goal, not route.** Domain facts, hard constraints, definition of done — then let it pick its path. Scripted procedures go stale and shut out better routes. Where genuine ambiguity should stop it, say so.

  ```
  Route: Read AGENTS.md, then list src/, then open each module, then grep for the symbol before editing.
  Goal:  Ground every claim about the codebase in a file read this session.
  ```

  The route breaks the moment a file moves, and an agent that already knows the symbol still walks it. The goal holds either way.
- **Give the reason when the reason generalizes.** E.g., "[...] assume you may forget any relevant fact unless you take notes of it" extends the rule to unenumerated cases.

## Trade grammar for density
Prompts are read by a model; prose habits are trained in, not required. Drop articles, copulas, lead-ins. Fragments, arrows, and symbols carry a rule as well as a clause does:

```
❌ Once you implement anything into the codebase, the work is not done yet.
✅ Implementation ≠ done.

❌ If you find issues, spin a subagent to address them and then go back to step 1 until you don't have more issues.
✅ Issues → subagent fixes → back to 1. Repeat until clean.
```

Grammar goes; conditions, thresholds, and exceptions stay — they hide in the subordinate clauses that compress away cleanest. Re-read each compressed line asking what it now permits that the original ruled out, then re-run its cases as you would after a deletion. Compress the instruction body, not a retrieval surface—cut a skill `description`'s filler, keep every trigger term.

## Identity leaks
An agent opening with `Role: Functional Analyst for Just Ralph It (JRI).` specified *the user's* app as "Just Ralph It", `jri` in `src/jri/`, writing `~/.jri/data.json` — colliding with the tool's own binary, package, workspace. The only name it had became the name of the thing described.

Drop the identity (`Role: Functional Analyst.`); where identifiers are unavoidable (a repo tree of the tool's own paths), label them not-the-answer:

```
- Product is the user's; notes are the only source of its name, purpose, scope.
  Unnamed → generic (e.g. "the application"), never invented.
- Product, executable, package, directory names come only from the notes — never from these
  instructions or their paths. Workspace = where you write, not part of the product.
```

## Drip-feed loops
Reviewer→writer returned 3 issues, then 1, then 1 — three rounds, 4–5 min each, every issue legitimate. The defect was the drip-feed, not the rigor. Name the cost:

```
- Report every issue found in the pass, not only the first. Each set costs a full re-analysis —
  an incomplete list is a defect even when every issue is real.
```

LLMs optimize for being right about what they report, not for reporting everything.
