# Testing
- 80/20
- Black-box
- Deterministic, except the live call a `contract` makes: a double cannot falsify the contract it is the oracle for
- Only a test marked `contract` may make that call, and the mark is deselected until a run asks for it: `./scripts/check.py --contracts`
- A `contract` fails where it would skip: an endpoint nothing can pay for or reach is a failed release, not a quiet pass
- Brave and models.dev are the only endpoints with one; the Responses API, YouTube, Codex's auth file and macOS `defaults` still rest on what a double says they answer
- YouTube can have none: it serves no transcript API, so the library reads the watch page, and YouTube answers a datacenter address with `429` — a release gate runs on a hosted runner, so that contract would fail every release for the endpoint's reason and not JRI's
- Verb-led behavioral names
- Name conflict -> prefix with nearest sub-package(s) until unique
