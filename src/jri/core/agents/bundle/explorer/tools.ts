import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { lookup } from "node:dns/promises";
import { isIP } from "node:net";
import { spawn } from "node:child_process";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";
import { text } from "../_shared/registry.ts";
import {
  CHILD_PI_MAX_BUFFER,
  EXPLORER_TASK_TIMEOUT_MS,
  WEB_SEARCH_TIMEOUT_MS,
  configuredModel,
  finalAssistantText,
  getPiInvocation,
  terminalAssistantText,
} from "../_shared/subagents.ts";
import { resourcePath } from "../_shared/assets.ts";

const EXPLORER_MAX_TASKS = 8;
const EXPLORER_MAX_CONCURRENCY = 4;
const WEB_FETCH_MAX_REDIRECTS = 5;
const WEB_FETCH_MAX_CHARS = 200_000;

type ExplorerRequest = { task: string; index?: number };
type ExplorerResult = {
  task: string;
  exitCode: number;
  output: string;
  stderr: string;
  error?: string;
  index?: number;
};

export function registerExplorerTools(pi: ExtensionAPI) {
  pi.registerTool({
    name: "fetch-url",
    label: "fetch-url",
    description:
      "Fetch a specific public HTTP(S) URL and return cleaned, capped text for explorer research.",
    parameters: Type.Object({
      url: Type.String(),
    }),
    async execute(_toolCallId, params) {
      const rawUrl = (params as { url?: unknown }).url;
      if (typeof rawUrl !== "string" || !rawUrl.trim()) {
        return text("`url` must be a non-empty string");
      }

      const result = await fetchPublicUrl(rawUrl.trim());
      if (typeof result === "string") {
        return { ...text(result), isError: true };
      }
      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify(result, null, 2),
          },
        ],
        details: result,
      };
    },
  });

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
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), WEB_SEARCH_TIMEOUT_MS);
      let response: Response;
      try {
        response = await fetch(url, {
          headers: {
            "user-agent": "jri-explorer/1.0",
          },
          signal: controller.signal,
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return {
          ...text(`web search failed: ${message}`),
          isError: true,
        };
      } finally {
        clearTimeout(timeout);
      }
      if (!response.ok) {
        return {
          ...text(`web search failed with HTTP ${response.status}`),
          isError: true,
        };
      }
      const html = (await response.text()).slice(0, 1_000_000);
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

function runExplorerTask(
  packageRoot: string,
  request: ExplorerRequest,
): Promise<ExplorerResult> {
  const jriExtension = resourcePath("extensions.default", packageRoot);
  const explorerPrompt = resourcePath("prompts.explorer", packageRoot);
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
    "read,grep,find,ls,fetch-url,web-search",
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
      detached: process.platform !== "win32",
    });
    let stdout = "";
    let stderr = "";
    let truncated = false;
    let settled = false;
    const finish = (result: ExplorerResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    const killChild = () => {
      try {
        if (process.platform !== "win32" && child.pid !== undefined) {
          process.kill(-child.pid, "SIGTERM");
        } else {
          child.kill("SIGTERM");
        }
      } catch {
        child.kill("SIGTERM");
      }
    };
    const finishIfTerminal = () => {
      const output = terminalAssistantText(stdout);
      if (!output) return;
      killChild();
      finish({
        task: request.task,
        index: request.index,
        exitCode: truncated ? 1 : 0,
        output,
        stderr: stderr.trim(),
        error: truncated ? "explorer task output exceeded buffer limit" : undefined,
      });
    };
    const timer = setTimeout(() => {
      killChild();
      finish({
        task: request.task,
        index: request.index,
        exitCode: 1,
        output: finalAssistantText(stdout),
        stderr: stderr.trim(),
        error: `explorer task timed out after ${EXPLORER_TASK_TIMEOUT_MS}ms`,
      });
    }, EXPLORER_TASK_TIMEOUT_MS);
    const append = (current: string, data: unknown) => {
      if (current.length >= CHILD_PI_MAX_BUFFER) {
        truncated = true;
        return current;
      }
      const next = current + String(data);
      if (next.length > CHILD_PI_MAX_BUFFER) {
        truncated = true;
        return next.slice(0, CHILD_PI_MAX_BUFFER);
      }
      return next;
    };
    child.stdout.on("data", (data) => {
      stdout = append(stdout, data);
      finishIfTerminal();
    });
    child.stderr.on("data", (data) => {
      stderr = append(stderr, data);
    });
    child.on("error", (error) => {
      finish({
        task: request.task,
        index: request.index,
        exitCode: 1,
        output: finalAssistantText(stdout),
        stderr,
        error: error.message,
      });
    });
    child.on("close", (code) => {
      finish({
        task: request.task,
        index: request.index,
        exitCode: truncated ? 1 : (code ?? 0),
        output: finalAssistantText(stdout),
        stderr: stderr.trim(),
        error: truncated ? "explorer task output exceeded buffer limit" : undefined,
      });
    });
  });
}

type FetchUrlResult = {
  url: string;
  status: number;
  contentType: string;
  text: string;
  truncated: boolean;
};

async function fetchPublicUrl(rawUrl: string): Promise<FetchUrlResult | string> {
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch {
    return "fetch-url failed: invalid URL";
  }

  for (let redirect = 0; redirect <= WEB_FETCH_MAX_REDIRECTS; redirect++) {
    const validationError = await validatePublicHttpUrl(url);
    if (validationError) return `fetch-url failed: ${validationError}`;

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), WEB_SEARCH_TIMEOUT_MS);
    let response: Response;
    try {
      response = await fetch(url, {
        headers: { "user-agent": "jri-explorer/1.0" },
        redirect: "manual",
        signal: controller.signal,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return `fetch-url failed: ${message}`;
    } finally {
      clearTimeout(timeout);
    }

    if (response.status >= 300 && response.status < 400) {
      const location = response.headers.get("location");
      if (!location) return `fetch-url failed: HTTP ${response.status} redirect without location`;
      url = new URL(location, url);
      continue;
    }

    if (!response.ok) return `fetch-url failed with HTTP ${response.status}`;

    const contentType = response.headers.get("content-type") ?? "";
    if (contentType && !isTextLikeContent(contentType)) {
      return `fetch-url failed: unsupported content-type ${contentType}`;
    }

    const body = await response.text();
    const cleaned = cleanFetchedText(body, contentType);
    return {
      url: url.toString(),
      status: response.status,
      contentType,
      text: cleaned.slice(0, WEB_FETCH_MAX_CHARS),
      truncated: cleaned.length > WEB_FETCH_MAX_CHARS,
    };
  }

  return `fetch-url failed: too many redirects (max ${WEB_FETCH_MAX_REDIRECTS})`;
}

async function validatePublicHttpUrl(url: URL): Promise<string | undefined> {
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return "only http and https URLs are allowed";
  }
  if (url.username || url.password) {
    return "URLs with credentials are not allowed";
  }
  if (!url.hostname) return "URL must include a host";
  if (isBlockedHostname(url.hostname)) return "private or local hosts are not allowed";

  const literalIpVersion = isIP(url.hostname);
  if (literalIpVersion !== 0) {
    return isPrivateAddress(url.hostname) ? "private or local hosts are not allowed" : undefined;
  }

  let addresses: { address: string }[];
  try {
    addresses = await lookup(url.hostname, { all: true });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return `could not resolve host: ${message}`;
  }
  if (addresses.length === 0) return "could not resolve host";
  if (addresses.some((entry) => isPrivateAddress(entry.address))) {
    return "private or local hosts are not allowed";
  }
  return undefined;
}

function isBlockedHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/\.$/, "");
  return normalized === "localhost" || normalized.endsWith(".localhost");
}

function isPrivateAddress(address: string): boolean {
  if (address === "::1" || address === "0:0:0:0:0:0:0:1") return true;
  if (address.startsWith("fe80:") || address.startsWith("fc") || address.startsWith("fd")) {
    return true;
  }

  const parts = address.split(".").map((part) => Number(part));
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part))) return false;
  const [a, b] = parts;
  return (
    a === 0 ||
    a === 10 ||
    a === 127 ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168)
  );
}

function isTextLikeContent(contentType: string): boolean {
  const normalized = contentType.toLowerCase();
  return (
    normalized.startsWith("text/") ||
    normalized.includes("application/json") ||
    normalized.includes("application/xml") ||
    normalized.includes("application/xhtml") ||
    normalized.includes("application/rss") ||
    normalized.includes("application/atom")
  );
}

function cleanFetchedText(body: string, contentType: string): string {
  if (!contentType.toLowerCase().includes("html")) {
    return body.replace(/\s+/g, " ").trim();
  }
  return decodeHtml(
    body
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
      .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]*>/g, " ")
      .replace(/\s+/g, " ")
      .trim(),
  );
}
