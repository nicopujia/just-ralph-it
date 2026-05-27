# SDK Runtime Contracts

## Topic

JRI keeps Pi SDK sessions, daemon starts, interrogator memory, and capability
processes behind explicit JRI-owned contracts.

## Job To Be Done

When the scaffolding is replaced with the real Pi-backed implementation, I want
the adapter, daemon, and durable state boundaries to be clear enough that tests
can use fakes and users never depend on accidental Pi or process behavior.

## Pi SDK Harness Adapter

- Core talks to a JRI harness adapter in JRI terms: project directory, optional
  loop id, agent, phase, model config, selected context, allowed capabilities,
  output sink, and cancellation signal.
- The adapter boundary is equivalent to this shape:

```ts
type HarnessInvocation = {
  owner: { kind: "chat"; turnId: string } | { kind: "loop"; loopId: string };
  projectDir: string;
  agent: AgentName;
  phase: "interrogation" | "auditing" | "planning" | "building" | "explorer";
  model: Required<AgentConfig>;
  context: { refs: string[]; inline: string[] };
  capabilities: CapabilityDescriptor[];
  output: HarnessOutputSink;
  signal: AbortSignal;
};

type HarnessResult = {
  handoff: AgentHandoff;
  artifacts?: ArtifactRef[];
};
```

- The adapter owns Pi SDK session construction. Raw Pi sessions, package flags,
  SDK event shapes, and provider details do not leak into public core APIs,
  specs, status, or handoffs.
- Each interrogator, auditor, planner, builder, and explorer invocation uses a
  fresh controlled SDK session reconstructed from durable JRI state.
- The adapter selects only JRI-approved tools, capabilities, session storage,
  provider auth, and model settings. Ambient user Pi skills, MCPs, sessions,
  prompts, themes, and package config are excluded by default.
- The adapter maps provider auth, model resolution, missing capability,
  timeout, cancellation, invalid handoff, and SDK failures into actionable JRI
  errors.
- Agent lifecycle decisions are returned through the handoff contracts in
  `agent-handoff-contracts.md`, not inferred from raw model prose.

## Fake Harness Expectations

- Test fakes implement the same harness adapter request/result contract as the
  real Pi SDK adapter. There should be no fake-only start path or fake-only
  handoff parser.
- A fake harness can script assistant output chunks, handoffs, capability
  results, artifacts, delays, failures, auth errors, and cancellation.
- Fake assertions should prefer adapter request fields such as agent, phase,
  model, capabilities, selected context refs, owner, and cancellation behavior
  over brittle full-prompt string matching.
- Fakes are deterministic and offline. They must not spawn Pi, read ambient user
  Pi config, use network access, or depend on wall-clock timing except where a
  test explicitly controls time.
- If production code asks for an undeclared capability or ambient config, the
  fake should fail the test instead of silently accepting the request.

## Durable Interrogator State

- Conversation turns remain in `.jri/logs/interrogation.jsonl`, but topic state
  is machine-readable durable state, generated lazily under
  `.jri/interrogation-state.json`.
- Interrogation state records each known topic, its spec file, whether it is
  open or sealed, the last reconciled spec fingerprint, and any pending manual
  edit reconciliation.
- Sealing a topic is durable state. It omits old chat turns from routine context
  reconstruction, but the sealed spec file remains requirements truth and is
  still audited before Ralph starts.
- On every bare `jri` chat open and before processing an accepted start trigger,
  JRI compares current spec fingerprints with the last reconciled fingerprints.
- Manual edits are preserved. JRI must not overwrite user-edited spec text
  silently, even when it conflicts with scratchpad notes or prior chat.
- A manual edit to a sealed topic unseals that topic and records a
  `manualSpecEdit` reconciliation reason.
- Deleted, renamed, or newly added spec files are treated as reconciliation
  work, not as automatic deletion or acceptance of a topic.
- Reconciliation asks targeted questions only for conflicts, deleted behavior,
  or ambiguity. If the manual edit is clear and consistent, JRI records the new
  fingerprint and continues.
- A pending reconciliation blocks the start gate until it is resolved or
  explicitly deferred out of the current build scope.

## Chat Start And Daemon Protocol

- `chat.send()` is the only public API path that can authorize a new Ralph
  lifecycle, but an accepted trigger must start through the daemon-managed
  protocol.
- After normalizing a standalone `just ralph it` or `ralfealo` trigger,
  `chat.send()` records the user turn, emits the accepted assistant message, and
  sends an internal daemon stream request:

```ts
type LoopStartRequest = {
  method: "loop.start";
  params: {
    projectDir: string;
    trigger: "just ralph it" | "ralfealo";
  };
};
```

- The daemon starts lazily when needed, completes the protocol handshake, and
  applies the same incompatible-daemon safety rules as loop controls.
- The daemon owns loop id selection, registry updates, lock acquisition, status
  transition, runner process spawn, and initial `loopStarted` event emission.
- Daemon ownership is lifecycle authority; runner ownership is execution
  ownership. The daemon acquires the initial lock, spawns the runner, and
  transfers the lock pid to that runner. While a phase runs,
  `status.process.pid` and `status.lock.pid` refer to the daemon-spawned runner;
  `lock.owner: "daemon"` remains the authority label, not the daemon process id.
- Loop lifecycle mutation is daemon-owned in normal operation. Local in-process
  runtime mutation is allowed only for tests with explicit fakes or for read-only
  recovery/inspection fallbacks; public CLI controls must not bypass the daemon
  when the requested operation changes lifecycle state.
- `chat.send()` streams the daemon's lifecycle events back to the caller. The
  accepted-trigger stream must bridge into the newly authorized loop's audit,
  planning, build, blocker, stop, halt, failure, and completion events; callers
  must not need a separate `loop.observe()` call just to receive lifecycle events
  from the start they requested. The implementation may satisfy this by keeping
  `loop.start` open or by chaining observation after daemon startup, but it must
  not finish after only `loopStarted` while later lifecycle events are available
  solely through observation. It must not spawn an invisible local runner when
  the daemon is required.
- `loop.start` rejects active loops, unresolved human-task blockers, pending
  reconciliation, and invalid trigger text with state-specific actionable
  errors.
- Unit tests may inject an in-process fake daemon/start transport, but the fake
  must expose the same `loop.start` stream semantics.

## Capability Process Ownership

- Every capability process has an owner: either the current interrogator
  `chat.send()` invocation or a daemon-managed loop runner.
- Loop-owned capability processes are bound to
  `{ owner: { kind: "loop", loopId }, projectDir, capability }` and are
  registered with the runner so halt can cancel them and logs/events attach to the
  correct lifecycle.
- Chat-owned capability processes may not mutate loop status or create loop
  events. If they need artifacts, they write under
  `.jri/logs/interrogation-artifacts/` rather than a loop directory.
- Chat-owned capability processes are bound to
  `{ owner: { kind: "chat", turnId }, projectDir, capability }`; internal
  capability entrypoints must distinguish chat ownership from loop ownership
  instead of accepting only an active loop id.
- Internal commands such as `jri --run-web` and `jri --run-explorer` are adapter
  entrypoints, not public commands. They must validate their owner metadata and
  refuse missing, stale, or mismatched project/owner ownership.
- Graceful stop does not interrupt an in-flight capability. It prevents new
  loop-owned capability work at the next safe phase or iteration boundary.
- Halt cancels the runner and all registered loop-owned capability children.
  Cancellation is best-effort first, then forceful after a short grace period.
- Capability timeouts use the same cancellation path and produce structured
  capability errors plus artifact refs when output was captured.

## Stdout Channel Policy

- `.jri/logs/<loopId>/stdout.log` is the ordered merged display/replay stream for
  attach and later inspection.
- The merged log is written through one JRI output writer per loop. Harnesses and
  capability wrappers must not append stdout and stderr concurrently to the same
  file.
- MVP `stdout.log` does not preserve stdout/stderr channel provenance as a core
  contract. When channel-specific information matters, JRI records it in
  structured events, handoffs, or artifacts.
- Machine-readable capability results use stdout only for their declared result
  format, such as JSON. Diagnostics, debug output, and partial failures go to
  stderr and are normalized before entering agent context.
- JRI-owned terminal UI bytes, attach footer redraws, and control prompts are
  never written to `stdout.log`.

## Acceptance Criteria

- The real Pi SDK adapter and test fakes share one contract.
- Interrogation can resume from durable topic state without replaying the whole
  transcript.
- Manual spec edits are reconciled before a start trigger can authorize Ralph.
- Accepted chat triggers create daemon-owned, observable loop lifecycles.
- Capability children are owned, cancellable, and logged without relying on
  accidental process or stdout behavior.
