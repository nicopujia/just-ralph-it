# Testing
- 80/20.
- Verb-led behavioral names.
- Name conflict -> prefix with nearest sub-package(s) until unique.
- Black-box: assert the result, not the way it was reached. A rewrite that keeps the result keeps the test green. A result is a returned value, the bytes of a file, the state of the repository, the wording of an error, or the prompt sent to the model. The command that ran, the request that was posted, and the order of the calls behind them are not.
- Deterministic, except a `contract`, which calls the endpoint for real. A double answers with what JRI expects of an endpoint, so no test against it can prove that expectation wrong — its calls show only that something happened, or how often.
- A `contract` is deselected until a run asks for it: `./scripts/check.py --contracts`.
- A `contract` with no key, or no way to reach its endpoint, fails. It does not skip. A skip is green, and the release would ship with nothing checked.
- Brave and models.dev are the only endpoints with one; the Responses API, YouTube, Codex's auth file and macOS `defaults` still rest on what a double says they answer. YouTube gets none because it answers a hosted runner with `429`, so the gate would fail for YouTube's reason and not JRI's.
