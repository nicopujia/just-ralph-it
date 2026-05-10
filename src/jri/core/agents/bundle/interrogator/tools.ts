import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { ExactEdit, registerPythonTool } from "../_shared/registry.ts";
import { registerExplorer } from "../explorer/tools.ts";

export function registerChatTools(pi: ExtensionAPI) {
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
    "read-readme",
    "Read the repo-root README.md.",
    Type.Object({}),
  );
  registerPythonTool(
    pi,
    "edit-readme",
    "Edit only the repo-root README.md using exact oldText/newText replacements.",
    Type.Object({ edits: Type.Array(ExactEdit) }),
  );
  registerPythonTool(
    pi,
    "upsert-task",
    "Create or update one todo task.",
    Type.Object({
      title: Type.String(),
      body: Type.String(),
      assignee: Type.Union([Type.Literal("Ralph"), Type.Literal("Human")]),
      priority: Type.Number(),
      depends_on: Type.Optional(Type.Array(Type.String())),
      acceptance_criteria: Type.Array(Type.String()),
    }),
  );
  registerExplorer(pi);
}
