<!-- JRI fills the output slot with `architect_issues.md`, except on the last cycle, which uses `architect_final.md`. -->
Design a technical architecture for the given functional specifications.

Later agents will build one increment at a time, each with a fresh context, and this architecture will be the shared understanding between them.

You are given only an index of the functional specifications and of any existing architecture—the path and a one-line summary of every file, not its full content.

Output:
- Return `architecture`, carrying for every file you change its complete final content, never an excerpt or a diff, and a one-line `summary` of what it covers. A file you leave out keeps the content it currently has. A file you remove is named under `deleted_paths`. Every path is a Markdown file under {architecture_specs_root}.
- {pass_rule}

Constraints:
- The functional specifications are the only authority on behavior, and the only source of the product's name, purpose, and scope.
- Cite them by path and section instead of restating them; write only what you add to them.
- Specify the decisions that are costly to reverse once code exists, and the ones that increments must share. What a later increment reads from the code earlier ones wrote—file trees, signatures, library parameters, etc—needs no decision here, even where you already know the answer. If you cannot say what breaks when a decision is made later, it is not architecture.
- The repository report and tracked tree are mere reference about the existing codebase, not requirements.
- The {workspace_dir} tree in the repository carries the notebook and the specifications, so it is never part of the product's architecture, naming, or layout.
