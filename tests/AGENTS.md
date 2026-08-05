# Testing
- 80/20
- Black-box
- Deterministic, except one live call per external endpoint: a double cannot falsify the contract it is the oracle for
- Only a test marked `contract` may make that call, and only `./scripts/check.py --contracts` runs one
- A `contract` fails where it would skip: an endpoint nothing can pay for or reach is a failed release, not a quiet pass
- Verb-led behavioral names
- Name conflict -> prefix with nearest sub-package(s) until unique
