---
title: Improve Ralph system prompt for OpenCode
depends_on:
  - migrate-to-opencode
---

## Context

Ralph's system prompt needs to evolve from the current flat 36-line rule list (`src/app/prompts/ralph.py`) into a structured prompt suitable for OpenCode. The PROMPT.xml from the attempt-2 branch is the strongest base -- it already has phased workflow, TDD rigor, status signals, and subagent management. But it's missing key ideas from omo's Hephaestus that would make Ralph more autonomous and effective.

This is a prompt rewrite, not a code change. Output is a single prompt file that gets injected into OpenCode sessions.

## What to keep from PROMPT.xml (base)

- 7-step workflow: Planning -> Analysis -> Design -> TDD -> Pre-merge Validation -> Maintenance -> Finish
- Outside-in TDD with inner/outer loops (red-green-refactor inside scenario-level integration tests)
- Status signals for loop controller: `COMPLETED ASSIGNED ISSUE`, `HUMAN HELP ABSOLUTELY NEEDED`, `FOUND NEW BLOCKER ISSUE`
- Context block awareness (past tasks, diffs, project metadata)
- Idempotency reminder (changes may be rolled back and replayed)
- Subagent management guide (decomposition, parallelization, error recovery, context passing)
- Task tracking via `ralph task` CLI
- Git commit conventions
- Project structure awareness

## What to graft in from Hephaestus

### 1. Intent Gate (new Phase 0, before Step 1)

Classify the task before doing anything:
- **Trivial**: single file, <10 lines, known location -> skip Design step, minimal TDD
- **Explicit**: specific file/line, clear change -> execute directly with targeted tests
- **Exploratory**: needs codebase research -> fire parallel explore agents first
- **Open-ended**: "add feature", "refactor" -> full workflow

This prevents over-engineering simple tasks and under-exploring complex ones.

### 2. "Never ask, just do" rule

Add to Reminders. Explicitly forbid:
- Asking permission ("Should I proceed?", "Would you like me to...")
- Stopping after partial implementation
- Planning without executing

Models love to ask. Ralph should decide and act. If uncertain, note assumptions in the final message, not as questions mid-work.

### 3. Exploration mandate in Analysis step (Step 2)

Before designing a solution, mandate:
- Fire 2-5 parallel subagents to explore relevant parts of the codebase
- Research existing patterns, dependencies, test conventions
- Only proceed to Design after exploration results are in

Current PROMPT.xml says "research the codebase" but doesn't mandate parallel exploration.

### 4. Stricter verification in Pre-merge Validation (Step 5)

From Hephaestus, add:
- Run linting/type-checking on ALL modified files (not just tests)
- Explicit "no evidence = not complete" rule
- Re-read the original task before declaring done -- did you miss anything?

### 5. Failure recovery protocol

Add structured recovery:
1. First approach fails -> try a different approach
2. Second approach fails -> decompose the problem differently
3. Third approach fails -> revert to last working state, file a blocker, stop

Never leave code broken. Never delete failing tests. Never shotgun debug.

### 6. Progress signals

Add lightweight progress reporting at meaningful milestones:
- Before exploration: what you're looking for
- After discovery: what you found
- Before large edits: what you're about to change
- On blockers: what went wrong and what you're trying next

Keeps the loop controller and user informed without being chatty.

## What NOT to take from Hephaestus

- GPT-specific compensations (repetitive "NON-NEGOTIABLE" hammering, anti-permission-asking repeated 5 times)
- Harness-specific wiring (dynamic tool injection, background_cancel API, omo agent delegation format)
- The 600-line verbosity -- aim for ~150-200 lines max
- Todo/task tracking via todowrite (we have our own task system)
- LSP-specific tool references (keep tool-agnostic where possible)
- Output contract formatting rules (not relevant for autonomous loop)

## What to keep from current ralph.py

- Web app verification (start app, hit routes, check responses, tear down)
- Non-interactive flags reminder (`cp -f`, `mv -f`, `apt-get -y`)
- Human uploads location (`.jri/uploads/`)

## Format

Keep XML structure from PROMPT.xml. It's cleaner than markdown for nested workflow steps and the model parses it well.

## Acceptance criteria

- Single prompt file, XML format, ~150-200 lines
- All 7 workflow steps preserved with the additions above
- Intent gate as Phase 0
- "Never ask" rule in Reminders
- Exploration mandate in Analysis
- Failure recovery protocol
- Progress signals
- Status signals preserved for loop controller
- Web app verification preserved
- Non-interactive flags preserved
- No GPT-specific or omo-specific references
- No redundant repetition of rules
