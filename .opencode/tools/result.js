import { tool } from "@opencode-ai/plugin";
import { writeFileSync } from "fs";

export default tool({
  name: "result",
  description:
    "Report the final outcome of this task. Call exactly once as your very last action.",
  args: {
    outcome: tool.schema
      .enum(["completed", "failed", "needs_human"])
      .describe("The task outcome"),
  },
  async execute({ outcome }) {
    const path = process.env.JRI_OUTCOME_PATH;
    if (!path) return "JRI_OUTCOME_PATH not set";
    writeFileSync(path, outcome);
    return `Outcome recorded: ${outcome}`;
  },
});
