import { appendFile, readFile } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { JriError } from "./errors";
import {
  acquireLock,
  appendLoopEvent,
  heartbeatLock,
  isActiveState,
  readStatus,
  releaseLock,
  transitionStatus,
  updateStatus,
  writeStatusAtomic,
} from "./runtime-state";
import { buildPiPrompt, modelForAgent } from "./prompts";
import type { CoreEvent, LockOperation, ProjectStatus } from "./types";

export type ProcessAliveCheck = (pid: number) => boolean;
export type ProcessKiller = (pid: number) => void;

export type RuntimeOptions = {
  now?: Date;
  isProcessAlive?: ProcessAliveCheck;
  killProcess?: ProcessKiller;
  spawnRunner?: RunnerSpawner;
};

export type RunnerPhase = "planning" | "building";

export type RunnerSpawner = (request: RunnerSpawnRequest) => RunnerProcess;

export type RunnerSpawnRequest = {
  projectDir: string;
  loopId: string;
  phase: RunnerPhase;
};

export type RunnerProcess = {
  pid: number;
  command: string;
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

export async function* resumeLoop(projectDir: string, options: RuntimeOptions = {}): AsyncIterable<CoreEvent> {
  const status = await getRecoveredStatus(projectDir, options);
  const eligibleStopped = status.state === "stopped";
  const eligibleHumanTask =
    status.state === "blocked" && status.blocker?.reason === "needsHumanTask" && status.blocker.resolution?.status === "verified";
  if (eligibleStopped || eligibleHumanTask) {
    const loopId = status.activeLoopId;
    if (!loopId) {
      throw new JriError("Cannot resume without an active loop id.", "missing-loop-id", "Return to bare jri and authorize a new Ralph lifecycle.");
    }

    const phase = await chooseResumePhase(projectDir);
    const runner = (options.spawnRunner ?? defaultSpawnRunner)({ projectDir, loopId, phase });
    const lock = await acquireLock(projectDir, phaseToOperation(phase), {
      pid: runner.pid,
      ...(options.now ? { now: options.now } : {}),
      ...(options.isProcessAlive ? { isProcessAlive: options.isProcessAlive } : {}),
    });

    try {
      await transitionStatus(projectDir, phase, {
        loopId,
        ...(status.blocker?.reason ? { blockerReason: status.blocker.reason } : {}),
        ...(options.now ? { now: options.now } : {}),
        update: {
          stopRequested: false,
          startedAt: (options.now ?? new Date()).toISOString(),
          process: {
            pid: runner.pid,
            command: runner.command,
            startedAt: (options.now ?? new Date()).toISOString(),
          },
          lock,
          ...(eligibleHumanTask
            ? {
                blocker: status.blocker,
              }
            : {}),
        },
      });

      yield await appendLoopEvent(projectDir, {
        type: "loopStarted",
        loopId,
        message: `Started JRI ${phase} runner with pid ${runner.pid}.`,
        data: { projectDir, pid: runner.pid },
      });
      return;
    } catch (error) {
      try {
        await releaseLock(projectDir, lock);
      } catch {
        // Preserve the original startup error.
      }
      throw error;
    }
  }
  throw new JriError(
    `Cannot resume while JRI is ${status.state}.`,
    "resume-not-allowed",
    "Resume is only allowed from stopped loops or verified needs-human-task blockers.",
  );
}

export async function runLoopProcess(projectDir: string, loopId: string, phase: RunnerPhase, options: RuntimeOptions = {}): Promise<void> {
  const status = await getRecoveredStatus(projectDir, options);
  const lock = status.lock;
  if (!lock || lock.pid !== process.pid || lock.operation !== phaseToOperation(phase)) {
    throw new JriError("The JRI runner does not own the project lock.", "lock-lost", "Resume the loop again so the daemon can start a fresh runner.");
  }

  let currentLock = lock;
  const heartbeat = setInterval(() => {
    heartbeatLock(projectDir, currentLock)
      .then((next) => {
        currentLock = next;
      })
      .catch(() => {
        clearInterval(heartbeat);
      });
  }, 10_000);

  try {
    let currentPhase: RunnerPhase = phase;
    for (;;) {
      const statusAtPhaseStart = await readStatus(projectDir);
      if (currentPhase === "planning") {
        await appendLoopEvent(projectDir, { type: "planningStarted", loopId, data: {} });
        const exitCode = await runPiSession(projectDir, loopId, "planning");
        if (exitCode !== 0) {
          await finishFailedRun(projectDir, loopId, "planning", exitCode);
          return;
        }
        await appendLoopEvent(projectDir, {
          type: "planningFinished",
          loopId,
          data: { planPath: ".jri/IMPLEMENTATION_PLAN.md" },
        });
        currentLock = await switchRunnerPhase(projectDir, currentLock, "building");
        currentPhase = "building";
        continue;
      }

      const iteration = (statusAtPhaseStart.iteration ?? statusAtPhaseStart.iterations ?? 0) + 1;
      await updateStatus(projectDir, (current) => ({
        ...current,
        iteration,
        currentIteration: {
          iteration,
          trackedTreeCleanAtStart: true,
        },
      }));
      await appendLoopEvent(projectDir, {
        type: "iterationStarted",
        loopId,
        iteration,
        data: { trackedTreeCleanAtStart: true },
      });

      const exitCode = await runPiSession(projectDir, loopId, "building");
      if (exitCode !== 0) {
        await finishFailedRun(projectDir, loopId, "building", exitCode);
        return;
      }

      const latest = await readStatus(projectDir);
      const finishedIteration = latest.currentIteration?.iteration ?? latest.iteration ?? 1;
      await appendLoopEvent(projectDir, {
        type: "iterationFinished",
        loopId,
        iteration: finishedIteration,
        data: { outcome: "noChanges" },
      });
      await appendLoopEvent(projectDir, {
        type: "loopFinished",
        loopId,
        data: { outcome: "completed", summary: "Pi runner exited successfully." },
      });
      await transitionStatus(projectDir, "idle", {
        loopId,
        update: {
          ...ownershipCleared(latest),
          iterations: finishedIteration,
          lastResult: {
            outcome: "completed",
            summary: "Pi runner exited successfully.",
          },
        },
      });
      return;
    }
  } finally {
    clearInterval(heartbeat);
    const latest = await readStatus(projectDir);
    if (latest.lock && latest.lock.pid === currentLock.pid && latest.lock.acquiredAt === currentLock.acquiredAt) {
      try {
        await releaseLock(projectDir, latest.lock);
      } catch {
        // Status may already have been repaired or completed.
      }
    }
  }
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

async function chooseResumePhase(projectDir: string): Promise<RunnerPhase> {
  return (await Bun.file(join(projectDir, ".jri", "IMPLEMENTATION_PLAN.md")).exists()) ? "building" : "planning";
}

function phaseToOperation(phase: RunnerPhase): LockOperation {
  return phase === "planning" ? "plan" : "build";
}

async function switchRunnerPhase(
  projectDir: string,
  currentLock: NonNullable<ProjectStatus["lock"]>,
  nextPhase: RunnerPhase,
): Promise<NonNullable<ProjectStatus["lock"]>> {
  const now = new Date();
  const nextLock = {
    ...currentLock,
    operation: phaseToOperation(nextPhase),
    heartbeatAt: now.toISOString(),
    expiresAt: new Date(now.getTime() + 30_000).toISOString(),
  };
  await transitionStatus(projectDir, nextPhase, {
    update: {
      lock: nextLock,
    },
  });
  return nextLock;
}

function defaultSpawnRunner(request: RunnerSpawnRequest): RunnerProcess {
  const cliPath = fileURLToPath(new URL("../cli/index.ts", import.meta.url));
  const command = [process.execPath, cliPath, "--run-loop", request.projectDir, request.loopId, request.phase];
  const proc = Bun.spawn(command, {
    cwd: request.projectDir,
    env: process.env,
    stdout: "ignore",
    stderr: "ignore",
    stdin: "ignore",
  });
  proc.unref();
  if (!proc.pid) {
    throw new JriError("Failed to start the JRI runner process.", "runner-start-failed", "Retry resume; if it repeats, inspect daemon logs and .jri/status.json.");
  }
  return { pid: proc.pid, command: command.join(" ") };
}

async function runPiSession(projectDir: string, loopId: string, phase: RunnerPhase): Promise<number> {
  const piPath = process.env.JRI_PI_COMMAND ?? "pi";
  const prompt = await buildPiPrompt(projectDir, phase);
  const agent = phase === "planning" ? "planner" : "builder";
  const model = modelForAgent(await readProjectConfig(projectDir), agent);
  const command = [
    piPath,
    "--provider",
    "openai",
    "--model",
    model.model,
    "--thinking",
    model.reasoning,
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--no-context-files",
    "--tools",
    "read,bash,edit,write,grep,find,ls",
    "--print",
    prompt,
  ];
  const proc = Bun.spawn(command, {
    cwd: projectDir,
    stdout: "pipe",
    stderr: "pipe",
    stdin: "ignore",
    env: {
      ...process.env,
      PI_CODING_AGENT_SESSION_DIR: join(projectDir, ".jri", "logs", loopId, "pi-sessions"),
    },
  });
  await appendStreamsToStdoutLog(projectDir, loopId, proc);
  return await proc.exited;
}

async function appendStreamsToStdoutLog(projectDir: string, loopId: string, proc: Bun.Subprocess<"ignore", "pipe", "pipe">): Promise<void> {
  const stdoutPath = join(projectDir, ".jri", "logs", loopId, "stdout.log");
  await Promise.all([appendStream(stdoutPath, proc.stdout), appendStream(stdoutPath, proc.stderr)]);
}

async function appendStream(path: string, stream: ReadableStream<Uint8Array>): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    await appendFile(path, decoder.decode(chunk.value, { stream: true }), "utf8");
  }
  const tail = decoder.decode();
  if (tail) await appendFile(path, tail, "utf8");
}

async function finishFailedRun(projectDir: string, loopId: string, phase: RunnerPhase, exitCode: number): Promise<void> {
  const summary = `Pi ${phase} runner exited with code ${exitCode}.`;
  if (phase === "building") {
    const status = await readStatus(projectDir);
    const iteration = status.currentIteration?.iteration ?? status.iteration ?? 1;
    await appendLoopEvent(projectDir, {
      type: "iterationFinished",
      loopId,
      iteration,
      data: { outcome: "validationFailed" },
    });
  }
  await appendLoopEvent(projectDir, {
    type: "loopFinished",
    loopId,
    data: { outcome: "failed", summary },
  });
  await transitionStatus(projectDir, "stopped", {
    loopId,
    update: {
      ...ownershipCleared(await readStatus(projectDir)),
      stopRequested: false,
      lastResult: {
        outcome: "failed",
        summary,
      },
    },
  });
}

function ownershipCleared(status: ProjectStatus): { [K in keyof ProjectStatus]?: ProjectStatus[K] | undefined } {
  return { ...status, process: undefined, lock: undefined };
}

async function readProjectConfig(projectDir: string) {
  const path = join(projectDir, ".jri", "config.json");
  if (!(await Bun.file(path).exists())) return undefined;
  return JSON.parse(await Bun.file(path).text()) as unknown;
}
