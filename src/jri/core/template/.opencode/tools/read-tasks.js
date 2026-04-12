import { tool } from "@opencode-ai/plugin";
import { runPythonTool } from "./_run-python-tool.mjs";

export default tool({
  name: "read-tasks",
  description:
    "Read one or more JRI tasks by slug and return their structured contents.",
  args: {
    slugs: tool.schema
      .array(tool.schema.string())
      .describe("Task slugs to read"),
  },
  async execute(args) {
    return runPythonTool("read-tasks", args);
  },
});
