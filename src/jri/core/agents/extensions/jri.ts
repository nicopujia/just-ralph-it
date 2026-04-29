import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { runPythonTool } from "../tools/_run-python-tool.mjs";

const RESERVED_PREFIX = "jri:";

function extractCommitMessage(command: string): string | null {
  const patterns = [
    /(?:^|[;&|]\s*)git\s+commit\b[\s\S]*?--message\s*=\s*(["'])(.*?)\1/i,
    /(?:^|[;&|]\s*)git\s+commit\b[\s\S]*?--message\s+(["'])(.*?)\1/i,
    /(?:^|[;&|]\s*)git\s+commit\b[\s\S]*?-m\s+(["'])(.*?)\1/i,
  ];
  for (const pattern of patterns) {
    const match = command.match(pattern);
    if (match) return match[2].trimStart();
  }
  return null;
}

function text(content: string) {
  return { content: [{ type: "text" as const, text: content }], details: {} };
}

function registerPythonTool(
  pi: ExtensionAPI,
  name: string,
  description: string,
  parameters: object,
  toolName = name,
) {
  pi.registerTool({
    name,
    label: name,
    description,
    parameters,
    async execute(_toolCallId, params) {
      return text(runPythonTool(toolName, params));
    },
  });
}

function registerMappedPythonTool(
  pi: ExtensionAPI,
  name: string,
  description: string,
  parameters: object,
  toolName: string,
  mapParams: (params: Record<string, unknown>) => Record<string, unknown>,
) {
  pi.registerTool({
    name,
    label: name,
    description,
    parameters,
    async execute(_toolCallId, params) {
      return text(runPythonTool(toolName, mapParams(params as Record<string, unknown>)));
    },
  });
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return;
    const input = event.input as { command?: unknown } | undefined;
    if (typeof input?.command !== "string") return;
    const message = extractCommitMessage(input.command);
    if (message === null || !message.toLowerCase().startsWith(RESERVED_PREFIX)) {
      return;
    }
    return {
      block: true,
      reason: `Prefix "${RESERVED_PREFIX}" is reserved for JRI-managed commits. Update your commit message.`,
    };
  });

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
    "approve-draft-promotion",
    "Record validator approval for an exact draft task promotion set.",
    Type.Object({ slugs: Type.Optional(Type.Array(Type.String())) }),
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
}
