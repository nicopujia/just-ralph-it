import { stat } from "node:fs/promises";
import { join } from "node:path";
import { startRalphLoop, type RuntimeOptions } from "./daemon-runtime";
import { JriError } from "./errors";
import { invokeDefaultHarness, readProjectConfig, type HarnessAdapter } from "./harness";
import { checkInterrogationStartGate, recordInterrogatorSpecUpdate } from "./interrogation-state";
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
    yield* emitAssistant(projectDir, "Send a message, clarify the specs, say done after a human-task blocker is resolved, or say just ralph it when the specs are ready.");
    return;
  }

  yield await recordTurn(projectDir, "user", input.message);

  const status = await readStatus(projectDir);
  if (isDoneMessage(message)) {
    yield* handleDone(projectDir, status, message, options);
    return;
  }

  if (options.interrogatorHarness) {
    yield* runInterrogator(projectDir, message, options);
    return;
  }

  const trigger = normalizeStartTrigger(message);
  if (trigger) {
    const startGate = await checkInterrogationStartGate(projectDir, options.now ? { now: options.now } : {});
    if (!startGate.ok) {
      const pending = startGate.pending[0];
      const summary = pending?.topic.pendingReconciliation?.summary ?? "A pending spec reconciliation must be resolved before Ralph can start.";
      yield* emitAssistant(
        projectDir,
        [
          "Ralph cannot start until pending spec reconciliation is resolved.",
          summary,
          "Clarify the changed requirement in bare jri, then say just ralph it again when the specs are ready.",
        ].join("\n"),
      );
      return;
    }
    yield* emitAssistant(projectDir, `Start request accepted (${trigger}). Running the specs auditor now.`);
    yield* (options.startLoop ?? startLoopLocally)(projectDir, trigger, options);
    return;
  }

  yield* emitAssistant(projectDir, responseForStatus(status));
}

async function* runInterrogator(projectDir: string, message: string, options: ChatRuntimeOptions): AsyncIterable<CoreEvent> {
  const harness = options.interrogatorHarness ?? invokeDefaultHarness;
  const started = await appendInterrogationEvent(projectDir, {
    type: "chatMessageStarted",
    data: { role: "assistant" },
  });
  yield started;

  const chunks: string[] = [];
  const result = await harness({
    owner: { kind: "chat", turnId: started.id },
    projectDir,
    agent: "interrogator",
    phase: "interrogation",
    model: modelForAgent(await readProjectConfig(projectDir), "interrogator"),
    context: {
      refs: [".jri/specs", ".jri/scratchpad.md", ".jri/status.json", ".jri/logs/interrogation.jsonl"],
      inline: [message],
    },
    capabilities: [],
    output: {
      write: (chunk) => {
        if (chunk) chunks.push(chunk);
      },
    },
    signal: new AbortController().signal,
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
  yield* handleInterrogatorHandoff(projectDir, handoff, options);
}

async function* handleInterrogatorHandoff(
  projectDir: string,
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

  const startGate = await checkInterrogationStartGate(projectDir, options.now ? { now: options.now } : {});
  if (!startGate.ok) {
    const pending = startGate.pending[0];
    const summary = pending?.topic.pendingReconciliation?.summary ?? "A pending spec reconciliation must be resolved before Ralph can start.";
    yield* emitAssistant(
      projectDir,
      [
        "Ralph cannot start until pending spec reconciliation is resolved.",
        summary,
        "Clarify the changed requirement in bare jri, then say just ralph it again when the specs are ready.",
      ].join("\n"),
    );
    return;
  }

  yield* emitAssistant(projectDir, `Start request accepted (${handoff.trigger}). Running the specs auditor now.`);
  yield* (options.startLoop ?? startLoopLocally)(projectDir, handoff.trigger, options);
}

async function* startLoopLocally(projectDir: string, _trigger: StartTrigger, options: RuntimeOptions): AsyncIterable<CoreEvent> {
  yield await startRalphLoop(projectDir, options);
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

  yield* emitAssistant(projectDir, "There is no unresolved human-task blocker to verify. I recorded your message.");
}

async function markHumanTaskVerified(projectDir: string, status: ProjectStatus, verificationSummary?: string): Promise<CoreEvent | undefined> {
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

  if (!status.activeLoopId) return undefined;
  return await appendLoopEvent(projectDir, {
    type: "blockerResolved",
    loopId: status.activeLoopId,
    data: { reason: "needsHumanTask" },
  });
}

function assistantTextForInterrogatorHandoff(handoff: InterrogatorHandoff): string {
  if (handoff.action === "specsUpdated" || handoff.action === "scratchpadUpdated") return handoff.summary;
  if (handoff.action === "humanTaskVerified") return handoff.verificationSummary ?? "The human task is verified.";
  if (handoff.action === "humanTaskStillBlocked") return handoff.blocker.resolutionGuide.summary;
  if (handoff.action === "startRequested") return `Start request accepted (${handoff.trigger}).`;
  return handoff.summary ?? "I recorded your note.";
}

function defaultHumanTaskVerifier(request: { blocker: Blocker }): HumanTaskVerificationHandoff {
  return {
    agent: "verifier",
    action: "stillBlocked",
    blocker: {
      ...request.blocker,
      resolutionGuide: {
        ...request.blocker.resolutionGuide,
        summary: "JRI needs a verifier that can inspect the external condition before this blocker can be marked resolved.",
        steps: [
          ...request.blocker.resolutionGuide.steps,
          "Retry after the verification capability or required observable evidence is available.",
        ],
        resumeInstruction: request.blocker.resolutionGuide.resumeInstruction,
      },
    },
  };
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
