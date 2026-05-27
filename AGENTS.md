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

### Codebase Patterns

...
