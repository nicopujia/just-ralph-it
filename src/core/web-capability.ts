import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { webCapabilityDescriptor } from "./capabilities";
import type { CapabilityInvocationMetadata } from "./capability-ownership";
import { JriError } from "./errors";

const maxSearchResults = webCapabilityDescriptor.limits.searchResults;
const maxFetchExcerptChars = webCapabilityDescriptor.limits.fetchMarkdownChars;
const maxFetchArtifactBytes = webCapabilityDescriptor.limits.artifactBytes;
const defaultFetchTimeoutMs = webCapabilityDescriptor.limits.fetchTimeoutMs;
const defaultRedirectLimit = webCapabilityDescriptor.limits.redirects;

export type WebSearchResult = {
  title: string;
  url: string;
  snippet: string;
  retrievedAt: string;
};

export type WebFetchResult = {
  url: string;
  title?: string;
  fetchedAt: string;
  markdown: string;
  artifactRef?: string;
  omittedBytes?: number;
};

export type WebCapabilityOptions = {
  projectDir: string;
  owner: CapabilityInvocationMetadata["owner"];
  env?: NodeJS.ProcessEnv;
  signal?: AbortSignal;
};

export async function runWebSearch(
  options: WebCapabilityOptions & { query: string; limit?: number },
): Promise<WebSearchResult[]> {
  const query = options.query.trim();
  if (!query) {
    throw new JriError("Web search query must not be empty.", "invalid-web-query", "Pass a focused search query.");
  }

  const limit = Math.min(Math.max(Math.trunc(options.limit ?? maxSearchResults), 1), maxSearchResults);
  const parsed = await runWebJsonCommand(options, ["search", "--query", query, "--limit", String(limit)]);
  if (!Array.isArray(parsed.results)) {
    throw invalidWebShapeError("search response must include a results array.");
  }
  const retrievedAt = stringField(parsed, "retrievedAt");

  return parsed.results.slice(0, limit).map((result: unknown, index: number) => normalizeSearchResult(result, retrievedAt, index));
}

export async function runWebFetch(
  options: WebCapabilityOptions & { url: string; timeoutMs?: number; redirectLimit?: number },
): Promise<WebFetchResult> {
  const url = options.url.trim();
  if (!url) {
    throw new JriError("Web fetch URL must not be empty.", "invalid-web-url", "Pass an absolute URL to fetch.");
  }

  const timeoutMs = Math.min(Math.max(Math.trunc(options.timeoutMs ?? defaultFetchTimeoutMs), 1_000), defaultFetchTimeoutMs);
  const redirectLimit = Math.min(Math.max(Math.trunc(options.redirectLimit ?? defaultRedirectLimit), 0), defaultRedirectLimit);
  const parsed = await runWebJsonCommand(
    options,
    [
      "fetch",
      "--url",
      url,
      "--timeout-ms",
      String(timeoutMs),
      "--redirects",
      String(redirectLimit),
      "--format",
      "markdown",
    ],
    timeoutMs,
  );

  const markdown = stringField(parsed, "markdown") ?? stringField(parsed, "content") ?? "";
  const fetchedAt = stringField(parsed, "fetchedAt") ?? new Date().toISOString();
  const sourceUrl = stringField(parsed, "url") ?? url;
  const title = stringField(parsed, "title");
  const bytes = encodeUtf8(markdown);
  const artifactMarkdown = truncateUtf8ByBytes(markdown, maxFetchArtifactBytes);
  const excerpt = truncateUnicode(markdown, maxFetchExcerptChars);
  const needsArtifact = markdown.length > excerpt.length || bytes.length > maxFetchArtifactBytes;

  if (!needsArtifact) {
    return { url: sourceUrl, ...(title ? { title } : {}), fetchedAt, markdown: excerpt };
  }

  const artifactRef = await writeWebArtifact(options.projectDir, options.owner, sourceUrl, artifactMarkdown);
  return {
    url: sourceUrl,
    ...(title ? { title } : {}),
    fetchedAt,
    markdown: excerpt,
    artifactRef,
    omittedBytes: Math.max(0, bytes.length - encodeUtf8(excerpt).length),
  };
}

async function runWebJsonCommand(
  options: WebCapabilityOptions,
  args: string[],
  timeoutMs = defaultFetchTimeoutMs,
): Promise<Record<string, unknown>> {
  const env = options.env ?? process.env;
  const command = env.JRI_PI_WEB_COMMAND ?? "pi-web-access";
  if (options.signal?.aborted) {
    throw webCancelledError();
  }
  const proc = Bun.spawn([command, ...args, "--json"], {
    cwd: options.projectDir,
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
    env,
  });
  let timedOut = false;
  let cancelled = false;
  let forceKill: Timer | undefined;
  const terminate = (reason: "timeout" | "cancelled"): void => {
    if (reason === "timeout") timedOut = true;
    if (reason === "cancelled") cancelled = true;
    proc.kill("SIGTERM");
    forceKill = setTimeout(() => proc.kill("SIGKILL"), 250);
  };
  const timeout = setTimeout(() => terminate("timeout"), timeoutMs);
  const abort = (): void => terminate("cancelled");
  options.signal?.addEventListener("abort", abort, { once: true });
  let stdout = "";
  let stderr = "";
  let exitCode = 0;
  try {
    [stdout, stderr, exitCode] = await Promise.all([streamText(proc.stdout), streamText(proc.stderr), proc.exited]);
  } finally {
    clearTimeout(timeout);
    if (forceKill) clearTimeout(forceKill);
    options.signal?.removeEventListener("abort", abort);
  }
  if (cancelled) {
    throw webCancelledError();
  }
  if (timedOut) {
    throw new JriError(
      `JRI web capability timed out after ${timeoutMs}ms.`,
      "web-capability-timeout",
      "Retry when web access is responsive, or continue only with a clearly labeled degraded answer if current facts are not required.",
    );
  }
  if (exitCode !== 0) {
    throw new JriError(
      `JRI web capability failed with exit code ${exitCode}.`,
      "web-capability-failed",
      stderr.trim() || "Check that JRI web access is available, then retry or continue with a clearly labeled degraded answer if safe.",
    );
  }
  try {
    const parsed = JSON.parse(stdout);
    if (!parsed || typeof parsed !== "object") throw new Error("JSON root is not an object.");
    return parsed as Record<string, unknown>;
  } catch (error) {
    throw new JriError(
      "JRI web capability returned invalid JSON.",
      "web-capability-invalid-json",
      error instanceof Error ? error.message : "Retry after fixing the web capability wrapper output.",
    );
  }
}

function webCancelledError(): JriError {
  return new JriError(
    "JRI web capability was cancelled.",
    "web-capability-cancelled",
    "Retry from the active Ralph loop if web access is still needed.",
  );
}

function normalizeSearchResult(value: unknown, fallbackRetrievedAt: string | undefined, index: number): WebSearchResult {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw invalidWebShapeError(`search result ${index + 1} must be an object.`);
  }
  const record = value as Record<string, unknown>;
  const fields = {
    title: stringField(record, "title"),
    url: stringField(record, "url"),
    snippet: stringField(record, "snippet"),
    retrievedAt: stringField(record, "retrievedAt") ?? fallbackRetrievedAt,
  };
  const missing = Object.entries(fields)
    .filter(([, fieldValue]) => fieldValue === undefined)
    .map(([fieldName]) => fieldName);
  if (missing.length > 0) {
    throw invalidWebShapeError(`search result ${index + 1} is missing string field(s): ${missing.join(", ")}.`);
  }
  return {
    title: fields.title!,
    url: fields.url!,
    snippet: fields.snippet!,
    retrievedAt: fields.retrievedAt!,
  };
}

function invalidWebShapeError(detail: string): JriError {
  return new JriError(
    `JRI web capability returned an invalid search result shape: ${detail}`,
    "web-capability-invalid-shape",
    "Check the JRI web access wrapper output and retry.",
  );
}

function stringField(record: Record<string, unknown>, key: string): string | undefined {
  const value = record[key];
  return typeof value === "string" ? value : undefined;
}

async function writeWebArtifact(projectDir: string, owner: CapabilityInvocationMetadata["owner"], url: string, markdown: string): Promise<string> {
  const artifactRef =
    owner.kind === "chat"
      ? `.jri/logs/interrogation-artifacts/web-${owner.turnId}-${crypto.randomUUID()}.md`
      : `.jri/logs/${owner.loopId}/artifacts/web-${crypto.randomUUID()}.md`;
  const absolutePath = join(projectDir, artifactRef);
  await mkdir(dirname(absolutePath), { recursive: true });
  await writeFile(absolutePath, [`Source: ${url}`, "", markdown].join("\n"), "utf8");
  return artifactRef;
}

function truncateUnicode(value: string, maxChars: number): string {
  return Array.from(value).slice(0, maxChars).join("");
}

function truncateUtf8ByBytes(value: string, maxBytes: number): string {
  if (encodeUtf8(value).length <= maxBytes) return value;
  const chars = Array.from(value);
  let low = 0;
  let high = chars.length;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (encodeUtf8(chars.slice(0, mid).join("")).length <= maxBytes) {
      low = mid;
    } else {
      high = mid - 1;
    }
  }
  return chars.slice(0, low).join("");
}

function encodeUtf8(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}

async function streamText(stream: ReadableStream<Uint8Array>): Promise<string> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let text = "";
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    text += decoder.decode(chunk.value, { stream: true });
  }
  return text + decoder.decode();
}
