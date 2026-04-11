import { tool } from "@opencode-ai/plugin";
import { runPythonTool } from "./_run-python-tool.mjs";

export default tool({
  name: "check-draft-promotion",
  description:
    "Validate draft task promotion readiness without moving tasks.",
  args: {
    slugs: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe(
        "Optional draft task slugs to validate; each item must be one task slug string, not Markdown or task contents; defaults to all draft tasks",
      ),
  },
  async execute(args) {
    return runPythonTool("promote-tasks", { ...args, check_only: true });
  },
});
