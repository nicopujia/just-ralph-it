import { tool } from "@opencode-ai/plugin";
import { runPythonTool } from "./_run-python-tool.mjs";

export default tool({
  name: "promote-tasks",
  description:
    "Validate or promote draft tasks to todo using JRI's core promotion logic. This is the canonical way to promote draft tasks; defaults to all draft tasks when `slugs` is omitted.",
  args: {
    slugs: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe(
        "Optional draft task slugs to validate or promote; each item must be one task slug string, not Markdown or task contents; defaults to all draft tasks",
      ),
    check_only: tool.schema
      .boolean()
      .optional()
      .describe("When true, validate promotion without moving any tasks"),
  },
  async execute(args) {
    return runPythonTool("promote-tasks", args);
  },
});
