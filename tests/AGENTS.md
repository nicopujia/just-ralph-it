# Automated testing guidelines

- 80/20-based
- Only for business logic (`core`, `lib`)
- Black-box style
- Deterministic
- Local-only
- Verb-led behavioral naming
- One test module per source module; on name conflict, prefix with closest sub-package(s) name until no conflict
- Doubles go under `doubles/`
