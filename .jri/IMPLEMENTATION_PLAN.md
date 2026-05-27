# Implementation Plan

- [ ] Ship the primary bare-`jri` Pi terminal chat UI as the MVP default.
  - Current state: bare interactive `jri` still enters the readline `runInteractiveChat()` fallback, and `tests/cli.test.ts` explicitly codifies that degraded path.
  - Progress note: verified that the Pi SDK exports `InteractiveMode`/TUI primitives, but JRI interactive flow still routes lifecycle decisions through core-owned `project.chat.send()` and one-shot harness paths (`--print`, `session.prompt(...)`, ignored stdin) instead of a JRI-owned bidirectional terminal session; because of that mismatch, the readline path remains a degraded fallback, and the public interactive CLI now declares that fallback explicitly and is smoke-tested through the installed `jri` bin in `tests/cli.test.ts`.

- [ ] Replace prompt-injected web/explorer shell escape hatches with harness-native, runtime-declared capabilities reachable by the intended agents.
  - Current state: SDK-native `jri_web_search` exists only for chat-owned interrogator SDK sessions; loop agents still rely on prompt text for `jri --run-web ...` and `jri --run-explorer ...`.
  - Remaining: add SDK-native `jri_web_fetch`, enforce native web search/fetch separation, and add mandatory native explorer/subagent delegation with durable dogfood evidence.

- [ ] Finish capability policy cleanup around native descriptors and grants.
  - Current state: `capabilities.ts`, `web-capability.ts`, and `harness.ts` enforce many ownership/shape rules, but descriptor-like policy is still mixed with prompt-driven paths.
  - Remaining: consolidate runtime-declared policy, reduce over-granted web/explorer access, and keep chat-owned capability ownership validation/preflight actionable.

- [ ] Improve interrogation context reconstruction beyond timestamp cutoffs.
  - Current state: reconstruction, start gating, manual spec edit detection, and topic sealing are implemented; recent-turn pruning is still timestamp-cutoff based.
  - Remaining: implement topic-aware excerpt selection and older relevant excerpt retrieval without reintroducing sealed-topic transcript noise.

- [ ] Backfill focused coverage for the remaining confirmed contract gaps.
  - Current state: the suite already covers CLI, chat, harness, runtime state, daemon runtime, auth, handoffs, and capabilities broadly.
  - Remaining: add coverage for the primary Pi-backed bare-`jri` chat path and the remaining native capability paths, especially web fetch and explorer delegation.
