# Improve Ralph system prompt for OpenCode

## TL;DR
> **Summary**: Rewrite `src/app/prompts/ralph.py` from a flat rule list into a balanced XML-structured prompt that uses `origin/attempt-2-(aleph-hackathon):pkgs/ralph/PROMPT.xml` as the base shape while preserving JRI-specific task/runtime rules.
> **Deliverables**:
> - XML-based `RALPH_SYSTEM_PROMPT` in `src/app/prompts/ralph.py`
> - Preserved status signals and JRI-specific operational rules
> - Automated prompt verification via string/import/serialization/smoke checks
> **Effort**: Short
> **Parallel**: NO
> **Critical Path**: 1 → 2 → 3 → 4 → 5

## Context
### Original Request
Prompt engineer `src/app/prompts/ralph.py` using the existing draft at `.jri/tasks/draft/improve-ralph-prompt.md` and the confirmed base file `origin/attempt-2-(aleph-hackathon):pkgs/ralph/PROMPT.xml`.

### Interview Summary
- User confirmed the intended base is `origin/attempt-2-(aleph-hackathon):pkgs/ralph/PROMPT.xml`.
- User chose a **balanced rewrite**: keep the XML workflow/status-contract shape, but rewrite sections as needed for brevity and JRI-specific behavior.
- User chose **prompt-only** scope: rewrite the prompt without adding new prompt-content tests to the repo.

### Metis Review (gaps addressed)
- Guard against obsolete-base leakage: remove `.ralphy/`, `tasks.yaml`, `ralph task`, `AGENTS.md`, and old project-structure instructions.
- Preserve exact status-signal strings while allowing semantic rewrites elsewhere.
- Add explicit exception boundaries so the new “never ask” rule does not conflict with blocker/human-help escalation.
- Verify runtime safety with import + JSON serialization smoke checks instead of assuming XML formatting is safe.

## Work Objectives
### Core Objective
Produce a single-file rewrite of `src/app/prompts/ralph.py` that upgrades Ralph from a flat prompt to an XML-structured OpenCode prompt while preserving the current JRI task/runtime contract and the required workflow/status behavior.

### Deliverables
- Rewritten `RALPH_SYSTEM_PROMPT` in XML structure inside `src/app/prompts/ralph.py`
- Phase 0 Intent Gate added ahead of the 7-step workflow
- Preserved exact status signals for the loop controller
- Preserved JRI-specific task management, uploads path, docs requirements, non-interactive flags, and web verification/deploy guidance
- Automated verification commands proving import safety, JSON serialization safety, required-string presence, forbidden-string absence, line-budget compliance, and no out-of-scope file edits

### Definition of Done (verifiable conditions with commands)
- `PYTHONPATH=src uv run python - <<'PY'` imports `RALPH_SYSTEM_PROMPT` from `app.prompts.ralph` without syntax/runtime errors
- `PYTHONPATH=src uv run python - <<'PY'` serializes `{"agent": {"ralph": {"prompt": RALPH_SYSTEM_PROMPT}}}` with `json.dumps(...)` successfully
- `PYTHONPATH=src uv run python - <<'PY'` asserts required XML section tags and required phrases are present
- `PYTHONPATH=src uv run python - <<'PY'` asserts forbidden stale strings are absent
- `PYTHONPATH=src uv run python - <<'PY'` asserts prompt line count is within 150-200 lines
- `uv run pytest tests/test_integration.py -q` passes
- `git diff --name-only` shows only `src/app/prompts/ralph.py`

### Must Have
- XML structure aligned with the structured-prompt precedent in `src/app/prompts/ralphy.py`
- Exact preservation of `COMPLETED ASSIGNED ISSUE`, `HUMAN HELP ABSOLUTELY NEEDED`, and `FOUND NEW BLOCKER ISSUE`
- A new Phase 0 Intent Gate before the existing 7 workflow steps
- A “Never ask, just do” rule with explicit exceptions for genuine blockers/human-help cases
- Mandatory 2-5 parallel exploration-agent guidance in Analysis
- Failure recovery protocol and progress signals
- JRI-specific references to `.jri/uploads/` and `.jri/tasks/{status}/{slug}.md`
- Explicit non-interactive flags guidance and web verification/deploy guidance

### Must NOT Have (guardrails, AI slop patterns, scope boundaries)
- No edits outside `src/app/prompts/ralph.py`
- No new tests or runtime code changes
- No stale `.ralphy/`, `tasks.yaml`, `ralph task`, `AGENTS.md`, or obsolete project-structure references
- No GPT-specific, OMO-specific, or tool-harness-specific wording
- No redundant rule repetition or bloated XML examples copied from the old base
- No weakening or paraphrasing of the exact status signals

## Verification Strategy
> ZERO HUMAN INTERVENTION — all verification is agent-executed.
- Test decision: tests-after using prompt-specific Python assertions + existing pytest smoke (`tests/test_integration.py`)
- QA policy: Every task includes agent-executed content checks or runtime-safe prompt checks
- Evidence: `.sisyphus/evidence/task-{N}-{slug}.{ext}`

## Execution Strategy
### Parallel Execution Waves
> This plan is intentionally sequential because all implementation work lands in one file (`src/app/prompts/ralph.py`).

Wave 1: task 1 (prompt scaffold + XML envelope)
Wave 2: task 2 (workflow + status contract)
Wave 3: task 3 (JRI operational rules + stale-reference replacement)
Wave 4: task 4 (new rules + compression pass)
Wave 5: task 5 (automated verification + scoped commit)

### Dependency Matrix (full, all tasks)
- 1 blocks 2, 3, 4, 5
- 2 blocks 3, 4, 5
- 3 blocks 4, 5
- 4 blocks 5
- 5 blocks Final Verification Wave

### Agent Dispatch Summary (wave → task count → categories)
- Wave 1 → 1 task → writing
- Wave 2 → 1 task → writing
- Wave 3 → 1 task → writing
- Wave 4 → 1 task → writing
- Wave 5 → 1 task → quick

## TODOs
> Implementation + Test = ONE task. Never separate.
> EVERY task MUST have: Agent Profile + Parallelization + QA Scenarios.

- [ ] 1. Replace the flat prompt with an XML scaffold

  **What to do**: Rewrite `src/app/prompts/ralph.py` from the current flat rule list into a single XML-structured `RALPH_SYSTEM_PROMPT` string. Establish the top-level envelope and section layout first: identity, goal, workflow, reminders, and guides. Match the repo’s structured-prompt style rather than inventing a new formatting convention.
  **Must NOT do**: Do not add business logic outside the prompt text. Do not keep the old flat bullet list alongside the XML. Do not edit `src/app/ralph_loop.py` or any tests.

  **Recommended Agent Profile**:
  - Category: `writing` — Reason: single-file prompt rewrite with strict wording/structure constraints
  - Skills: `[]` — no extra skill is required
  - Omitted: `oracle`, `deep` — unnecessary for a bounded single-file rewrite

  **Parallelization**: Can Parallel: NO | Wave 1 | Blocks: 2, 3, 4, 5 | Blocked By: none

  **References** (executor has NO interview context — be exhaustive):
  - Current prompt: `src/app/prompts/ralph.py:1-35` — existing flat prompt and JRI-specific rules that must not be lost during restructuring
  - XML prompt style precedent: `src/app/prompts/ralphy.py:1-6` — top-level XML prompt envelope style already used in-repo
  - XML workflow style precedent: `src/app/prompts/ralphy.py:44-54` — concise tagged workflow step formatting
  - XML rules style precedent: `src/app/prompts/ralphy.py:128-133` — dense XML rule sections without markdown headings
  - Runtime injection path: `src/app/ralph_loop.py:17-18` — confirms this file is imported as the source of truth
  - Runtime injection path: `src/app/ralph_loop.py:243-273` — confirms the prompt is injected as a plain string via JSON config, so XML is safe
  - Product spec: `.jri/tasks/draft/improve-ralph-prompt.md:96-113` — XML format target and acceptance constraints

  **Acceptance Criteria** (agent-executable only):
  - [ ] `src/app/prompts/ralph.py` contains a single XML-structured `RALPH_SYSTEM_PROMPT` string with top-level sections for identity, goal, workflow, reminders, and guides
  - [ ] No markdown heading-style structure from the old prompt remains in the final prompt body
  - [ ] `PYTHONPATH=src uv run python - <<'PY'` import of `app.prompts.ralph` succeeds after the rewrite

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: XML scaffold imports successfully
    Tool: Bash
    Steps: Run a Python one-liner/heredoc that imports `RALPH_SYSTEM_PROMPT`, prints its first line, and asserts XML opening/closing tags exist for the top-level structure.
    Expected: Import succeeds with exit code 0 and assertions pass.
    Evidence: .sisyphus/evidence/task-1-xml-scaffold.txt

  Scenario: Legacy flat structure removed
    Tool: Bash
    Steps: Run a Python check that reads `src/app/prompts/ralph.py` and fails if the old top-level `Rules:` marker still exists inside the prompt body.
    Expected: Check exits 0 because the old flat prompt structure is gone.
    Evidence: .sisyphus/evidence/task-1-xml-scaffold-error.txt
  ```

  **Commit**: NO | Message: `n/a` | Files: `src/app/prompts/ralph.py`

- [ ] 2. Rebuild the workflow around Phase 0 and preserve exact status signals

  **What to do**: Rework the prompt body so it keeps the 7 workflow steps from `PROMPT.xml`, but adds a new Phase 0 Intent Gate before them. Preserve the loop-controller status signals exactly and carry forward the strongest TDD and workflow guidance from the XML base in a shorter JRI-specific form.
  **Must NOT do**: Do not rename or paraphrase the three status signals. Do not drop the 7-step workflow. Do not turn the new Phase 0 into a vague note buried in reminders.

  **Recommended Agent Profile**:
  - Category: `writing` — Reason: wording-sensitive workflow rewrite with exact-string preservation
  - Skills: `[]` — no extra skill is required
  - Omitted: `quick` — too many interlocking wording constraints for a trivial pass

  **Parallelization**: Can Parallel: NO | Wave 2 | Blocks: 3, 4, 5 | Blocked By: 1

  **References** (executor has NO interview context — be exhaustive):
  - Base workflow source: `origin/attempt-2-(aleph-hackathon):pkgs/ralph/PROMPT.xml` — use as the structural base for Identity, Goal, Workflow, Reminders, and Guides
  - Draft keep-list: `.jri/tasks/draft/improve-ralph-prompt.md:13-24` — required PROMPT.xml concepts to preserve
  - Draft Intent Gate requirement: `.jri/tasks/draft/improve-ralph-prompt.md:27-35` — Phase 0 classification requirements
  - Draft acceptance criteria: `.jri/tasks/draft/improve-ralph-prompt.md:100-109` — must preserve all 7 workflow steps plus the new additions
  - Current prompt TDD baseline: `src/app/prompts/ralph.py:7-13` — minimum TDD/verification expectation that must remain semantically preserved

  **Acceptance Criteria** (agent-executable only):
  - [ ] The final prompt contains an explicit Phase 0 Intent Gate before the 7-step workflow
  - [ ] The final prompt still contains seven downstream workflow steps after Phase 0
  - [ ] The exact strings `COMPLETED ASSIGNED ISSUE`, `HUMAN HELP ABSOLUTELY NEEDED`, and `FOUND NEW BLOCKER ISSUE` all appear in the final prompt
  - [ ] The workflow still instructs Ralph to solve only the assigned issue and to use TDD rigorously

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Workflow and status contract survive the rewrite
    Tool: Bash
    Steps: Run a Python heredoc that imports `RALPH_SYSTEM_PROMPT`, counts the workflow step markers, asserts a Phase 0 marker exists before Step 1, and asserts the three exact status strings are present.
    Expected: All assertions pass and exit code is 0.
    Evidence: .sisyphus/evidence/task-2-workflow-status.txt

  Scenario: Status strings are not accidentally paraphrased
    Tool: Bash
    Steps: Run a Python heredoc that fails if near-miss variants such as `COMPLETED ISSUE`, `HUMAN HELP NEEDED`, or `FOUND BLOCKER` appear without the exact required strings.
    Expected: Check exits 0 because only the exact required controller strings remain authoritative.
    Evidence: .sisyphus/evidence/task-2-workflow-status-error.txt
  ```

  **Commit**: NO | Message: `n/a` | Files: `src/app/prompts/ralph.py`

- [ ] 3. Replace obsolete task-system guidance with JRI-specific operational rules

  **What to do**: Remove stale project/task-system guidance inherited from `PROMPT.xml` and replace it with the current JRI reality: `.jri/uploads/`, `.jri/tasks/{status}/{slug}.md`, README update requirements, explicit task closing/blocker creation behavior, and the non-interactive flag rule. Preserve web verification/deploy guidance from the current Ralph prompt.
  **Must NOT do**: Do not leave any references to `.ralphy/`, `tasks.yaml`, `ralph task`, or `AGENTS.md`. Do not weaken the `.jri/tasks/...` file-based task rules into generic “use the task system” language.

  **Recommended Agent Profile**:
  - Category: `writing` — Reason: precise replacement of stale operational text with current repo-specific instructions
  - Skills: `[]` — no extra skill is required
  - Omitted: `deep` — repository facts are already known; execution is straightforward text surgery

  **Parallelization**: Can Parallel: NO | Wave 3 | Blocks: 4, 5 | Blocked By: 1, 2

  **References** (executor has NO interview context — be exhaustive):
  - Current JRI rules to preserve: `src/app/prompts/ralph.py:9-35` — uploads path, docs expectations, non-interactive flags, web verification, and `.jri/tasks/...` management
  - Task-path reality: `src/app/prompts/ralphy.py:4-6` — repo-wide `.jri/tasks/` convention
  - Draft keep-list from current prompt: `.jri/tasks/draft/improve-ralph-prompt.md:90-95` — items that must survive from `ralph.py`
  - Draft exclusions: `.jri/tasks/draft/improve-ralph-prompt.md:81-89` — old harness/tooling patterns that must not be imported

  **Acceptance Criteria** (agent-executable only):
  - [ ] The final prompt contains `.jri/uploads/` and `.jri/tasks/{status}/{slug}.md` guidance in concrete file-path form
  - [ ] The final prompt contains the explicit non-interactive flags guidance including `cp -f`, `mv -f`, `rm -f`, and `apt-get -y`
  - [ ] The final prompt retains web verification/deploy guidance in the spirit of the current Ralph prompt
  - [ ] The final prompt contains none of the forbidden stale strings: `.ralphy/`, `tasks.yaml`, `ralph task`, `AGENTS.md`

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: JRI operational rules are preserved
    Tool: Bash
    Steps: Run a Python heredoc that imports `RALPH_SYSTEM_PROMPT` and asserts presence of `.jri/uploads/`, `.jri/tasks/`, `cp -f`, `mv -f`, `rm -f`, `apt-get -y`, and web verification wording referencing local start/route checks/deploy behavior.
    Expected: All required JRI-specific strings are present.
    Evidence: .sisyphus/evidence/task-3-jri-rules.txt

  Scenario: Obsolete PROMPT.xml task-system references are purged
    Tool: Bash
    Steps: Run a Python heredoc that fails if `.ralphy/`, `tasks.yaml`, `ralph task`, or `AGENTS.md` appear anywhere in `RALPH_SYSTEM_PROMPT`.
    Expected: Check exits 0 because all stale references were removed.
    Evidence: .sisyphus/evidence/task-3-jri-rules-error.txt
  ```

  **Commit**: NO | Message: `n/a` | Files: `src/app/prompts/ralph.py`

- [ ] 4. Add the new autonomy rules and compress the prompt to the target footprint

  **What to do**: Add the draft’s missing guidance: “Never ask, just do” with explicit blocker/human-help exceptions, mandatory 2-5 parallel exploration-agent usage in Analysis, stricter pre-merge verification, failure recovery protocol, and lightweight progress signals. Then compress the prompt to the target 150-200 lines without cutting required JRI rules or exact status strings.
  **Must NOT do**: Do not let the line-budget goal remove required behavior. Do not create contradictions between “never ask” and blocker escalation. Do not add repetitive “NON-NEGOTIABLE” style hammering or OMO/GPT-specific instructions.

  **Recommended Agent Profile**:
  - Category: `writing` — Reason: balancing clarity, compression, and non-contradictory instruction design in one file
  - Skills: `[]` — no extra skill is required
  - Omitted: `artistry` — this is precision editing, not creative divergence

  **Parallelization**: Can Parallel: NO | Wave 4 | Blocks: 5 | Blocked By: 1, 2, 3

  **References** (executor has NO interview context — be exhaustive):
  - Draft “Never ask” rule: `.jri/tasks/draft/improve-ralph-prompt.md:37-45` — required behavior and anti-patterns
  - Draft exploration mandate: `.jri/tasks/draft/improve-ralph-prompt.md:46-54` — mandatory parallel exploration in Analysis
  - Draft stricter verification: `.jri/tasks/draft/improve-ralph-prompt.md:55-61` — no evidence = not complete, re-read the task before declaring done
  - Draft failure recovery: `.jri/tasks/draft/improve-ralph-prompt.md:62-69` — three-step recovery path and anti-shotgun-debugging rule
  - Draft progress signals: `.jri/tasks/draft/improve-ralph-prompt.md:71-79` — lightweight milestone reporting requirements
  - Draft exclusions and line-budget target: `.jri/tasks/draft/improve-ralph-prompt.md:81-89` and `.jri/tasks/draft/improve-ralph-prompt.md:100-113` — what to avoid and final prompt size target

  **Acceptance Criteria** (agent-executable only):
  - [ ] The final prompt contains explicit “never ask” language plus explicit exceptions for blocker/human-help escalation
  - [ ] The final prompt mandates 2-5 parallel exploration agents during Analysis before Design proceeds
  - [ ] The final prompt contains a structured failure-recovery protocol and progress-signal guidance
  - [ ] The final prompt line count is between 150 and 200 lines inclusive
  - [ ] The final prompt contains no GPT-specific, OMO-specific, or redundant repetitive wording

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: New autonomy rules are present and internally consistent
    Tool: Bash
    Steps: Run a Python heredoc that imports `RALPH_SYSTEM_PROMPT` and asserts presence of the never-ask rule, blocker/human-help exception language, 2-5 parallel exploration guidance, failure-recovery language, and progress-signal language.
    Expected: All assertions pass and no contradictory omission is detected.
    Evidence: .sisyphus/evidence/task-4-autonomy-rules.txt

  Scenario: Prompt stays within the agreed footprint and avoids banned framing
    Tool: Bash
    Steps: Run a Python heredoc that counts lines in `RALPH_SYSTEM_PROMPT` and fails if the count is outside 150-200 or if banned strings like `NON-NEGOTIABLE`, `TodoWrite`, `background_cancel`, or `OMO` appear.
    Expected: Check exits 0 because the prompt is within budget and free of banned framing.
    Evidence: .sisyphus/evidence/task-4-autonomy-rules-error.txt
  ```

  **Commit**: NO | Message: `n/a` | Files: `src/app/prompts/ralph.py`

- [ ] 5. Run automated prompt verification and create the scoped commit

  **What to do**: Run the full prompt verification pass after the rewrite is complete: import safety, JSON serialization safety, required/forbidden string assertions, line-budget assertion, existing smoke tests, and diff-scope check. Only after all checks pass, create one conventional commit for the prompt rewrite.
  **Must NOT do**: Do not create a commit before the verification commands pass. Do not broaden the diff to other files. Do not skip the diff-scope check.

  **Recommended Agent Profile**:
  - Category: `quick` — Reason: bounded verification and commit flow once the prompt text is finalized
  - Skills: `[]` — no extra skill is required
  - Omitted: `writing` — this task is validation-first rather than drafting-heavy

  **Parallelization**: Can Parallel: NO | Wave 5 | Blocks: Final Verification Wave | Blocked By: 1, 2, 3, 4

  **References** (executor has NO interview context — be exhaustive):
  - Prompt source of truth: `src/app/prompts/ralph.py:1-35` — file to rewrite and verify
  - Import site: `src/app/ralph_loop.py:17-18` — proves the prompt must remain importable
  - JSON injection site: `src/app/ralph_loop.py:250-273` — proves serialization safety matters
  - Existing smoke test target: `tests/test_integration.py` — current endpoint-level regression check available without adding new tests
  - Commit style guidance: `CLAUDE.md` — use conventional commits, mostly lowercase, short subject

  **Acceptance Criteria** (agent-executable only):
  - [ ] A prompt-verification script proves required tags/phrases are present and forbidden stale strings are absent
  - [ ] A JSON serialization smoke check passes using the final prompt string
  - [ ] `uv run pytest tests/test_integration.py -q` passes
  - [ ] `git diff --name-only` reports only `src/app/prompts/ralph.py`
  - [ ] A single conventional commit is created only after all checks pass

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Full prompt verification passes end-to-end
    Tool: Bash
    Steps: Run one scripted verification pass that imports `RALPH_SYSTEM_PROMPT`, checks required tags/phrases, checks forbidden strings, checks line count, builds a dict with `{"agent": {"ralph": {"prompt": RALPH_SYSTEM_PROMPT}}}`, calls `json.dumps(...)`, and records all results.
    Expected: Script exits 0 and produces reproducible evidence of prompt validity.
    Evidence: .sisyphus/evidence/task-5-prompt-verification.txt

  Scenario: Scope guard blocks accidental extra-file edits
    Tool: Bash
    Steps: Run `git diff --name-only` after verification and fail if any path other than `src/app/prompts/ralph.py` appears; then run `uv run pytest tests/test_integration.py -q`.
    Expected: Only the prompt file is changed and the smoke test passes.
    Evidence: .sisyphus/evidence/task-5-prompt-verification-error.txt
  ```

  **Commit**: YES | Message: `feat: rewrite ralph prompt xml` | Files: `src/app/prompts/ralph.py`

## Final Verification Wave (MANDATORY — after ALL implementation tasks)
> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
> **Do NOT auto-proceed after verification. Wait for user's explicit approval before marking work complete.**
> **Never mark F1-F4 as checked before getting user's okay.** Rejection or user feedback -> fix -> re-run -> present again -> wait for okay.
- [ ] F1. Plan Compliance Audit — oracle
- [ ] F2. Code Quality Review — unspecified-high
- [ ] F3. Real Manual QA — unspecified-high (+ playwright if UI)
- [ ] F4. Scope Fidelity Check — deep

## Commit Strategy
- Single conventional commit after task 5 verification passes.
- Recommended message: `feat: rewrite ralph prompt xml`
- Do not commit if any forbidden-string, line-budget, or serialization check fails.

## Success Criteria
- Ralph prompt is XML-structured, JRI-accurate, and materially more autonomous than the current flat prompt.
- Required legacy behavior is preserved where the runtime depends on exact strings or exact paths.
- Obsolete PROMPT.xml content is removed rather than translated.
- Verification is automated and reproducible without human interpretation.
