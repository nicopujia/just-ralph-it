# Workflow
After changes: `scripts/check.py`

# Style
- Trust types and JRI-managed data
- DDD naming, modules included: `Agent.get_context`, NEVER `BaseOpenAIAgent.get_agent_context`; `repository.py`, NEVER `constants.py`
- Functions/methods = verbs; except properties, event handlers, decorators
- Helpers only for: repeated logic, unavoidable extractions, linter alerts
- Prefer cleanest long-term approach—no backwards-compat code
- `lib` = JRI-agnostic business logic only
- `tui` = UI only, no tests

## Boundaries
- Text JRI did not author reaches a model through `lib.prompt.render`, or whole with no structure of JRI's around it to forge
- State the model can change carries what it asserts about; no prose keeps a claim honest
- JRI commits `.jri` and nothing else, and no file of the user's stops a run
- A turn ends with a `TurnFinished`, and every row it opened is closed

## Order
- Module (groups 1 blank line apart): dunders, types, constants, public vars, private vars, public funcs, public classes, private funcs, private classes
- Class: constants, nested types, magic methods, public methods, private methods

# Project
[Concept doc](https://nicolaspujia.com/just-ralph-it.md)
