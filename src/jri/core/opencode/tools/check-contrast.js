import { tool } from "@opencode-ai/plugin";
import { runPythonTool } from "./_run-python-tool.mjs";

export default tool({
  name: "check-contrast",
  description:
    "Check WCAG contrast ratio and pass or fail thresholds for a foreground and background color. When color contrast is in question, use this tool as concrete evidence.",
  args: {
    foreground: tool.schema
      .string()
      .describe(
        "Foreground hex color, with optional leading # and optional alpha",
      ),
    background: tool.schema
      .string()
      .describe("Background hex color, with optional leading #"),
    standard: tool.schema
      .string()
      .describe(
        "WCAG target to check: AA, AALarge, AAA, AAALarge, or GraphicsAA",
      ),
  },
  async execute(args) {
    return runPythonTool("check-contrast", args);
  },
});
