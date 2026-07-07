# Interviewer Notes Spec

## Goal

Add structured project notes for JRI's interviewer-first flow so the interviewer can:

- keep durable project understanding,
- learn how much project detail the user wants to control,
- use that preference as ordinary interviewer judgment,
- capture open questions whenever a user answer reveals new unresolved branches,
- check existing notes or exploration before asking questions,
- isolate feature discussions from each other,
- safely change focus without carrying the full prior chat forever,
- preserve only surfaced truth so later agents do not invent unapproved architecture or stack choices.

The user-facing mental model must stay unchanged:

- the user only chats with the interviewer,
- the user does not manage files,
- the user does not manage context,
- the user does not have to think about switching topics,
- the user never needs to manage focus,
- generic tool progress may be visible while JRI works,
- JRI + the interviewer handle note-taking and topic changes automatically.

## Core Principles

1. The interviewer only knows two domains: exploration and notes.
2. The interviewer must never think in terms of file editing, patches, diffs, or paths.
3. Structured notes are the source of truth for project understanding.
4. Feature-level independence is required so feature A can be safely forgotten while discussing feature B.
5. Topic switching is a runtime-managed operation that rebuilds active context from structured notes.
6. The interviewer should learn how much detail the user wants to control and use that as soft guidance.
7. Missing product or technical detail is unresolved, not permission to invent.
8. Product and technical decisions belong to the user unless the user delegates a bounded decision area.

## Files

- Persisted structured notes
  - Canonical structured project notes.
  - Human-readable, but machine-owned.
  - Git-tracked.
- `.jri/state.json`
  - Runtime-only focus and visible interview items.
  - Not part of the user mental model.
  - Git-ignored.
- `.jri/logs/interview.json`
  - Full visible interview transcript mirror.
  - Git-ignored.

## Non-Goals

- No user-facing commands for notes or context.
- No storage-format data shown to the interviewer by default.
- No generic file-edit tool for the interviewer.
- No exact-text patching or diff-hunk patching for notes.
- No hard deletion of note history in MVP.
- No hidden persisted question tree for details JRI might ask later.

## Canonical Notes Shape

Persisted structured notes are the source of truth.

Example:

```text
project:
  name: MealMind
  tldr: Personal nutrition coach for people with celiac disease
  goal: Help users decide what they can safely eat
  target_user: People with celiac disease
  success_outcome: User can quickly check a food and trust the result
  software_type: Mobile app
  codebase_status: Greenfield

global:
  requirements:
    - id: r1
      text: Multi-platform support
      status: archived
      archive_reason: Scope narrowed
  constraints:
    - id: c1
      text: Start iPhone-first
      status: active
  questions:
    - id: q1
      text: Does the user want to choose the stack directly, approve proposals, or delegate it?
      status: resolved
      decision: d2
  decisions:
    - id: d1
      text: First release is mobile-only
      status: active
    - id: d2
      text: User is non-technical. We are allowed to make technical decisions ourselves.
      status: active

features:
  - id: f1
    name: food search
    summary: Let users search foods and see whether they are safe
    requirements:
      - id: f1/r1
        text: User can search by text
        status: active
      - id: f1/r2
        text: User can search by voice
        status: active
    constraints: []
    questions:
      - id: f1/q1
        text: Should search be barcode-first or text-first?
        status: open
      - id: f1/q2
        text: Should search have auto-completions?
        status: resolved
        decision: f1/d1
    decisions:
      - id: f1/d1
        text: Search must not have auto-completions
        status: active

  - id: f2
    name: saved foods
    summary: Let users save foods they trust
    requirements: []
    constraints: []
    questions: []
    decisions: []
```

## IDs

IDs must be short and LLM-friendly.

- Features: `f1`, `f2`, ...
- Requirements: `r1`, `r2`, ...
- Constraints: `c1`, `c2`, ...
- Open questions: `q1`, `q2`, ...
- Decisions: `d1`, `d2`, ...

Feature-specific IDs must be bounded, in the shape of `f{feature number}/{feature-bounded id}`, e.g. `f1/r3`. Each feature has its own ID counter independent of the other features and of global IDs.

Rules:

- Do not use UUIDs.
- Do not use exact note text as the primary key.
- Slugs are not required for MVP.
- Tool results should echo both the ID and the human text.

Example:

```text
Added open question q3: Should this support multiple workspaces?
```

## Note Semantics

### Project

`project` holds top-level project framing only.

It is not feature-specific.

### Global

`global` holds cross-cutting notes that apply across the whole project.

Examples:

- platform decisions,
- budget constraints,
- project-wide risks,
- product-wide questions.
- project-wide decision-involvement guidance,
- delegation boundaries for technical decisions.

When decision-involvement guidance applies across the whole project, the
interviewer can store it as ordinary global questions and decisions.

Examples:

- a global question asking whether the user wants to choose the stack directly, approve proposals, or delegate it.
- a global decision saying the user only cares about product behavior right now, so JRI should not force architecture discussion.
- a global decision saying the user wants to review stack choices before they are treated as settled.

### Features

`features` hold feature-local understanding.

Each feature has:

- stable `id`,
- `name`,
- short `summary`,
- its own requirements,
- its own constraints,
- its own open questions,
- its own decisions.

This split exists for two reasons:

1. project organization,
2. interviewer context management.

The interviewer must be able to discuss one feature deeply without dragging unrelated feature details into active context.

### Questions And Decisions

Questions and decisions cover surfaced uncertainty and settled guidance.

Rules:

- only surfaced uncertainties become questions.
- only user-approved guidance becomes decisions.
- silent interviewer guesses are not stored as decisions.
- if a technical detail is missing from notes, later agents must treat it as unresolved.
- if the user delegates implementation choice, store that delegation boundary as a decision, not an invented stack or architecture decision.

## Status Rules

For MVP:

- requirements, constraints, and decisions use `status: active|archived`
- open questions use `status: open|resolved|archived`

Resolved open questions link to a decision.

There is no separate free-form "answer" field in MVP. A resolved question is
closed by linking to a decision and optionally upserting it.

The same question/decision flow can capture how much detail the user wants to
control. For example, the interviewer may ask whether the user wants to choose
the stack, approve proposals, or delegate technical choices, then store the
answer as an ordinary decision.

Archived notes store a reason, for example:

```text
- id: d4
  text: Start with web first
  status: archived
  archive_reason: Direction changed to iPhone-first
```

## Interviewer Tool Surface

The interviewer should keep `explore` and gain note tools only.

It should not receive generic file or patch tools.

### 1. `read_notes`

Purpose:

- read a compact rendered view of current notes,
- avoid loading the entire notes file every time,
- support targeted rehydration.

Parameters:

```python
read_notes(
    scope: Literal["all", "project", "global", "feature"] | None,
    kind: Literal["all", "brief", "requirements", "constraints", "questions", "decisions", "features"] | None,
    feature_id: str | None,
    ids: list[str] | None,
    include_archived: bool | None,
) -> str
```

Rules:

- Returns compact human-readable summaries rather than storage-format data.
- `scope` selects where to read from.
- `kind` selects which note class to render inside that scope.
- `feature_id` scopes feature-local reads and is required when `scope="feature"`.
- `ids` supports targeted reads like `["q2", "d1"]`.
- `include_archived` defaults to false in implementation.

Example output:

```text
Open questions
- q1: Should search be barcode-first or text-first?
- q2: Is this for strict celiac only or broader gluten-free users too?
```

### 2. `set_project_brief`

Purpose:

- update top-level project framing fields.

Parameters:

```python
set_project_brief(
    name: str | None,
    tldr: str | None,
    goal: str | None,
    target_user: str | None,
    success_outcome: str | None,
    software_type: str | None,
    codebase_status: str | None,
) -> str
```

### 3. `add_feature`

Purpose:

- create a new feature container.

Parameters:

```python
add_feature(name: str, summary: str) -> str
```

Example result:

```text
Added feature f2: saved foods
```

### 4. `set_feature_brief`

Purpose:

- rename or resummarize an existing feature.

Parameters:

```python
set_feature_brief(
    feature_id: str,
    name: str | None,
    summary: str | None,
) -> str
```

### 5. `add_note`

Purpose:

- add one semantic note without rewriting whole sections at the tool API level.

Parameters:

```python
add_note(
    kind: Literal["requirement", "constraint", "question", "decision"],
    text: str,
    feature_id: str | None,
) -> str
```

Rules:

- `feature_id=None` means the note belongs in `global`.
- otherwise the note belongs to the given feature.

### 6. `resolve_question`

Purpose:

- mark a question resolved by linking an existing decision or creating a new one.

Parameters:

```python
resolve_question(
    question_id: str,
    decision_id: str | None,
    decision_text: str | None,
) -> str
```

Rules:

- exactly one of `decision_id` or `decision_text` must be provided.
- if `decision_id` is provided, the question is resolved by linking that existing decision.
- if `decision_text` is provided, JRI creates a new decision in the same scope and links it.
- the resolved question stores the linked decision ID.
- tool results should echo both the resolved question ID and the linked decision ID.

Example result:

```text
Resolved question f1/q1 with decision f1/d2: Search should be barcode-first.
```

### 7. `revise_note`

Purpose:

- change the text of an existing note without changing its identity.

Parameters:

```python
revise_note(note_id: str, text: str) -> str
```

### 8. `archive_note`

Purpose:

- archive obsolete notes instead of deleting them.

Parameters:

```python
archive_note(note_id: str, reason: str) -> str
```

Rule:

- archive is preferred over delete because pivots are normal in JRI.

### 9. `switch_focus`

Purpose:

- change the active discussion topic,
- trigger context rebuilding,
- let the interviewer safely forget unrelated details during focused feature work.

Parameters:

```python
switch_focus(
    scope: Literal["project", "global", "feature"],
    feature_id: str | None,
    carry_ids: list[str] | None,
    reason: str,
) -> str
```

Rules:

- The user never needs to manage focus directly.
- Generic tool progress may be visible while JRI organizes notes.
- `carry_ids` exists for cross-feature context that should stay in scope.
- The host runtime must handle this as a control operation, not a normal note-edit tool call.
- Calling `switch_focus` must rebuild active context from notes + state instead of continuing to append to the old full-session context.

Example result:

```text
Switched focus to feature f2: saved foods. Carrying: c1, d1.
```

## Write Strategy

All note mutations are semantic.

The runtime owns rendering and persistence.

Implementation rule:

- tools are not for patching persisted notes directly,
- tools are not for sending raw diffs,
- Python loads structured state,
- Python applies semantic mutation,
- Python writes the full persisted structured notes.
- Interviewer only knows about the concept of taking notes
- `switch_focus` is special: the runtime updates `.jri/state.json` and rebuilds the interviewer's active context from structured notes.
- `switch_focus` must not merely append a tool result to the existing conversation state.

This keeps the interviewer in the notes domain instead of the file-edit domain.

## Context Management

`switch_focus` is the bridge between notes and active LLM context.

When focus changes, JRI should rebuild the interviewer's active context from structured state instead of carrying a long unbounded transcript.

`.jri/state.json` should persist the minimum runtime state needed for this.

Suggested state shape:

```json
{
  "focus": {
    "scope": "feature",
    "feature_id": "f2",
    "carry_ids": ["c1", "d1"],
    "reason": "User moved from food search to saved foods."
  },
  "interview": {
    "items": [
      {"type": "user", "text": "I want to build a food app."},
      {"type": "assistant", "text": "What should the first version do?"}
    ]
  }
}
```

## Context Rebuild Rules

On focus switch, JRI should rebuild the active context from scratch with:

1. the system prompt,
2. the project brief,
3. active global constraints and decisions,
4. the active feature brief when focused on a feature,
5. carried note IDs,
6. a minimal bounded recent conversational tail if implementation needs it.

Rules:

- Feature A should not remain in active context while discussing feature B unless explicitly carried.
- After note-mutating tools, active context should contain rebuilt structured notes plus only the current tool call and result needed to continue the response.
- Earlier same-turn tool calls, especially large exploration outputs, must not be carried through context rebuilds.
- Restore should rebuild active model context from notes and focus state, not from a saved Responses transcript.
- The exact recent-tail policy is not part of the user model.

## User Experience Rules

- The user only talks to the interviewer.
- The user never has to say "switch topic".
- The user never has to say "summarize context".
- The user never has to know that persisted notes or runtime state exist.
- The interviewer and JRI infer topic shifts from normal conversation and manage notes/focus automatically.

## `read_notes` Example

Full compact example:

```text
# MealMind

**TL;DR**: Personal nutrition coach for people with celiac disease

- Goal: Help users decide what they can safely eat
- Target user: People with celiac disease
- Success outcome: User can quickly check a food and trust the result
- Software type: Mobile app
- Codebase status: Greenfield

Global constraints
- c1: Start iPhone-first

Global decisions
- d1: First release is mobile-only

Features
- f1: food search — Let users search foods and see whether they are safe
- f2: saved foods — Let users save foods they trust
```

Feature-scoped example:

```text
Feature f1: food search. Let users search foods and see whether they are safe

Requirements
- r1: User can search by text

Open questions
- q1: Should search be barcode-first or text-first?
```

## Acceptance Criteria

This feature is complete for MVP when:

- the interviewer can keep structured notes without any generic edit tool,
- notes are split into project, global, and feature-local scopes,
- note identities use short typed IDs,
- `read_notes` can return compact targeted summaries,
- the interviewer can add/revise/archive notes incrementally,
- the interviewer can resolve open questions into decisions by ID,
- the interviewer can capture the user's desired level of detail/control as ordinary notes,
- the interviewer checks notes or explores before asking when existing context can answer,
- the interviewer suggests product and technical options without making decisions unless delegated,
- the interviewer can switch focus across scopes,
- JRI rebuilds active context from notes + state on focus changes instead of keeping the full prior session in active context,
- JRI rebuilds active context from notes + focus state after restart,
- JRI mirrors the full visible interview transcript under `.jri/logs/`,
- later agents treat missing product or technical detail as unresolved instead of inventing architecture or stack choices,
- the user experience remains pure chat with no file/context management burden.

## Future Extensions

- Add stronger automatic focus-switch heuristics in the runtime.
- Add merge operations for duplicated features or notes.
