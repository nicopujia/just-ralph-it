import { tool } from "@opencode-ai/plugin";
import { runPythonTool } from "./_run-python-tool.mjs";

export default tool({
  name: "upsert-task",
  description:
    "Create or update one draft task at .jri/tasks/draft/<slug>.md from structured fields. This is the canonical way to manage draft task contents.",
  args: {
    title: tool.schema.string().describe("Brief task title, max 50 chars"),
    body: tool.schema.string().describe("Markdown task body"),
    assignee: tool.schema
      .enum(["Ralph", "Human"])
      .describe("Who should own the draft task"),
    priority: tool.schema
      .number()
      .int()
      .min(0)
      .max(4)
      .describe("Task priority from 0 to 4"),
    slug: tool.schema
      .string()
      .optional()
      .describe(
        "Optional slug for the draft task to create or update; otherwise derived from title",
      ),
    depends_on: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe("Optional list of blocking task slugs"),
    acceptance_criteria: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe("Optional acceptance criteria; drafts may omit this"),
  },
  async execute(args) {
    return runPythonTool("upsert-task", args);
  },
});
