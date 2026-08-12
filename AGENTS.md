# Just Ralph It (JRI)
[Concept doc](https://nicolaspujia.com/just-ralph-it.md)

## Workflow
After changes: `scripts/check.py`

## Style
- Prefer cleanest long-term approach—no backwards-compat code
- Trust types and JRI-managed data
- `lib` = JRI-agnostic business logic only
- `tui` = UI only, no tests
- Helpers only for: repeated logic, unavoidable extractions, linter alerts
- DDD naming, modules included: `Agent.get_context`, NEVER `BaseOpenAIAgent.get_agent_context`; `repository.py`, NEVER `constants.py`
- Functions/methods = verbs; except properties, event handlers, decorators
- Comments and setting descriptions in ASD-STE100

## Boundaries
- JRI must pass text it did not write through `lib.prompt.render`, or send it alone as a whole message with no JRI wording beside it to copy
- JRI must remove a file only when no other process may still hold it, and must leave it otherwise, whether JRI's own run finished, stopped, or failed
- JRI must resolve concurrent access by exclusion, not coordination: a second instance takes the hold or stops, and only the holder writes a project file
- Every turn must end with a `TurnFinished` and close every row it opened, whether the run finished, stopped, or failed
