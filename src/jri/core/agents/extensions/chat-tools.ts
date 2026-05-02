import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { ExactEdit, registerMappedPythonTool, registerPythonTool } from "./common.ts";
import { registerExplorer } from "./explorer.ts";
import { registerInterrogatorValidator } from "./validators.ts";

export function registerChatTools(pi: ExtensionAPI) {
  registerPythonTool(
    pi,
    "list-tasks",
    "List tasks, optionally filtered by status, and return structured task summaries.",
    Type.Object({
      status: Type.Optional(Type.Union([
        Type.Literal("draft"),
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
    "Create or update one draft task.",
    Type.Object({
      title: Type.String(),
      body: Type.String(),
      assignee: Type.Union([Type.Literal("Ralph"), Type.Literal("Human")]),
      priority: Type.Number(),
      depends_on: Type.Optional(Type.Array(Type.String())),
      acceptance_criteria: Type.Array(Type.String()),
    }),
  );
  registerPythonTool(
    pi,
    "edit-draft-task",
    "Edit one draft task file using exact oldText/newText replacements, then validate it.",
    Type.Object({ slug: Type.String(), edits: Type.Array(ExactEdit) }),
  );
  registerPythonTool(
    pi,
    "rename-task",
    "Rename one draft task slug and rewrite draft-task dependencies that reference it.",
    Type.Object({ from_slug: Type.String(), to_slug: Type.String() }),
  );
  registerPythonTool(
    pi,
    "delete-task",
    "Delete one draft task when no other draft tasks depend on it.",
    Type.Object({ slug: Type.String() }),
  );
  registerPythonTool(
    pi,
    "promote-tasks",
    "Validate or promote draft tasks to todo using the core promotion logic.",
    Type.Object({
      slugs: Type.Optional(Type.Array(Type.String())),
      check_only: Type.Optional(Type.Boolean()),
    }),
  );
  registerMappedPythonTool(
    pi,
    "check-draft-promotion",
    "Validate draft task promotion readiness without moving tasks.",
    Type.Object({ slugs: Type.Optional(Type.Array(Type.String())) }),
    "promote-tasks",
    (params) => ({ ...params, check_only: true }),
  );
  registerExplorer(pi);
  registerInterrogatorValidator(pi);
}
