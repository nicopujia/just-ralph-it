# Core And Agent Architecture

## Topic

Core exposes JRI behavior through a public TypeScript API while the MVP uses Pi
as the agent/runtime harness implementation.

## Job To Be Done

When JRI grows beyond one interface or one execution environment, I want the
core product logic to stay stable, so that CLI, web, and future clients can
reuse the same Ralph workflow without duplicating behavior or leaking Pi-specific
concepts into the public JRI model.

## Module Boundaries

- `core` contains the main JRI product logic.
- `core` is accessible only through a public TypeScript API.
- `core` exposes no user interface.
- `core` does not parse CLI arguments.
- `core` does not render terminal UI.
- `core` does not expose web handlers.
- `cli` is a user-facing developer interface that consumes `core`.
- Future clients, including web, consume `core` through the same public API.
- Modules are independent except where they depend on `core`.
- The MVP repository uses a single TypeScript package with top-level `src`.
- Core and CLI boundaries are represented by directories under `src`, not by
  separate workspace packages.
- The daemon implementation belongs to core runtime behavior, not to the public
  CLI surface. Packaging may require an internal process entrypoint, but it must
  stay hidden from help and normal user documentation.
- The public TypeScript API for external clients is the package-level core
  entrypoint (`src/core/index.ts` in the MVP source layout, and the equivalent
  package export once packaging is added). Other files under `src/core` are
  internal modules even when TypeScript can import them inside this repository.
- Internal daemon, runner, and capability entrypoints may exist for packaging and
  tests, but they are not public product APIs and must not appear in user-facing
  help.

Illustrative repository shape:

```text
src/
  core/
  cli/
```

## Public Core API Posture

- Core is async-first from day one.
- Public core operations should return promises even when the current
  implementation is local filesystem work.
- Long-running workflows should expose async event streams rather than hiding
  progress behind blocking calls.
- Chat, specs audit, planning, Ralph loop execution, loop observation, and
  status changes should be observable through streamed events.
- Simple reads such as current status, config, specs listing, and recent log
  lookup may still return a single async result.
- CLI and future web clients should consume the same async core events instead
  of reimplementing orchestration or polling private files directly.
- Core exposes a project object API rather than a flat function list.
- Public methods are namespaced one level below the project object so client
  code reads by product area.
- `attach` is not a core concept. It is a CLI behavior that renders a loop
  observation stream and adds terminal controls.
- Core exposes loop observation/streaming primitives that CLI and future web
  clients can consume.
- Core uses a canonical runtime event contract as the `CoreEvent` shape for all
  streamed outputs. The canonical type name is `RuntimeStateEvent`; `CoreEvent`
  may be an exported alias for client ergonomics.
- `open(projectDir)` binds and validates a project context only; it does not
  create or mutate files. A missing `.jri` directory means the project is
  uninitialized, not invalid. If `.jri` files already exist, `open()` validates
  the durable JSON/schema-bearing files and fails with a recovery path when they
  are malformed or unsupported.
- `ensureInitialized()` is idempotent and only creates missing durable scaffold
  artifacts.

Illustrative public API shape:

```ts
type CoreEvent = RuntimeStateEvent; // canonical runtime-state discriminated union

type Core = {
  open(projectDir: string): Promise<Project>;
};

type Project = {
  lifecycle: {
    // open() only validates and binds the project; it must not create or mutate.
    // ensureInitialized() performs idempotent project scaffolding only.
    ensureInitialized(): Promise<void>;
  };
  chat: {
    send(input: ChatInput): AsyncIterable<CoreEvent>;
  };
  auth: {
    status(): Promise<AuthState>;
    login(): Promise<AuthResult>;
    logout(): Promise<void>;
  };
  status: {
    get(): Promise<ProjectStatus>;
  };
  loop: {
    observe(options?: LoopObserveOptions): AsyncIterable<CoreEvent>;
    requestStop(): Promise<void>;
    halt(options: HaltOptions): AsyncIterable<CoreEvent>;
    resume(): AsyncIterable<CoreEvent>;
  };
};
```

`chat.send()` is the only public API path that can move from interrogation into
audit/planning/build. When the user message is exactly an accepted trigger after
the start-gate normalization rules from the interrogation spec, the interrogator
invokes an internal core start-loop capability, and the returned event stream
includes audit, planning, and loop lifecycle events. Core does not expose a
public `loop.start()` method.

`loop.resume()` is a continuation control for an already-authorized lifecycle,
not a second start path. It never creates a loop id, never authorizes new or
changed requirements, and never clears an `ambiguousSpecs` blocker. It may only
continue the current `activeLoopId` from `stopped`, or from a
`needsHumanTask` blocker after bare `jri` has received `done` and core has
verified and recorded the blocker resolution. If specs changed while stopped,
the user must return through `chat.send()` with `just ralph it` or `ralfealo` so
audit and planning rerun before building.

Loop control methods operate on the current `activeLoopId` recorded in
`.jri/status.json`. `resume()` reconstructs from durable `.jri` state and starts
fresh harness sessions; it never depends on resuming a previous Pi-native
session.

Auth results are UI-neutral. `login()` returns either a completed auth state or
a user-action payload such as a browser URL, device code, expiry, and concise
instructions. CLI/web clients choose how to present that payload; core does not
open browsers, render prompts, or own a terminal flow.

## Implementation Baseline

- JRI is implemented in TypeScript.
- JRI uses the Pi TypeScript SDK as the MVP agent/runtime harness.
- JRI being implemented in TypeScript does not imply the target project is
  TypeScript, JavaScript, Node, web, or any other specific software type.
- Prefer `bun` for JRI package management unless Pi tooling requires an `npm`
  command for a specific install/auth step.
- Keep external dependencies minimal where practical.

## Model Preset

JRI supports agent-specific model configuration in `.jri/config.json` from day
one.

The generated MVP config is minimal:

```json
{
  "$schema": "https://justralph.it/schemas/config.schema.json",
  "schemaVersion": 1,
  "provider": "openai",
  "modelPreset": "openai"
}
```

JRI owns built-in agent defaults in core rather than materializing every default
agent model into project config. Users only add agent overrides when needed.

Model resolution priority is behavioral:

- explicit `agents.<name>` overrides,
- selected `modelPreset` built-in defaults,
- provider defaults as fallback.
- If no model is resolvable through those tiers, core returns an actionable
  configuration error instead of silently choosing a default.

The MVP ships with one built-in preset for OpenAI. This preset is core-owned
data, not generated project JSON:

```ts
const openAiPreset = {
  interrogator: { model: "gpt-5.5", reasoning: "xhigh" },
  explorer: { model: "gpt-5.3-codex-spark", reasoning: "xhigh" },
  auditor: { model: "gpt-5.4", reasoning: "xhigh" },
  planner: { model: "gpt-5.4", reasoning: "xhigh" },
  builder: { model: "gpt-5.5", reasoning: "xhigh" },
};
```

The `explorer` agent is for Ralph/codebase subagents.

Agent overrides use the `agents` object:

```json
{
  "$schema": "https://justralph.it/schemas/config.schema.json",
  "schemaVersion": 1,
  "provider": "openai",
  "modelPreset": "openai",
  "agents": {
    "builder": {
      "model": "gpt-5.5",
      "reasoning": "xhigh"
    },
    "auditor": {
      "model": "gpt-5.4",
      "reasoning": "xhigh"
    }
  }
}
```

JRI ships the canonical JSON Schema in core and validates `.jri/config.json`
against it before using any values. The `$schema` field is an editor hint and
does not require network access at runtime.

Initial schema shape:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://justralph.it/schemas/config.schema.json",
  "type": "object",
  "additionalProperties": false,
  "required": ["schemaVersion", "provider", "modelPreset"],
  "properties": {
    "$schema": { "type": "string" },
    "schemaVersion": { "const": 1 },
    "provider": { "enum": ["openai"] },
    "modelPreset": { "enum": ["openai"] },
    "agents": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "interrogator": { "$ref": "#/$defs/agentConfig" },
        "explorer": { "$ref": "#/$defs/agentConfig" },
        "auditor": { "$ref": "#/$defs/agentConfig" },
        "planner": { "$ref": "#/$defs/agentConfig" },
        "builder": { "$ref": "#/$defs/agentConfig" }
      }
    }
  },
  "$defs": {
    "agentConfig": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "model": { "type": "string", "minLength": 1 },
        "reasoning": { "enum": ["low", "medium", "high", "xhigh"] }
      },
      "anyOf": [
        { "required": ["model"] },
        { "required": ["reasoning"] }
      ]
    }
  }
}
```

## Naming And Repository Structure

- Repository directories, files, and symbols should use generic JRI domain names
  rather than dependency names.
- File and directory names use kebab-case.
- Prefer names such as `agent`, `harness`, `runtime`, `session`, `capability`,
  `interrogator`, `auditor`, `planner`, and `builder`.
- Avoid names such as `pi-harness` or `PiAgent` outside the narrow integration
  boundary.
- Dependency-specific names belong only in implementation adapters/wrappers that
  directly call Pi APIs.
- Do not repeat `JRI` throughout symbols and filenames. The repository already
  provides that context.
- For example, `Agent` is the primitive. Pi is the MVP implementation behind
  that primitive.

## Harness Boundary

- Pi SDK is the MVP implementation for agent execution, tool use, streaming
  events, and terminal UI primitives where useful.
- JRI owns Ralph product concepts: specs, readiness, audit, planning, loop
  lifecycle, status, logs, and user-facing CLI semantics.
- Pi sessions, slash commands, packages, settings, context files, and extension
  details must not become JRI's public domain model by accident.
- `core` should isolate Pi-specific calls behind a small internal harness
  boundary, even if Pi is the only MVP harness implementation.
- JRI uses the Pi TypeScript SDK by default.
- Pi RPC mode and Pi JSON event stream mode are fallback integration paths, not
  the primary MVP architecture.

## JRI Agents

JRI needs at least four agents, all executed through the Pi harness:

- Interrogator/orchestrator: talks with the user, elicits requirements, manages
  specs, invokes audit, and starts planning/building through `core`.
- Specs auditor: evaluates whether specs are ready for Ralph and returns focused
  feedback to the interrogator when they are not.
- Planner: runs the internal planning phase that creates or regenerates
  `.jri/IMPLEMENTATION_PLAN.md`.
- Ralph builder: executes one build task per fresh outer-loop iteration using
  the Ralph prompt and durable state.

## Context Ownership

- JRI persists chat transcript, extracted decisions/open questions where needed,
  specs, plan, logs, and status under `.jri`.
- Generated chat summaries are not an MVP requirement unless transcript/spec
  context proves insufficient.
- The Pi harness receives explicit context chosen by JRI for each invocation or
  session.
- JRI-managed context is the MVP decision. JRI should create fresh/in-memory Pi
  execution sessions from JRI-owned durable state rather than relying on
  Pi-managed long sessions as project memory.
- The Ralph builder uses fresh context per outer-loop iteration.
- Pi's native session history must not be the source of truth.
- Pi persistence may be used only as an optimization when JRI can reconstruct
  behavior from its own stored state.
- After a successful `just ralph it` or `ralfealo` transition, JRI compacts or
  clears the interrogator context and resumes future chat from durable specs,
  status, logs, and minimal extracted state.
- The minimum durable state for reconstruction is current specs,
  `.jri/scratchpad.md`, `.jri/status.json`, `.jri/IMPLEMENTATION_PLAN.md` when
  present, relevant recent loop events, and recent unsealed chat turns from
  `.jri/logs/interrogation.jsonl`.

## Session Construction

- JRI uses controlled Pi SDK sessions for the MVP.
- JRI should call the Pi SDK directly, but construct each session from explicit
  JRI-owned pieces rather than ambient Pi defaults.
- Each agent invocation should receive:
  - Pi-backed auth and model registry.
  - The agent-specific model preset from `.jri/config.json`.
  - In-memory or JRI-controlled session/settings storage.
  - Controlled resource loading.
  - Explicitly selected context from `.jri`.
  - Explicitly selected capabilities for that agent.
  - The project root `AGENTS.md` when relevant.
- JRI should not use raw Pi default sessions as product memory.
- Shelling out to Pi CLI JSON mode is a fallback integration path, not the MVP
  architecture.

## Runtime Isolation

- JRI should protect agent quality from unrelated user-specific context such as
  MCPs, skills, plugins, memories, profiles, global instructions, and rules.
- Do not call `createAgentSession()` with naïve defaults for JRI runs. Pi SDK
  defaults use `DefaultResourceLoader`, which discovers global/project
  extensions, skills, prompts, settings, context files, and sessions.
- MVP default behavior is clean execution with no inherited Pi packages, MCPs,
  skills, prompts, themes, global context files, sessions, or unrelated provider
  context.
- JRI should construct SDK sessions deliberately with explicit auth/model
  registry, agent-specific model selection, in-memory or JRI-controlled
  session/settings where appropriate, explicit tool/resource loading, and
  controlled context injection.
- Pi injects `AGENTS.md` by default, but default discovery may also include
  global/user context. JRI should include only the project root `AGENTS.md` by
  using controlled resource loading, such as `agentsFilesOverride`, instead of
  unrestricted default discovery.
- The MVP does not expose a broad flag for including the user's Pi/provider
  configuration.
- If JRI needs a capability, JRI explicitly enables that capability through its
  own capability model.
- Capability extensibility should be agent-agnostic. Core should model selected
  skills, MCPs, plugins, tools, or similar provider features as explicit JRI
  capabilities, not raw Pi package/extension concepts.
- Agent capability configuration should be declarative enough that adding a
  future JRI-owned skill inside the JRI repo is a small local change, ideally
  close to adding a markdown file or descriptor, not a broad refactor.
- JRI-owned capability descriptors live inside the JRI repo. The preferred
  pattern is markdown for agent-facing instructions plus a small TypeScript
  manifest for runtime wiring.
- This does not imply a user-facing plugin/skill system in the MVP.
- Pi ecosystem packages may be used inside the harness boundary when they are
  wrapped as JRI capabilities. For MVP Ralph subagents, JRI uses `pi-subagent`
  behind an `explorer` capability rather than exposing the package directly.

Capability descriptors are JRI-owned. A descriptor names the capability, the
agents allowed to use it, its agent-facing instruction markdown, the runtime
wiring module, and any default limits. For MVP, descriptors cover web
search/fetch and `explorer` delegation. Filesystem, shell, and deployment
authority are execution permissions, not user-visible Pi packages.

## Execution And Auth

- MVP execution posture is YOLO for interrogator, specs auditor, planner, and
  Ralph builder: local filesystem/shell tools may run without approval prompts.
- Build/deploy commands may use network side effects when the target project
  requires them, such as package installation or `wrangler` deployment. External
  fact gathering should use JRI's web capability instead of ad hoc raw HTML
  fetching through shell commands.
- Agents may use existing local provider/deployment auth and environment
  variables needed by project commands, but JRI must avoid asking users to paste
  secrets into chat unless the user explicitly chooses that path.
- JRI reuses Pi's provider authentication system instead of implementing its own
  model auth flow.
- Core exposes Pi-backed authentication operations and `jri` consumes them from the
  public core auth API, which future clients can consume unchanged.
- The stable MVP auth surface is `jri auth status`, `jri auth login`, and
  `jri auth logout`. Those commands should use Pi-backed provider auth where
  available so users do not need to run Pi directly for normal JRI auth setup.
- Additional Pi provider-auth operations may be forwarded later as auth-only
  passthrough commands, but they are not required for the MVP. Unsupported or
  unknown auth operations must return normalized actionable JRI errors rather
  than exposing raw Pi behavior.
- Bare `jri` may also launch or guide the same Pi-backed auth flow inline when
  auth is missing, then continue into interrogation after auth succeeds.
- Users should not need to know or run Pi directly for normal JRI auth setup.
- JRI may use Pi `AuthStorage` and `ModelRegistry` to read existing Pi auth from
  `~/.pi/agent/auth.json` and supported environment variables.
- JRI has one authentication truth for the configured provider/model preset:
  the same Pi-backed provider/model preflight used by controlled SDK sessions.
  `jri auth status`, inline auth, and harness startup must agree. `jri auth
  status` must not report `authenticated` merely because an auth file contains a
  plausible credential shape unless the configured SDK model path would also
  accept that auth.
- After explicit or inline auth, JRI validates that it can create a controlled
  SDK session or equivalent provider/model preflight with the configured
  provider/model preset.
- If a credential exists but the configured SDK model path cannot use it, JRI
  reports `not authenticated` or an actionable auth recovery state rather than
  letting a later chat or loop fail with a contradictory auth error.
- If Pi SDK/package is unavailable or auth cannot be completed inline, JRI fails
  with a direct actionable message that points to `jri auth`.
- The MVP has no fallback harness and no JRI-managed API key flow.

Core owns the orchestration state machine. Agents can request actions and return
judgments, but core performs state transitions, loop id creation, lock checks,
event emission, audit/planning/build phase boundaries, and resume eligibility.
This keeps readiness, planning starts, build iteration boundaries, and
deployment completion consistent across CLI and future clients.

## Acceptance Criteria

- A new client can be added without reimplementing Ralph workflow rules.
- The core API can drive the CLI, future web UI, and internal tools.
- JRI can recover or continue from `.jri` state even if Pi-native session
  history is unavailable.
- A polluted personal Pi setup does not silently affect JRI's default behavior.
- Pi-specific details are isolated enough that JRI's specs, status, and loop
  semantics remain understandable without knowing Pi internals.
