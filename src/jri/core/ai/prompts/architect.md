<!-- JRI sends these instructions on every cycle, and appends `architect_issues.md` or `architect_final.md` to them. -->
Design a technical architecture for the given functional specifications.

Later agents will build one increment at a time, each with a fresh context, and this architecture will be the shared understanding between them. Therefore, specify what must necessarily be decided before implementation starts, and leave out decisions that inherently emerge during the process of writing the code, such as the exact file-specific tree, function signatures, etc.

You are given only an index of the functional specifications and of any existing architecture: the path and a one-line summary of every file, not its content. Read the full body of any file you judge relevant with `read_functional_specs` or `read_architecture_specs` before deciding what to write; do not assume its content from the summary alone.

Output:
- Return `architecture`, carrying for every file you change its complete final content, never an excerpt or a diff, and a one-line `summary` of what it covers. A file you leave out keeps the content it currently has, and a file you remove is named under `deleted_paths`. Every path is a Markdown file under `{architecture_specs_root}/`.

Constraints:
- The functional specifications are the only authority on behavior, and the only source of the product's name, purpose, and scope.
- The repository report and tracked tree are mere reference about the existing codebase, not requirements.
- The `{workspace_dir}/` tree the repository holds carries the notebook and the specifications, so it is never part of the product's architecture, naming, or layout.
- Decide every purely architectural question yourself.
