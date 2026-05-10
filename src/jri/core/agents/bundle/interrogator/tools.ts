import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { ExactEdit, registerPythonTool } from "../_shared/registry.ts";
import { registerExplorer } from "../explorer/tools.ts";

export function registerChatTools(pi: ExtensionAPI) {
  registerPythonTool(
    pi,
    "create-node",
    "Create one Intent Graph node using a semantic Intent Graph path. Missing parent nodes are created automatically.",
    Type.Object({
      path: Type.String(),
      title: Type.String(),
      body: Type.Optional(Type.String()),
    }),
  );
  registerPythonTool(
    pi,
    "read-node",
    "Read one Intent Graph node by semantic Intent Graph path, including metadata, body, and child summaries.",
    Type.Object({
      path: Type.String(),
      depth: Type.Optional(Type.Number()),
    }),
  );
  registerPythonTool(
    pi,
    "apply-graph-patch",
    "Apply a body-only Intent Graph patch to existing nodes and return changed node summaries.",
    Type.Object({ patch: Type.String() }),
  );
  registerPythonTool(
    pi,
    "update-node-metadata",
    "Update title, state, or archive reason for one Intent Graph node addressed by semantic Intent Graph path.",
    Type.Object({
      path: Type.String(),
      title: Type.Optional(Type.String()),
      state: Type.Optional(Type.Union([
        Type.Literal("active"),
        Type.Literal("archived"),
      ])),
      archive_reason: Type.Optional(Type.String()),
    }),
  );
  registerPythonTool(
    pi,
    "move-node",
    "Move an Intent Graph node subtree from one semantic Intent Graph path to another.",
    Type.Object({
      source_path: Type.String(),
      destination_path: Type.String(),
    }),
  );
  registerPythonTool(
    pi,
    "compile-graph",
    "Compile the Intent Graph into validated todo tasks after explicit user confirmation.",
    Type.Object({}),
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
  registerExplorer(pi);
}
