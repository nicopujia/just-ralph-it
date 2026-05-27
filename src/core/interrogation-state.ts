import { createHash } from "node:crypto";
import { mkdir, readdir, rename, stat, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { JriError } from "./errors";
import { parseJsonObject } from "./schema";

export type InterrogationTopicState = {
  specFile: `.jri/specs/${string}`;
  status: "open" | "sealed";
  lastReconciledSpecFingerprint?: string;
  pendingReconciliation?: PendingSpecReconciliation;
};

export type PendingSpecReconciliation = {
  reason: "manualSpecEdit" | "specFileDeleted";
  detectedAt: string;
  summary: string;
};

export type InterrogationState = {
  schemaVersion: 1;
  topics: Record<string, InterrogationTopicState>;
};

export type StartGateResult =
  | { ok: true; state: InterrogationState | null }
  | { ok: false; state: InterrogationState; pending: Array<{ topicId: string; topic: InterrogationTopicState }> };

export async function readInterrogationState(projectDir: string): Promise<InterrogationState | null> {
  const path = interrogationStatePath(projectDir);
  if (!(await pathExists(path))) return null;
  return validateInterrogationState(parseJsonObject(await Bun.file(path).text(), path), path);
}

export async function writeInterrogationState(projectDir: string, state: InterrogationState): Promise<void> {
  const path = interrogationStatePath(projectDir);
  validateInterrogationState(state as unknown as Record<string, unknown>, path);
  await mkdir(dirname(path), { recursive: true });
  await atomicWrite(path, `${JSON.stringify(state, null, 2)}\n`);
}

export async function checkInterrogationStartGate(projectDir: string, options: { now?: Date } = {}): Promise<StartGateResult> {
  const state = await readInterrogationState(projectDir);
  if (!state) return { ok: true, state };

  const now = (options.now ?? new Date()).toISOString();
  let changed = false;
  const next: InterrogationState = structuredClone(state);

  for (const topic of Object.values(next.topics)) {
    if (topic.pendingReconciliation) continue;
    if (topic.status !== "sealed" || !topic.lastReconciledSpecFingerprint) continue;

    const absoluteSpecPath = join(projectDir, topic.specFile);
    if (!(await pathExists(absoluteSpecPath))) {
      topic.status = "open";
      topic.pendingReconciliation = {
        reason: "specFileDeleted",
        detectedAt: now,
        summary: `${topic.specFile} was deleted after this topic was sealed. Confirm whether the requirement was removed or should be restored.`,
      };
      changed = true;
      continue;
    }

    const currentFingerprint = await fingerprintFile(absoluteSpecPath);
    if (currentFingerprint !== topic.lastReconciledSpecFingerprint) {
      topic.status = "open";
      topic.pendingReconciliation = {
        reason: "manualSpecEdit",
        detectedAt: now,
        summary: `${topic.specFile} changed after this topic was sealed. Reconcile the edit in chat before starting Ralph.`,
      };
      changed = true;
    }
  }

  if (changed) await writeInterrogationState(projectDir, next);

  const pending = Object.entries(next.topics)
    .filter(([, topic]) => Boolean(topic.pendingReconciliation))
    .map(([topicId, topic]) => ({ topicId, topic }));

  return pending.length > 0 ? { ok: false, state: next, pending } : { ok: true, state: next };
}

function validateInterrogationState(value: Record<string, unknown>, filePath: string): InterrogationState {
  rejectUnknownKeys(value, new Set(["schemaVersion", "topics"]), filePath);
  if (value.schemaVersion !== 1) {
    throw new JriError(`${filePath} uses an unsupported schemaVersion.`, "unsupported-interrogation-state-version", "Use schemaVersion 1.");
  }
  if (!isRecord(value.topics) || Array.isArray(value.topics)) {
    throw new JriError(`${filePath} topics must be an object.`, "invalid-interrogation-state", "Set topics to an object keyed by topic id.");
  }

  const topics: Record<string, InterrogationTopicState> = {};
  for (const [topicId, topic] of Object.entries(value.topics)) {
    if (!isRecord(topic) || Array.isArray(topic)) {
      throw new JriError(`${filePath} topic ${topicId} must be an object.`, "invalid-interrogation-state", "Set each topic to a valid topic object.");
    }
    rejectUnknownKeys(topic, new Set(["specFile", "status", "lastReconciledSpecFingerprint", "pendingReconciliation"]), filePath);
    if (typeof topic.specFile !== "string" || !topic.specFile.startsWith(".jri/specs/") || topic.specFile.endsWith("/")) {
      throw new JriError(`${filePath} topic ${topicId} has an invalid specFile.`, "invalid-interrogation-state", "Use a .jri/specs/* relative path.");
    }
    if (topic.status !== "open" && topic.status !== "sealed") {
      throw new JriError(`${filePath} topic ${topicId} has an invalid status.`, "invalid-interrogation-state", "Use open or sealed.");
    }
    if (topic.lastReconciledSpecFingerprint !== undefined && typeof topic.lastReconciledSpecFingerprint !== "string") {
      throw new JriError(
        `${filePath} topic ${topicId} has an invalid lastReconciledSpecFingerprint.`,
        "invalid-interrogation-state",
        "Use a string fingerprint.",
      );
    }
    topics[topicId] = {
      specFile: topic.specFile as `.jri/specs/${string}`,
      status: topic.status,
      ...(topic.lastReconciledSpecFingerprint ? { lastReconciledSpecFingerprint: topic.lastReconciledSpecFingerprint } : {}),
      ...(topic.pendingReconciliation ? { pendingReconciliation: validatePending(topic.pendingReconciliation, filePath, topicId) } : {}),
    };
  }

  return { schemaVersion: 1, topics };
}

function validatePending(value: unknown, filePath: string, topicId: string): PendingSpecReconciliation {
  if (!isRecord(value) || Array.isArray(value)) {
    throw new JriError(`${filePath} topic ${topicId} pendingReconciliation must be an object.`, "invalid-interrogation-state", "Set a valid reconciliation object.");
  }
  rejectUnknownKeys(value, new Set(["reason", "detectedAt", "summary"]), filePath);
  if (value.reason !== "manualSpecEdit" && value.reason !== "specFileDeleted") {
    throw new JriError(`${filePath} topic ${topicId} has an invalid reconciliation reason.`, "invalid-interrogation-state", "Use manualSpecEdit or specFileDeleted.");
  }
  if (typeof value.detectedAt !== "string" || value.detectedAt.length === 0) {
    throw new JriError(`${filePath} topic ${topicId} reconciliation needs detectedAt.`, "invalid-interrogation-state", "Set detectedAt to an ISO timestamp.");
  }
  if (typeof value.summary !== "string" || value.summary.length === 0) {
    throw new JriError(`${filePath} topic ${topicId} reconciliation needs summary.`, "invalid-interrogation-state", "Set a concise summary.");
  }
  return {
    reason: value.reason,
    detectedAt: value.detectedAt,
    summary: value.summary,
  };
}

export async function fingerprintSpecFile(projectDir: string, specFile: `.jri/specs/${string}`): Promise<string> {
  return await fingerprintFile(join(projectDir, specFile));
}

export async function listSpecFiles(projectDir: string): Promise<Array<`.jri/specs/${string}`>> {
  const specsDir = join(projectDir, ".jri", "specs");
  if (!(await pathExists(specsDir))) return [];
  const files: Array<`.jri/specs/${string}`> = [];
  for (const entry of await readdir(specsDir, { withFileTypes: true })) {
    if (entry.isFile()) files.push(`.jri/specs/${entry.name}`);
  }
  return files.sort();
}

function fingerprintFile(path: string): Promise<string> {
  return Bun.file(path)
    .arrayBuffer()
    .then((buffer) => createHash("sha256").update(Buffer.from(buffer)).digest("hex"));
}

function rejectUnknownKeys(value: Record<string, unknown>, allowed: Set<string>, filePath: string): void {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new JriError(`${filePath} has an unsupported key: ${key}.`, "invalid-interrogation-state", `Remove ${key} from ${filePath}.`);
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object");
}

function interrogationStatePath(projectDir: string): string {
  return join(projectDir, ".jri", "interrogation-state.json");
}

async function atomicWrite(path: string, contents: string): Promise<void> {
  const tmpPath = `${path}.${process.pid}.${Date.now()}.${crypto.randomUUID()}.tmp`;
  await writeFile(tmpPath, contents, "utf8");
  await rename(tmpPath, path);
}

async function pathExists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") return false;
    throw error;
  }
}
