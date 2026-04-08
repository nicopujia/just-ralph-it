import { tool } from "@opencode-ai/plugin";
import { writeFileSync } from "fs";

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
    "Write the final structured result payload for this task to JRI_RESULT_PATH. Call exactly once as your very last action. Use only completed, incomplete, or needs_human. Missing or invalid payloads are treated as JRI-level failures.",
  args: {
    result: tool.schema
      .enum(["completed", "incomplete", "needs_human"])
      .describe(
        "Final task status: completed when validated, incomplete when more autonomous work remains, needs_human when blocked on a specific human action"
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
        "Required when result is needs_human: concise explanation of what is blocking progress"
      ),
    human_task: humanTaskSchema
      .optional()
      .describe(
        "Required when result is needs_human: structured Human task with title, body, acceptance_criteria, and optional priority"
      ),
  },
  async execute({ result, summary, learnings, blocker, human_task }) {
    if (result === "needs_human" && (!blocker || !human_task)) {
      throw new Error("needs_human requires blocker and human_task");
    }
    const path = process.env.JRI_RESULT_PATH;
    if (!path) return "JRI_RESULT_PATH not set";
    const payload = { result };
    if (summary) payload.summary = summary;
    if (learnings?.length) payload.learnings = learnings;
    if (blocker) payload.blocker = blocker;
    if (human_task) payload.human_task = human_task;
    writeFileSync(path, JSON.stringify(payload, null, 2) + "\n");
    return `Result recorded: ${result}`;
  },
});
