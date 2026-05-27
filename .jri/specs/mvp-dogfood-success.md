# MVP Dogfood Success

## Topic

JRI proves the MVP by using JRI itself to complete and deploy a real project.

## Job To Be Done

When JRI reaches its first useful implementation, I want to dogfood it on a real
software project using only JRI as the development interface, so that the MVP is
validated by the same workflow it claims to simplify.

## Success Contract

The MVP succeeds when JRI can complete the project at:

```text
/home/nico/just-ralph-it-dogfood/gupta-to-web
```

and deploy it to:

```text
gupta-to-web.mpujia.justralph.it
```

The dogfood run must use JRI exclusively as the development interface.

Allowed operation:

- Running `jri`.
- Using `jri auth ...`.
- Using `jri loop attach`, `jri loop stop`, `jri loop halt`, and
  `jri loop resume`.
- Driving JRI through terminal automation such as `tmux send-keys`.
- Reading JRI-visible status, logs, specs, and output.

Disallowed operation:

- The implementer agent directly editing, coding, testing, or deploying
  `gupta-to-web` outside JRI.
- Bypassing the JRI interrogator, auditor, planner, or Ralph loop to make the
  target project succeed.
- Manually operating the Ralph process outside JRI.

The target repository contains the project-specific details for what must be
built. JRI should discover and use those details through the normal
interrogation/specification workflow rather than through out-of-band
implementation by the supervising agent.

For MVP acceptance, "JRI exclusively" is proven by durable JRI artifacts, not by
trusting operator memory. The reviewable evidence must include:

- interrogation turns and specs that show how target requirements were
  discovered and accepted;
- loop events showing audit, planning, at least one explorer delegation, build
  iterations, validation, deployment-related work, commits, and tags;
- stdout/artifact logs sufficient to understand any blockers, validation
  failures, and deployment commands;
- git history in the target repository created by Ralph/JRI, not direct
  supervising-agent edits.

Terminal automation may drive public JRI commands and may observe JRI-visible
files. It must not directly edit, test, commit, tag, or deploy the target
repository outside the active JRI loop.

The machine has access to the `wrangler` CLI. Deployment through `wrangler` is
allowed when required by the target project.

## Validation Requirements

- Before JRI is considered ready to attempt the target dogfood run, the real
  installed public CLI must pass a smoke path without fake harness environment
  variables or direct internal entrypoints:
  - `jri auth status` reports the same provider/model readiness that the real
    controlled SDK session path will use.
  - Bare `jri` can accept a normal user message and either produce an
    interrogator response through the production harness or fail before chat with
    consistent actionable setup guidance.
  - If the primary terminal UI is the fallback REPL rather than Pi terminal chat
    primitives, the implementation plan records the evidence and rationale for
    that fallback.
- The target project is completed according to its own repository instructions
  and requirements.
- The deployed result is available at `gupta-to-web.mpujia.justralph.it` over
  HTTPS, returns a successful HTTP response, and serves the target application's
  expected user-visible behavior as discovered from the target repository.
- JRI produces durable specs, logs, status, and local commits that explain what
  happened.
- A reviewer can inspect JRI artifacts to understand the interrogation,
  planning, build iterations, blockers if any, validation, and deployment.

Target-scope discovery precedence is: target `AGENTS.md` or equivalent agent
instructions first; then target README/docs; then package/build scripts and
deployment configuration; then existing target tests. If these sources conflict
materially, JRI must resolve the conflict through interrogation/specs before
Ralph builds.

## Non-Goals

- This dogfood target does not mean JRI assumes web projects, Cloudflare,
  Wrangler, JavaScript, TypeScript, or any deployment provider by default.
- The dogfood target does not replace JRI's generic software-building contract.
