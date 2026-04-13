import { tool } from "@opencode-ai/plugin";
import { runPythonTool } from "./_run-python-tool.mjs";

export default tool({
  name: "list-tasks",
  description:
    "List tasks, optionally filtered by status, and return structured task summaries.",
  args: {
    status: tool.schema
      .enum(["draft", "todo", "doing", "done"])
      .optional()
      .describe("Optional task status filter"),
  },
  async execute(args) {
    return runPythonTool("list-tasks", args);
  },
});
