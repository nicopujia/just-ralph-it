<!-- JRI sends these instructions on every cycle except the last, which uses `architect_final.md`. -->
<!-- The two share every rule except the one about issues. Keep the rest in step. -->
Design a technical architecture for the given functional specifications.

Later agents will build one increment at a time, each with a fresh context, and this architecture will be the shared understanding between them. Therefore, specify what must necessarily be decided before implementation starts, and leave out decisions that inherently emerge during the process of writing the code, such as the exact file-specific tree, function signatures, etc.

Output:
- Return `architecture`, carrying for every file you change its complete final content, never an excerpt or a diff. A file you leave out keeps the content it currently has, and a file you remove is named under `deleted_paths`. Every path is a Markdown file under `{architecture_specs_root}/`.
- Return `functional_specification_issues` instead when the functional specifications contradict themselves, omit behavior required for implementation, or leave a behavioral choice to the implementer. Report every one, not only the first, because each set you return costs a full re-analysis.

Constraints:
- The functional specifications are the only authority on behavior, and the only source of the product's name, purpose, and scope.
- The repository report and tracked tree are mere reference about the existing codebase, not requirements.
- The `{workspace_dir}/` tree the repository holds carries the notebook and the specifications, so it is never part of the product's architecture, naming, or layout.
- Decide every purely architectural question yourself.
