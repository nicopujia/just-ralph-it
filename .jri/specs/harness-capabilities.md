# Harness Capabilities

## Topic

JRI gives its agents the extra capabilities needed for Ralph without cluttering
their context or inheriting unrelated user configuration.

## Job To Be Done

When an agent needs external facts or Ralph needs parallel codebase study, I
want JRI to provide those capabilities deliberately, so that agents can work
effectively without bloating their main context or depending on accidental
user-installed Pi resources. For the dogfood MVP path, this should work using
only the JRI interface for `/home/nico/just-ralph-it-dogfood/gupta-to-web` and
its deployment target `gupta-to-web.mpujia.justralph.it`.

## Web Search And Fetch

- Interrogator, auditor, planner, builder, and explorer agents may use web
  search/fetch when the current task needs external facts.
- Web access should not dump raw HTML into any agent context.
- Web access is for information gathering. Build/deploy network side effects
  such as package installs or `wrangler` commands happen through shell
  execution, not through the web capability.
- Agents should not use ad hoc shell `curl`, browser automation, package CLIs,
  or scratch scripts to fetch documentation/current facts into model context
  when the JRI web capability can satisfy the task.
- Web search returns up to 5 results with title, URL, snippet, and retrieval
  timestamp.
- Web fetch returns typed bounded markdown with source URL, title when known,
  fetched timestamp, content excerpt capped at 12,000 characters, and artifact
  references for omitted content.
- Fetch timeout is 20 seconds, redirects are capped at 5, and a single fetched
  artifact is capped at 5 MB in the MVP.
- Synthesis happens in the calling agent unless the wrapper has an explicit
  narrow operation such as "answer from this page"; citations remain mandatory.
- Large fetched pages must be stored and referenced (chunked or artifact-backed)
  rather than copied into the main context.
- For MVP, JRI web capability should be provided by a wrapped `pi-web-access`
  implementation behind a JRI web capability.
- JRI should expose this as its own web search/fetch capability, not raw package
  commands.
- Users should not need to manually install or configure arbitrary web/MCP
  packages for MVP web access.
- The production harness exposes web as a declared SDK/runtime capability. Any
  CLI wrapper is an internal compatibility entrypoint only; agents should not
  need shell access to use web.
- Web operation declarations are enforced. A session granted only `search` may
  not fetch, and a session granted only `fetch` may not search.
- Web fetch results must validate the typed result shape before entering agent
  context: source URL and fetched timestamp are required, returned content must
  be markdown/plain text for the declared markdown operation, and raw HTML is a
  capability error unless it is stored only as an artifact outside model
  context.
- If web is required and unavailable, the interrogator must return a clear
  actionable capability error or a labeled degraded response (never
  guess/fabricate current facts).
- If the current task cannot proceed safely without web facts, JRI blocks that
  agent action with an actionable capability error. If web is useful but not
  required, the agent may continue only with a visibly labeled degraded answer.
  Capability failures do not create a `ProjectStatus.blocker` in the MVP unless
  the agent can continue to a legitimate `ambiguousSpecs` or `needsHumanTask`
  handoff for a separate product or human-action reason.

## Ralph Subagents

- Ralph builder needs subagents for codebase search, focused investigation, and
  other parallel study work, following the Ralph/playbook emphasis on using
  subagents for search and context gathering.
- The JRI agent name for search/focused investigation subagents is `explorer`.
- Subagents should be available to the Ralph builder/planner without exposing
  unrelated user Pi packages or skills.
- Subagent context decision is deterministic by default: fresh/spawn context.
  Core may choose forked context only when the task explicitly requires selected
  parent context; that selected context and decision are logged.
- `explorer` delegation is mandatory for the dogfood MVP Ralph loop. If it is
  unavailable, JRI fails the loop with an actionable capability error rather
  than pretending completion.
- Subagent outputs should be concise handoffs, not full raw transcripts dumped
  into the builder context.
- JRI should record subagent starts, completions, failures, and important
  handoff outputs in `.jri/logs/<loopId>/events.jsonl`.
- MVP implementation uses a wrapped `pi-subagent` behind a JRI-owned
  `explorer` subagent capability.
- `pi-subagent` is accepted for the MVP only if it can run with JRI-controlled
  auth/model selection, spawn/fresh context by default, bounded handoffs, and no
  inherited user Pi packages/settings. If those fit checks fail, the MVP should
  fail loudly with a capability error rather than building a weaker ad hoc
  subagent system.
- JRI must expose this as an `explorer` delegation capability, not as raw
  `pi-subagent` package behavior or Pi-specific language.
- JRI should use `pi-subagent` in `spawn` mode by default. Forked parent context
  is allowed only when core explicitly decides the task requires it.
- JRI should provide JRI-owned explorer descriptors and avoid inheriting global
  or project Pi agent discovery by accident.
- JRI should pass only explicit, allowed runtime flags/context into subagents:
  project read access, current specs/plan excerpts selected by core, root
  `AGENTS.md`, web access when task-relevant, and no write permission by
  default.
- Explorers are read-only in the MVP. The Ralph builder owns code edits.
- User Pi skills, MCPs, prompts, settings, themes, and arbitrary agent config
  must not affect default subagent behavior.
- JRI-owned explorer concurrency is capped at 6 concurrent explorers per loop.
  Additional explorer tasks queue. Default timeout is 10 minutes per explorer.
  Halt cancels active explorers; graceful stop prevents new explorer tasks after
  the current iteration boundary.
- JRI must bound result handoff size before injecting subagent output into the
  builder context. Oversized raw output must be stored as artifacts/log references
  and summarized for handoff.
- Explorer handoffs injected into parent context are capped at 4,000 characters.
  Larger results are summarized and linked through `.jri/logs/<loopId>/artifacts/`.
- Explorer output injected into the parent is a JRI-owned concise result, not a
  truncated raw transcript. If the underlying subagent does not produce a
  bounded structured result, JRI stores the raw output as an artifact and
  creates or requests a concise summary before continuing.
- JRI should normalize subagent starts, progress, completions, failures, and
  result handoffs into `subagentStarted`, `subagentFinished`, and
  `subagentFailed` events.

## Capability Isolation

- Harness capabilities are explicit JRI-selected capabilities.
- The default clean runtime includes only capabilities JRI intentionally enables.
- Capability descriptors are the source of truth for runtime wiring, not prompt
  text alone. Prompt instructions may describe a capability, but enforcement
  happens at the adapter/runtime boundary.
- MVP JRI must either bundle the required capability implementations or preflight
  them before a loop can depend on them. Missing `pi-web-access` or `pi-subagent`
  is a capability failure with an actionable recovery path.
- The MVP does not expose a broad flag for including the user's Pi/provider
  configuration.
- The only inherited Pi/provider state allowed by default is provider auth and
  model registry data needed to create controlled SDK sessions. User Pi
  packages, skills, MCPs, prompts, themes, settings, memories, sessions, and
  arbitrary config are excluded.
- Future MCP/skills/plugins support should enter through the same explicit JRI
  capability model.
- Adding a JRI skill or agent capability later should be simple and local inside
  the JRI repo, ideally close to adding a markdown file or small descriptor.
- This is not a user-facing plugin/skill system in the MVP.
- If extending an agent requires a broad refactor across core, CLI, prompts, and
  harness code, the capability model is wrong.
- Agents should receive capabilities through declarative agent
  configuration rather than hardcoded prompt/runtime branches wherever practical.
- JRI-owned capability descriptors should pair agent-facing markdown
  instructions with small TypeScript manifests for runtime wiring.
- `context-mode` is not an MVP default capability. It is useful for large
  context reduction and indexed retrieval, but its current package behavior
  relies on broad hooks, ambient Pi/package configuration, and injected session
  memory that conflicts with JRI-managed `.jri` context ownership.
- A future `context-mode` integration should be a reduced JRI-owned wrapper with
  `.jri`-scoped data and explicit capability surfaces, not raw package behavior.
- Without `context-mode`, fetched artifacts live under `.jri/logs` with stable
  artifact refs. Agents should not inject whole large artifacts into context. A
  future JRI web operation may reread referenced artifacts by ref and byte/section
  range; this reread-by-ref operation is not required for the MVP core/CLI
  dogfood path unless a concrete implementation task needs it.

## Acceptance Criteria

- The interrogator can use external sources without filling its context with raw
  HTML and without leaking package-specific details into model-facing
  prompts.
- Web answers are bounded and cite sources; large page content is fetched by
  reference instead of raw dump.
- Ralph can delegate codebase investigation to subagents without manual user
  setup, with spawn/fresh context by default.
- If a required capability is missing, JRI reports an actionable capability error
  or a clearly labeled degraded answer before guessing. Capability failures do
  not create new `ProjectStatus.blocker.reason` values in the MVP unless a
  future runtime-state spec adds them.
- Capabilities appear in JRI specs/status/logs as JRI concepts, not accidental Pi
  package details.
- A clean user environment and a heavily customized Pi environment produce the
  same default JRI behavior.
