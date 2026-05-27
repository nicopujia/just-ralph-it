## Build & Run

- Bun is the package manager and test runner.

## Validation

Run these after implementing to get immediate feedback:

Run validation commands serially; running `bun run test` concurrently with typecheck or lint can cause the timing-sensitive CLI attach test to time out.

- Tests: `bun run test`
- Typecheck: `bun run typecheck`
- Lint: `bun run lint`

## Operational Notes

Use Bun for project scripts and execution.
- The public `jri` bin points at `src/cli/index.ts`; keep that file executable.

### Codebase Patterns

- Core product logic lives under `src/core`; CLI rendering and argument handling live under `src/cli`.
- Public lifecycle mutation goes through daemon-owned APIs; local fallbacks are for read-only recovery/observation.
- Handoff contracts are parsed from single-line `JRI_HANDOFF_JSON:` frames and should stay JRI-owned.
