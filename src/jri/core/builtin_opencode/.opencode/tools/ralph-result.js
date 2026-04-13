import { tool } from "@opencode-ai/plugin";
import { runPythonTool } from "./_run-python-tool.mjs";

const humanTaskSchema = tool.schema.object({
  title: tool.schema.string().describe("Short title for the Human task"),
  body: tool.schema.string().describe("Markdown body for the Human task"),
  acceptance_criteria: tool.schema
    .array(tool.schema.string())
    .min(1)
    .describe("Concrete completion criteria for the Human task"),
  priority: tool.schema
    .number()
    .int()
    .min(0)
    .max(4)
    .optional()
    .describe("Optional Human task priority"),
});

export default tool({
  name: "ralph-result",
  description:
    "Report final status for the current task. This is the canonical way to record Ralph's result, and it must be called exactly once as the final action.",
  args: {
    result: tool.schema
      .enum(["completed", "incomplete", "needs_human"])
      .describe(
        "Final task status: completed when validated, incomplete when work was done but validation failed, needs_human when blocked on a specific human action",
      ),
    summary: tool.schema
      .string()
      .optional()
      .describe("Optional concise summary of what happened"),
    learnings: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe("Optional durable learnings to preserve"),
    blocker: tool.schema
      .string()
      .optional()
      .describe(
        "REQUIRED when result is needs_human: concise explanation of what is blocking progress",
      ),
    human_task: humanTaskSchema
      .optional()
      .describe(
        "REQUIRED when result is needs_human: structured Human task with title, body, acceptance_criteria, and optional priority",
      ),
  },
  async execute(args) {
    return runPythonTool("ralph-result", args);
  },
});
