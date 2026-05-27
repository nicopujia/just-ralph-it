import { appendFile, mkdir, readdir, readFile, stat } from "node:fs/promises";
import { createHash } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { JriError } from "./errors";
import {
  invokeDefaultHarness,
  assertHarnessCapabilities,
  readActiveLoopChildren,
  readProjectConfig,
  runControlledPiSession,
  type CapabilityDescriptor,
  type HarnessAdapter,
  type HarnessInvocation,
  type HarnessOutputSink,
  type HarnessSessionRunner,
} from "./harness";
import { checkInterrogationStartGate } from "./interrogation-state";
import {
  acquireLock,
  appendLoopEvent,
  generateLoopId,
  heartbeatLock,
  isActiveState,
  readStatus,
  releaseLock,
  transitionStatus,
  updateStatus,
  writeStatusAtomic,
} from "./runtime-state";
import { extractLatestBuilderHandoffFromText, extractLatestHandoffFromText } from "./handoffs";
import { modelForAgent } from "./prompts";
import type {
  AgentName,
  AgentHandoff,
  AuditorHandoff,
  Blocker,
  BuilderHandoff,
  CoreEvent,
  LockOperation,
  LoopObserveOptions,
  PlannerHandoff,
  ProjectStatus,
  ValidationHandoff,
} from "./types";

export type ProcessAliveCheck = (pid: number) => boolean;
export type KillSignal = "SIGTERM" | "SIGKILL";
export type ProcessKiller = (pid: number, signal?: KillSignal) => void;
export type GitResetRunner = (projectDir: string, rollbackCommit: string) => Promise<GitResetResult>;

export type GitResetResult = {
  succeeded: boolean;
  error?: string;
};

export type RuntimeOptions = {
  now?: Date;
  signal?: AbortSignal;
  isProcessAlive?: ProcessAliveCheck;
  killProcess?: ProcessKiller;
  resetGit?: boolean;
  gitResetRunner?: GitResetRunner;
  spawnRunner?: RunnerSpawner;
  harnessAdapter?: HarnessAdapter;
  harnessRunner?: HarnessSessionRunner;
  observePollIntervalMs?: number;
  childKillGraceMs?: number;
};

export type RunnerPhase = "auditing" | "planning" | "building";

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
  const missingRuntimeOwnership = isActiveState(status.state) && !status.process && !status.lock;

  if (!processDead && !staleLock && !missingRuntimeOwnership) {
    if (isActiveState(status.state) && status.process) {
      await ensureStartupMilestone(projectDir, status);
    }
    return { status };
  }

  const repairedFrom = status.state;
  const reason = repairReason(status, processDead, staleLock, missingRuntimeOwnership);
  const terminalRepair =
    isActiveState(status.state) && (processDead || staleLock || missingRuntimeOwnership)
      ? await statusRepairFromLatestTerminalEvent(projectDir, status, reason)
      : undefined;
  const recoveryMessage = terminalRepair?.reason ?? reason;
  const timestamp = now.toISOString();
  const { process, lock, ...withoutRuntimeOwnership } = status;
  void process;
  void lock;

  const repaired: ProjectStatus = {
    ...withoutRuntimeOwnership,
    ...(terminalRepair?.patch ??
      (isActiveState(status.state)
        ? {
            state: "stopped",
            finishedAt: timestamp,
            stopRequested: false,
            lastResult: {
              outcome: "failed",
              summary: reason,
            },
          }
        : {})),
    recoveryNote: {
      timestamp,
      message: recoveryMessage,
      repairedFrom,
    },
  };

  await writeStatusAtomic(projectDir, repaired);
  const repairedEvent = await appendRecoveryEvent(projectDir, repairedFrom, repaired.state, recoveryMessage);
  return repairedEvent ? { status: repaired, repairedEvent } : { status: repaired };
}

type RuntimeLoopObserveOptions = RuntimeOptions &
  LoopObserveOptions & {
    afterSequence?: number;
  };

export async function* observeLoop(projectDir: string, options: RuntimeLoopObserveOptions = {}): AsyncIterable<CoreEvent> {
  const { status, repairedEvent } = await recoverRuntimeStatus(projectDir, options);
  if (repairedEvent) yield repairedEvent;

  const loopId = status.activeLoopId ?? status.lastLoopId;
  if (!loopId) {
    throw new JriError("There is no JRI loop to observe.", "no-loop", "Start Ralph from bare jri after specs are ready.");
  }

  let stdoutOffset = options.includeStdout ? await stdoutLogSize(projectDir, loopId) : 0;
  if (options.includeStdout) {
    const output = await readRecentStdout(projectDir, loopId, options.recentStdoutLines ?? 100);
    if (output) {
      yield syntheticLoopOutputEvent(loopId, output.text, output.stdoutOffset);
      stdoutOffset = output.stdoutOffset + Buffer.byteLength(output.text, "utf8");
    }
  }

  let lastSequence = options.afterSequence ?? 0;
  for (const event of (await readLoopEvents(projectDir, loopId)).filter((event) => event.sequence > lastSequence)) {
    lastSequence = Math.max(lastSequence, event.sequence);
    yield event;
  }

  if (!options.follow || !isActiveState(status.state)) return;

  const pollIntervalMs = options.observePollIntervalMs ?? 250;
  for (;;) {
    if (options.includeStdout) {
      const output = await readStdoutFromOffset(projectDir, loopId, stdoutOffset);
      if (output) {
        yield syntheticLoopOutputEvent(loopId, output.text, stdoutOffset, false);
        stdoutOffset += Buffer.byteLength(output.text, "utf8");
      }
    }

    const events = await readLoopEventsAfter(projectDir, loopId, lastSequence);
    for (const event of events) {
      lastSequence = Math.max(lastSequence, event.sequence);
      yield event;
    }

    const latest = await readStatus(projectDir);
    if (!isActiveState(latest.state)) {
      const finalEvents = await readLoopEventsAfter(projectDir, loopId, lastSequence);
      for (const event of finalEvents) {
        lastSequence = Math.max(lastSequence, event.sequence);
        yield event;
      }
      if (options.includeStdout) {
        const output = await readStdoutFromOffset(projectDir, loopId, stdoutOffset);
        if (output) {
          yield syntheticLoopOutputEvent(loopId, output.text, stdoutOffset, false);
        }
      }
      return;
    }

    await sleep(pollIntervalMs);
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

export async function startRalphLoop(projectDir: string, options: RuntimeOptions = {}): Promise<CoreEvent> {
  await assertInterrogationStartGateClear(projectDir, options);

  const status = await getRecoveredStatus(projectDir, options);
  if (isActiveState(status.state)) {
    throw new JriError(
      `Cannot start a new Ralph lifecycle while JRI is ${status.state}.`,
      "loop-already-active",
      "Use jri loop attach to observe the current loop, or jri loop stop to request a graceful stop.",
    );
  }
  if (status.state === "blocked" && status.blocker?.reason === "needsHumanTask") {
    if (status.blocker.resolution?.status === "verified") {
      throw new JriError(
        "Cannot start a new Ralph lifecycle while a verified human-task blocker is waiting to resume.",
        "human-task-resume-required",
        "Run jri loop resume to continue the existing verified human-task lifecycle.",
      );
    }
    throw new JriError(
      "Cannot start while a human-task blocker is unresolved.",
      "human-task-blocked",
      status.blocker.resolutionGuide.resumeInstruction,
    );
  }
  if (status.state === "stopped" && status.authorizedSpecsFingerprint) {
    const currentSpecsFingerprint = await computeSpecsFingerprint(projectDir);
    if (status.authorizedSpecsFingerprint === currentSpecsFingerprint) {
      throw new JriError(
        "Cannot start because the stopped loop has unchanged specs.",
        "stopped-loop-resume-required",
        "Run jri loop resume to continue the existing authorized lifecycle, or change the specs and say just ralph it to reauthorize.",
      );
    }
  }

  const loopId =
    status.state === "stopped" || status.blocker?.reason === "ambiguousSpecs"
      ? (status.activeLoopId ?? (await generateLoopId(projectDir, options.now ?? new Date())))
      : await generateLoopId(projectDir, options.now ?? new Date());
  const lock = await acquireLock(projectDir, "audit", {
    pid: process.pid,
    ...(options.now ? { now: options.now } : {}),
    ...(options.isProcessAlive ? { isProcessAlive: options.isProcessAlive } : {}),
  });

  let runner: RunnerProcess | undefined;
  let statusChanged = false;
  try {
    await transitionStatus(projectDir, "auditing", {
      loopId,
      ...(status.blocker?.reason ? { blockerReason: status.blocker.reason } : {}),
      ...(options.now ? { now: options.now } : {}),
      update: {
        stopRequested: false,
        startedAt: (options.now ?? new Date()).toISOString(),
        lock,
        ...(status.blocker?.reason === "ambiguousSpecs" ? {} : { blocker: undefined }),
        currentIteration: undefined,
      },
    });
    statusChanged = true;

    runner = (options.spawnRunner ?? defaultSpawnRunner)({ projectDir, loopId, phase: "auditing" });
    await transferStartupOwnership(projectDir, loopId, lock, runner, options.now ?? new Date());

    return await appendLoopEvent(projectDir, {
      type: "loopStarted",
      loopId,
      message: `Started JRI auditing runner with pid ${runner.pid}.`,
      data: { projectDir, pid: runner.pid },
    });
  } catch (error) {
    try {
      if (runner) (options.killProcess ?? defaultKillProcess)(runner.pid);
    } catch {
      // Preserve the original startup error.
    }
    try {
      if (statusChanged) await writeStatusAtomic(projectDir, status);
      else await releaseLock(projectDir, lock);
    } catch {
      // Preserve the original startup error.
    }
    throw error;
  }
}

async function assertInterrogationStartGateClear(projectDir: string, options: RuntimeOptions): Promise<void> {
  const startGate = await checkInterrogationStartGate(projectDir, options.now ? { now: options.now } : {});
  if (startGate.ok) return;

  const pending = startGate.pending[0];
  const summary = pending?.topic.pendingReconciliation?.summary ?? "A pending spec reconciliation must be resolved before Ralph can start.";
  throw new JriError(
    "Cannot start while spec reconciliation is pending.",
    "pending-spec-reconciliation",
    `${summary} Resolve or defer the changed requirement in bare jri, then say just ralph it again.`,
  );
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

  const { status: haltStatus, lock: haltLock } = await acquireHaltLock(projectDir, status, options);
  const loopId = haltStatus.activeLoopId;
  if (!loopId) {
    throw new JriError("Cannot halt without an active loop id.", "missing-loop-id", "Reload status and retry halt.");
  }
  const killedProcesses = await haltProcesses(projectDir, loopId, haltStatus, options.killProcess ?? defaultKillProcess, options.childKillGraceMs);
  const rollbackCommit = haltStatus.currentIteration?.rollbackCommit;
  const resetOffered = Boolean(rollbackCommit && haltStatus.currentIteration?.trackedTreeCleanAtStart);
  const resetAccepted = Boolean(options.resetGit && resetOffered && rollbackCommit);
  const resetResult = resetAccepted
    ? await (options.gitResetRunner ?? resetTrackedFiles)(projectDir, rollbackCommit as string)
    : undefined;
  const event = await appendLoopEvent(projectDir, {
    type: "loopHalted",
    loopId,
    data: {
      ...(killedProcesses.runnerPid === undefined ? {} : { killedPid: killedProcesses.runnerPid }),
      ...(killedProcesses.childPids.length === 0 ? {} : { killedChildPids: killedProcesses.childPids }),
      resetOffered,
      resetAccepted,
      ...(resetResult ? { resetSucceeded: resetResult.succeeded } : {}),
      ...(resetResult?.error ? { resetError: resetResult.error } : {}),
      ...(rollbackCommit ? { rollbackCommit } : {}),
    },
  });

  await updateStatus(projectDir, (current) => {
    if (current.activeLoopId !== loopId || !isActiveState(current.state) || !locksMatch(current.lock, haltLock)) {
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
        summary: haltSummary(killedProcesses, resetOffered, resetAccepted, resetResult),
      },
    };
  });

  yield event;
}

async function acquireHaltLock(
  projectDir: string,
  status: ProjectStatus,
  options: RuntimeOptions,
): Promise<{ status: ProjectStatus; lock: NonNullable<ProjectStatus["lock"]> }> {
  const now = options.now ?? new Date();
  const acquiredAt = now.toISOString();
  const lock: NonNullable<ProjectStatus["lock"]> = {
    owner: "daemon",
    pid: process.pid,
    operation: "halt",
    acquiredAt,
    heartbeatAt: acquiredAt,
    expiresAt: new Date(now.getTime() + 30_000).toISOString(),
  };

  const locked = await updateStatus(projectDir, (current) => {
    if (current.activeLoopId !== status.activeLoopId || !isActiveState(current.state)) {
      throw new JriError("The loop changed before halt could acquire ownership.", "status-race", "Reload status and retry halt.");
    }
    return { ...current, lock };
  });
  return { status: locked, lock };
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

    const currentSpecsFingerprint = await computeSpecsFingerprint(projectDir);
    if (!status.authorizedSpecsFingerprint) {
      throw new JriError(
        "Cannot resume because the authorized specs fingerprint is missing.",
        "specs-fingerprint-missing",
        "Return to bare jri, confirm the requirements, then say just ralph it so audit and planning authorize the lifecycle.",
      );
    }
    if (status.authorizedSpecsFingerprint !== currentSpecsFingerprint) {
      throw new JriError(
        "Cannot resume because specs changed after the loop stopped.",
        "specs-changed",
        "Return to bare jri, resolve or confirm the changed requirements, then say just ralph it so audit and planning rerun.",
      );
    }

    const phase = eligibleHumanTask ? resumePhaseFromBlocker(status.blocker) : await chooseResumePhase(projectDir, loopId);
    const lock = await acquireLock(projectDir, phaseToOperation(phase), {
      pid: process.pid,
      ...(options.now ? { now: options.now } : {}),
      ...(options.isProcessAlive ? { isProcessAlive: options.isProcessAlive } : {}),
    });

    let runner: RunnerProcess | undefined;
    let statusChanged = false;
    try {
      await transitionStatus(projectDir, phase, {
        loopId,
        ...(status.blocker?.reason ? { blockerReason: status.blocker.reason } : {}),
        ...(options.now ? { now: options.now } : {}),
        update: {
          stopRequested: false,
          startedAt: (options.now ?? new Date()).toISOString(),
          authorizedSpecsFingerprint: currentSpecsFingerprint,
          lock,
          ...(eligibleHumanTask ? { blocker: undefined } : {}),
        },
      });
      statusChanged = true;
      runner = (options.spawnRunner ?? defaultSpawnRunner)({ projectDir, loopId, phase });
      await transferStartupOwnership(projectDir, loopId, lock, runner, options.now ?? new Date());

      if (eligibleHumanTask && status.blocker?.reason && !(await hasBlockerResolvedEvent(projectDir, loopId, status.blocker.reason))) {
        yield await appendLoopEvent(projectDir, {
          type: "blockerResolved",
          loopId,
          data: { reason: status.blocker.reason },
        });
      }
      yield await appendLoopEvent(projectDir, {
        type: "loopStarted",
        loopId,
        message: `Started JRI ${phase} runner with pid ${runner.pid}.`,
        data: { projectDir, pid: runner.pid },
      });
      return;
    } catch (error) {
      try {
        if (runner) (options.killProcess ?? defaultKillProcess)(runner.pid);
      } catch {
        // Preserve the original startup error.
      }
      try {
        if (statusChanged) await writeStatusAtomic(projectDir, status);
        else await releaseLock(projectDir, lock);
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
  throwIfRuntimeCancelled(options.signal);
  const status = await getRecoveredStatus(projectDir, options);
  const lock = await waitForRunnerLock(projectDir, loopId, phase, status, options);
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

  let currentPhase: RunnerPhase = phase;
  try {
    for (;;) {
      throwIfRuntimeCancelled(options.signal);
      const statusAtPhaseStart = await readStatus(projectDir);
      if (currentPhase === "auditing") {
        await appendLoopEvent(projectDir, { type: "auditStarted", loopId, data: {} });
        const auditorHandoff = await runAuditor(projectDir, loopId, options);
        if (auditorHandoff.action === "failed") {
          await finishAuditFailedRun(projectDir, loopId, auditorHandoff);
          return;
        }
        const authorizedSpecsFingerprint = await computeAuthorizedSpecsFingerprint(projectDir, auditorHandoff);
        await appendLoopEvent(projectDir, {
          type: "auditPassed",
          loopId,
          data: { specFiles: auditorHandoff.specFiles, specsFingerprint: authorizedSpecsFingerprint },
        });
        if (statusAtPhaseStart.blocker?.reason === "ambiguousSpecs" && !(await hasBlockerResolvedEvent(projectDir, loopId, "ambiguousSpecs"))) {
          await appendLoopEvent(projectDir, {
            type: "blockerResolved",
            loopId,
            data: { reason: "ambiguousSpecs" },
          });
        }
        await updateStatus(projectDir, (current) => {
          if (current.blocker?.reason !== "ambiguousSpecs") {
            return {
              ...current,
              authorizedSpecsFingerprint,
            };
          }
          const { blocker, ...withoutBlocker } = current;
          void blocker;
          return {
            ...withoutBlocker,
            authorizedSpecsFingerprint,
          };
        });
        if (await stopIfRequested(projectDir, loopId, "planning")) return;
        currentLock = await switchRunnerPhase(projectDir, currentLock, "planning");
        currentPhase = "planning";
        continue;
      }

      if (currentPhase === "planning") {
        await appendLoopEvent(projectDir, { type: "planningStarted", loopId, data: {} });
        const plannerHandoff = await runPlanner(projectDir, loopId, options);
        if (plannerHandoff.action === "blocked") {
          await finishPlanningBlockedRun(projectDir, loopId, plannerHandoff.blocker);
          return;
        }
        await assertPlannerPlanPersisted(projectDir, plannerHandoff);
        await appendLoopEvent(projectDir, {
          type: "planningFinished",
          loopId,
          data: { planPath: ".jri/IMPLEMENTATION_PLAN.md" },
        });
        if (await stopIfRequested(projectDir, loopId, "building")) return;
        currentLock = await switchRunnerPhase(projectDir, currentLock, "building");
        currentPhase = "building";
        continue;
      }

      const iteration = (statusAtPhaseStart.iteration ?? statusAtPhaseStart.iterations ?? 0) + 1;
      const iterationStartGit = await readGitSnapshot(projectDir);
      await updateStatus(projectDir, (current) => ({
        ...current,
        iteration,
        currentIteration: {
          iteration,
          trackedTreeCleanAtStart: iterationStartGit.trackedTreeClean,
          ...(iterationStartGit.head ? { rollbackCommit: iterationStartGit.head } : {}),
          ...(iterationStartGit.dirtySummary ? { dirtySummary: iterationStartGit.dirtySummary } : {}),
        },
      }));
      await appendLoopEvent(projectDir, {
        type: "iterationStarted",
        loopId,
        iteration,
        data: {
          trackedTreeCleanAtStart: iterationStartGit.trackedTreeClean,
          ...(iterationStartGit.head ? { rollbackCommit: iterationStartGit.head } : {}),
          ...(iterationStartGit.dirtySummary ? { dirtySummary: iterationStartGit.dirtySummary } : {}),
        },
      });

      const builderHandoff = await runBuilder(projectDir, loopId, options);
      const validationEvidence = validationEvidenceFromBuilder(builderHandoff);
      await recordValidationEvidence(projectDir, loopId, iteration, validationEvidence);
      if (builderHandoff.action === "blocked") {
        if (await hasUnexpectedGitMutation(projectDir, iterationStartGit)) {
          await finishUnexpectedGitMutationRun(projectDir, loopId, iteration, "blocked");
          return;
        }
        await finishBlockedRun(projectDir, loopId, builderHandoff.blocker);
        return;
      }
      if (builderHandoff.action === "failedValidation") {
        if (await hasUnexpectedGitMutation(projectDir, iterationStartGit)) {
          await finishUnexpectedGitMutationRun(projectDir, loopId, iteration, "failedValidation");
          return;
        }
        await finishValidationFailedRun(projectDir, loopId, builderHandoff);
        return;
      }
      if ((await hasUnexpectedGitMutation(projectDir, iterationStartGit)) && !hasPassingValidation(validationEvidence)) {
        await finishUnsafeGitSuccessRun(projectDir, loopId, iteration);
        return;
      }
      if ((builderHandoff.action === "continue" || builderHandoff.action === "complete") && hasFailingValidation(validationEvidence)) {
        await finishInvalidSuccessfulValidationRun(projectDir, loopId, iteration);
        return;
      }

      const latest = await readStatus(projectDir);
      const finishedIteration = latest.currentIteration?.iteration ?? latest.iteration ?? 1;
      const iterationResult = await observeIterationGitResult(projectDir, loopId, finishedIteration, iterationStartGit);
      if (iterationResult.tagIssue) {
        await finishUnsafeGitTagRun(projectDir, loopId, finishedIteration, iterationResult.tagIssue, hasPassingValidation(validationEvidence));
        return;
      }
      await appendLoopEvent(projectDir, {
        type: "iterationFinished",
        loopId,
        iteration: finishedIteration,
        data: iterationResult,
      });
      if (await stopIfRequested(projectDir, loopId, "building", finishedIteration)) return;
      const explorerProof = await successfulExplorerProof(projectDir, loopId);
      if (builderHandoff.action === "complete" && !explorerProof) {
        await finishMissingExplorerProofRun(projectDir, loopId);
        return;
      }
      if (builderHandoff.action === "needsReplan") {
        await appendLoopEvent(projectDir, {
          type: "planRegenerationRequested",
          loopId,
          data: { reason: "needsReplan" },
        });
        currentLock = await switchRunnerPhase(projectDir, currentLock, "planning");
        currentPhase = "planning";
        await appendLoopEvent(projectDir, { type: "planRegenerationStarted", loopId, data: {} });
        const plannerHandoff = await runPlanner(projectDir, loopId, options);
        if (plannerHandoff.action === "blocked") {
          await finishPlanningBlockedRun(projectDir, loopId, plannerHandoff.blocker);
          return;
        }
        await assertPlannerPlanPersisted(projectDir, plannerHandoff);
        await appendLoopEvent(projectDir, { type: "planRegenerationFinished", loopId, data: {} });
        if (await stopIfRequested(projectDir, loopId, "building")) return;
        currentLock = await switchRunnerPhase(projectDir, currentLock, "building");
        currentPhase = "building";
        continue;
      }
      if (builderHandoff.action === "continue") {
        continue;
      }
      await appendLoopEvent(projectDir, {
        type: "loopFinished",
        loopId,
        data: {
          outcome: "completed",
          summary: builderHandoff.summary,
          ...(builderHandoff.url ? { url: builderHandoff.url } : {}),
          ...(iterationResult.commit ? { commit: iterationResult.commit } : {}),
          ...(iterationResult.tag ? { tag: iterationResult.tag } : {}),
          ...(explorerProof ? { explorer: explorerProof } : {}),
        },
      });
      await transitionStatus(projectDir, "idle", {
        loopId,
        update: {
          ...ownershipCleared(latest),
          iterations: finishedIteration,
          lastResult: {
            outcome: "completed",
            summary: builderHandoff.summary,
            validationPassed: hasPassingValidation(validationEvidence),
            ...(builderHandoff.url ? { url: builderHandoff.url } : {}),
            ...(iterationResult.commit ? { commit: iterationResult.commit } : {}),
            ...(iterationResult.tag ? { tag: iterationResult.tag } : {}),
            ...(explorerProof ? { explorer: explorerProof } : {}),
          },
        },
      });
      return;
    }
  } catch (error) {
    if (error instanceof LoopAlreadyFinished) return;
    if (error instanceof JriError && isLoopFailureError(error)) {
      if (isCancellationFanoutError(error)) {
        await terminateRegisteredLoopProcesses(projectDir, loopId, options.killProcess ?? defaultKillProcess, options.childKillGraceMs);
      }
      await finishRuntimeFailureRun(projectDir, loopId, currentPhase, error);
      return;
    }
    throw error;
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

async function stopIfRequested(projectDir: string, loopId: string, nextPhase: Exclude<RunnerPhase, "auditing">, iteration?: number): Promise<boolean> {
  const status = await readStatus(projectDir);
  if (!status.stopRequested) return false;
  const authorizedSpecsFingerprint = await computeSpecsFingerprint(projectDir);

  await appendLoopEvent(projectDir, {
    type: "loopStopped",
    loopId,
    data: {
      reason: "gracefulStopRequested",
      nextPhase,
      ...(iteration === undefined ? {} : { iteration }),
      specsFingerprint: authorizedSpecsFingerprint,
    },
  });
  await transitionStatus(projectDir, "stopped", {
    loopId,
    update: {
      ...ownershipCleared(status),
      stopRequested: false,
      authorizedSpecsFingerprint,
      lastResult: {
        outcome: "stopped",
        summary:
          iteration === undefined
            ? "Graceful stop completed after planning finished."
            : `Graceful stop completed after iteration ${iteration}.`,
      },
    },
  });
  return true;
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

async function readLoopEventsAfter(projectDir: string, loopId: string, sequence: number): Promise<CoreEvent[]> {
  return (await readLoopEvents(projectDir, loopId)).filter((event) => event.sequence > sequence);
}

async function readRecentStdout(projectDir: string, loopId: string, maxLines: number): Promise<{ text: string; stdoutOffset: number } | undefined> {
  const path = join(projectDir, ".jri", "logs", loopId, "stdout.log");
  if (!(await Bun.file(path).exists())) return undefined;
  const text = await readFile(path, "utf8");
  if (!text) return undefined;
  const lines = text.endsWith("\n") ? text.slice(0, -1).split("\n") : text.split("\n");
  const selected = lines.slice(-Math.max(1, maxLines));
  const selectedText = `${selected.join("\n")}${text.endsWith("\n") ? "\n" : ""}`;
  return {
    text: selectedText,
    stdoutOffset: Buffer.byteLength(text.slice(0, text.length - selectedText.length), "utf8"),
  };
}

async function readStdoutFromOffset(projectDir: string, loopId: string, stdoutOffset: number): Promise<{ text: string } | undefined> {
  const path = join(projectDir, ".jri", "logs", loopId, "stdout.log");
  if (!(await Bun.file(path).exists())) return undefined;
  const bytes = await readFile(path);
  const currentSize = bytes.byteLength;
  if (currentSize <= stdoutOffset) return undefined;
  return { text: bytes.subarray(stdoutOffset).toString("utf8") };
}

function syntheticLoopOutputEvent(loopId: string, text: string, stdoutOffset: number, replayed = true): CoreEvent {
  return {
    id: crypto.randomUUID(),
    sequence: -(stdoutOffset + 1),
    timestamp: new Date().toISOString(),
    type: "loopOutput",
    loopId,
    stdoutOffset,
    message: text,
    data: { text, replayed },
  };
}

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
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

async function ensureStartupMilestone(projectDir: string, status: ProjectStatus): Promise<void> {
  const loopId = status.activeLoopId ?? status.lastLoopId;
  if (!loopId) return;
  const processInfo = status.process;
  if (!processInfo) return;
  if ((await readLoopEvents(projectDir, loopId)).some((event) => event.type === "loopStarted")) return;

  await appendLoopEvent(projectDir, {
    type: "loopStarted",
    loopId,
    message: `Recovered missing JRI startup milestone for pid ${processInfo.pid}.`,
    data: {
      projectDir,
      pid: processInfo.pid,
    },
  });
}

type TerminalStatusRepair = {
  reason: string;
  patch: Partial<ProjectStatus>;
};

async function statusRepairFromLatestTerminalEvent(
  projectDir: string,
  status: ProjectStatus,
  recoveryReason: string,
): Promise<TerminalStatusRepair | undefined> {
  const loopId = status.activeLoopId ?? status.lastLoopId;
  if (!loopId) return undefined;
  const latestEvent = (await readLoopEvents(projectDir, loopId)).at(-1);
  if (!latestEvent) return undefined;

  if (latestEvent.type === "loopFinished") {
    if (latestEvent.data.outcome === "completed") {
      return {
        reason: `${recoveryReason} Latest durable loop event completed the loop.`,
        patch: {
          state: "idle",
          activeLoopId: null,
          stopRequested: false,
          finishedAt: latestEvent.timestamp,
          lastResult: {
            outcome: "completed",
            ...(latestEvent.data.summary ? { summary: latestEvent.data.summary } : {}),
            ...(latestEvent.data.url ? { url: latestEvent.data.url } : {}),
            ...(latestEvent.data.commit ? { commit: latestEvent.data.commit } : {}),
            ...(latestEvent.data.tag ? { tag: latestEvent.data.tag } : {}),
          },
        },
      };
    }
    return {
      reason: `${recoveryReason} Latest durable loop event failed the loop.`,
      patch: {
        state: "stopped",
        stopRequested: false,
        finishedAt: latestEvent.timestamp,
        lastResult: {
          outcome: "failed",
          ...(latestEvent.data.summary ? { summary: latestEvent.data.summary } : {}),
        },
      },
    };
  }

  if (latestEvent.type === "loopStopped") {
    return {
      reason: `${recoveryReason} Latest durable loop event stopped the loop.`,
      patch: {
        state: "stopped",
        stopRequested: false,
        finishedAt: latestEvent.timestamp,
        ...(latestEvent.data.specsFingerprint ? { authorizedSpecsFingerprint: latestEvent.data.specsFingerprint } : {}),
        lastResult: {
          outcome: "stopped",
          summary:
            latestEvent.data.iteration === undefined
              ? "Graceful stop completed after planning finished."
              : `Graceful stop completed after iteration ${latestEvent.data.iteration}.`,
        },
      },
    };
  }

  if (latestEvent.type === "loopHalted") {
    return {
      reason: `${recoveryReason} Latest durable loop event halted the loop.`,
      patch: {
        state: "halted",
        stopRequested: false,
        finishedAt: latestEvent.timestamp,
        lastResult: {
          outcome: "halted",
          summary: haltSummary(
            {
              ...(latestEvent.data.killedPid === undefined ? {} : { runnerPid: latestEvent.data.killedPid }),
              childPids: latestEvent.data.killedChildPids ?? [],
            },
            latestEvent.data.resetOffered,
            latestEvent.data.resetAccepted,
            {
              succeeded: Boolean(latestEvent.data.resetSucceeded),
              ...(latestEvent.data.resetError ? { error: latestEvent.data.resetError } : {}),
            },
          ),
        },
      },
    };
  }

  return undefined;
}

function repairReason(status: ProjectStatus, processDead: boolean, staleLock: boolean, missingRuntimeOwnership: boolean): string {
  const reasons = [];
  if (processDead && status.process) reasons.push(`recorded process ${status.process.pid} is no longer running`);
  if (staleLock && status.lock) reasons.push(`lock for ${status.lock.operation} expired and owner process ${status.lock.pid} is not running`);
  if (missingRuntimeOwnership) reasons.push(`active ${status.state} status has no recorded process or lock`);
  return `Recovered runtime ownership because ${reasons.join(" and ")}.`;
}

type HaltedProcesses = {
  runnerPid?: number;
  childPids: number[];
};

async function haltProcesses(
  projectDir: string,
  loopId: string,
  status: ProjectStatus,
  killProcess: ProcessKiller,
  childKillGraceMs = 250,
): Promise<HaltedProcesses> {
  const childPids = (await readActiveLoopChildren(projectDir, loopId)).map((child) => child.pid);
  const pids = [...childPids, ...(status.process ? [status.process.pid] : [])];
  await terminatePids(pids, killProcess, childKillGraceMs);

  return {
    ...(status.process ? { runnerPid: status.process.pid } : {}),
    childPids: childPids.filter((pid) => pid !== status.process?.pid),
  };
}

async function terminateRegisteredLoopProcesses(
  projectDir: string,
  loopId: string,
  killProcess: ProcessKiller,
  childKillGraceMs = 250,
): Promise<number[]> {
  const pids = (await readActiveLoopChildren(projectDir, loopId)).map((child) => child.pid);
  await terminatePids(pids, killProcess, childKillGraceMs);
  return uniquePids(pids);
}

async function terminatePids(pids: number[], killProcess: ProcessKiller, childKillGraceMs: number): Promise<void> {
  const unique = uniquePids(pids);
  if (unique.length === 0) return;
  for (const pid of unique) {
    killProcess(pid, "SIGTERM");
  }
  await sleep(childKillGraceMs);
  for (const pid of unique) {
    killProcess(pid, "SIGKILL");
  }
}

function uniquePids(pids: number[]): number[] {
  return [...new Set(pids.filter((pid) => Number.isInteger(pid) && pid > 0))];
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

function defaultKillProcess(pid: number, signal: KillSignal = "SIGTERM"): void {
  try {
    process.kill(pid, signal);
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ESRCH") return;
    throw error;
  }
}

async function chooseResumePhase(projectDir: string, loopId: string): Promise<RunnerPhase> {
  const events = await readLoopEvents(projectDir, loopId);
  let stoppedIndex = -1;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index]?.type === "loopStopped") {
      stoppedIndex = index;
      break;
    }
  }
  if (stoppedIndex >= 0) {
    if (stoppedIndex !== events.length - 1) {
      throw new JriError(
        "Cannot resume because the recorded stop is not the latest loop event.",
        "resume-lineage-invalid",
        "Return to bare jri, confirm the requirements, then say just ralph it so audit and planning authorize a fresh lifecycle.",
      );
    }
    const stoppedEvent = events[stoppedIndex];
    if (stoppedEvent?.type !== "loopStopped") {
      throw new JriError(
        "Cannot resume because the next safe phase was not recorded.",
        "resume-phase-missing",
        "Return to bare jri, confirm the requirements, then say just ralph it so audit and planning authorize a fresh lifecycle.",
      );
    }
    const priorEvent = events[stoppedIndex - 1];
    if (!priorEvent || !isValidStopPredecessor(priorEvent, stoppedEvent)) {
      throw new JriError(
        "Cannot resume because the stop event is missing its prior phase milestone.",
        "resume-lineage-invalid",
        "Return to bare jri, confirm the requirements, then say just ralph it so audit and planning authorize a fresh lifecycle.",
      );
    }
    return stoppedEvent.data.nextPhase;
  }
  throw new JriError(
    "Cannot resume because the next safe phase was not recorded.",
    "resume-phase-missing",
    "Return to bare jri, confirm the requirements, then say just ralph it so audit and planning authorize a fresh lifecycle.",
  );
}

function isValidStopPredecessor(priorEvent: CoreEvent, stoppedEvent: Extract<CoreEvent, { type: "loopStopped" }>): boolean {
  if (stoppedEvent.data.nextPhase === "planning") {
    return priorEvent.type === "auditPassed" && priorEvent.loopId === stoppedEvent.loopId;
  }
  if (stoppedEvent.data.iteration !== undefined) {
    return priorEvent.type === "iterationFinished" && priorEvent.loopId === stoppedEvent.loopId && priorEvent.iteration === stoppedEvent.data.iteration;
  }
  return (
    priorEvent.loopId === stoppedEvent.loopId &&
    (priorEvent.type === "planningFinished" || priorEvent.type === "planRegenerationFinished")
  );
}

function resumePhaseFromBlocker(blocker: Blocker | undefined): RunnerPhase {
  if (blocker?.resumePhase === "planning" || blocker?.resumePhase === "building") return blocker.resumePhase;
  throw new JriError(
    "Cannot resume because the human-task blocker did not record a resume phase.",
    "resume-phase-missing",
    "Return to bare jri, confirm the requirements, then say just ralph it so audit and planning authorize a fresh lifecycle.",
  );
}

async function hasBlockerResolvedEvent(projectDir: string, loopId: string, reason: Blocker["reason"]): Promise<boolean> {
  return (await readLoopEvents(projectDir, loopId)).some((event) => event.type === "blockerResolved" && event.data.reason === reason);
}

function phaseToOperation(phase: RunnerPhase): LockOperation {
  if (phase === "auditing") return "audit";
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

async function transferStartupOwnership(
  projectDir: string,
  loopId: string,
  expectedLock: NonNullable<ProjectStatus["lock"]>,
  runner: RunnerProcess,
  now: Date,
): Promise<void> {
  const runnerLock = {
    ...expectedLock,
    pid: runner.pid,
    heartbeatAt: now.toISOString(),
    expiresAt: new Date(now.getTime() + 30_000).toISOString(),
  };
  await updateStatus(projectDir, (current) => {
    if (current.activeLoopId !== loopId || !locksMatch(current.lock, expectedLock)) {
      throw new JriError("The loop changed before runner ownership could be recorded.", "status-race", "Reload status and retry the start request.");
    }
    return {
      ...current,
      process: {
        pid: runner.pid,
        command: runner.command,
        startedAt: now.toISOString(),
      },
      lock: runnerLock,
    };
  });
}

async function waitForRunnerLock(
  projectDir: string,
  loopId: string,
  phase: RunnerPhase,
  initialStatus: ProjectStatus,
  options: RuntimeOptions,
): Promise<ProjectStatus["lock"]> {
  const expectedOperation = phaseToOperation(phase);
  let status = initialStatus;
  const deadline = Date.now() + 2_000;
  for (;;) {
    if (status.activeLoopId !== loopId || status.state !== phase) {
      throw new JriError("The JRI runner startup state changed before ownership was confirmed.", "lock-lost", "Resume the loop again so the daemon can start a fresh runner.");
    }
    if (status.lock?.pid === process.pid && status.lock.operation === expectedOperation) return status.lock;
    if (status.lock?.operation !== expectedOperation || Date.now() >= deadline) return status.lock;
    await sleep(options.observePollIntervalMs ?? 25);
    status = await readStatus(projectDir);
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

async function runAuditor(projectDir: string, loopId: string, options: RuntimeOptions): Promise<AuditorHandoff> {
  const handoff = await runAgentPhase(projectDir, loopId, "auditing", options);
  if (handoff.agent !== "auditor") {
    throw new JriError("The auditing harness returned a non-auditor handoff.", "invalid-agent-handoff", "Emit an auditor handoff for the auditing phase.");
  }
  return handoff;
}

async function runPlanner(projectDir: string, loopId: string, options: RuntimeOptions): Promise<PlannerHandoff> {
  const handoff = await runAgentPhase(projectDir, loopId, "planning", options);
  if (handoff.agent !== "planner") {
    throw new JriError("The planning harness returned a non-planner handoff.", "invalid-agent-handoff", "Emit a planner handoff for the planning phase.");
  }
  return handoff;
}

async function assertPlannerPlanPersisted(projectDir: string, handoff: Extract<PlannerHandoff, { action: "planned" }>): Promise<void> {
  const planPath = join(projectDir, handoff.planPath);
  try {
    const planStats = await stat(planPath);
    if (planStats.isFile()) return;
  } catch (error) {
    if (!error || typeof error !== "object" || !("code" in error) || error.code !== "ENOENT") throw error;
  }
  throw new JriError(
    `The planner reported success but did not create ${handoff.planPath}.`,
    "planner-plan-missing",
    "Rerun planning so the implementation plan is written before building starts.",
  );
}

async function runBuilder(projectDir: string, loopId: string, options: RuntimeOptions): Promise<BuilderHandoff> {
  const handoff = await runAgentPhase(projectDir, loopId, "building", options);
  if (handoff.agent !== "builder") {
    throw new JriError("The building harness returned a non-builder handoff.", "invalid-agent-handoff", "Emit a builder handoff for the building phase.");
  }
  return handoff;
}

async function runAgentPhase(projectDir: string, loopId: string, phase: RunnerPhase, options: RuntimeOptions): Promise<AgentHandoff> {
  throwIfRuntimeCancelled(options.signal);
  if (options.harnessRunner) {
    const stdoutOffset = await stdoutLogSize(projectDir, loopId);
    const exitCode = await runLegacyHarnessSession(projectDir, loopId, phase, options);
    if (exitCode !== 0) {
      await finishFailedRun(projectDir, loopId, phase, exitCode);
      throw new LoopAlreadyFinished();
    }
    if (phase === "auditing") return await readLatestAuditorHandoff(projectDir, loopId, stdoutOffset);
    if (phase === "planning") return await readLatestPlannerHandoff(projectDir, loopId, stdoutOffset);
    return await readLatestBuilderHandoff(projectDir, loopId, stdoutOffset);
  }

  const agent = agentForRunnerPhase(phase);
  const invocation: HarnessInvocation = {
    owner: { kind: "loop", loopId },
    projectDir,
    agent,
    phase,
    model: modelForAgent(await readProjectConfig(projectDir), agent),
    context: await buildLoopHarnessContext(projectDir, loopId, phase),
    capabilities: capabilitiesForRunnerPhase(phase),
    output: stdoutOutputSink(projectDir, loopId),
    signal: options.signal ?? new AbortController().signal,
  };
  assertHarnessCapabilities(invocation);
  const result = await (options.harnessAdapter ?? invokeDefaultHarness)(invocation);
  throwIfRuntimeCancelled(options.signal);
  return result.handoff;
}

class LoopAlreadyFinished extends Error {}

async function runLegacyHarnessSession(
  projectDir: string,
  loopId: string,
  phase: RunnerPhase,
  options: RuntimeOptions,
): Promise<number> {
  return await (options.harnessRunner ?? runControlledPiSession)({
    projectDir,
    loopId,
    phase,
    stdoutPath: join(projectDir, ".jri", "logs", loopId, "stdout.log"),
    ...(options.signal ? { signal: options.signal } : {}),
  });
}

function throwIfRuntimeCancelled(signal: AbortSignal | undefined): void {
  if (!signal?.aborted) return;
  throw new JriError("JRI runtime execution was cancelled.", "runtime-cancelled", "Retry the operation if it is still needed.");
}

async function buildLoopHarnessContext(
  projectDir: string,
  loopId: string,
  phase: RunnerPhase,
): Promise<{ refs: string[]; inline: string[] }> {
  const refs = new Set<string>([".jri/status.json"]);
  if (await relativePathExists(projectDir, ".jri/IMPLEMENTATION_PLAN.md")) refs.add(".jri/IMPLEMENTATION_PLAN.md");
  if (phase === "auditing") {
    if (await relativePathExists(projectDir, ".jri/interrogation-state.json")) refs.add(".jri/interrogation-state.json");
    if (await relativePathExists(projectDir, ".jri/scratchpad.md")) refs.add(".jri/scratchpad.md");
  }
  for (const specFile of await listSpecFiles(projectDir)) refs.add(specFile);
  return {
    refs: [...refs],
    inline: [`Loop ${loopId} phase ${phase}.`],
  };
}

async function listSpecFiles(projectDir: string): Promise<string[]> {
  const specsDir = join(projectDir, ".jri", "specs");
  try {
    return (await readdir(specsDir, { withFileTypes: true }))
      .filter((entry) => entry.isFile() && entry.name.endsWith(".md"))
      .map((entry) => `.jri/specs/${entry.name}`)
      .sort();
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") return [];
    throw error;
  }
}

async function relativePathExists(projectDir: string, path: string): Promise<boolean> {
  return await Bun.file(join(projectDir, path)).exists();
}

function agentForRunnerPhase(phase: RunnerPhase): AgentName {
  if (phase === "auditing") return "auditor";
  return phase === "planning" ? "planner" : "builder";
}

function capabilitiesForRunnerPhase(phase: RunnerPhase): CapabilityDescriptor[] {
  if (phase === "auditing") return [{ name: "web", operation: "search" }, { name: "web", operation: "fetch" }];
  return [{ name: "web", operation: "search" }, { name: "web", operation: "fetch" }, { name: "explorer" }];
}

function stdoutOutputSink(projectDir: string, loopId: string): HarnessOutputSink {
  const path = join(projectDir, ".jri", "logs", loopId, "stdout.log");
  return {
    write: async (chunk) => {
      if (!chunk) return;
      await mkdir(dirname(path), { recursive: true });
      await appendFile(path, chunk.endsWith("\n") ? chunk : `${chunk}\n`, "utf8");
    },
  };
}

type GitSnapshot = {
  head?: string;
  tags: string[];
  tagTargets: Record<string, string>;
  trackedTreeClean: boolean;
  dirtySummary?: string;
};

type IterationGitResult = {
  outcome: "committed" | "noChanges";
  commit?: string;
  tag?: string;
  tagIssue?: string;
  changedFiles?: string[];
};

async function readGitSnapshot(projectDir: string): Promise<GitSnapshot> {
  const head = await gitOutput(projectDir, ["rev-parse", "--verify", "HEAD"]);
  const tagTargets = parseGitTagTargets(await gitOutput(projectDir, ["for-each-ref", "--format=%(refname:short)%09%(objectname)", "refs/tags"]));
  const tags = Object.keys(tagTargets);
  const status = await gitOutput(projectDir, ["status", "--porcelain", "--untracked-files=no"]);
  if (status === undefined) {
    return { tags, tagTargets, trackedTreeClean: true };
  }
  const dirtySummary = status.trim();
  return {
    ...(head?.trim() ? { head: head.trim() } : {}),
    tags,
    tagTargets,
    trackedTreeClean: dirtySummary.length === 0,
    ...(dirtySummary ? { dirtySummary } : {}),
  };
}

async function hasUnexpectedGitMutation(projectDir: string, before: GitSnapshot): Promise<boolean> {
  const after = await readGitSnapshot(projectDir);
  if (after.head !== before.head) return true;
  return hasTagMutation(before, after);
}

function hasTagMutation(before: GitSnapshot, after: GitSnapshot): boolean {
  if (!sameStringSet(after.tags, before.tags)) return true;
  return after.tags.some((tag) => after.tagTargets[tag] !== before.tagTargets[tag]);
}

async function observeIterationGitResult(
  projectDir: string,
  loopId: string,
  iteration: number,
  before: GitSnapshot,
): Promise<IterationGitResult> {
  const afterHead = (await gitOutput(projectDir, ["rev-parse", "--verify", "HEAD"]))?.trim();
  if (!afterHead || afterHead === before.head) {
    const after = await readGitSnapshot(projectDir);
    if (hasTagMutation(before, after)) {
      return { outcome: "noChanges", tagIssue: "Builder changed git tags without creating an iteration commit." };
    }
    if ((after.dirtySummary ?? "") !== (before.dirtySummary ?? "")) {
      return { outcome: "noChanges", tagIssue: "Builder left tracked working-tree changes without creating an iteration commit." };
    }
    return { outcome: "noChanges" };
  }

  const commitCount = await countIterationCommits(projectDir, before.head, afterHead);
  if (commitCount === undefined) {
    return { outcome: "committed", commit: afterHead, tagIssue: "Builder changed git history in a way JRI could not validate against the iteration rollback commit." };
  }
  if (commitCount !== 1) {
    return {
      outcome: "committed",
      commit: afterHead,
      tagIssue: `Builder created ${commitCount} commits during one iteration; expected exactly one coherent iteration commit.`,
    };
  }

  const subject = (await gitOutput(projectDir, ["log", "-1", "--pretty=%s", afterHead]))?.trim();
  await appendLoopEvent(projectDir, {
    type: "commitCreated",
    loopId,
    iteration,
    data: {
      sha: afterHead,
      ...(subject ? { subject } : {}),
    },
  });

  const tagResult = await validateIterationTag(projectDir, afterHead, before.tags);
  if (tagResult.tag) {
    await appendLoopEvent(projectDir, {
      type: "tagCreated",
      loopId,
      iteration,
      data: { tag: tagResult.tag, sha: afterHead },
    });
  }

  return {
    outcome: "committed",
    commit: afterHead,
    ...(tagResult.tag ? { tag: tagResult.tag } : {}),
    ...(tagResult.issue ? { tagIssue: tagResult.issue } : {}),
  };
}

async function countIterationCommits(projectDir: string, beforeHead: string | undefined, afterHead: string): Promise<number | undefined> {
  const range = beforeHead ? `${beforeHead}..${afterHead}` : afterHead;
  const output = await gitOutput(projectDir, ["rev-list", "--count", range]);
  const count = output === undefined ? Number.NaN : Number(output.trim());
  return Number.isInteger(count) && count >= 0 ? count : undefined;
}

async function validateIterationTag(
  projectDir: string,
  afterHead: string,
  tagsBeforeIteration: string[],
): Promise<{ tag?: string; issue?: string }> {
  const expectedTag = nextPatchTag(tagsBeforeIteration);
  const tagsAfterIteration = parseGitLines(await gitOutput(projectDir, ["tag", "--list"]));
  const newSemverTags = tagsAfterIteration.filter((tag) => !tagsBeforeIteration.includes(tag) && isSemverPatchTag(tag));
  const tagsAtHead = parseGitLines(await gitOutput(projectDir, ["tag", "--points-at", afterHead]));
  if (newSemverTags.length > 1) {
    return { issue: `Builder created multiple new semantic-version tags (${newSemverTags.join(", ")}); expected exactly ${expectedTag} on the iteration commit.` };
  }
  if (tagsAtHead.includes(expectedTag)) return { tag: expectedTag };
  if (newSemverTags.includes(expectedTag)) {
    return { issue: `Builder created expected tag ${expectedTag}, but it does not point at the iteration commit.` };
  }
  return { issue: `Builder committed changes without creating expected semantic-version tag ${expectedTag} on the iteration commit.` };
}

function nextPatchTag(tags: string[]): string {
  const versions = tags.filter(isSemverPatchTag).map(parseSemverPatchTag);
  if (versions.length === 0) return "0.0.1";
  versions.sort((left, right) => left.major - right.major || left.minor - right.minor || left.patch - right.patch);
  const latest = versions[versions.length - 1];
  if (!latest) return "0.0.1";
  return `${latest.major}.${latest.minor}.${latest.patch + 1}`;
}

function isSemverPatchTag(tag: string): boolean {
  return /^\d+\.\d+\.\d+$/.test(tag);
}

function parseSemverPatchTag(tag: string): { major: number; minor: number; patch: number } {
  const [major, minor, patch] = tag.split(".").map((part) => Number(part));
  return { major: major ?? 0, minor: minor ?? 0, patch: patch ?? 0 };
}

function parseGitLines(text: string | undefined): string[] {
  return text
    ? text
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
    : [];
}

function parseGitTagTargets(text: string | undefined): Record<string, string> {
  const targets: Record<string, string> = {};
  for (const line of parseGitLines(text)) {
    const [tag, sha] = line.split("\t");
    if (tag && sha) targets[tag] = sha;
  }
  return targets;
}

function sameStringSet(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false;
  const rightSet = new Set(right);
  return left.every((value) => rightSet.has(value));
}

async function gitOutput(projectDir: string, args: string[]): Promise<string | undefined> {
  const proc = Bun.spawn(["git", ...args], {
    cwd: projectDir,
    stdout: "pipe",
    stderr: "ignore",
    stdin: "ignore",
  });
  const output = await new Response(proc.stdout).text();
  const exitCode = await proc.exited;
  return exitCode === 0 ? output : undefined;
}

async function resetTrackedFiles(projectDir: string, rollbackCommit: string): Promise<GitResetResult> {
  const proc = Bun.spawn(["git", "reset", "--hard", rollbackCommit], {
    cwd: projectDir,
    stdout: "pipe",
    stderr: "pipe",
    stdin: "ignore",
  });
  const [stdout, stderr, exitCode] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited]);
  if (exitCode === 0) return { succeeded: true };
  const error = [stdout.trim(), stderr.trim()].filter(Boolean).join("\n") || `git reset --hard exited with code ${exitCode}.`;
  return { succeeded: false, error };
}

function haltSummary(killed: HaltedProcesses, resetOffered: boolean, resetAccepted: boolean, resetResult: GitResetResult | undefined): string {
  const killedTargets = [
    ...(killed.runnerPid === undefined ? [] : [`runner pid ${killed.runnerPid}`]),
    ...(killed.childPids.length === 0 ? [] : [`child pid${killed.childPids.length === 1 ? "" : "s"} ${killed.childPids.join(", ")}`]),
  ];
  const halt = killedTargets.length === 0 ? "Loop halted; no live process was recorded." : `Loop halted by killing ${killedTargets.join(" and ")}.`;
  if (!resetOffered) return `${halt} No rollback reset was available.`;
  if (!resetAccepted) return `${halt} Rollback reset was skipped.`;
  if (resetResult?.succeeded) return `${halt} Rollback reset completed.`;
  return `${halt} Rollback reset failed${resetResult?.error ? `: ${resetResult.error}` : "."}`;
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
        validationPassed: false,
      },
    },
  });
}

async function finishRuntimeFailureRun(projectDir: string, loopId: string, phase: RunnerPhase, error: JriError): Promise<void> {
  const summary = `${phaseFailureLabel(phase)} failed: ${error.message}`;
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
    message: error.recovery,
  });
  await transitionStatus(projectDir, "stopped", {
    loopId,
    update: {
      ...ownershipCleared(await readStatus(projectDir)),
      stopRequested: false,
      lastResult: {
        outcome: "failed",
        summary,
        validationPassed: false,
      },
    },
  });
}

async function finishMissingExplorerProofRun(projectDir: string, loopId: string): Promise<void> {
  const summary = "Building failed: Ralph cannot complete without durable successful explorer delegation evidence.";
  const recovery =
    "Run at least one JRI explorer delegation during planning or building so the loop records subagentStarted and subagentFinished evidence before reporting completion.";
  await appendLoopEvent(projectDir, {
    type: "loopFinished",
    loopId,
    data: { outcome: "failed", summary },
    message: recovery,
  });
  await transitionStatus(projectDir, "stopped", {
    loopId,
    update: {
      ...ownershipCleared(await readStatus(projectDir)),
      stopRequested: false,
      lastResult: {
        outcome: "failed",
        summary,
        validationPassed: false,
      },
    },
  });
}

function isLoopFailureError(error: JriError): boolean {
  if (error.code.startsWith("capability-") || error.code.startsWith("web-capability-")) return true;
  return [
    "auth-required",
    "explorer-failed",
    "harness-failed",
    "harness-cancelled",
    "harness-timeout",
    "invalid-agent-handoff",
    "missing-agent-handoff",
    "multiple-agent-handoffs",
    "planner-plan-missing",
    "runtime-cancelled",
    "unsupported-harness-agent",
  ].includes(error.code);
}

function isCancellationFanoutError(error: JriError): boolean {
  return error.code === "runtime-cancelled" || error.code === "harness-cancelled" || error.code === "harness-timeout";
}

async function successfulExplorerProof(projectDir: string, loopId: string): Promise<{ used: boolean; summary?: string; artifactRef?: string } | undefined> {
  const events = await readLoopEvents(projectDir, loopId);
  const latestFinished = events
    .filter((event): event is Extract<CoreEvent, { type: "subagentFinished" }> => event.type === "subagentFinished" && event.data.agent === "explorer")
    .at(-1);
  if (!latestFinished) return undefined;
  return {
    used: true,
    summary: latestFinished.data.summary,
    ...(latestFinished.data.artifactRef ? { artifactRef: latestFinished.data.artifactRef } : {}),
  };
}

function phaseFailureLabel(phase: RunnerPhase): string {
  if (phase === "auditing") return "Auditing";
  if (phase === "planning") return "Planning";
  return "Building";
}

async function finishAuditFailedRun(projectDir: string, loopId: string, handoff: Extract<AuditorHandoff, { action: "failed" }>): Promise<void> {
  await appendLoopEvent(projectDir, {
    type: "auditFailed",
    loopId,
    data: {
      feedback: handoff.feedback,
      ...(handoff.ambiguousSpecFiles ? { ambiguousSpecFiles: handoff.ambiguousSpecFiles } : {}),
      ...(handoff.affectedTopics ? { affectedTopics: handoff.affectedTopics } : {}),
      ...(handoff.findings ? { findings: handoff.findings } : {}),
      questions: handoff.questions,
    },
  });
  const blocker: Blocker = {
    reason: "ambiguousSpecs",
    description: handoff.feedback,
    resolutionGuide: {
      summary: "The current specs are not ready for Ralph to build safely.",
      steps: handoff.questions,
      resumeInstruction: "Answer the audit questions in bare jri, then say just ralph it.",
    },
    ...(handoff.ambiguousSpecFiles ? { changedFiles: handoff.ambiguousSpecFiles } : {}),
    validationRan: false,
  };
  await appendLoopEvent(projectDir, {
    type: "blockerReported",
    loopId,
    data: {
      reason: blocker.reason,
      description: blocker.description,
      resolutionGuide: blocker.resolutionGuide,
      ...(blocker.changedFiles ? { changedFiles: blocker.changedFiles } : {}),
      validationRan: blocker.validationRan,
    },
  });
  await transitionStatus(projectDir, "blocked", {
    loopId,
    update: {
      ...ownershipCleared(await readStatus(projectDir)),
      stopRequested: false,
      blocker,
      lastResult: {
        outcome: "blocked",
        summary: handoff.feedback,
      },
    },
  });
}

async function readLatestAuditorHandoff(projectDir: string, loopId: string, offset: number): Promise<AuditorHandoff> {
  const path = join(projectDir, ".jri", "logs", loopId, "stdout.log");
  const currentSessionOutput = (await readFile(path, "utf8")).slice(offset);
  const handoff = extractLatestHandoffFromText("auditor", currentSessionOutput, "auditing");
  return handoff as AuditorHandoff;
}

async function finishBlockedRun(projectDir: string, loopId: string, blocker: Blocker): Promise<void> {
  const status = await readStatus(projectDir);
  const iteration = status.currentIteration?.iteration ?? status.iteration ?? 1;
  const changedFiles = mergeChangedFiles(blocker.changedFiles, await readChangedFiles(projectDir));
  const recordedBlocker = {
    ...blocker,
    ...(changedFiles.length > 0 ? { changedFiles } : {}),
    ...(blocker.reason === "needsHumanTask" ? { resumePhase: "building" as const } : {}),
  };

  await appendLoopEvent(projectDir, {
    type: "blockerReported",
    loopId,
    data: {
      reason: recordedBlocker.reason,
      description: recordedBlocker.description,
      resolutionGuide: recordedBlocker.resolutionGuide,
      ...(recordedBlocker.changedFiles ? { changedFiles: recordedBlocker.changedFiles } : {}),
      ...(recordedBlocker.validationRan === undefined ? {} : { validationRan: recordedBlocker.validationRan }),
    },
  });
  await appendLoopEvent(projectDir, {
    type: "iterationFinished",
    loopId,
    iteration,
    data: {
      outcome: "blocked",
      ...(recordedBlocker.changedFiles ? { changedFiles: recordedBlocker.changedFiles } : {}),
    },
  });
  await transitionStatus(projectDir, "blocked", {
    loopId,
    update: {
      ...ownershipCleared(status),
      stopRequested: false,
      blocker: recordedBlocker,
      lastResult: {
        outcome: "blocked",
        summary: recordedBlocker.description,
      },
    },
  });
}

async function finishPlanningBlockedRun(projectDir: string, loopId: string, blocker: Blocker): Promise<void> {
  await appendLoopEvent(projectDir, {
    type: "blockerReported",
    loopId,
    data: {
      reason: blocker.reason,
      description: blocker.description,
      resolutionGuide: blocker.resolutionGuide,
      ...(blocker.changedFiles ? { changedFiles: blocker.changedFiles } : {}),
      ...(blocker.validationRan === undefined ? {} : { validationRan: blocker.validationRan }),
    },
  });
  await transitionStatus(projectDir, "blocked", {
    loopId,
    update: {
      ...ownershipCleared(await readStatus(projectDir)),
      stopRequested: false,
      blocker: {
        ...blocker,
        ...(blocker.reason === "needsHumanTask" ? { resumePhase: "planning" as const } : {}),
      },
      lastResult: {
        outcome: "blocked",
        summary: blocker.description,
      },
    },
  });
}

async function finishValidationFailedRun(projectDir: string, loopId: string, handoff: Extract<BuilderHandoff, { action: "failedValidation" }>): Promise<void> {
  const status = await readStatus(projectDir);
  const iteration = status.currentIteration?.iteration ?? status.iteration ?? 1;
  await appendLoopEvent(projectDir, {
    type: "iterationFinished",
    loopId,
    iteration,
    data: { outcome: "validationFailed" },
  });
  const summary = handoff.summary ?? handoff.validation.summary;
  await appendLoopEvent(projectDir, {
    type: "loopFinished",
    loopId,
    data: { outcome: "failed", summary },
  });
  await transitionStatus(projectDir, "stopped", {
    loopId,
    update: {
      ...ownershipCleared(status),
      stopRequested: false,
      lastResult: {
        outcome: "failed",
        summary,
        validationPassed: false,
      },
    },
  });
}

async function finishUnexpectedGitMutationRun(
  projectDir: string,
  loopId: string,
  iteration: number,
  handoffAction: "blocked" | "failedValidation",
): Promise<void> {
  const changedFiles = await readChangedFiles(projectDir);
  const summary = `Builder reported ${handoffAction}, but git commits or tags changed during the iteration. Inspect the working tree before resuming.`;
  await appendLoopEvent(projectDir, {
    type: "iterationFinished",
    loopId,
    iteration,
    data: {
      outcome: "validationFailed",
      ...(changedFiles.length > 0 ? { changedFiles } : {}),
    },
  });
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
        validationPassed: false,
      },
    },
  });
}

async function finishUnsafeGitSuccessRun(projectDir: string, loopId: string, iteration: number): Promise<void> {
  const changedFiles = await readChangedFiles(projectDir);
  const summary = "Builder changed git history or tags without passing validation evidence. Inspect the changes and rerun validation before resuming.";
  await appendLoopEvent(projectDir, {
    type: "iterationFinished",
    loopId,
    iteration,
    data: {
      outcome: "validationFailed",
      ...(changedFiles.length > 0 ? { changedFiles } : {}),
    },
  });
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
        validationPassed: false,
      },
    },
  });
}

async function finishInvalidSuccessfulValidationRun(projectDir: string, loopId: string, iteration: number): Promise<void> {
  const changedFiles = await readChangedFiles(projectDir);
  const summary = "Builder reported a successful handoff with failed validation evidence. Inspect the validation output before resuming.";
  await appendLoopEvent(projectDir, {
    type: "iterationFinished",
    loopId,
    iteration,
    data: {
      outcome: "validationFailed",
      ...(changedFiles.length > 0 ? { changedFiles } : {}),
    },
  });
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
        validationPassed: false,
      },
    },
  });
}

async function finishUnsafeGitTagRun(
  projectDir: string,
  loopId: string,
  iteration: number,
  issue: string,
  validationPassed: boolean,
): Promise<void> {
  const changedFiles = await readChangedFiles(projectDir);
  const summary = `${issue} Inspect the commit and tag state before resuming.`;
  await appendLoopEvent(projectDir, {
    type: "iterationFinished",
    loopId,
    iteration,
    data: {
      outcome: "validationFailed",
      ...(changedFiles.length > 0 ? { changedFiles } : {}),
    },
  });
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
        validationPassed,
      },
    },
  });
}

function hasPassingValidation(validations: ValidationHandoff[]): boolean {
  return validations.some((validation) => validation.passed);
}

function hasFailingValidation(validations: ValidationHandoff[]): boolean {
  return validations.some((validation) => !validation.passed);
}

async function readLatestPlannerHandoff(projectDir: string, loopId: string, offset: number): Promise<PlannerHandoff> {
  const path = join(projectDir, ".jri", "logs", loopId, "stdout.log");
  const currentSessionOutput = (await readFile(path, "utf8")).slice(offset);
  const handoff = extractLatestHandoffFromText("planner", currentSessionOutput, "planning");
  return handoff as PlannerHandoff;
}

async function readLatestBuilderHandoff(projectDir: string, loopId: string, offset: number): Promise<BuilderHandoff> {
  const path = join(projectDir, ".jri", "logs", loopId, "stdout.log");
  const currentSessionOutput = (await readFile(path, "utf8")).slice(offset);
  return extractLatestBuilderHandoffFromText(currentSessionOutput, "building");
}

async function recordValidationEvidence(projectDir: string, loopId: string, iteration: number, validations: ValidationHandoff[]): Promise<void> {
  for (const validation of validations) {
    await appendLoopEvent(projectDir, {
      type: "validationStarted",
      loopId,
      iteration,
      data: { command: validation.command },
    });
    await appendLoopEvent(projectDir, {
      type: "validationFinished",
      loopId,
      iteration,
      data: {
        command: validation.command,
        exitCode: validation.exitCode,
        passed: validation.passed,
        ...(validation.artifacts ? { artifacts: validation.artifacts } : {}),
      },
      message: validation.summary,
    });
  }
}

function validationEvidenceFromBuilder(handoff: BuilderHandoff): ValidationHandoff[] {
  if (handoff.action === "failedValidation") return [handoff.validation];
  return handoff.validation ?? [];
}

async function computeAuthorizedSpecsFingerprint(projectDir: string, handoff: Extract<AuditorHandoff, { action: "passed" }>): Promise<string> {
  const authorizedSpecsFingerprint = await computeSpecsFingerprint(projectDir);
  if (handoff.specsFingerprint !== authorizedSpecsFingerprint) {
    throw new JriError(
      "The auditor reported a specs fingerprint that does not match the current specs.",
      "invalid-agent-handoff",
      "Rerun auditing against the current .jri/specs/*.md contents; JRI authorizes only its core-computed specs fingerprint.",
    );
  }
  return authorizedSpecsFingerprint;
}

async function computeSpecsFingerprint(projectDir: string): Promise<string> {
  const specsDir = join(projectDir, ".jri", "specs");
  const hash = createHash("sha256");
  try {
    const specsStats = await stat(specsDir);
    if (!specsStats.isDirectory()) return hash.digest("hex");
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") return hash.digest("hex");
    throw error;
  }

  const entries = (await readdir(specsDir, { withFileTypes: true }))
    .filter((entry) => entry.isFile() && entry.name.endsWith(".md"))
    .map((entry) => entry.name)
    .sort();

  for (const name of entries) {
    hash.update(name);
    hash.update("\0");
    hash.update(await readFile(join(specsDir, name)));
    hash.update("\0");
  }
  return hash.digest("hex");
}

async function readChangedFiles(projectDir: string): Promise<string[]> {
  const status = await gitOutput(projectDir, ["status", "--porcelain"]);
  if (!status) return [];
  return status
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => line.slice(3).trim())
    .filter(Boolean);
}

async function stdoutLogSize(projectDir: string, loopId: string): Promise<number> {
  try {
    return (await stat(join(projectDir, ".jri", "logs", loopId, "stdout.log"))).size;
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") return 0;
    throw error;
  }
}

function mergeChangedFiles(reported: string[] | undefined, observed: string[]): string[] {
  return [...new Set([...(reported ?? []), ...observed])].sort();
}

function ownershipCleared(status: ProjectStatus): { [K in keyof ProjectStatus]?: ProjectStatus[K] | undefined } {
  return { ...status, process: undefined, lock: undefined };
}
