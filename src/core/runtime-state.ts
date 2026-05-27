import { appendFile, mkdir, readdir, rename, stat, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { JriError } from "./errors";
import { parseJsonObject, validateStatus } from "./schema";
import type { BlockerReason, CoreEvent, LockOperation, ProjectState, ProjectStatus } from "./types";

const activeStates = new Set<ProjectState>(["auditing", "planning", "building"]);

export type StatusMutator = (status: ProjectStatus) => ProjectStatus | Promise<ProjectStatus>;
export type StatusPatch = { [K in keyof ProjectStatus]?: ProjectStatus[K] | undefined };

export async function readStatus(projectDir: string): Promise<ProjectStatus> {
  const path = statusPath(projectDir);
  if (!(await Bun.file(path).exists())) {
    throw new JriError("JRI status does not exist yet.", "uninitialized", "Run bare jri or call ensureInitialized() to create the scaffold.");
  }
  return validateStatus(parseJsonObject(await Bun.file(path).text(), path), path);
}

export async function writeStatusAtomic(projectDir: string, status: ProjectStatus): Promise<void> {
  const path = statusPath(projectDir);
  validateStatus(status as unknown as Record<string, unknown>, path);
  await mkdir(dirname(path), { recursive: true });
  await atomicWrite(path, `${JSON.stringify(status, null, 2)}\n`);
}

export async function updateStatus(projectDir: string, mutate: StatusMutator): Promise<ProjectStatus> {
  const current = await readStatus(projectDir);
  const next = await mutate(structuredClone(current));
  await writeStatusAtomic(projectDir, next);
  return next;
}

export async function transitionStatus(
  projectDir: string,
  nextState: ProjectState,
  options: {
    loopId?: string;
    blockerReason?: BlockerReason;
    now?: Date;
    update?: StatusPatch;
  } = {},
): Promise<ProjectStatus> {
  return await updateStatus(projectDir, (current) => {
    assertLegalTransition(current, nextState, options.blockerReason);
    const activeLoopId = nextState === "idle" ? null : (options.loopId ?? current.activeLoopId);
    if (nextState !== "idle" && !activeLoopId) {
      throw new JriError(
        `Cannot enter ${nextState} without an active loop id.`,
        "missing-loop-id",
        "Generate or preserve a loop id before moving into a loop lifecycle state.",
      );
    }

    const timestamp = (options.now ?? new Date()).toISOString();
    const patched = applyStatusPatch(current, options.update);
    return {
      ...patched,
      state: nextState,
      activeLoopId,
      ...(activeLoopId ? { lastLoopId: activeLoopId } : {}),
      ...(nextState === "idle" || nextState === "stopped" || nextState === "halted" ? { finishedAt: timestamp } : {}),
    };
  });
}

function applyStatusPatch(status: ProjectStatus, patch?: StatusPatch): ProjectStatus {
  if (!patch) return status;
  const next: Record<string, unknown> = { ...status };
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) delete next[key];
    else next[key] = value;
  }
  return next as ProjectStatus;
}

export function assertLegalTransition(current: ProjectStatus, nextState: ProjectState, blockerReason = current.blocker?.reason): void {
  const allowed = legalNextStates(current.state, blockerReason);
  if (!allowed.has(nextState)) {
    throw new JriError(
      `Illegal JRI status transition: ${current.state} -> ${nextState}.`,
      "illegal-status-transition",
      `Use one of the legal next states from ${current.state}: ${[...allowed].join(", ")}.`,
    );
  }
}

export async function generateLoopId(projectDir: string, now = new Date()): Promise<string> {
  const base = toLoopSlug(now);
  for (let suffix = 1; ; suffix += 1) {
    const loopId = suffix === 1 ? base : `${base}-${suffix}`;
    if (!(await pathExists(join(projectDir, ".jri", "logs", loopId)))) return loopId;
  }
}

export async function acquireLock(
  projectDir: string,
  operation: LockOperation,
  options: { pid?: number; now?: Date; ttlMs?: number; isProcessAlive?: (pid: number) => boolean } = {},
): Promise<NonNullable<ProjectStatus["lock"]>> {
  const pid = options.pid ?? process.pid;
  const now = options.now ?? new Date();
  const ttlMs = options.ttlMs ?? 30_000;
  const acquiredAt = now.toISOString();
  const lock: NonNullable<ProjectStatus["lock"]> = {
    owner: "daemon",
    pid,
    operation,
    acquiredAt,
    heartbeatAt: acquiredAt,
    expiresAt: new Date(now.getTime() + ttlMs).toISOString(),
  };

  await updateStatus(projectDir, (status) => {
    if (status.lock && !isStaleLock(status.lock, now, options.isProcessAlive)) {
      throw new JriError(
        `JRI is already running ${status.lock.operation}.`,
        "lock-held",
        `Wait for ${status.lock.operation} to finish, attach to the active loop, or retry after ${status.lock.expiresAt}.`,
      );
    }
    return { ...status, lock };
  });

  const confirmed = await readStatus(projectDir);
  if (!locksMatch(confirmed.lock, lock)) {
    throw new JriError("Failed to acquire the JRI project lock.", "lock-race", "Retry the command; another JRI process changed status concurrently.");
  }
  return lock;
}

export async function heartbeatLock(
  projectDir: string,
  expected: NonNullable<ProjectStatus["lock"]>,
  options: { now?: Date; ttlMs?: number } = {},
): Promise<NonNullable<ProjectStatus["lock"]>> {
  const now = options.now ?? new Date();
  const heartbeatAt = now.toISOString();
  const nextLock = {
    ...expected,
    heartbeatAt,
    expiresAt: new Date(now.getTime() + (options.ttlMs ?? 30_000)).toISOString(),
  };
  await updateStatus(projectDir, (status) => {
    if (!locksMatch(status.lock, expected)) {
      throw new JriError("Cannot heartbeat a lock owned by another process.", "lock-lost", "Reload project status before continuing the operation.");
    }
    return { ...status, lock: nextLock };
  });
  return nextLock;
}

export async function releaseLock(projectDir: string, expected: NonNullable<ProjectStatus["lock"]>): Promise<void> {
  await updateStatus(projectDir, (status) => {
    if (!locksMatch(status.lock, expected)) {
      throw new JriError("Cannot release a lock owned by another process.", "lock-lost", "Reload project status before continuing the operation.");
    }
    const { lock, ...rest } = status;
    void lock;
    return rest;
  });
}

export async function appendLoopEvent(projectDir: string, event: Omit<CoreEvent, "id" | "sequence" | "timestamp"> & Partial<Pick<CoreEvent, "id" | "timestamp">>): Promise<CoreEvent> {
  if (!("loopId" in event) || !event.loopId) {
    throw new JriError("Loop events require a loop id.", "missing-loop-id", "Generate a loop id before appending loop lifecycle events.");
  }
  const persisted = {
    ...event,
    id: event.id ?? crypto.randomUUID(),
    sequence: await nextEventSequence(projectDir),
    timestamp: event.timestamp ?? new Date().toISOString(),
  } as CoreEvent;
  const path = join(projectDir, ".jri", "logs", event.loopId, "events.jsonl");
  await mkdir(dirname(path), { recursive: true });
  await appendFile(path, `${JSON.stringify(persisted)}\n`, "utf8");
  return persisted;
}

export async function appendInterrogationEvent(
  projectDir: string,
  event: Omit<CoreEvent, "id" | "sequence" | "timestamp"> & Partial<Pick<CoreEvent, "id" | "timestamp">>,
): Promise<CoreEvent> {
  const persisted = {
    ...event,
    id: event.id ?? crypto.randomUUID(),
    sequence: await nextEventSequence(projectDir),
    timestamp: event.timestamp ?? new Date().toISOString(),
  } as CoreEvent;
  const path = join(projectDir, ".jri", "logs", "interrogation.jsonl");
  await mkdir(dirname(path), { recursive: true });
  await appendFile(path, `${JSON.stringify(persisted)}\n`, "utf8");
  return persisted;
}

export async function nextEventSequence(projectDir: string): Promise<number> {
  let max = 0;
  const logsDir = join(projectDir, ".jri", "logs");
  if (!(await pathExists(logsDir))) return 1;

  for (const entry of await readdir(logsDir, { withFileTypes: true })) {
    const path = entry.isDirectory() ? join(logsDir, entry.name, "events.jsonl") : entry.name === "interrogation.jsonl" ? join(logsDir, entry.name) : null;
    if (!path || !(await Bun.file(path).exists())) continue;
    max = Math.max(max, await maxSequenceInJsonl(path));
  }
  return max + 1;
}

export function isActiveState(state: ProjectState): boolean {
  return activeStates.has(state);
}

function legalNextStates(state: ProjectState, blockerReason?: BlockerReason): Set<ProjectState> {
  if (state === "idle") return new Set(["auditing"]);
  if (state === "auditing") return new Set(["planning", "blocked", "stopped", "halted"]);
  if (state === "planning") return new Set(["building", "blocked", "stopped", "halted"]);
  if (state === "building") return new Set(["planning", "blocked", "stopped", "halted", "idle"]);
  if (state === "blocked" && blockerReason === "ambiguousSpecs") return new Set(["auditing"]);
  if (state === "blocked" && blockerReason === "needsHumanTask") return new Set(["building", "planning"]);
  if (state === "stopped") return new Set(["auditing", "building", "planning"]);
  return new Set(["auditing", "halted"]);
}

function isStaleLock(
  lock: NonNullable<ProjectStatus["lock"]>,
  now: Date,
  isProcessAlive: (pid: number) => boolean = defaultProcessAlive,
): boolean {
  return Date.parse(lock.expiresAt) <= now.getTime() && !isProcessAlive(lock.pid);
}

function defaultProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "EPERM") return true;
    return false;
  }
}

function locksMatch(left: ProjectStatus["lock"], right: ProjectStatus["lock"]): boolean {
  return Boolean(
    left &&
      right &&
      left.owner === right.owner &&
      left.pid === right.pid &&
      left.operation === right.operation &&
      left.acquiredAt === right.acquiredAt,
  );
}

function toLoopSlug(date: Date): string {
  return date.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

async function maxSequenceInJsonl(path: string): Promise<number> {
  const text = await Bun.file(path).text();
  let max = 0;
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const parsed = JSON.parse(line) as { sequence?: unknown };
    if (typeof parsed.sequence === "number" && Number.isInteger(parsed.sequence)) {
      max = Math.max(max, parsed.sequence);
    }
  }
  return max;
}

function statusPath(projectDir: string): string {
  return join(projectDir, ".jri", "status.json");
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
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
      return false;
    }
    throw error;
  }
}
