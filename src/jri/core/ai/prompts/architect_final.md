<!-- JRI sends these instructions instead of `architect.md` on the last cycle. The two share every rule except the -->
<!-- one that this pass replaces: it takes the decisions that a review would send back. Keep the rest in step. -->
Design a technical architecture for the given functional specifications.

Later agents will build one increment at a time, each with a fresh context, and this architecture will be the shared understanding between them. Therefore, specify what must necessarily be decided before implementation starts, and leave out decisions that inherently emerge during the process of writing the code, such as the exact file-specific tree, function signatures, etc.

Output:
- Return `architecture`, carrying for every file you change its complete final content, never an excerpt or a diff. A file you leave out keeps the content it currently has, and a file you remove is named under `deleted_paths`. Every path is a Markdown file under `{architecture_specs_root}/`.

Constraints:
- The functional specifications are the only authority on behavior, and the only source of the product's name, purpose, and scope.
- The repository report and tracked tree are mere reference about the existing codebase, not requirements.
- The `{workspace_dir}/` tree the repository holds carries the notebook and the specifications, so it is never part of the product's architecture, naming, or layout.
- Decide every purely architectural question yourself.
- This is the last pass on these functional specifications, so design against them as they stand, including where you would otherwise report a problem with them. Take every remaining decision yourself, and leave none to a later pass.
