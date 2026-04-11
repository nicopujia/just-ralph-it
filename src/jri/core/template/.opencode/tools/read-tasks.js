import { tool } from "@opencode-ai/plugin";
import { runPythonTool } from "./_run-python-tool.mjs";

export default tool({
  name: "read-tasks",
  description:
    "Read one or more JRI tasks by slug and return their structured contents.",
  args: {
    slug: tool.schema
      .string()
      .optional()
      .describe("Single task slug to read"),
    slugs: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe("Multiple task slugs to read"),
  },
  async execute(args) {
    return runPythonTool("read-tasks", args);
  },
});
