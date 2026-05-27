import { stat } from "node:fs/promises";
import { isAbsolute, join, normalize, sep } from "node:path";
import { daemonStartLoop } from "./daemon-ipc";
import type { RuntimeOptions } from "./daemon-runtime";
import { JriError } from "./errors";
import { invokeDefaultHarness, readProjectConfig, type HarnessAdapter } from "./harness";
import { checkInterrogationStartGate, listSpecFiles, readInterrogationState, recordInterrogatorSpecUpdate } from "./interrogation-state";
import { modelForAgent } from "./prompts";
import { appendInterrogationEvent, appendLoopEvent, readStatus, updateStatus } from "./runtime-state";
import type { Blocker, ChatInput, CoreEvent, HumanTaskVerificationHandoff, InterrogatorHandoff, ProjectStatus } from "./types";

const interrogationLogPath = ".jri/logs/interrogation.jsonl" as const;
export type StartTrigger = "just ralph it" | "ralfealo";

export type HumanTaskVerifier = (request: {
  projectDir: string;
  blocker: Blocker;
  userMessage: string;
  status: ProjectStatus;
}) => Promise<HumanTaskVerificationHandoff> | HumanTaskVerificationHandoff;

export type ChatRuntimeOptions = RuntimeOptions & {
  verifyHumanTask?: HumanTaskVerifier;
  startLoop?: (projectDir: string, trigger: StartTrigger, options: RuntimeOptions) => AsyncIterable<CoreEvent>;
  interrogatorHarness?: HarnessAdapter;
};

export async function* sendChat(projectDir: string, input: ChatInput, options: ChatRuntimeOptions = {}): AsyncIterable<CoreEvent> {
  const message = input.message.trim();
  if (!message) {
    const startGate = await checkInterrogationStartGate(projectDir, options.now ? { now: options.now } : {});
    yield* emitAssistant(
      projectDir,
      startGate.ok
        ? "Send a message, clarify the specs, say done after a human-task blocker is resolved, or say just ralph it when the specs are ready."
        : reconciliationPrompt(startGate),
    );
    return;
  }

  yield await recordTurn(projectDir, "user", input.message);

  const status = await readStatus(projectDir);
  if (isActiveLoopState(status)) {
    yield* runInterrogator(projectDir, message, options, { mode: "observation", status });
    return;
  }

  if (isDoneMessage(message)) {
    yield* handleDone(projectDir, status, message, options);
    return;
  }

  const startGate = await checkInterrogationStartGate(projectDir, options.now ? { now: options.now } : {});
  const trigger = normalizeStartTrigger(message);
  if (trigger) {
    if (!startGate.ok) {
      yield* emitAssistant(projectDir, reconciliationPrompt(startGate));
      return;
    }
    yield* emitAssistant(projectDir, `Start request accepted (${trigger}). Running the specs auditor now.`);
    yield* startLoop(projectDir, trigger, options);
    return;
  }

  if (options.interrogatorHarness) {
    if (!startGate.ok) yield* emitAssistant(projectDir, reconciliationPrompt(startGate));
    yield* runInterrogator(projectDir, message, options);
    return;
  }

  yield* emitAssistant(projectDir, responseForStatus(status));
}

async function* runInterrogator(
  projectDir: string,
  message: string,
  options: ChatRuntimeOptions,
  observation?: { mode: "observation"; status: ProjectStatus },
): AsyncIterable<CoreEvent> {
  const harness = options.interrogatorHarness ?? invokeDefaultHarness;
  const started = await appendInterrogationEvent(projectDir, {
    type: "chatMessageStarted",
    data: { role: "assistant" },
  });
  yield started;
  const context = await buildInterrogatorContext(projectDir, message, observation);

  const chunks: string[] = [];
  const result = await harness({
    owner: { kind: "chat", turnId: started.id },
    projectDir,
    agent: "interrogator",
    phase: "interrogation",
    model: modelForAgent(await readProjectConfig(projectDir), "interrogator"),
    context,
    capabilities: [{ name: "web", operation: "search" }, { name: "web", operation: "fetch" }],
    output: {
      write: (chunk) => {
        if (chunk) chunks.push(chunk);
      },
    },
    signal: options.signal ?? new AbortController().signal,
  });

  const handoff = result.handoff;
  if (handoff.agent !== "interrogator") {
    throw new Error("Interrogation harness returned a non-interrogator handoff.");
  }
  const assistantText = chunks.join("").trim() || assistantTextForInterrogatorHandoff(handoff);
  yield await appendInterrogationEvent(projectDir, {
    type: "chatMessageDelta",
    data: { role: "assistant", text: assistantText },
    message: assistantText,
  });
  yield await appendInterrogationEvent(projectDir, {
    type: "chatMessageFinished",
    data: { role: "assistant" },
  });
  yield await recordTurn(projectDir, "assistant", assistantText);
  if (observation) {
    yield* handleObservationHandoff(projectDir, handoff);
    return;
  }
  yield* handleInterrogatorHandoff(projectDir, message, handoff, options);
}

async function buildInterrogatorContext(
  projectDir: string,
  message: string,
  observation?: { mode: "observation"; status: ProjectStatus },
): Promise<{ refs: string[]; inline: string[] }> {
  const refs = new Set<string>();
  if (await relativePathExists(projectDir, ".jri/status.json")) refs.add(".jri/status.json");

  const state = await readInterrogationState(projectDir);
  if (state) refs.add(".jri/interrogation-state.json");

  for (const specFile of await listSpecFiles(projectDir)) refs.add(specFile);
  if (await relativePathExists(projectDir, ".jri/scratchpad.md")) refs.add(".jri/scratchpad.md");
  if (observation?.status.activeLoopId) {
    const loopId = observation.status.activeLoopId;
    if (await relativePathExists(projectDir, ".jri/IMPLEMENTATION_PLAN.md")) refs.add(".jri/IMPLEMENTATION_PLAN.md");
    if (await relativePathExists(projectDir, `.jri/logs/${loopId}/events.jsonl`)) refs.add(`.jri/logs/${loopId}/events.jsonl`);
    if (await relativePathExists(projectDir, `.jri/logs/${loopId}/stdout.log`)) refs.add(`.jri/logs/${loopId}/stdout.log`);
  }

  const inline = [message];
  if (observation) {
    inline.push(
      [
        "Observation mode restrictions:",
        `JRI is currently ${observation.status.state} for loop ${observation.status.activeLoopId ?? "unknown"}.`,
        "You may explain status, logs, specs, and the implementation plan; record durable notes only in .jri/scratchpad.md; and suggest jri loop stop for a graceful stop.",
        "You must not mutate .jri/specs/*, trigger replanning, authorize a new lifecycle, or change active requirements.",
        'Emit messageOnly when you only answer, or scratchpadUpdated only if you updated .jri/scratchpad.md. Do not emit specsUpdated or startRequested in observation mode.',
      ].join("\n"),
    );
  }
  const needsRecentTurns =
    !state ||
    Object.values(state.topics).some((topic) => topic.status === "open" || Boolean(topic.pendingReconciliation));
  const recentTurns = needsRecentTurns ? await recentInterrogationTurns(projectDir) : "";
  if (recentTurns) {
    refs.add(".jri/logs/interrogation.jsonl#recent-unsealed-turns");
    inline.push(recentTurns);
  }

  return { refs: [...refs], inline };
}

async function* handleObservationHandoff(projectDir: string, handoff: InterrogatorHandoff): AsyncIterable<CoreEvent> {
  if (handoff.action === "messageOnly") return;

  if (handoff.action === "scratchpadUpdated") {
    await assertScratchpadExists(projectDir);
    yield await appendInterrogationEvent(projectDir, {
      type: "scratchpadUpdated",
      data: { scratchpadPath: ".jri/scratchpad.md", summary: handoff.summary },
    });
    return;
  }

  throw new JriError(
    `The interrogator returned ${handoff.action} while JRI is in observation mode.`,
    "invalid-observation-handoff",
    "Observation mode may answer from current status/logs/specs or update .jri/scratchpad.md, but it cannot update specs, verify blockers, or start Ralph.",
  );
}

async function recentInterrogationTurns(projectDir: string): Promise<string> {
  const path = join(projectDir, interrogationLogPath);
  if (!(await pathExists(path))) return "";
  const turns = (await Bun.file(path).text())
    .split("\n")
    .filter(Boolean)
    .flatMap((line) => {
      try {
        const event = JSON.parse(line) as { type?: string; data?: { role?: string; content?: string } };
        if (event.type !== "chatTurnRecorded" || !event.data?.content) return [];
        return [`${event.data.role ?? "unknown"}: ${event.data.content}`];
      } catch {
        return [];
      }
    })
    .slice(-8);
  if (turns.length === 0) return "";
  return ["Recent unsealed interrogation turns:", ...turns].join("\n");
}

async function* handleInterrogatorHandoff(
  projectDir: string,
  message: string,
  handoff: InterrogatorHandoff,
  options: ChatRuntimeOptions,
): AsyncIterable<CoreEvent> {
  if (handoff.action === "specsUpdated") {
    await recordInterrogatorSpecUpdate(projectDir, handoff.specFiles, handoff.sealedSpecFiles ? { sealedSpecFiles: handoff.sealedSpecFiles } : {});
    yield await appendInterrogationEvent(projectDir, {
      type: "specsUpdated",
      data: { specFiles: handoff.specFiles, summary: handoff.summary, ...(handoff.sealedSpecFiles ? { sealedSpecFiles: handoff.sealedSpecFiles } : {}) },
    });
    return;
  }

  if (handoff.action === "scratchpadUpdated") {
    await assertScratchpadExists(projectDir);
    yield await appendInterrogationEvent(projectDir, {
      type: "scratchpadUpdated",
      data: { scratchpadPath: ".jri/scratchpad.md", summary: handoff.summary },
    });
    return;
  }

  if (handoff.action === "humanTaskVerified") {
    const status = await readStatus(projectDir);
    await markHumanTaskVerified(projectDir, status, handoff.verificationSummary);
    return;
  }

  if (handoff.action === "humanTaskStillBlocked") {
    await updateStatus(projectDir, (current) => {
      if (current.state !== "blocked" || current.blocker?.reason !== "needsHumanTask") return current;
      return { ...current, blocker: handoff.blocker };
    });
    return;
  }

  if (handoff.action !== "startRequested") return;

  const trigger = normalizeStartTrigger(message);
  if (trigger !== handoff.trigger) {
    yield* emitAssistant(
      projectDir,
      "I recorded your note. Ralph only starts after a standalone just ralph it or ralfealo message, so this start request was ignored.",
    );
    return;
  }

  const startGate = await checkInterrogationStartGate(projectDir, options.now ? { now: options.now } : {});
  if (!startGate.ok) {
    yield* emitAssistant(projectDir, reconciliationPrompt(startGate));
    return;
  }

  yield* emitAssistant(projectDir, `Start request accepted (${trigger}). Running the specs auditor now.`);
  yield* startLoop(projectDir, trigger, options);
}

function startLoop(projectDir: string, trigger: StartTrigger, options: ChatRuntimeOptions): AsyncIterable<CoreEvent> {
  return (options.startLoop ?? daemonStartLoop)(projectDir, trigger, options);
}

export function normalizeStartTrigger(message: string): StartTrigger | null {
  const normalized = message.trim().replace(/[.!?]+$/u, "").trim().toLowerCase();
  if (normalized === "just ralph it") return "just ralph it";
  if (normalized === "ralfealo") return "ralfealo";
  return null;
}

async function* handleDone(projectDir: string, status: ProjectStatus, userMessage: string, options: ChatRuntimeOptions): AsyncIterable<CoreEvent> {
  if (status.state === "blocked" && status.blocker?.reason === "needsHumanTask") {
    const verification = await (options.verifyHumanTask ?? defaultHumanTaskVerifier)({
      projectDir,
      blocker: status.blocker,
      userMessage,
      status,
    });

    if (verification.action === "stillBlocked") {
      await updateStatus(projectDir, (current) => {
        if (current.state !== "blocked" || current.blocker?.reason !== "needsHumanTask") return current;
        return {
          ...current,
          blocker: verification.blocker,
        };
      });
      yield* emitAssistant(
        projectDir,
        [
          "I could not verify the human task is complete yet, so JRI remains blocked.",
          verification.blocker.resolutionGuide.summary,
          `Next step: ${verification.blocker.resolutionGuide.steps[0] ?? verification.blocker.resolutionGuide.resumeInstruction}`,
          `Resume: ${verification.blocker.resolutionGuide.resumeInstruction}`,
        ].join("\n"),
      );
      return;
    }

    const resolved = await markHumanTaskVerified(projectDir, status, verification.verificationSummary);
    if (resolved) yield resolved;
    yield* emitAssistant(projectDir, "Marked the human task as verified. Run jri loop resume to continue the existing lifecycle.");
    return;
  }

  if (status.state === "blocked" && status.blocker?.reason === "ambiguousSpecs") {
    yield* emitAssistant(
      projectDir,
      [
        "The current blocker is ambiguous specs, not a human task, so done cannot resume Ralph.",
        status.blocker.resolutionGuide.summary,
        `Next step: ${status.blocker.resolutionGuide.steps[0] ?? status.blocker.resolutionGuide.resumeInstruction}`,
        `Resume: ${status.blocker.resolutionGuide.resumeInstruction}`,
      ].join("\n"),
    );
    return;
  }

  yield* emitAssistant(projectDir, "There is no unresolved human-task blocker to verify. I recorded your message.");
}

async function markHumanTaskVerified(projectDir: string, _status: ProjectStatus, verificationSummary?: string): Promise<CoreEvent | undefined> {
  const verifiedAt = new Date().toISOString();
  await updateStatus(projectDir, (current) => {
    if (current.state !== "blocked" || current.blocker?.reason !== "needsHumanTask") return current;
    return {
      ...current,
      blocker: {
        ...current.blocker,
        resolution: {
          status: "verified",
          verifiedAt,
          verificationSummary: verificationSummary ?? "JRI verified the required human task after the user said done.",
        },
      },
    };
  });

  return undefined;
}

function assistantTextForInterrogatorHandoff(handoff: InterrogatorHandoff): string {
  if (handoff.action === "specsUpdated" || handoff.action === "scratchpadUpdated") return handoff.summary;
  if (handoff.action === "humanTaskVerified") return handoff.verificationSummary ?? "The human task is verified.";
  if (handoff.action === "humanTaskStillBlocked") return handoff.blocker.resolutionGuide.summary;
  if (handoff.action === "startRequested") return `Start request accepted (${handoff.trigger}).`;
  return handoff.summary ?? "I recorded your note.";
}

async function defaultHumanTaskVerifier(request: { projectDir: string; blocker: Blocker }): Promise<HumanTaskVerificationHandoff> {
  const criteria = request.blocker.resolutionGuide.successCriteria ?? [];
  if (criteria.length > 0) {
    const checks = await Promise.all(criteria.map((criterion) => evaluateHumanTaskCriterion(request.projectDir, criterion)));
    const failed = checks.filter((check) => !check.passed);
    if (failed.length === 0) {
      return {
        agent: "verifier",
        action: "verified",
        verificationSummary: `Verified ${checks.length} machine-checkable success ${checks.length === 1 ? "criterion" : "criteria"}.`,
      };
    }

    return stillBlockedWithVerificationGuide(
      request.blocker,
      "JRI could not verify the human task is complete yet.",
      failed.map((check) => check.message),
    );
  }

  return stillBlockedWithVerificationGuide(request.blocker, "JRI has no machine-checkable success criteria for this human task.", [
    "Add observable success criteria to the blocker, such as an environment variable presence check or project-relative file/path existence check.",
  ]);
}

type HumanTaskCriterionCheck = {
  passed: boolean;
  message: string;
};

async function evaluateHumanTaskCriterion(projectDir: string, criterion: string): Promise<HumanTaskCriterionCheck> {
  const text = criterion.trim();
  const envName = parseEnvPresenceCriterion(text);
  if (envName) {
    return process.env[envName]
      ? { passed: true, message: `${envName} is present in the current process environment.` }
      : { passed: false, message: `Set environment variable ${envName} where JRI runs, then say done again.` };
  }

  const relativePath = parsePathExistsCriterion(text);
  if (relativePath) {
    if (!isSafeProjectRelativePath(relativePath)) {
      return { passed: false, message: `Use a project-relative verification path inside this project instead of ${relativePath}.` };
    }
    return (await relativePathExists(projectDir, relativePath))
      ? { passed: true, message: `${relativePath} exists.` }
      : { passed: false, message: `Create ${relativePath} or update the blocker with observable evidence JRI can inspect.` };
  }

  return {
    passed: false,
    message: `JRI does not know how to verify this success criterion safely: ${text}`,
  };
}

function parseEnvPresenceCriterion(criterion: string): string | undefined {
  const patterns = [
    /^(?:env|environment variable)\s+([A-Z_][A-Z0-9_]*)\s+(?:is\s+)?(?:set|present|configured)\.?$/iu,
    /^([A-Z_][A-Z0-9_]*)\s+(?:env|environment variable)\s+(?:is\s+)?(?:set|present|configured)\.?$/iu,
  ];
  for (const pattern of patterns) {
    const match = criterion.match(pattern);
    if (match?.[1]) return match[1].toUpperCase();
  }
  return undefined;
}

function parsePathExistsCriterion(criterion: string): string | undefined {
  const patterns = [/^(?:file|path)\s+exists:\s*(.+)$/iu, /^(.+)\s+(?:file|path)\s+exists\.?$/iu];
  for (const pattern of patterns) {
    const match = criterion.match(pattern);
    if (match?.[1]) return normalizeProjectRelativePath(match[1]);
  }
  return undefined;
}

function normalizeProjectRelativePath(path: string): string {
  return path.trim().replace(/^["']|["']$/g, "").replaceAll("\\", "/").replace(/^\.\//u, "");
}

function isSafeProjectRelativePath(path: string): boolean {
  const normalized = normalize(path);
  return Boolean(path) && !isAbsolute(path) && normalized !== ".." && !normalized.startsWith(`..${sep}`);
}

function stillBlockedWithVerificationGuide(blocker: Blocker, summary: string, steps: string[]): HumanTaskVerificationHandoff {
  return {
    agent: "verifier",
    action: "stillBlocked",
    blocker: {
      ...blocker,
      resolutionGuide: {
        ...blocker.resolutionGuide,
        summary,
        steps: [...blocker.resolutionGuide.steps, ...steps],
        resumeInstruction: blocker.resolutionGuide.resumeInstruction,
      },
    },
  };
}

function reconciliationPrompt(startGate: Extract<Awaited<ReturnType<typeof checkInterrogationStartGate>>, { ok: false }>): string {
  const pending = startGate.pending[0];
  const summary = pending?.topic.pendingReconciliation?.summary ?? "A pending spec reconciliation must be resolved before Ralph can start.";
  return [
    "Ralph cannot start until pending spec reconciliation is resolved.",
    summary,
    "Clarify the changed requirement in bare jri, then say just ralph it again when the specs are ready.",
  ].join("\n");
}

async function assertScratchpadExists(projectDir: string): Promise<void> {
  const path = join(projectDir, ".jri", "scratchpad.md");
  try {
    const info = await stat(path);
    if (info.isFile()) return;
  } catch {
    // Normalize missing or inaccessible scratchpad evidence below.
  }
  throw new JriError(
    "The interrogator reported a scratchpad update, but .jri/scratchpad.md does not exist.",
    "missing-updated-scratchpad",
    "Write .jri/scratchpad.md before emitting a scratchpadUpdated handoff.",
  );
}

function responseForStatus(status: ProjectStatus): string {
  if (status.state === "blocked" && status.blocker) {
    const guide = status.blocker.resolutionGuide;
    return [
      `JRI is blocked: ${status.blocker.description}`,
      guide.summary,
      `Next step: ${guide.steps[0] ?? guide.resumeInstruction}`,
      `Resume: ${guide.resumeInstruction}`,
    ].join("\n");
  }

  if (status.state === "stopped") {
    return "JRI is stopped. Run jri loop resume to continue if the specs have not changed, or clarify the specs here and say just ralph it to authorize a new audit.";
  }

  if (status.state === "halted") {
    return "JRI is halted. Clarify or confirm the current requirements here, then say just ralph it to authorize a new lifecycle.";
  }

  if (status.state === "auditing" || status.state === "planning" || status.state === "building") {
    return "JRI is already running. Use jri loop attach to observe it or jri loop stop to request a graceful stop.";
  }

  return "I recorded your note. Keep clarifying the specs here, then say just ralph it when the current scope is unambiguous.";
}

function isActiveLoopState(status: ProjectStatus): boolean {
  return status.state === "auditing" || status.state === "planning" || status.state === "building";
}

async function* emitAssistant(projectDir: string, text: string): AsyncIterable<CoreEvent> {
  yield await appendInterrogationEvent(projectDir, {
    type: "chatMessageStarted",
    data: { role: "assistant" },
  });
  yield await appendInterrogationEvent(projectDir, {
    type: "chatMessageDelta",
    data: { role: "assistant", text },
    message: text,
  });
  yield await appendInterrogationEvent(projectDir, {
    type: "chatMessageFinished",
    data: { role: "assistant" },
  });
  yield await recordTurn(projectDir, "assistant", text);
}

async function recordTurn(projectDir: string, role: "user" | "assistant", content: string): Promise<CoreEvent> {
  return await appendInterrogationEvent(projectDir, {
    type: "chatTurnRecorded",
    data: { role, logPath: interrogationLogPath, content },
  });
}

function isDoneMessage(message: string): boolean {
  return message.trim().replace(/[.!?]+$/u, "").trim().toLowerCase() === "done";
}

async function relativePathExists(projectDir: string, relativePath: string): Promise<boolean> {
  return await pathExists(join(projectDir, relativePath));
}

async function pathExists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}
