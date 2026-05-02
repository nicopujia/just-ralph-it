import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";
import { text } from "./common.ts";
import { configuredModel, finalAssistantText, getPiInvocation } from "./python-bridge.ts";

const EXPLORER_MAX_TASKS = 8;
const EXPLORER_MAX_CONCURRENCY = 4;

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
  const extensionDir = join(packageRoot, "extensions");
  const jriExtension = join(extensionDir, "jri.ts");
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
    "--extension",
    jriExtension,
    "--append-system-prompt",
    explorerPrompt,
    "--tools",
    "read,grep,find,ls,web-search",
  ];
  const model = configuredModel(packageRoot, "explore");
  if (model) args.push("--model", model);
  args.push(request.task);

  const invocation = getPiInvocation(args);
  return new Promise((resolve) => {
    const childEnv = { ...process.env };
    delete childEnv.JRI_CHAT_RUNTIME;
    childEnv.JRI_EXPLORER_RUNTIME = "1";
    const child = spawn(invocation.command, invocation.args, {
      cwd: process.cwd(),
      env: childEnv,
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

function decodeHtml(value: string): string {
  return value
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#x27;/g, "'")
    .replace(/&#x2F;/g, "/");
}

function stripTags(value: string): string {
  return decodeHtml(value.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim());
}

function extractDuckDuckGoUrl(rawUrl: string): string {
  const decoded = decodeHtml(rawUrl);
  const normalized = decoded.startsWith("//") ? `https:${decoded}` : decoded;
  try {
    const url = new URL(normalized);
    const uddg = url.searchParams.get("uddg");
    return uddg ? decodeURIComponent(uddg) : normalized;
  } catch {
    return normalized;
  }
}

function parseDuckDuckGoResults(html: string, maxResults: number) {
  const results: { title: string; url: string; snippet: string }[] = [];
  const resultPattern =
    /<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)<\/a>[\s\S]*?<a[^>]+class="result__snippet"[^>]*>([\s\S]*?)<\/a>/g;
  let match: RegExpExecArray | null;
  while ((match = resultPattern.exec(html)) !== null && results.length < maxResults) {
    results.push({
      title: stripTags(match[2]),
      url: extractDuckDuckGoUrl(match[1]),
      snippet: stripTags(match[3]),
    });
  }
  return results;
}

export function registerExplorerTools(pi: ExtensionAPI) {
  pi.registerTool({
    name: "web-search",
    label: "web-search",
    description:
      "Search the public web and return concise result titles, URLs, and snippets for explorer research.",
    parameters: Type.Object({
      query: Type.String(),
      max_results: Type.Optional(Type.Number()),
    }),
    async execute(_toolCallId, params) {
      const query = (params as { query?: unknown }).query;
      const rawMaxResults = (params as { max_results?: unknown }).max_results;
      if (typeof query !== "string" || !query.trim()) {
        return text("`query` must be a non-empty string");
      }
      const maxResults =
        typeof rawMaxResults === "number" && Number.isFinite(rawMaxResults)
          ? Math.max(1, Math.min(10, Math.floor(rawMaxResults)))
          : 5;
      const url = new URL("https://html.duckduckgo.com/html/");
      url.searchParams.set("q", query.trim());
      const response = await fetch(url, {
        headers: {
          "user-agent": "jri-explorer/1.0",
        },
      });
      if (!response.ok) {
        return {
          ...text(`web search failed with HTTP ${response.status}`),
          isError: true,
        };
      }
      const html = await response.text();
      const results = parseDuckDuckGoResults(html, maxResults);
      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify({ query: query.trim(), results }, null, 2),
          },
        ],
        details: { query: query.trim(), results },
      };
    },
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

export function registerExplorer(pi: ExtensionAPI) {
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
