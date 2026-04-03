# Roadmap

## 0. Core Thesis

JRI is:

> A repo-local task state machine with two agents:
 
- **Interrogator** → converts vague intent into precise, executable tasks
- **Ralph** → executes one task per loop deterministically

Everything else (CLI, Docker, VPS, UI) is infrastructure around this core.

## 1. Phase I — Core Engine (Local, No Infra)

### Goal

Prove that:

> idea → tasks → one-task loop → convergence


### Components

#### 1.1 Task Substrate (`.jri/`)

```
.jri/
  tasks/
    draft/
    todo/
    doing/
    done/
  logs/
  state.json
```

#### 1.2 Task Model

- atomic
- testable
- dependency-aware
- append-only corrections

#### 1.3 Task Lifecycle

```
draft → todo → doing → done
```

#### 1.4 Agents

- Interrogator → generates/refines tasks
- Ralph → executes exactly one task

### CLI (Phase I)

```bash
jri init
jri chat
jri start
jri stop
jri halt
jri reset
jri status
```

#### Command Meaning

- **init** → scaffold `.jri/`
- **chat** → Interrogator session
- **start** → begin Ralph loop
- **stop** → graceful stop
- **halt** → hard stop
- **reset** → rollback to last good state
- **status** → inspect system

### Success Criteria

- You can run JRI on itself
- Tasks converge without manual rewriting
- Failures are visible and Ralph can ask for human help when needed
- Loop is understandable and inspectable

## 2. Phase II — Loop Hardening

### Goal

Make the system **trustworthy**

### 2.1 Reliability

- crash-safe state
- resumable execution
- idempotent task execution
- no task corruption

### 2.2 Observability

Add:

- per-task logs
- diffs
- execution timeline
- structured output

Example:

```bash
jri status
```

Should clearly show:

- counts by state
- human-required tasks

### 2.3 Control

- stop / halt / resume must be reliable
- max iterations / time limits
- clear loop boundaries

### 2.4 Failure Semantics

Ralph must always resolve into:

- completed
- blocked
- needs clarification
- needs human

Never silent failure.

### Success Criteria

- You trust `jri start` unattended locally
- You can always recover with `jri reset`
- You understand why something failed

## 3. Phase III — Execution Backends

### Goal

Abstract execution away from local machine

### 3.1 Local Backend

- baseline
- direct execution

### 3.2 Docker Backend

```bash
jri start --backend docker
```

Capabilities:

- reproducible environment
- isolation
- dependency control

### 3.3 Backend Interface

All backends must support:

- run task
- capture logs
- return result
- resume execution
- stop execution

### Success Criteria

- Same behavior across local and Docker
- No model drift between environments

## 4. Phase IV — Remote Execution (VPS)

### Goal

Enable **unattended, long-running execution**

### 4.1 Basic Remote Runner

- SSH-based execution
- sync repo
- run loop remotely
- stream logs back

### 4.2 Managed VPS

- provisioning
- environment setup
- lifecycle management

### Why VPS matters

Enables:

- long-running loops
- real-world integrations
- autonomous execution
- deployment capability

### Success Criteria

- You can run JRI remotely without babysitting
- Failures are visible and recoverable

## 5. Phase V — Autonomy Layer

### Goal

Move from tool → system

### 5.1 Continuous Execution

- run until no tasks left
- or until the only ones left are human-blocked

Requires crash-resistant outer loop around `jri start`

### 5.2 Self-triggering

- new tasks → auto-run
- failures → retry or escalate

### Success Criteria

- JRI runs without constant human input
- Only intervenes when necessary

## 6. Phase VI — Reconciliation

### Goal

Continuously rediscover missing work

### Behavior

- scan codebase
- detect:
    - bugs
    - failing checks
    - inconsistencies
- generate new tasks in `draft/`

### Why this matters

Replaces `fix_plan.txt`:

- dynamic plan generation
- prevents stale task graphs
- enables continuous improvement

### Success Criteria

- JRI finds work you didn’t specify but expected
- System improves itself over time

## 7. Phase VII — Multi-Project Orchestration

### Goal

Scale beyond single repo

### Capabilities

- manage multiple projects
- queue execution
- prioritize tasks across repos
- parallel loops with limits

### Success Criteria

- JRI can operate across multiple codebases
- No state collision or confusion

## 8. Phase VIII — Interface Layer (Web UI)

### Goal

Visualization, not control logic

### UI should:

#### Display (in real-time):

- tasks
- human-readable logs
- execution timeline
- blocker notifications

#### Enable:

- chatting with Interrogator
- uploading files
- setting environment variables
- control loop (start/stop)

### Important

UI must be:

> a thin layer over the CLI/core


Not:

> where logic lives


## 9. Phase IX — Productization

### Goal

Turn system into a product

### Add:

- auth
- billing
- plans
- onboarding
- hosted execution
- templates

### This becomes:

> justralph.it


## System Architecture Summary

### Core

- `.jri/`
- Interrogator
- Ralph

### Execution

- local
- docker
- vps

### Control

- CLI (`jri`)
- Python package (`jri.core`)

### Intelligence

- reconciliation loop

### Interface

- web UI

### Product

- hosted platform

## Key Principles

### 1. Tasks are the source of truth

Not memory, not chat, not UI

### 2. One task per iteration

No batching

### 3. No guessing

Missing info → task

### 4. Append-only evolution

Never rewrite history silently

### 5. Observability is mandatory

If you can’t see it, you can’t fix it

### 6. Infrastructure comes after trust

Engine first, platform later

## Final Milestone

JRI is “done” (for v1) when:

> You can click “Just Ralph It,” walk away, and come back to a system that:

- made progress
- didn’t corrupt itself
- surfaced blockers clearly
- didn’t require babysitting

And essentially:

> matched your expectations


## Bottom Line

Build order:

```
Core → CLI → Reliability → Docker → VPS → Autonomy → Reconciliation → UI → Product
```

If you violate this order, you keep rebuilding it from scratch.

If you follow it, you get:

> a system that actually converges
