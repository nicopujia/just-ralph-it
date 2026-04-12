import { tool } from "@opencode-ai/plugin";
import { runPythonTool } from "./_run-python-tool.mjs";

export default tool({
  name: "upsert-task",
  description: "Create or update one draft task.",
  args: {
    title: tool.schema.string().describe("Brief task title, max 50 chars"),
    body: tool.schema
      .string()
      .describe(
        "Markdown task body. Do NOT include acceptance criteria checks on this field; use `acceptance_criteria` for that.",
      ),
    assignee: tool.schema
      .enum(["Ralph", "Human"])
      .describe("Who should own the draft task"),
    priority: tool.schema
      .number()
      .int()
      .min(0)
      .max(4)
      .describe("Task priority from 0 to 4"),
    depends_on: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe("Optional list of blocking task slugs"),
    acceptance_criteria: tool.schema
      .array(tool.schema.string())
      .describe(
        "Non-empty list of checks that indicate the task can be marked as completed.",
      ),
  },
  async execute(args) {
    return runPythonTool("upsert-task", args);
  },
});
