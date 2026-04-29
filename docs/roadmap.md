# Roadmap

## Phases

1. **Core Engine** (*Can it run?*): idea → tasks → loop → convergence.
2. **Loop Hardening** (*Can I trust it?*): reliability, observability, control, failure semantics.
3. **Remote Execution** (*Can it run elsewhere?*): VPS bootstrap, remote control, provisioning.
4. **Reconciliation** (*Can it find work?*): rediscover missing work → generate tasks from reality.
5. **Bounded Autonomy** (*Can it run without me?*): no human loop control inside validated intent; daemonized execution with process tracking.
6. **Machine Management** (*Can it sustain itself?*): detect usage growth and scale the user VPS safely.
7. **Web UI** (*Can I see/control it easily?*): visualization layer over CLI.
8. **Productization** (*Can I sell it?*): auth, billing, hosted platform.

Currently finishing phase 2.

## Done (v1)

> You click "Just Ralph It," walk away, and return to a software that fully matched your expectations.

The target is not open-ended autonomy.
It is convergence: JRI discovers and validates the owner's intent, then Ralph executes within that boundary.
