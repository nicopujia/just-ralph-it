# Testing
- 80/20
- Black-box
- Deterministic, except the live call a `contract` makes: a double cannot falsify the contract it is the oracle for
- Only a test marked `contract` may make that call, and only `./scripts/check.py --contracts` runs one
- A `contract` fails where it would skip: an endpoint nothing can pay for or reach is a failed release, not a quiet pass
- Brave and models.dev are the only endpoints with one; the Responses API, YouTube, Codex's auth file and macOS `defaults` still rest on what a double says they answer
- Verb-led behavioral names
- Name conflict -> prefix with nearest sub-package(s) until unique
