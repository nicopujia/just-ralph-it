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

The machine has access to the `wrangler` CLI. Deployment through `wrangler` is
allowed when required by the target project.

## Validation Requirements

- The target project is completed according to its own repository instructions
  and requirements.
- The deployed result is available at `gupta-to-web.mpujia.justralph.it`.
- JRI produces durable specs, logs, status, and local commits that explain what
  happened.
- A reviewer can inspect JRI artifacts to understand the interrogation,
  planning, build iterations, blockers if any, validation, and deployment.

## Non-Goals

- This dogfood target does not mean JRI assumes web projects, Cloudflare,
  Wrangler, JavaScript, TypeScript, or any deployment provider by default.
- The dogfood target does not replace JRI's generic software-building contract.
