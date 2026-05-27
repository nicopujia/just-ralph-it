import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { JriError } from "./errors";
import { appendLoopEvent, isActiveState, readStatus, updateStatus, writeStatusAtomic } from "./runtime-state";
import type { CoreEvent, ProjectStatus } from "./types";

export type ProcessAliveCheck = (pid: number) => boolean;
export type ProcessKiller = (pid: number) => void;

export type RuntimeOptions = {
  now?: Date;
  isProcessAlive?: ProcessAliveCheck;
  killProcess?: ProcessKiller;
};

export async function getRecoveredStatus(projectDir: string, options: RuntimeOptions = {}): Promise<ProjectStatus> {
  const { status } = await recoverRuntimeStatus(projectDir, options);
  return status;
}

export async function recoverRuntimeStatus(
  projectDir: string,
  options: RuntimeOptions = {},
): Promise<{ status: ProjectStatus; repairedEvent?: CoreEvent }> {
  const now = options.now ?? new Date();
  const isProcessAlive = options.isProcessAlive ?? defaultProcessAlive;
  const status = await readStatus(projectDir);
  const processDead = status.process ? !isProcessAlive(status.process.pid) : false;
  const staleLock = status.lock ? Date.parse(status.lock.expiresAt) <= now.getTime() && !isProcessAlive(status.lock.pid) : false;

  if (!processDead && !staleLock) {
    return { status };
  }

  const repairedFrom = status.state;
  const reason = repairReason(status, processDead, staleLock);
  const timestamp = now.toISOString();
  const { process, lock, ...withoutRuntimeOwnership } = status;
  void process;
  void lock;

  const repaired: ProjectStatus = {
    ...withoutRuntimeOwnership,
    ...(isActiveState(status.state)
      ? {
          state: "stopped",
          finishedAt: timestamp,
          stopRequested: false,
          lastResult: {
            outcome: "failed",
            summary: reason,
          },
        }
      : {}),
    recoveryNote: {
      timestamp,
      message: reason,
      repairedFrom,
    },
  };

  await writeStatusAtomic(projectDir, repaired);
  const repairedEvent = await appendRecoveryEvent(projectDir, repairedFrom, repaired.state, reason);
  return repairedEvent ? { status: repaired, repairedEvent } : { status: repaired };
}

export async function* observeLoop(projectDir: string, options: RuntimeOptions = {}): AsyncIterable<CoreEvent> {
  const { status, repairedEvent } = await recoverRuntimeStatus(projectDir, options);
  if (repairedEvent) yield repairedEvent;

  const loopId = status.activeLoopId ?? status.lastLoopId;
  if (!loopId) {
    throw new JriError("There is no JRI loop to observe.", "no-loop", "Start Ralph from bare jri after specs are ready.");
  }

  for (const event of await readLoopEvents(projectDir, loopId)) {
    yield event;
  }
}

export async function requestGracefulStop(projectDir: string, options: RuntimeOptions = {}): Promise<CoreEvent> {
  const { status } = await recoverRuntimeStatus(projectDir, options);
  if (!status.activeLoopId || !isActiveState(status.state)) {
    throw new JriError(
      `Cannot request stop while JRI is ${status.state}.`,
      "loop-not-active",
      "Use stop only while JRI is auditing, planning, or building. Use bare jri for blocked or idle projects.",
    );
  }

  const requested = !status.stopRequested;
  const event = await appendLoopEvent(projectDir, {
    type: "stopRequested",
    loopId: status.activeLoopId,
    data: { requested },
  });
  await updateStatus(projectDir, (current) => {
    if (current.activeLoopId !== status.activeLoopId || !isActiveState(current.state)) {
      throw new JriError("The loop changed before stop could be recorded.", "status-race", "Reload status and retry the stop request.");
    }
    return { ...current, stopRequested: requested };
  });
  return event;
}

export async function* haltLoop(projectDir: string, options: RuntimeOptions = {}): AsyncIterable<CoreEvent> {
  const { status } = await recoverRuntimeStatus(projectDir, options);
  if (status.state === "halted") {
    if (status.activeLoopId) {
      yield await appendLoopEvent(projectDir, {
        type: "loopHalted",
        loopId: status.activeLoopId,
        data: { resetOffered: false, resetAccepted: false },
      });
    }
    return;
  }
  if (!status.activeLoopId || !isActiveState(status.state)) {
    throw new JriError(
      `Cannot halt while JRI is ${status.state}.`,
      "loop-not-active",
      "Use halt only for an active loop. Blocked, stopped, and idle projects should be handled from bare jri or resume.",
    );
  }

  const killedPid = haltProcess(status, options.killProcess ?? defaultKillProcess);
  const rollbackCommit = status.currentIteration?.rollbackCommit;
  const event = await appendLoopEvent(projectDir, {
    type: "loopHalted",
    loopId: status.activeLoopId,
    data: {
      ...(killedPid === undefined ? {} : { killedPid }),
      resetOffered: Boolean(rollbackCommit && status.currentIteration?.trackedTreeCleanAtStart),
      resetAccepted: false,
      ...(rollbackCommit ? { rollbackCommit } : {}),
    },
  });

  await updateStatus(projectDir, (current) => {
    if (current.activeLoopId !== status.activeLoopId || !isActiveState(current.state)) {
      throw new JriError("The loop changed before halt could be recorded.", "status-race", "Reload status and retry halt.");
    }
    const { process, lock, ...withoutRuntimeOwnership } = current;
    void process;
    void lock;
    return {
      ...withoutRuntimeOwnership,
      state: "halted",
      stopRequested: false,
      finishedAt: (options.now ?? new Date()).toISOString(),
      lastResult: {
        outcome: "halted",
        summary: killedPid === undefined ? "Loop halted; no live process was recorded." : `Loop halted by killing pid ${killedPid}.`,
      },
    };
  });

  yield event;
}

export async function* resumeLoop(projectDir: string, _options: RuntimeOptions = {}): AsyncIterable<CoreEvent> {
  const status = await getRecoveredStatus(projectDir, _options);
  if (status.state === "stopped") {
    throw new JriError(
      "Loop resume requires the Pi-backed daemon runner, which is not implemented yet.",
      "runtime-runner-missing",
      "The durable state is eligible for resume, but starting fresh Pi sessions is still a remaining P0 item.",
    );
  }
  if (status.state === "blocked" && status.blocker?.reason === "needsHumanTask" && status.blocker.resolution?.status === "verified") {
    throw new JriError(
      "Verified human-task resume requires the Pi-backed daemon runner, which is not implemented yet.",
      "runtime-runner-missing",
      "The blocker is resolved; resume will be available after the runtime runner is implemented.",
    );
  }
  throw new JriError(
    `Cannot resume while JRI is ${status.state}.`,
    "resume-not-allowed",
    "Resume is only allowed from stopped loops or verified needs-human-task blockers.",
  );
}

async function readLoopEvents(projectDir: string, loopId: string): Promise<CoreEvent[]> {
  const path = join(projectDir, ".jri", "logs", loopId, "events.jsonl");
  if (!(await Bun.file(path).exists())) return [];
  const text = await readFile(path, "utf8");
  const events: CoreEvent[] = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    events.push(JSON.parse(line) as CoreEvent);
  }
  return events;
}

async function appendRecoveryEvent(projectDir: string, repairedFrom: string, repairedTo: string, reason: string): Promise<CoreEvent | undefined> {
  const status = await readStatus(projectDir);
  const loopId = status.activeLoopId ?? status.lastLoopId;
  if (!loopId) return undefined;
  return await appendLoopEvent(projectDir, {
    type: "statusRepaired",
    loopId,
    data: { repairedFrom, repairedTo, reason },
  });
}

function repairReason(status: ProjectStatus, processDead: boolean, staleLock: boolean): string {
  const reasons = [];
  if (processDead && status.process) reasons.push(`recorded process ${status.process.pid} is no longer running`);
  if (staleLock && status.lock) reasons.push(`lock for ${status.lock.operation} expired and owner process ${status.lock.pid} is not running`);
  return `Recovered runtime ownership because ${reasons.join(" and ")}.`;
}

function haltProcess(status: ProjectStatus, killProcess: ProcessKiller): number | undefined {
  if (!status.process) return undefined;
  killProcess(status.process.pid);
  return status.process.pid;
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

function defaultKillProcess(pid: number): void {
  try {
    process.kill(pid, "SIGTERM");
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ESRCH") return;
    throw error;
  }
}
