import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";
import { runPythonTool } from "../tools/_run-python-tool.mjs";

const RESERVED_PREFIX = "jri:";
const SLUG_RE = /^[a-zA-Z0-9][-a-zA-Z0-9_.]*$/;
const CHILD_PI_MAX_BUFFER = 64 * 1024 * 1024;
const EXPLORER_MAX_TASKS = 8;
const EXPLORER_MAX_CONCURRENCY = 4;

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

const ExactEdit = Type.Object({
  oldText: Type.String(),
  newText: Type.String(),
});

function textParts(parts: unknown): string {
  if (!Array.isArray(parts)) return "";
  return parts
    .filter(
      (part): part is { type: string; text: string } =>
        typeof part === "object" &&
        part !== null &&
        "type" in part &&
        "text" in part &&
        part.type === "text" &&
        typeof part.text === "string",
    )
    .map((part) => part.text)
    .join("");
}

function finalAssistantText(stdout: string): string {
  let final = "";
  for (const line of stdout.split("\n")) {
    if (!line.trim()) continue;
    try {
      const event = JSON.parse(line);
      if (event?.type === "message_end" && event?.message?.role === "assistant") {
        const text = textParts(event.message.content);
        if (text) final = text;
        continue;
      }
      if (event?.type === "agent_end" && event?.message?.role === "assistant") {
        const text = textParts(event.message.content);
        if (text) final = text;
        continue;
      }
      if (
        event?.type === "message_update" &&
        event?.assistantMessageEvent?.type === "text_end" &&
        typeof event.assistantMessageEvent.content === "string"
      ) {
        final = event.assistantMessageEvent.content;
      }
    } catch {
      continue;
    }
  }
  return final.trim() || stdout.trim();
}

function configuredModel(packageRoot: string, name: string): string | undefined {
  try {
    const manifest = JSON.parse(readFileSync(join(packageRoot, "package.json"), "utf-8"));
    const model = manifest?.jri?.models?.[name];
    return typeof model === "string" && model ? model : undefined;
  } catch {
    return undefined;
  }
}

function getPiInvocation(args: string[]): { command: string; args: string[] } {
  const currentScript = process.argv[1];
  const isBunVirtualScript = currentScript?.startsWith("/$bunfs/root/");
  if (currentScript && !isBunVirtualScript && existsSync(currentScript)) {
    return { command: process.execPath, args: [currentScript, ...args] };
  }

  const execName = basename(process.execPath).toLowerCase();
  const isGenericRuntime = /^(node|bun)(\.exe)?$/.test(execName);
  if (!isGenericRuntime) {
    return { command: process.execPath, args };
  }

  return { command: "pi", args };
}

function registerInterrogatorValidator(pi: ExtensionAPI) {
  const extensionDir = dirname(fileURLToPath(import.meta.url));
  const packageRoot = dirname(extensionDir);
  const jriExtension = join(extensionDir, "jri.ts");
  const validatorExtension = join(extensionDir, "jri-validator.ts");
  const validatorPrompt = join(packageRoot, "prompts", "interrogator-validator.md");

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
      const result = spawnSync(invocation.command, invocation.args, {
        cwd: process.cwd(),
        env: childEnv,
        encoding: "utf-8",
        maxBuffer: CHILD_PI_MAX_BUFFER,
      });
      const output = finalAssistantText(result.stdout ?? "");
      if (result.error) {
        return text(`${output}\n${result.error.message}`.trim());
      }
      if (result.status !== 0) {
        const stderr = (result.stderr ?? "").trim();
        return text(stderr ? `${output}\n${stderr}`.trim() : output);
      }
      return text(output);
    },
  });
}

function registerRalphValidator(pi: ExtensionAPI) {
  const extensionDir = dirname(fileURLToPath(import.meta.url));
  const packageRoot = dirname(extensionDir);
  const jriExtension = join(extensionDir, "jri.ts");
  const validatorPrompt = join(packageRoot, "prompts", "ralph-validator.md");

  pi.registerTool({
    name: "ralph-validator",
    label: "ralph-validator",
    description: "Run the Ralph validator in an isolated read-only runtime for one task slug.",
    parameters: Type.Object({ slug: Type.String() }),
    async execute(_toolCallId, params) {
      const slug = (params as { slug?: unknown }).slug;
      if (typeof slug !== "string" || !SLUG_RE.test(slug)) {
        return text("`slug` must be a valid task slug");
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
        "--append-system-prompt",
        validatorPrompt,
        "--tools",
        "read,bash,grep,find,ls,list-tasks,read-tasks,check-contrast",
      ];
      const model = configuredModel(packageRoot, "ralph-validator");
      if (model) args.push("--model", model);
      args.push(slug);

      const invocation = getPiInvocation(args);
      const childEnv = { ...process.env };
      delete childEnv.JRI_CHAT_RUNTIME;
      const result = spawnSync(invocation.command, invocation.args, {
        cwd: process.cwd(),
        env: childEnv,
        encoding: "utf-8",
        maxBuffer: CHILD_PI_MAX_BUFFER,
      });
      const output = finalAssistantText(result.stdout ?? "");
      if (result.error) {
        return text(`${output}\n${result.error.message}`.trim());
      }
      if (result.status !== 0) {
        const stderr = (result.stderr ?? "").trim();
        return text(stderr ? `${output}\n${stderr}`.trim() : output);
      }
      return text(output);
    },
  });
}

type ExplorerRequest = { task: string; index?: number };
type ExplorerResult = {
  task: string;
  exitCode: number;
  output: string;
  stderr: string;
  error?: string;
  index?: number;
};

function runExplorerTask(
  packageRoot: string,
  request: ExplorerRequest,
): Promise<ExplorerResult> {
  const explorerPrompt = join(packageRoot, "prompts", "explorer.md");
  const args = [
    "--mode",
    "json",
    "-p",
    "--no-session",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-context-files",
    "--append-system-prompt",
    explorerPrompt,
    "--tools",
    "read,grep,find,ls",
  ];
  const model = configuredModel(packageRoot, "explore");
  if (model) args.push("--model", model);
  args.push(request.task);

  const invocation = getPiInvocation(args);
  return new Promise((resolve) => {
    const child = spawn(invocation.command, invocation.args, {
      cwd: process.cwd(),
      env: { ...process.env },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (data) => {
      stdout += data.toString();
    });
    child.stderr.on("data", (data) => {
      stderr += data.toString();
    });
    child.on("error", (error) => {
      resolve({
        task: request.task,
        index: request.index,
        exitCode: 1,
        output: finalAssistantText(stdout),
        stderr,
        error: error.message,
      });
    });
    child.on("close", (code) => {
      resolve({
        task: request.task,
        index: request.index,
        exitCode: code ?? 0,
        output: finalAssistantText(stdout),
        stderr: stderr.trim(),
      });
    });
  });
}

async function mapExplorerTasks(
  packageRoot: string,
  requests: ExplorerRequest[],
): Promise<ExplorerResult[]> {
  const results: ExplorerResult[] = new Array(requests.length);
  let nextIndex = 0;
  const workerCount = Math.min(EXPLORER_MAX_CONCURRENCY, requests.length);
  await Promise.all(
    new Array(workerCount).fill(null).map(async () => {
      while (nextIndex < requests.length) {
        const current = nextIndex++;
        results[current] = await runExplorerTask(packageRoot, requests[current]);
      }
    }),
  );
  return results;
}

function registerExplorer(pi: ExtensionAPI) {
  const extensionDir = dirname(fileURLToPath(import.meta.url));
  const packageRoot = dirname(extensionDir);
  const TaskItem = Type.Object({ task: Type.String() });

  pi.registerTool({
    name: "explore",
    label: "explore",
    description:
      "Delegate read-only repository exploration to isolated explorer subagents. Explorers cannot edit files or call JRI tools.",
    parameters: Type.Object({
      task: Type.Optional(Type.String()),
      tasks: Type.Optional(Type.Array(TaskItem)),
    }),
    async execute(_toolCallId, params) {
      const singleTask = (params as { task?: unknown }).task;
      const taskItems = (params as { tasks?: unknown }).tasks;
      const requests: ExplorerRequest[] = [];
      if (typeof singleTask === "string" && singleTask.trim()) {
        requests.push({ task: singleTask.trim() });
      }
      if (Array.isArray(taskItems)) {
        for (let index = 0; index < taskItems.length; index++) {
          const item = taskItems[index];
          if (
            typeof item !== "object" ||
            item === null ||
            typeof (item as { task?: unknown }).task !== "string" ||
            !(item as { task: string }).task.trim()
          ) {
            return text("`tasks` must contain objects with non-empty `task` strings");
          }
          requests.push({ task: (item as { task: string }).task.trim(), index });
        }
      }
      if (requests.length === 0) {
        return text("provide either `task` or `tasks`");
      }
      if (requests.length > EXPLORER_MAX_TASKS) {
        return text(`too many explorer tasks (${requests.length}); max is ${EXPLORER_MAX_TASKS}`);
      }

      const results = await mapExplorerTasks(packageRoot, requests);
      const failed = results.filter((result) => result.exitCode !== 0 || result.error);
      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify({ results }, null, 2),
          },
        ],
        details: { results },
        isError: failed.length > 0,
      };
    },
  });
}

function registerCommitPrefixGuard(pi: ExtensionAPI) {
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
}

function registerChatTools(pi: ExtensionAPI) {
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

function registerInterrogatorValidationTools(pi: ExtensionAPI) {
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

function registerRalphTools(pi: ExtensionAPI) {
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

export default function (pi: ExtensionAPI) {
  registerCommitPrefixGuard(pi);
  if (process.env.JRI_CHAT_RUNTIME === "1") {
    registerChatTools(pi);
  } else if (process.env.JRI_INTERROGATOR_VALIDATOR_RUNTIME === "1") {
    registerInterrogatorValidationTools(pi);
  } else {
    registerRalphTools(pi);
  }
}
