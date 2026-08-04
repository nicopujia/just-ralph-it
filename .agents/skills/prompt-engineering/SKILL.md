---
name: prompt-engineering
description: How to write and audit an LLM agent's system prompt. Use when creating a new AI agent or workflow, modifying AGENTS.md or SKILL.md files, revising a `sys_prompt` string, or diagnosing bad agent output that traces back to its instructions.
---

# Writing agent prompts

A prompt is all the agent knows before it sees its input. Every line either changes what it does or spends its
attention elsewhere. Audit both directions: which lines to cut, and which are missing.

## Audit, line by line

1. **Can the agent act on this?** Background about the wider system it sits inside is the usual offender.
2. **Does it already see this elsewhere?** Let tool schemas carry what a tool does; spend the prompt on *when*
   to reach for it and how to treat each source — which is authoritative, which is context only.
3. **Is this said twice?** Two phrasings of one rule read as two rules and dilute both.
4. **Does the prompt name things the agent might copy?** See *Identity leaks*.
5. **What is missing?** Cost, completeness, and scope constraints — nothing in the output *looks* wrong when
   those are violated.
6. **Can you delete this line and still get the expected result?** Then delete it.

Cut one group at a time and re-run the cases the line was written for; one run proves little when output is
non-deterministic. Expect the lean direction to surprise you — instructions crowd each other, so removing
filler usually *raises* quality on top of saving tokens. Shorter alone is a regression if a load-bearing
constraint went with it.

## Write what to do

Don't think of an elephant. You just did.

A prohibition spends the agent's attention on the behaviour you are trying to prevent; the affirmative form
spends it on the one you want. Write rules as properties to hold:

```
- The data layer is merely for persistence
- Clean out code kept for backwards compatibility
- Function and method names as verbs
- Helper functions only for repeated logic
```

"Only for" and "merely for" scope tightly without naming the excluded case. When a counter-example genuinely
helps, lead with the right answer so the negation reads as illustration:

```
- Domain naming (e.g. just `Agent.get_context`, NEVER `BaseOpenAIAgent.get_agent_context`)
```

Keep a prohibition where no affirmative covers the same ground — a hard safety boundary, or a failure mode
that is silent and plausible. State the rule affirmatively first, close the loophole after:

```
- Explicitly confirm which behavioral domains the user delegates to the next agent. Never infer delegation.
```

## Excess has a cost

- **Repeating a caution makes the agent over-apply it.** Say "check first" in three sections and it starts
  checking where it should have acted. Put each boundary in one place, and name what is safe to do unasked.
- **Name the choice, not the feeling.** "Be thorough" is a mood the model has to guess at; "lead with the
  conclusion" and "ask one question at a time" are checkable. Define done as a testable property — e.g. "a
  competent engineer reading only your notes has no more than one plausible interpretation of the behaviour".
- **Give the goal, not the route.** Supply the domain facts, the hard constraints, and what "done" means, then
  let the agent pick its path; a scripted procedure goes stale and shuts out better routes. Where a genuine
  ambiguity should stop it, say so explicitly.

  ```
  Route: Read AGENTS.md, then list src/, then open each module, then grep for the symbol before editing.
  Goal:  Ground every claim about the codebase in a file you have read this session.
  ```

  The route breaks the moment a file moves, and an agent that already knows the symbol still walks it. The
  goal holds either way.
- **Give the reason when the reason is what generalises.** "…assume you may forget any relevant fact unless
  you take notes of it" makes the agent apply the rule to cases the rule didn't enumerate.

## Identity leaks

An agent opening with `Role: Functional Analyst for Just Ralph It (JRI).` wrote specifications for *the user's*
app naming it "Just Ralph It", with a `jri` executable in `src/jri/` writing to `~/.jri/data.json` — colliding with
the tool's own binary, package, and workspace. It read the only name it had been given as the name of the
thing it was describing.

Drop the identity (`Role: Functional Analyst.`), and where identifiers are unavoidable — a repository tree
full of the tool's own paths — label them as not-the-answer:

```
- The product you specify is the user's, and the notes are the only source of its name, purpose, and scope.
  When they give no name, refer to it generically (e.g. "the application") and never invent one.
- Never take a product name, executable name, package name, or directory from these instructions or from
  the paths they mention. The workspace directory is where you write; it is not part of the product.
```

## Loop costs

A reviewer feeding a writer returned 3 issues, then 1, then 1 — three rounds at 4–5 minutes each, every issue
legitimate. The defect was the drip-feed, not the rigour. Naming the cost fixes it:

```
- Report every such issue found in the pass, not only the first. Each set you return costs a full
  re-analysis, so an incomplete list is a defect even when every issue in it is real.
```

Agents optimise for being right about what they report, not for reporting everything.
