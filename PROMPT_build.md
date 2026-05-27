0a. Study `.jri/specs/*` with up to 500 parallel mini subagents to learn the application specifications.
0b. Study @.jri/IMPLEMENTATION_PLAN.md.
0c. For reference, the application source code is in `src/*`, if any.

1. Your task is to implement functionality per the specifications using parallel subagents. Follow @IMPLEMENTATION_PLAN.md and choose the most important item to address. Before making changes, search the codebase (don't assume not implemented) using mini subagents. You may use up to 500 parallel mini subagents for searches/reads and only 1 Codex subagent for build/tests. Use GPT 5.5 subagents when complex reasoning is needed (debugging, architectural decisions).
2. Use acceptance-first TDD for each increment. Derive the smallest high-signal behavior from the specs, write or run a failing public-interface acceptance/smoke test for that behavior first, then implement the narrowest vertical slice until it passes. Add narrower unit tests only where they clarify tricky logic. Do not write a bulk horizontal test suite before the implementation path is understood.
3. Public behavior must be verified through the same interface users or clients will use. If the increment affects a user-facing boundary, include at least one smoke/acceptance check through the production path, not only mocked or fake internals. If such a check cannot run, document the blocker in @.jri/IMPLEMENTATION_PLAN.md and do not treat the increment as complete.
4. After implementing functionality or resolving problems, run the focused tests for the improved behavior and the relevant public smoke check. If functionality is missing then it's your job to add it as per the application specifications. Ultrathink.
5. When you discover issues, immediately update @IMPLEMENTATION_PLAN.md with your findings using a subagent. When resolved, update and remove the item.
6. When the acceptance path and tests pass, update @IMPLEMENTATION_PLAN.md, then `git add -A` then `git commit` with a message describing the changes. After the commit, `git push`.

99999. Important: When authoring documentation, capture the why — tests and implementation importance.
999999. Important: Single sources of truth, no migrations/adapters. If tests unrelated to your work fail, resolve them as part of the increment.
9999999. Create a git tag only after there are no build/test errors and the required public-interface acceptance/smoke check for the increment has passed. If that smoke check is blocked or skipped, document the blocker in @.jri/IMPLEMENTATION_PLAN.md and do not tag the increment as green. If there are no git tags start at 0.0.0 and increment patch by 1 for example 0.0.1 if 0.0.0 does not exist.
99999999. You may add extra logging if required to debug issues.
999999999. Keep @.jri/IMPLEMENTATION_PLAN.md current with learnings using a subagent — future work depends on this to avoid duplicating efforts. Update especially after finishing your turn.
9999999999. When you learn something new about how to run the application, update @AGENTS.md using a subagent but keep it brief. For example if you run commands multiple times before learning the correct command then that file should be updated.
99999999999. For any bugs you notice, resolve them or document them in @IMPLEMENTATION_PLAN.md using a subagent even if it is unrelated to the current piece of work.
999999999999. Implement functionality completely. Placeholders and stubs waste efforts and time redoing the same work.
9999999999999. When @IMPLEMENTATION_PLAN.md becomes large periodically clean out the items that are completed from the file using a subagent.
99999999999999. If you find inconsistencies in the specs/* then use an GPT 5.5 subagent with xhigh reasoning to update the specs.
999999999999999. IMPORTANT: Keep @AGENTS.md operational only — status updates and progress notes belong in `IMPLEMENTATION_PLAN.md`. A bloated AGENTS.md pollutes every future loop's context.
