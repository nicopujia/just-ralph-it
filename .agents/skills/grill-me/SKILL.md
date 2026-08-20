---
name: grill-me
description: Grill the user relentlessly about a plan, decision, or TODO until the design is agreed, then build and ship it on their word. Use to stress-test thinking, on any 'grill' trigger phrase, and before building anything the user has not decided.
---
# Grilling
Interview the user until you reach a shared understanding. Write no code until every branch is agreed.

## TODO list
Write every TODO down as it arrives, in your task list. Take them in list order, one design tree at a time: the tree in front of you holds the frontier, the TODOs behind it wait their turn. A TODO that arrives mid-grilling joins the end of the list, not the round in front of you.

A TODO is agreed when its frontier is empty and the user confirms. Write the settled design to `.tmp/designs/<todo>.md` before you open the next tree: whoever builds it reads that file, and a compacted thread cannot lose it. Grilling ends when every TODO is agreed: restate each agreed design in one short list, and act on it only on the user's word.

## Rounds
Map the TODO as a **design tree**: every decision branches into the decisions that hang off it.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are settled — the questions you can ask now without guessing at answers you have not heard. Ask the whole frontier in one round: number each question and give your recommended answer. Then wait.

```
❓ **Q1** — **<question title>**: <question body, options included>

➡️ <your recommended answer>

---

❓ **Q2** — **<question title>**: <question body, options included>

➡️ <your recommended answer>
```

Each round of answers reshapes the tree: settled decisions push the frontier outward and unblock the questions that waited on them. Recompute the frontier and ask the next round. A question whose answer depends on another question open in this round belongs to a later round.

An answer can reopen a tree already agreed. Say which decision moved, settle it with the user, and go on.

## Facts and decisions
Facts are your job, never the user's. A frontier question that needs a fact from the environment goes to a subagent, not to the user: how the code works today, what already exists, what a change would touch. Give it the question, take back the conclusion — the files it read belong in its context, not yours. Run the explorations of one round at once. Do not block on them: a question waiting on a fact is not on the frontier yet, so ask the round without it.

The decisions are the user's: put each one to them and wait.

Your context holds the design tree, the answers, and the state of the list. Everything else — files, findings, code, test output, release logs — is a subagent's to carry, for the whole run and not only for the grilling. The user keeps talking to this thread long after the code is written, so take back conclusions and never the work that produced them. A conclusion too long to read in the conversation goes to a file, and you take the path.

## Building
Build on the user's word, once every TODO is agreed — an answer about a later TODO reshapes the design of an earlier one, so the whole shape is decided before any of it is built.

Take the TODOs in dependency order, one to a pull request. Delegate each: the code from the agreed design, held to it exactly and only, then the `ship` skill. Point the delegate at the design file rather than restating the design, because it did not hear the grilling. Take back the pull request link and nothing else, tell the user what is left on the list, then take the next TODO. A TODO that builds on a pull request still open goes on top of it with `gh stack add` — see the stack in the `ship` skill.

How far each delegate carries the `ship` skill is the user's word on the restated list:
- **Propose** — step 2, then stop. Verified, pull request open, nothing merged and nothing released.
- **Ship** — step 3, then steps 4 and 5 delegated once, after the last pull request is on `main`. One version for the batch, numbered by the largest thing a user sees in it — one new behavior among five fixes still earns a minor. Take back the version and the result.

Two things reopen the grilling: a design that building shows cannot work, and a step that stops and reports after its three rounds. Settle that one decision with the user, then carry on.
