import { tool } from "@opencode-ai/plugin";
import { runPythonTool } from "./_run-python-tool.mjs";

export default tool({
  name: "delete-task",
  description: "Delete one draft task when no other draft tasks depend on it.",
  args: {
    slug: tool.schema.string().describe("Draft task slug to delete"),
  },
  async execute(args) {
    return runPythonTool("delete-task", args);
  },
});
