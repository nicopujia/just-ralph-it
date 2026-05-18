import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { registerPythonTool } from "../(shared)/registry.ts";
import { registerRalphValidator } from "./validator/tools.ts";

export function registerRalphTools(pi: ExtensionAPI) {
  registerPythonTool(
    pi,
    "check-contrast",
    "Check WCAG contrast ratio and pass or fail thresholds for a foreground and background color.",
    Type.Object({
      foreground: Type.String(),
      background: Type.String(),
      standard: Type.String(),
    }),
  );
  registerPythonTool(
    pi,
    "list-tasks",
    "List tasks, optionally filtered by status, and return structured task summaries.",
    Type.Object({
      status: Type.Optional(Type.Union([
        Type.Literal("todo"),
        Type.Literal("doing"),
        Type.Literal("done"),
      ])),
    }),
  );
  registerPythonTool(
    pi,
    "read-tasks",
    "Read one or more tasks by slug and return their structured contents.",
    Type.Object({ slugs: Type.Array(Type.String()) }),
  );
  registerPythonTool(
    pi,
    "ralph-result",
    "Report final status for the current task. This must be called exactly once as the final action.",
    Type.Object({
      result: Type.Union([
        Type.Literal("completed"),
        Type.Literal("incompleted"),
        Type.Literal("needs_human"),
      ]),
      summary: Type.Optional(Type.String()),
      learnings: Type.Optional(Type.Array(Type.String())),
      blocker: Type.Optional(Type.String()),
      human_task: Type.Optional(Type.Object({
        title: Type.String(),
        body: Type.String(),
        acceptance_criteria: Type.Array(Type.String()),
        priority: Type.Optional(Type.Number()),
      })),
    }),
  );
  registerRalphValidator(pi);
}
