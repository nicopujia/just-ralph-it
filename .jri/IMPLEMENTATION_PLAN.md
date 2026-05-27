# Implementation Plan

- [ ] Ship the primary bare-`jri` Pi terminal chat UI as the MVP default.
  - Current state: bare interactive `jri` now uses a CLI-owned Pi-backed terminal surface via `@earendil-works/pi-tui`, preserves inline auth, routes turns through `project.chat.send()`, streams assistant output incrementally, and is smoke-tested through the installed `jri` bin.
  - Remaining: confirm whether the CLI-owned Pi terminal surface satisfies the spec's intended Pi-primitives requirement or record the fallback rationale as durable dogfood evidence.

- [ ] Replace prompt-injected web/explorer shell escape hatches with harness-native, runtime-declared capabilities reachable by the intended agents.
  - Current state: SDK-native `jri_web_search` exists for chat-owned interrogator SDK sessions, and loop-owned planner/builder sessions now reach native explorer work through `jri_explorer -> invokePiSdkHarness() -> runExplorerTask()`, which records durable `subagentStarted`/`subagentFinished` evidence instead of relying on prompt text for `jri --run-explorer ...`.
  - Remaining: add SDK-native `jri_web_fetch`, enforce native web search/fetch separation, and remove the remaining prompt-driven `jri --run-web ...` escape hatch. The new acceptance test matters because it proves the loop-owned explorer path executes through the harness and leaves durable subagent evidence, protecting the contract from regressions.

- [ ] Finish capability policy cleanup around native descriptors and grants.
  - Current state: `capabilities.ts`, `web-capability.ts`, and `harness.ts` enforce many ownership/shape rules, but descriptor-like policy is still mixed with prompt-driven paths.
  - Remaining: consolidate runtime-declared policy, reduce over-granted web/explorer access, and keep chat-owned capability ownership validation/preflight actionable.

- [ ] Improve interrogation context reconstruction beyond timestamp cutoffs.
  - Current state: reconstruction, start gating, manual spec edit detection, and topic sealing are implemented; recent-turn pruning is still timestamp-cutoff based.
  - Remaining: implement topic-aware excerpt selection and older relevant excerpt retrieval without reintroducing sealed-topic transcript noise.

- [ ] Backfill focused coverage for the remaining confirmed contract gaps.
  - Current state: the suite already covers CLI, chat, harness, runtime state, daemon runtime, auth, handoffs, and capabilities broadly.
  - Remaining: add coverage for the remaining native capability paths, especially web fetch and explorer delegation.
