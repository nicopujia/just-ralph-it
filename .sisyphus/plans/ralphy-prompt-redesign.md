# Redesign Ralphy Prompt Defaults for Progressive Product Planning

## TL;DR
> **Summary**: Redesign `src/app/prompts/ralphy.py` so Ralphy starts at product + system planning, progressively goes deeper over the conversation in a product-specific way, extracts all client-facing behavior completely, avoids unnecessary implementation internals unless the user opts in, and keeps draft-task capture early while reserving strict coherence checks for `todo` promotion.
> **Deliverables**:
> - rewritten `RALPHY_SYSTEM_PROMPT` policy sections in `src/app/prompts/ralphy.py`
> - rewritten prompt policy covering progressive product-depth, trade-off guidance, and draft/promotion policy
> - manual chat-based verification checklist proving implementation-first bias is reduced without weakening client-facing clarity
> **Effort**: Medium
> **Parallel**: NO
> **Critical Path**: 1 → 2 → 3 → 4 → 5

## Context
### Original Request
Help with prompt engineering for `src/app/prompts/ralphy.py`, specifically making Ralphy less implementation-deep by default and more oriented toward system design. File structures and similar internals should not be pushed unless the user raises them.

### Interview Summary
- Default interview altitude should be a **product + system** blend, not implementation-ready detail.
- Ralphy should go into implementation details only on explicit user cue, or when a choice is truly blocking externally visible behavior.
- Ralphy should explicitly tell users it can stay high-level unless they want implementation detail.
- Task artifacts should be **outcomes/contracts first**; internal structure is opt-in.
- Ralphy should extract **all client-facing behavior and expectations** completely to avoid mismatched expectations.
- Draft tasks should still be created **early**, as soon as the user provides meaningful new information; `draft/` is a live capture space.
- Promotion to `todo` must require whole-task-set coherence and expectation safety.
- Verification strategy: **manual chat review**, not automated tests.
- Change scope: **major redesign** of the prompt file, not just wording tweaks.
- Ralphy should get lower level as the conversation progresses, but in a **product sense**: more specific screens, views, states, copy, and behavior rather than internal architecture.
- Ralphy should help the user decide by presenting trade-offs between alternatives, while leaving the decision to the user unless they explicitly say they do not care.

### Metis Review (gaps addressed)
- Locked the default-depth contract: high-level by default, deeper only on explicit implementation-seeking wording.
- Added a guardrail to keep scope on `src/app/prompts/ralphy.py` plus verification, not general prompt-framework cleanup.
- Added explicit acceptance criteria for progressive product-depth, stack-discussion permission, early draft creation, trade-off framing, and promotion coherence.
- Shifted verification from automated coverage to manual chat-based review scenarios.
- Kept `src/app/prompts/ralph.py` and runtime refactors out of scope except as compatibility watchpoints.

## Work Objectives
### Core Objective
Rewrite Ralphy’s prompt policy so it behaves like a high-leverage planner: start high-level, then progressively deepen into user-visible product detail as understanding improves, fully extract externally visible behavior and success criteria, surface trade-offs clearly, and treat internal implementation decisions as opt-in rather than default.

### Deliverables
- Updated `src/app/prompts/ralphy.py` with redesigned sections for framing, questioning, workflow, interviewing rules, task quality, ambiguity handling, promotion rules, and examples.
- Manual verification checklist covering depth progression, trade-off framing, and draft/promotion behavior.
- Evidence artifacts under `.sisyphus/evidence/` for each manual verification run.

### Definition of Done (verifiable conditions)
- `src/app/prompts/ralphy.py` explicitly states that Ralphy starts high-level and goes deeper over time in a product-specific way.
- `src/app/prompts/ralphy.py` explicitly states that Ralphy should discuss trade-offs of alternatives and let the user decide unless the user says they do not care.
- `src/app/prompts/ralphy.py` explicitly distinguishes product-detail depth from implementation-internal depth.
- Manual review scenarios for opening chat turns, mid-conversation deepening, and alternative/trade-off handling are documented in this plan.

### Must Have
- High-level-first planning stance stated explicitly in the prompt.
- Progressive product-depth stance stated explicitly in the prompt.
- Explicit depth-escalation rule: implementation detail only on user cue or true blocking need.
- Explicit trade-off rule: present alternatives and their trade-offs, but defer the decision to the user unless they explicitly delegate it.
- Clear distinction between early draft capture and stricter `todo` promotion readiness.
- Strong extraction of client-facing behavior, edge cases, and expectation-setting.
- No weakening of Ralphy’s no-code/planner-only identity.
- Manual review criteria that validate prompt behavior without relying on implementation tests.

### Must NOT Have (guardrails, AI slop patterns, scope boundaries)
- No refactor of prompt architecture beyond what is needed inside `src/app/prompts/ralphy.py`.
- No changes to `src/app/prompts/ralph.py`.
- No runtime/tooling refactor in `src/app/routers/chat.py` unless required to keep prompt references/tests correct.
- No generic “make it better” language; every prompt change must map to an explicit policy contract.
- No automated test work in this scope.

## Verification Strategy
> ZERO HUMAN INTERVENTION — all verification is agent-executed.
- Test decision: no automated tests in scope; manual chat/prompt review only
- QA policy: Every task has manual chat-review scenarios
- Evidence: `.sisyphus/evidence/task-{N}-{slug}.{ext}`
- Verification style: review prompt text directly and validate it against manual conversation scenarios; avoid exact-output promises for live model prose.

## Execution Strategy
### Parallel Execution Waves
> Target: 5-8 tasks per wave. <3 per wave (except final) = under-splitting.
> Because the redesign is concentrated in one prompt file, execution is primarily sequential.

Wave 1: prompt policy redesign tasks (1-3)
Wave 2: manual verification and refinement tasks (4-5)

### Dependency Matrix (full, all tasks)
| Task | Depends On |
|---|---|
| 1 | — |
| 2 | 1 |
| 3 | 2 |
| 4 | 3 |
| 5 | 4 |
| F1 | 5 |
| F2 | 5 |
| F3 | 5 |
| F4 | 5 |

### Agent Dispatch Summary (wave → task count → categories)
| Wave | Task Count | Categories |
|---|---:|---|
| 1 | 3 | deep, unspecified-high |
| 2 | 2 | unspecified-high, deep |
| Final | 4 | oracle, unspecified-high, deep |

## TODOs
> Implementation + Verification = ONE task. Never separate.
> EVERY task MUST have: Agent Profile + Parallelization + QA Scenarios.

- [ ] 1. Redesign Ralphy framing for high-level-first planning

  **What to do**: Rewrite the opening and guidance sections in `src/app/prompts/ralphy.py` so Ralphy explicitly defaults to product + system planning, tells users it can stay high-level unless they want implementation detail, and makes it clear that depth should increase over time in a product-specific way. Update the `You are Ralphy...` description, `<personality>`, `<core_traits>`, `<questioning_strategy>`, and the bad/good examples in lines 2-42. Add explicit language that Ralphy should deepen into screens, views, states, copy, and interaction behavior as the conversation matures, but should not proactively choose frameworks, file structures, module layouts, or internal architecture unless the user asks for that depth or it is necessary to resolve an externally visible behavior.
  **Must NOT do**: Do not remove the no-code/planner-only identity. Do not reduce curiosity about client-facing behavior, edge cases, or success criteria. Do not introduce runtime/tooling changes outside `src/app/prompts/ralphy.py`.

  **Recommended Agent Profile**:
  - Category: `deep` — Reason: this task changes the prompt’s governing stance, examples, and escalation model.
  - Skills: [`git-master`] — safe atomic commit with conventional-commit formatting.
  - Omitted: [] — no additional specialized skill is required.

  **Parallelization**: Can Parallel: NO | Wave 1 | Blocks: [2, 3, 4, 5] | Blocked By: []

  **References** (executor has NO interview context — be exhaustive):
  - Pattern: `src/app/prompts/ralphy.py:2-42` — current identity, personality, questioning strategy, and examples being redesigned.
  - Pattern: `src/app/prompts/ralphy.py:128-145` — preserve planner-only/no-code constraints while changing the interview stance.
  - Pattern: `src/app/prompts/ralph.py` — sibling prompt shows the repo convention of a module-level constant string; do not change this structure.
  - API/Type: `src/app/routers/chat.py:16,155-181` — runtime imports and injects `RALPHY_SYSTEM_PROMPT`; keep the constant name and import path stable.

  **Acceptance Criteria** (agent-executable only):
  - [ ] `src/app/prompts/ralphy.py` includes an explicit statement that Ralphy defaults to product + system planning and can stay high-level unless the user asks for implementation details.
  - [ ] `src/app/prompts/ralphy.py` includes an explicit statement that Ralphy should get more specific over time in a product sense, such as views, states, and behavior.
  - [ ] `src/app/prompts/ralphy.py` includes an explicit prohibition on proactively choosing frameworks, file structures, or internal architecture without user opt-in or a truly blocking external-behavior reason.
  - [ ] The good/bad examples now demonstrate the new altitude policy instead of jumping directly to implementation decomposition.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Opening stance now describes progressive product depth
    Tool: Read
    Steps: Read the rewritten opening/personality/questioning sections in `src/app/prompts/ralphy.py`.
    Expected: The text says Ralphy starts high-level, can stay high-level unless asked otherwise, and later goes deeper into product behavior rather than implementation internals.
    Evidence: .sisyphus/evidence/task-1-high-level-framing.txt

  Scenario: Examples teach product-specific deepening, not internal decomposition
    Tool: Read
    Steps: Read the updated good/bad examples in `src/app/prompts/ralphy.py`.
    Expected: The examples show deeper questioning about what the user sees and how it behaves, not module/file/build decomposition.
    Evidence: .sisyphus/evidence/task-1-high-level-framing-error.txt
  ```

  **Commit**: YES | Message: `refactor(ralphy): reset high-level interview stance` | Files: [`src/app/prompts/ralphy.py`]

- [ ] 2. Separate discovery depth from task-capture timing

  **What to do**: Rewrite the workflow and interviewing rules in `src/app/prompts/ralphy.py` so early conversation flow prioritizes problem, actors, outcomes, flows, constraints, and system shape before implementation specifics, then progressively deepens into more specific product detail as understanding improves. Update `<workflow>` and `<interviewing_rules>` to say: (a) tech-stack discussion requires permission, (b) draft tasks may be created as soon as meaningful new information appears, even if incomplete, (c) README/task writing should track clarified slices incrementally, and (d) labels/validation/view behavior become appropriate as the conversation matures and as long as they remain product-facing rather than implementation-facing.
  **Must NOT do**: Do not remove early draft-task creation. Do not force README/task creation to wait until the entire project is fully known. Do not preserve rules that make low-level copy, validation, or structure the default first-pass questioning style.

  **Recommended Agent Profile**:
  - Category: `deep` — Reason: this task changes the prompt’s conversation flow and when artifacts are created.
  - Skills: [`git-master`] — safe atomic commit with conventional-commit formatting.
  - Omitted: [] — no additional specialized skill is required.

  **Parallelization**: Can Parallel: NO | Wave 1 | Blocks: [3, 4, 5] | Blocked By: [1]

  **References** (executor has NO interview context — be exhaustive):
  - Pattern: `src/app/prompts/ralphy.py:44-54` — workflow currently drives early implementation-oriented tasking and stack decisions.
  - Pattern: `src/app/prompts/ralphy.py:135-146` — interviewing rules currently bias toward low-level probing; redesign these rules instead of only tweaking examples.
  - Pattern: `src/app/prompts/ralphy.py:171-203` — task-file format/commands stay valid; redesign policy around when and why tasks are written, not the file format itself.
  - External: `README.md` — current repo architecture confirms Ralphy is the interviewer/planner and Ralph is the builder; preserve that division.

  **Acceptance Criteria** (agent-executable only):
  - [ ] `<workflow>` and `<interviewing_rules>` explicitly require permission before stack/framework discussion.
  - [ ] `<workflow>` and/or `<interviewing_rules>` explicitly allow draft-task creation as soon as meaningful new information arrives, even before full clarity.
  - [ ] The rewritten rules state that client-facing behavior, outcomes, actors, flows, and constraints come before internal implementation detail.
  - [ ] The rewritten rules explicitly allow increasing specificity about views, states, copy, and behavior over time without collapsing into technical internals.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Workflow supports progressive product-specific deepening
    Tool: Read
    Steps: Read `<workflow>` and `<interviewing_rules>` in `src/app/prompts/ralphy.py`.
    Expected: The rules move from high-level framing toward more detailed product behavior over time, while keeping stack/internal design opt-in.
    Evidence: .sisyphus/evidence/task-2-discovery-vs-capture.txt

  Scenario: Early drafting still happens before the conversation is complete
    Tool: Read
    Steps: Read the workflow rules governing draft task creation.
    Expected: Drafts can be created as information arrives; only `todo` promotion requires full coherence.
    Evidence: .sisyphus/evidence/task-2-discovery-vs-capture-error.txt
  ```

  **Commit**: YES | Message: `refactor(ralphy): split discovery from task capture` | Files: [`src/app/prompts/ralphy.py`]

- [ ] 3. Rebuild promotion and ambiguity rules around expectation safety

  **What to do**: Rewrite `<task_promotion>`, `<definition_of_ambiguous>`, `<task_quality_rules>`, and `<dependency_rules>` so they enforce complete extraction of externally visible behavior and whole-task-set coherence before promotion, without demanding opt-in internals up front. Replace the current setup-task-first/directory-structure-first bias with a rule that foundational tasks should be inferred by Ralphy during drafting, not pushed onto the user as an implementation interview topic unless the user asks for that depth. Define ambiguity primarily around user-visible behavior, contracts, failure modes, and contradictory expectations; treat hidden implementation structure as optional unless it affects those outcomes.
  **Must NOT do**: Do not weaken the `draft -> todo` promotion gate. Do not remove dependency thinking altogether. Do not leave a path where Ralph would have to guess user-visible behavior at `todo` time.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` — Reason: this is a policy-heavy rewrite with careful guardrails but limited external integration.
  - Skills: [`git-master`] — safe atomic commit with conventional-commit formatting.
  - Omitted: [] — no additional specialized skill is required.

  **Parallelization**: Can Parallel: NO | Wave 1 | Blocks: [4, 5] | Blocked By: [2]

  **References** (executor has NO interview context — be exhaustive):
  - Pattern: `src/app/prompts/ralphy.py:56-87` — current task-promotion process and ambiguity handling to be preserved structurally but rewritten semantically.
  - Pattern: `src/app/prompts/ralphy.py:95-97` — current definition of ambiguity is broad; narrow it to externally relevant ambiguity first.
  - Pattern: `src/app/prompts/ralphy.py:148-161` — current task-quality and dependency rules encode implementation-first pressure, including setup/build-system/directory-structure-first requirements.
  - Pattern: `tests/test_e2e_happy_paths.py:220` and `tests/test_integration.py:801` — existing coverage does not protect this behavior; the prompt policy must become explicitly testable.

  **Acceptance Criteria** (agent-executable only):
  - [ ] The prompt explicitly states that draft tasks may be partial/live, but promotion to `todo` requires a coherent, expectation-safe task set.
  - [ ] The definition of ambiguity explicitly prioritizes unresolved client-facing behavior, failure states, and contradictory expectations over hidden internals.
  - [ ] The exact sentence `Every project must have a foundation/setup task (project skeleton, build system, dependencies, directory structure). Create this task first.` is removed.
  - [ ] The promotion rules do not require hidden implementation detail unless it is needed to remove ambiguity about user-visible behavior.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Promotion gate emphasizes expectation safety
    Tool: Read
    Steps: Read `<task_promotion>`, `<definition_of_ambiguous>`, and related task-quality sections.
    Expected: Promotion is blocked by unresolved client-facing ambiguity or incoherent expectations, not by unspecified internal structure alone.
    Evidence: .sisyphus/evidence/task-3-promotion-policy.txt

  Scenario: Old setup-task-first sentence removed
    Tool: Read
    Steps: Inspect the dependency rules in `src/app/prompts/ralphy.py`.
    Expected: The old setup-task-first sentence is absent, and dependency guidance no longer forces implementation-first interrogation.
    Evidence: .sisyphus/evidence/task-3-promotion-policy-error.txt
  ```

  **Commit**: YES | Message: `refactor(ralphy): tighten promotion contracts` | Files: [`src/app/prompts/ralphy.py`]

- [ ] 4. Add explicit trade-off guidance to the conversation model

  **What to do**: Rewrite the relevant guidance in `src/app/prompts/ralphy.py` so Ralphy actively helps the user decide when multiple plausible product directions exist. Add explicit instruction that Ralphy should present trade-offs of alternatives clearly, recommend only when the user asks or says they do not care, and otherwise leave the decision to the user. Update examples and questioning guidance so this behavior shows up in the conversation style.
  **Must NOT do**: Do not turn Ralphy into a decisive product owner who silently picks options. Do not remove its ability to push back or explain trade-offs.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` — Reason: this is a policy/style change touching multiple prompt sections and examples.
  - Skills: [`git-master`] — safe atomic commit with conventional-commit formatting.
  - Omitted: [] — no additional specialized skill is required.

  **Parallelization**: Can Parallel: NO | Wave 2 | Blocks: [5, F1, F2, F3, F4] | Blocked By: [3]

  **References** (executor has NO interview context — be exhaustive):
  - Pattern: `src/app/prompts/ralphy.py:8-25` — personality and questioning guidance should instruct trade-off framing.
  - Pattern: `src/app/prompts/ralphy.py:27-41` — examples should demonstrate Ralphy surfacing alternatives without making the decision for the user.
  - Pattern: `src/app/prompts/ralphy.py:163-169` — existing communication rules can be tightened so decisions are explained briefly but not imposed.

  **Acceptance Criteria** (agent-executable only):
  - [ ] `src/app/prompts/ralphy.py` explicitly instructs Ralphy to present trade-offs between alternatives when helping the user decide.
  - [ ] `src/app/prompts/ralphy.py` explicitly says the decision stays with the user unless they say they do not care or explicitly delegate it.
  - [ ] At least one example demonstrates trade-off framing without silent decision-making.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Prompt now teaches trade-off framing
    Tool: Read
    Steps: Read the updated personality/questioning/example sections.
    Expected: The prompt explicitly tells Ralphy to explain trade-offs and keep the final choice with the user by default.
    Evidence: .sisyphus/evidence/task-4-tradeoff-guidance.txt

  Scenario: User retains decision authority
    Tool: Read
    Steps: Inspect the updated decision/trade-off language.
    Expected: The prompt says Ralphy should not decide for the user unless the user explicitly says they do not care or asks Ralphy to choose.
    Evidence: .sisyphus/evidence/task-4-tradeoff-guidance-error.txt
  ```

  **Commit**: YES | Message: `refactor(ralphy): add tradeoff decision guidance` | Files: [`src/app/prompts/ralphy.py`]

- [ ] 5. Perform manual conversation review against the redesigned prompt

  **What to do**: Create a concise manual review checklist in the plan itself and use it to inspect the final `src/app/prompts/ralphy.py`. The checklist must cover: opening turns stay high-level, mid-conversation turns deepen into product detail, implementation internals remain opt-in, trade-offs are surfaced without hijacking the decision, early drafts are allowed, and `todo` promotion remains strict about expectation safety. Record evidence paths for each review item.
  **Must NOT do**: Do not add automated tests. Do not claim live-model behavior is guaranteed by prompt text alone; this is a manual readiness review.

  **Recommended Agent Profile**:
  - Category: `deep` — Reason: this task synthesizes the final manual-review rubric across all prompt changes.
  - Skills: [`git-master`] — safe atomic commit with conventional-commit formatting.
  - Omitted: [] — no additional specialized skill is required.

  **Parallelization**: Can Parallel: NO | Wave 2 | Blocks: [F1, F2, F3, F4] | Blocked By: [4]

  **References** (executor has NO interview context — be exhaustive):
  - Pattern: `src/app/prompts/ralphy.py` — final review target for the redesigned conversation model.
  - Pattern: `.sisyphus/plans/ralphy-prompt-redesign.md` — use this plan’s stated behavior contracts as the review rubric.
  - Pattern: `README.md` — maintain the planner/builder distinction while reviewing final prompt text.

  **Acceptance Criteria** (agent-executable only):
  - [ ] The plan contains a manual review checklist covering opening altitude, progressive product-depth, implementation opt-in, trade-off framing, early drafting, and strict promotion readiness.
  - [ ] The checklist points to evidence files for each manual review item.
  - [ ] The checklist is sufficient for a human to inspect whether the prompt reflects the intended conversation style.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Manual review checklist covers the full intended conversation arc
    Tool: Read
    Steps: Read the checklist added to this plan.
    Expected: It covers early high-level exploration, later product-specific deepening, trade-offs, and promotion readiness.
    Evidence: .sisyphus/evidence/task-5-manual-review.txt

  Scenario: Verification remains manual rather than automated
    Tool: Read
    Steps: Inspect the verification strategy and task list in this plan.
    Expected: No automated test tasks remain; verification is manual chat/prompt review.
    Evidence: .sisyphus/evidence/task-5-manual-review-error.txt
  ```

  **Commit**: YES | Message: `docs(plan): define ralphy manual review checklist` | Files: [`.sisyphus/plans/ralphy-prompt-redesign.md`]

## Manual Review Checklist
- [ ] Opening turns stay at problem, actors, outcomes, flows, and system shape before diving into specifics.
- [ ] The prompt explicitly allows staying high-level until the user wants more detail.
- [ ] The prompt explicitly says depth should increase over time in a product sense: screens, views, states, copy, interaction behavior, and edge cases.
- [ ] The prompt explicitly distinguishes product-depth from implementation-internal depth.
- [ ] The prompt says frameworks, file structures, and internal architecture are opt-in unless needed to resolve user-visible behavior.
- [ ] The prompt explicitly says Ralphy should present trade-offs between alternatives and leave the decision to the user by default.
- [ ] The prompt explicitly allows Ralphy to choose only when the user says they do not care or explicitly delegates the decision.
- [ ] Draft tasks can be created early as new information appears.
- [ ] `todo` promotion still requires a coherent, expectation-safe task set with no unresolved client-facing ambiguity.

### Manual Review Evidence
- `.sisyphus/evidence/task-1-high-level-framing.txt`
- `.sisyphus/evidence/task-2-discovery-vs-capture.txt`
- `.sisyphus/evidence/task-3-promotion-policy.txt`
- `.sisyphus/evidence/task-4-tradeoff-guidance.txt`
- `.sisyphus/evidence/task-5-manual-review.txt`

## Final Verification Wave (MANDATORY — after ALL implementation tasks)
> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
> **Do NOT auto-proceed after verification. Wait for user's explicit approval before marking work complete.**
> **Never mark F1-F4 as checked before getting user's okay.** Rejection or user feedback -> fix -> re-run -> present again -> wait for okay.
- [ ] F1. Plan Compliance Audit — oracle

  **What to do**: Review the finished implementation against this plan and verify that the rewritten `src/app/prompts/ralphy.py` actually matches the intended behavior contracts: high-level opening stance, progressive product-specific deepening, implementation-internal opt-in, early draft creation, strict `todo` promotion, and trade-off framing with user-owned decisions.
  **Must NOT do**: Do not rewrite code during this audit. Do not approve based on intent alone; compare the actual prompt text to the plan’s stated contracts.
  **Parallelization**: Can Parallel: YES | Final Wave | Blocked By: [5]
  **QA Scenario**:
  ```
  Scenario: Implementation matches plan contracts
    Tool: Read
    Steps: Read `src/app/prompts/ralphy.py` and this plan side by side.
    Expected: Every Must Have item and every item in the manual review checklist is reflected in the prompt text with no obvious contradiction.
    Evidence: .sisyphus/evidence/f1-plan-compliance.txt
  ```

- [ ] F2. Code Quality Review — unspecified-high

  **What to do**: Review the final `src/app/prompts/ralphy.py` for prompt clarity, internal consistency, duplication, contradictory rules, and maintainability. Confirm that the redesign did not create conflicting instructions about depth, task timing, or decision ownership.
  **Must NOT do**: Do not judge product direction; judge prompt quality and internal consistency only.
  **Parallelization**: Can Parallel: YES | Final Wave | Blocked By: [5]
  **QA Scenario**:
  ```
  Scenario: Prompt rules remain internally consistent
    Tool: Read
    Steps: Read the full `src/app/prompts/ralphy.py` from top to bottom.
    Expected: No section tells Ralphy to both stay high-level and immediately demand implementation detail; no section tells Ralphy to both leave the decision to the user and silently choose by default.
    Evidence: .sisyphus/evidence/f2-code-quality.txt
  ```

- [ ] F3. Real Manual QA — unspecified-high

  **What to do**: Run manual scenario review using the checklist in this plan and evaluate whether a human reader would expect the resulting chat to feel like the intended product-planning conversation. Cover opening-turn behavior, mid-conversation deepening, trade-off handling, and promotion readiness.
  **Must NOT do**: Do not claim model-output guarantees; this is a manual prompt-readiness review.
  **Parallelization**: Can Parallel: YES | Final Wave | Blocked By: [5]
  **QA Scenario**:
  ```
  Scenario: Manual conversation expectations are satisfied
    Tool: Read
    Steps: Read the manual review checklist in this plan, then inspect `src/app/prompts/ralphy.py` against each item.
    Expected: A reviewer can plausibly picture Ralphy starting broad, getting more product-specific over time, surfacing trade-offs, and avoiding implementation internals unless invited.
    Evidence: .sisyphus/evidence/f3-manual-qa.txt
  ```

- [ ] F4. Scope Fidelity Check — deep

  **What to do**: Verify that the implementation stayed within agreed scope: `src/app/prompts/ralphy.py` redesign plus manual verification guidance only. First create a concrete changed-files artifact from the five task commits, then review that artifact to confirm no unnecessary expansion into `src/app/prompts/ralph.py`, runtime refactors, or automated test work.
  **Must NOT do**: Do not approve if extra scope was added “helpfully.” Scope discipline is part of the acceptance bar.
  **Parallelization**: Can Parallel: YES | Final Wave | Blocked By: [5]
  **QA Scenario**:
  ```
  Scenario: No out-of-scope work was introduced
    Tool: Bash + Read
    Steps: Run `git diff --name-only HEAD~5 HEAD > .sisyphus/evidence/f4-changed-files.txt`, then read `.sisyphus/evidence/f4-changed-files.txt` and compare it to this plan’s scope boundaries.
    Expected: Changes are limited to `src/app/prompts/ralphy.py`, `.sisyphus/plans/ralphy-prompt-redesign.md`, and explicitly planned manual-review artifacts under `.sisyphus/evidence/`; no automated test files or unrelated prompt/runtime refactors were added.
    Evidence: .sisyphus/evidence/f4-scope-fidelity.txt
  ```

## Commit Strategy
- Commit after each task with a conventional-commit message.
- Keep prompt-policy rewrites and verification additions in separate commits.
- Do not batch runtime-adjacent cleanup into this work unless explicitly approved later.

## Success Criteria
- Ralphy no longer defaults to implementation-level probing when the user has not asked for it.
- Ralphy explicitly offers high-level planning as the default stance.
- Draft tasks can be created early without forcing `todo`-level completeness.
- `todo` promotion remains strict about client-facing expectation safety and whole-task coherence.
- Manual review catches regressions to implementation-first behavior or missing promotion-depth rules before the prompt is considered ready.
