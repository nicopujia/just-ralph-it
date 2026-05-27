import { createHash } from "node:crypto";
import { mkdir, readdir, rename, stat, writeFile } from "node:fs/promises";
import { basename, dirname, extname, join } from "node:path";
import { JriError } from "./errors";
import { parseJsonObject } from "./schema";

export type InterrogationTopicState = {
  specFile: `.jri/specs/${string}`;
  status: "open" | "sealed";
  lastReconciledSpecFingerprint?: string;
  scratchpadClearance?: ScratchpadClearance;
  pendingReconciliation?: PendingSpecReconciliation;
};

export type ScratchpadClearance = {
  scratchpadFingerprint: string;
  recordedAt: string;
  summary: string;
};

export type PendingSpecReconciliation = {
  reason: "manualSpecEdit" | "specFileDeleted" | "specFileAdded" | "scratchpadClearanceMissing" | "scratchpadChanged";
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
  const scratchpadFingerprint = await fingerprintScratchpad(projectDir);
  let changed = false;
  const next: InterrogationState = structuredClone(state);

  for (const topic of Object.values(next.topics)) {
    if (topic.pendingReconciliation) continue;

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

    if (!topic.lastReconciledSpecFingerprint) {
      topic.status = "open";
      topic.pendingReconciliation = {
        reason: "manualSpecEdit",
        detectedAt: now,
        summary: `${topic.specFile} was tracked without a reconciled fingerprint. Reconcile the current spec in chat before starting Ralph.`,
      };
      changed = true;
      continue;
    }

    const wasSealed = topic.status === "sealed";
    const currentFingerprint = await fingerprintFile(absoluteSpecPath);
    if (currentFingerprint !== topic.lastReconciledSpecFingerprint) {
      topic.status = "open";
      topic.pendingReconciliation = {
        reason: "manualSpecEdit",
        detectedAt: now,
        summary: wasSealed
          ? `${topic.specFile} changed after this topic was sealed. Reconcile the edit in chat before starting Ralph.`
          : `${topic.specFile} changed after the last interrogator reconciliation. Reconcile the edit in chat before starting Ralph.`,
      };
      changed = true;
      continue;
    }

    if (!topic.scratchpadClearance) {
      topic.status = "open";
      topic.pendingReconciliation = {
        reason: "scratchpadClearanceMissing",
        detectedAt: now,
        summary: `${topic.specFile} has no scratchpad clearance evidence. Reconcile or defer related scratchpad notes in chat before starting Ralph.`,
      };
      changed = true;
      continue;
    }

    if (topic.scratchpadClearance.scratchpadFingerprint !== scratchpadFingerprint) {
      topic.status = "open";
      topic.pendingReconciliation = {
        reason: "scratchpadChanged",
        detectedAt: now,
        summary: `.jri/scratchpad.md changed after ${topic.specFile} was last reconciled. Resolve the note into specs or explicitly defer it before starting Ralph.`,
      };
      changed = true;
    }
  }

  const knownSpecFiles = new Set(Object.values(next.topics).map((topic) => topic.specFile));
  for (const specFile of await listSpecFiles(projectDir)) {
    if (knownSpecFiles.has(specFile)) continue;

    const topicId = uniqueTopicId(next, topicIdForSpecFile(specFile));
    next.topics[topicId] = {
      specFile,
      status: "open",
      lastReconciledSpecFingerprint: await fingerprintSpecFile(projectDir, specFile),
      pendingReconciliation: {
        reason: "specFileAdded",
        detectedAt: now,
        summary: `${specFile} was added outside the interrogator. Confirm whether it belongs in the current build scope before starting Ralph.`,
      },
    };
    knownSpecFiles.add(specFile);
    changed = true;
  }

  if (changed) await writeInterrogationState(projectDir, next);

  const pending = Object.entries(next.topics)
    .filter(([, topic]) => Boolean(topic.pendingReconciliation))
    .map(([topicId, topic]) => ({ topicId, topic }));

  return pending.length > 0 ? { ok: false, state: next, pending } : { ok: true, state: next };
}

export async function recordInterrogatorSpecUpdate(
  projectDir: string,
  specFiles: string[],
  options: { sealedSpecFiles?: string[]; now?: Date } = {},
): Promise<InterrogationState> {
  const existing = await readInterrogationState(projectDir);
  const next: InterrogationState = structuredClone(existing ?? { schemaVersion: 1, topics: {} });
  const sealedSpecFiles = new Set((options.sealedSpecFiles ?? []).map(validateSpecFilePath));
  const filesToRecord = new Set([...specFiles.map(validateSpecFilePath), ...sealedSpecFiles]);
  const scratchpadFingerprint = await fingerprintScratchpad(projectDir);
  const recordedAt = (options.now ?? new Date()).toISOString();

  for (const stableSpecFile of filesToRecord) {
    const absoluteSpecPath = join(projectDir, stableSpecFile);
    if (!(await pathExists(absoluteSpecPath))) {
      const current = findTopicEntry(next, stableSpecFile);
      if (current?.topic.pendingReconciliation?.reason === "specFileDeleted" && specFiles.includes(stableSpecFile) && !sealedSpecFiles.has(stableSpecFile)) {
        delete next.topics[current.topicId];
        continue;
      }

      throw new JriError(
        `The interrogator reported ${stableSpecFile} as updated, but the file does not exist.`,
        "missing-updated-spec",
        "Write the spec file before emitting a specsUpdated handoff, or report the deleted file only while resolving a pending specFileDeleted reconciliation.",
      );
    }

    const topicId = topicIdForSpecFile(stableSpecFile);
    const current = findTopicEntry(next, stableSpecFile);
    const fingerprint = await fingerprintSpecFile(projectDir, stableSpecFile);
    const prior = current ? current.topic : next.topics[topicId];
    const targetTopicId = current?.topicId ?? uniqueTopicId(next, topicId);

    next.topics[targetTopicId] = {
      specFile: stableSpecFile,
      status: sealedSpecFiles.has(stableSpecFile) || (prior?.status === "sealed" && !prior.pendingReconciliation) ? "sealed" : "open",
      lastReconciledSpecFingerprint: fingerprint,
      scratchpadClearance: {
        scratchpadFingerprint,
        recordedAt,
        summary: sealedSpecFiles.has(stableSpecFile)
          ? `The interrogator sealed ${stableSpecFile} with scratchpad notes resolved or deferred out of current build scope.`
          : `The interrogator reconciled ${stableSpecFile} with current scratchpad notes accounted for.`,
      },
    };
  }

  await writeInterrogationState(projectDir, next);
  return next;
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
    rejectUnknownKeys(topic, new Set(["specFile", "status", "lastReconciledSpecFingerprint", "scratchpadClearance", "pendingReconciliation"]), filePath);
    if (typeof topic.specFile !== "string" || !isStableSpecFilePath(topic.specFile)) {
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
      ...(topic.scratchpadClearance ? { scratchpadClearance: validateScratchpadClearance(topic.scratchpadClearance, filePath, topicId) } : {}),
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
  if (
    value.reason !== "manualSpecEdit" &&
    value.reason !== "specFileDeleted" &&
    value.reason !== "specFileAdded" &&
    value.reason !== "scratchpadClearanceMissing" &&
    value.reason !== "scratchpadChanged"
  ) {
    throw new JriError(
      `${filePath} topic ${topicId} has an invalid reconciliation reason.`,
      "invalid-interrogation-state",
      "Use manualSpecEdit, specFileDeleted, specFileAdded, scratchpadClearanceMissing, or scratchpadChanged.",
    );
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

function validateScratchpadClearance(value: unknown, filePath: string, topicId: string): ScratchpadClearance {
  if (!isRecord(value) || Array.isArray(value)) {
    throw new JriError(`${filePath} topic ${topicId} scratchpadClearance must be an object.`, "invalid-interrogation-state", "Set a valid scratchpad clearance object.");
  }
  rejectUnknownKeys(value, new Set(["scratchpadFingerprint", "recordedAt", "summary"]), filePath);
  if (typeof value.scratchpadFingerprint !== "string" || value.scratchpadFingerprint.length === 0) {
    throw new JriError(
      `${filePath} topic ${topicId} scratchpadClearance needs scratchpadFingerprint.`,
      "invalid-interrogation-state",
      "Record the scratchpad fingerprint that was cleared or deferred.",
    );
  }
  if (typeof value.recordedAt !== "string" || value.recordedAt.length === 0) {
    throw new JriError(`${filePath} topic ${topicId} scratchpadClearance needs recordedAt.`, "invalid-interrogation-state", "Set recordedAt to an ISO timestamp.");
  }
  if (typeof value.summary !== "string" || value.summary.length === 0) {
    throw new JriError(`${filePath} topic ${topicId} scratchpadClearance needs summary.`, "invalid-interrogation-state", "Set a concise summary.");
  }
  return {
    scratchpadFingerprint: value.scratchpadFingerprint,
    recordedAt: value.recordedAt,
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

async function fingerprintScratchpad(projectDir: string): Promise<string> {
  const path = join(projectDir, ".jri", "scratchpad.md");
  if (!(await pathExists(path))) return createHash("sha256").update("").digest("hex");
  return await fingerprintFile(path);
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

function validateSpecFilePath(specFile: string): `.jri/specs/${string}` {
  if (!isStableSpecFilePath(specFile)) {
    throw new JriError(
      `Invalid interrogator spec path: ${specFile}`,
      "invalid-spec-path",
      "Use a stable .jri/specs/<file> relative path.",
    );
  }
  return specFile as `.jri/specs/${string}`;
}

function isStableSpecFilePath(specFile: string): specFile is `.jri/specs/${string}` {
  const relative = specFile.slice(".jri/specs/".length);
  return (
    specFile.startsWith(".jri/specs/") &&
    relative.length > 0 &&
    !relative.includes("/") &&
    !relative.includes("\\") &&
    relative !== "." &&
    relative !== ".." &&
    !relative.includes("..")
  );
}

function topicIdForSpecFile(specFile: `.jri/specs/${string}`): string {
  const filename = basename(specFile, extname(specFile));
  const normalized = filename
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return normalized || "topic";
}

function findTopicEntry(state: InterrogationState, specFile: `.jri/specs/${string}`): { topicId: string; topic: InterrogationTopicState } | undefined {
  for (const [topicId, topic] of Object.entries(state.topics)) {
    if (topic.specFile === specFile) return { topicId, topic };
  }
  return undefined;
}

function uniqueTopicId(state: InterrogationState, topicId: string): string {
  if (!state.topics[topicId]) return topicId;
  for (let suffix = 2; ; suffix += 1) {
    const candidate = `${topicId}-${suffix}`;
    if (!state.topics[candidate]) return candidate;
  }
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
