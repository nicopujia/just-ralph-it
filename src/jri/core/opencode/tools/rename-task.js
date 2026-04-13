import { tool } from "@opencode-ai/plugin";
import { runPythonTool } from "./_run-python-tool.mjs";

export default tool({
  name: "rename-task",
  description:
    "Rename one draft task slug and rewrite draft-task dependencies that reference it.",
  args: {
    from_slug: tool.schema
      .string()
      .describe("Existing draft task slug to rename"),
    to_slug: tool.schema.string().describe("New slug for the draft task"),
  },
  async execute(args) {
    return runPythonTool("rename-task", args);
  },
});
