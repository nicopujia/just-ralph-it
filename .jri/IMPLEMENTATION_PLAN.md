# Implementation Plan

- [ ] Ship the primary bare-`jri` Pi terminal chat UI as the MVP default.
  - Current state: bare interactive `jri` now uses a CLI-owned Pi-backed terminal surface via `@earendil-works/pi-tui`, preserves inline auth, routes turns through `project.chat.send()`, streams assistant output incrementally, and is smoke-tested through the installed `jri` bin.
  - Remaining: confirm whether the CLI-owned Pi terminal surface satisfies the spec's intended Pi-primitives requirement or record the fallback rationale as durable dogfood evidence.

- [ ] Replace prompt-injected web/explorer shell escape hatches with harness-native, runtime-declared capabilities reachable by the intended agents.
  - Current state: SDK-native `jri_web_search` and `jri_web_fetch` now exist, SDK tool registration enforces declared web search/fetch separation, the generated explorer compatibility descriptor no longer tells explorers to use `jri --run-web` wrapper commands directly, and SDK prompts for interrogator/planner/builder/auditor now reference native `jri_web_search`, `jri_web_fetch`, and `jri_explorer` tool names instead of leaking hidden wrapper commands. Loop-owned planner/builder explorer work still flows through `jri_explorer -> invokePiSdkHarness() -> runExplorerTask()`, which records durable `subagentStarted`/`subagentFinished` evidence.
  - Remaining: retire any other legacy wrapper-command guidance and preserve regression coverage around native web/explorer routing so the harness-native contract stays durable.

- [ ] Finish capability policy cleanup around native descriptors and grants.
  - Current state: `capabilities.ts`, `web-capability.ts`, and `harness.ts` enforce many ownership/shape rules, but descriptor-like policy is still mixed with prompt-driven paths.
  - Remaining: consolidate runtime-declared policy, reduce over-granted web/explorer access, and keep chat-owned capability ownership validation/preflight actionable.

- [x] Improve interrogation context reconstruction.
  - Current state: reconstruction, start gating, manual spec edit detection, topic sealing, topic-aware active-topic transcript selection, and older relevant turn backfill are implemented; recent-turn pruning is no longer timestamp-cutoff based.
  - Why it matters: topic-aware selection keeps the active conversation focused on the current interrogation thread, while older relevant backfill restores missing context without reintroducing sealed-topic transcript noise.

- [ ] Backfill focused coverage for the remaining confirmed contract gaps.
  - Current state: the suite already covers CLI, chat, harness, runtime state, daemon runtime, auth, handoffs, and capabilities broadly.
  - Remaining: add coverage for the remaining native capability paths, especially web fetch and explorer delegation.
