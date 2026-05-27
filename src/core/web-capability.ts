import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { webCapabilityDescriptor } from "./capabilities";
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
  loopId: string;
  env?: NodeJS.ProcessEnv;
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
  const rawResults = Array.isArray(parsed) ? parsed : Array.isArray(parsed.results) ? parsed.results : [];
  const retrievedAt = typeof parsed.retrievedAt === "string" ? parsed.retrievedAt : new Date().toISOString();

  return rawResults.slice(0, limit).map((result: unknown) => normalizeSearchResult(result, retrievedAt));
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
  const parsed = await runWebJsonCommand(options, [
    "fetch",
    "--url",
    url,
    "--timeout-ms",
    String(timeoutMs),
    "--redirects",
    String(redirectLimit),
    "--format",
    "markdown",
  ]);

  const markdown = stringField(parsed, "markdown") ?? stringField(parsed, "content") ?? "";
  const fetchedAt = stringField(parsed, "fetchedAt") ?? new Date().toISOString();
  const sourceUrl = stringField(parsed, "url") ?? url;
  const title = stringField(parsed, "title");
  const bytes = new TextEncoder().encode(markdown);
  const excerpt = new TextDecoder().decode(bytes.slice(0, maxFetchArtifactBytes)).slice(0, maxFetchExcerptChars);
  const needsArtifact = markdown.length > excerpt.length || bytes.length > maxFetchArtifactBytes;

  if (!needsArtifact) {
    return { url: sourceUrl, ...(title ? { title } : {}), fetchedAt, markdown: excerpt };
  }

  const artifactRef = await writeWebArtifact(options.projectDir, options.loopId, sourceUrl, bytes.slice(0, maxFetchArtifactBytes));
  return {
    url: sourceUrl,
    ...(title ? { title } : {}),
    fetchedAt,
    markdown: excerpt,
    artifactRef,
    omittedBytes: Math.max(0, bytes.length - new TextEncoder().encode(excerpt).length),
  };
}

async function runWebJsonCommand(options: WebCapabilityOptions, args: string[]): Promise<Record<string, unknown>> {
  const env = options.env ?? process.env;
  const command = env.JRI_PI_WEB_COMMAND ?? "pi-web-access";
  const proc = Bun.spawn([command, ...args, "--json"], {
    cwd: options.projectDir,
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
    env,
  });
  const [stdout, stderr, exitCode] = await Promise.all([streamText(proc.stdout), streamText(proc.stderr), proc.exited]);
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

function normalizeSearchResult(value: unknown, fallbackRetrievedAt: string): WebSearchResult {
  const record = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  return {
    title: stringField(record, "title") ?? "",
    url: stringField(record, "url") ?? "",
    snippet: stringField(record, "snippet") ?? "",
    retrievedAt: stringField(record, "retrievedAt") ?? fallbackRetrievedAt,
  };
}

function stringField(record: Record<string, unknown>, key: string): string | undefined {
  const value = record[key];
  return typeof value === "string" ? value : undefined;
}

async function writeWebArtifact(projectDir: string, loopId: string, url: string, bytes: Uint8Array): Promise<string> {
  const artifactRef = `.jri/logs/${loopId}/artifacts/web-${crypto.randomUUID()}.md`;
  const absolutePath = join(projectDir, artifactRef);
  await mkdir(dirname(absolutePath), { recursive: true });
  await writeFile(absolutePath, [`Source: ${url}`, "", new TextDecoder().decode(bytes)].join("\n"), "utf8");
  return artifactRef;
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
