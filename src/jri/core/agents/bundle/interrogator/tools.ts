import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { ExactEdit, registerMappedPythonTool, registerPythonTool, SLUG_RE, text } from "../_shared/registry.ts";
import { registerExplorer } from "../explorer/tools.ts";
import {
  CHILD_PI_MAX_BUFFER,
  VALIDATOR_TIMEOUT_MS,
  configuredModel,
  finalAssistantText,
  getPiInvocation,
  runUntilTerminalOutput,
} from "../_shared/subagents.ts";
import { resourcePath } from "../_shared/assets.ts";
import { runPythonTool } from "../_shared/tools/runner.ts";

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

export function registerInterrogatorValidator(pi: ExtensionAPI) {
  const extensionDir = dirname(fileURLToPath(import.meta.url));
  const packageRoot = dirname(extensionDir);
  const jriExtension = resourcePath("extensions.default", packageRoot);
  const validatorExtension = resourcePath("extensions.validator", packageRoot);
  const validatorPrompt = resourcePath("prompts.interrogatorValidator", packageRoot);

  pi.registerTool({
    name: "interrogator-validator",
    label: "interrogator-validator",
    description: "Run the Interrogator validator in an isolated runtime for selected draft task slugs.",
    parameters: Type.Object({ slugs: Type.Array(Type.String()) }),
    async execute(_toolCallId, params) {
      const slugs = (params as { slugs?: unknown }).slugs;
      if (
        !Array.isArray(slugs) ||
        !slugs.every((slug) => typeof slug === "string" && SLUG_RE.test(slug))
      ) {
        return text("`slugs` must be an array of valid task slugs");
      }
      const args = [
        "--mode",
        "json",
        "-p",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--extension",
        jriExtension,
        "--extension",
        validatorExtension,
        "--append-system-prompt",
        validatorPrompt,
        "--tools",
        "read-tasks,check-draft-promotion,approve-draft-promotion",
      ];
      const model = configuredModel(packageRoot, "interrogator-validator");
      if (model) args.push("--model", model);
      args.push(slugs.join("\n"));

      const invocation = getPiInvocation(args);
      const childEnv = { ...process.env };
      delete childEnv.JRI_CHAT_RUNTIME;
      childEnv.JRI_INTERROGATOR_VALIDATOR_RUNTIME = "1";
      const result = await runUntilTerminalOutput(invocation.command, invocation.args, {
        cwd: process.cwd(),
        env: childEnv,
        timeoutMs: VALIDATOR_TIMEOUT_MS,
        maxBuffer: CHILD_PI_MAX_BUFFER,
      });
      const output = finalAssistantText(result.stdout);
      if (result.error) {
        return text(`${output}\n${result.error}`.trim());
      }
      if (result.status !== 0) {
        const stderr = result.stderr.trim();
        return text(stderr ? `${output}\n${stderr}`.trim() : output);
      }
      if (normalizedFinalAssistantText(output) === "APPROVED") {
        runPythonTool("approve-draft-promotion", { slugs });
      }
      return text(output);
    },
  });
}

function normalizedFinalAssistantText(output: string): string {
  const match = output.match(/^```(?:md|markdown)?\s*\n([\s\S]*?)\n```$/i);
  return (match ? match[1] : output).trim();
}


export function registerInterrogatorValidationTools(pi: ExtensionAPI) {
  registerPythonTool(
    pi,
    "read-tasks",
    "Read one or more tasks by slug and return their structured contents.",
    Type.Object({ slugs: Type.Array(Type.String()) }),
  );
  registerMappedPythonTool(
    pi,
    "check-draft-promotion",
    "Validate draft task promotion readiness without moving tasks.",
    Type.Object({ slugs: Type.Optional(Type.Array(Type.String())) }),
    "promote-tasks",
    (params) => ({ ...params, check_only: true }),
  );
}
