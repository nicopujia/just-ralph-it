# Testing
- 80/20
- Black-box
- Deterministic, except one live call per external endpoint: a double cannot falsify the contract it is the oracle for
- Only a test marked `contract` may make that call, and only `./scripts/check.py --contracts` runs one
- Verb-led behavioral names
- Name conflict -> prefix with nearest sub-package(s) until unique
