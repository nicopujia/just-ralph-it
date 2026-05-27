#!/usr/bin/env bun
import { stat } from "node:fs/promises";
import { join } from "node:path";
import { createInterface } from "node:readline/promises";
import { open, isJriError, JriError } from "../core";
import type { CoreEvent, Project, ProjectStatus, ProjectState } from "../core";
import { runDaemon } from "../core/daemon-ipc";
import { runLoopProcess, type RunnerPhase } from "../core/daemon-runtime";
import { runExplorerTask } from "../core/harness";
import { checkInterrogationStartGate } from "../core/interrogation-state";
import { runWebFetch, runWebSearch } from "../core/web-capability";

async function main(argv: string[]): Promise<number> {
  const [command, subcommand] = argv;
  if (command === "--daemon") {
    await runDaemon();
    return 0;
  }
  if (command === "--run-loop") {
    const [projectDir, loopId, phase] = argv.slice(1);
    if (!projectDir || !loopId || !isRunnerPhase(phase)) {
      return usage("Invalid internal runner invocation.");
    }
    await runLoopProcess(projectDir, loopId, phase);
    return 0;
  }
  if (command === "--run-explorer") {
    const [projectDir, loopId, ...taskParts] = argv.slice(1);
    const task = taskParts.join(" ").trim();
    if (!projectDir || !loopId || !task) {
      return usage("Invalid internal explorer invocation.");
    }
    const result = await runExplorerTask({ projectDir, loopId, task });
    console.log(result.summary);
    if (result.artifactRef) console.log(`artifactRef: ${result.artifactRef}`);
    return 0;
  }
  if (command === "--run-web") {
    const [operation, projectDir, loopId, ...rest] = argv.slice(1);
    if (operation === "search") {
      const query = rest.join(" ").trim();
      if (!projectDir || !loopId || !query) {
        return usage("Invalid internal web search invocation.");
      }
      console.log(JSON.stringify(await runWebSearch({ projectDir, loopId, query }), null, 2));
      return 0;
    }
    if (operation === "fetch") {
      const [url] = rest;
      if (!projectDir || !loopId || !url) {
        return usage("Invalid internal web fetch invocation.");
      }
      console.log(JSON.stringify(await runWebFetch({ projectDir, loopId, url }), null, 2));
      return 0;
    }
    return usage("Invalid internal web invocation.");
  }
  if (command === "--web-search" || command === "--web-fetch") {
    const [projectDir, loopId, ...rest] = argv.slice(1);
    const operation = command === "--web-search" ? "search" : "fetch";
    return await main(["--run-web", operation, projectDir ?? "", loopId ?? "", ...rest]);
  }

  const project = await open(process.cwd());

  if (!command) {
    const shouldPrintInitialized = await needsInitializationNotice(project);
    await project.lifecycle.ensureInitialized();
    if (shouldPrintInitialized) {
      console.error(`Initialized JRI in ${project.projectDir}`);
    }
    if (process.stdin.isTTY) {
      const auth = await project.auth.login();
      if (auth.status === "userActionRequired") {
        console.error(auth.instructions);
        return 1;
      }
    }
    if (process.stdin.isTTY) {
      await runInteractiveChat(project);
      return 0;
    }

    const input = await new Response(Bun.stdin.stream()).text();
    if (!input.trim()) {
      const reconciliation = await pendingReconciliationMessage(project.projectDir);
      if (reconciliation) {
        console.log(reconciliation);
      } else {
        const status = await project.status.get();
        console.log(formatStatus(status));
      }
      return 0;
    }
    for await (const event of project.chat.send({ message: input })) {
      if (event.type === "chatMessageDelta") {
        console.log(event.data.text);
      }
    }
    return 0;
  }

  if (command === "auth") {
    if (subcommand === "--help" || subcommand === "-h" || !subcommand) {
      console.log(formatAuthHelp());
      return 0;
    }
    if (subcommand === "status") {
      const status = await project.auth.status();
      console.log(`${status.provider}: ${status.authenticated ? "authenticated" : "not authenticated"}`);
      return 0;
    }
    if (subcommand === "login") {
      const result = await project.auth.login();
      console.log(result.status === "authenticated" ? "Authenticated." : result.instructions);
      return result.status === "authenticated" ? 0 : 1;
    }
    if (subcommand === "logout") {
      await project.auth.logout();
      console.log("Logged out.");
      return 0;
    }
    return usage(`Unsupported auth command: ${subcommand ?? ""}`.trim());
  }

  if (command === "loop") {
    if (subcommand === "attach") {
      const status = await project.status.get();
      if (!isActiveLoopState(status.state)) {
        throw loopStateError("attach", status);
      }
      await attachLoop(project, status);
      return 0;
    }
    if (subcommand === "stop") {
      const before = await project.status.get();
      if (!isActiveLoopState(before.state)) {
        throw loopStateError("stop", before);
      }
      await project.loop.requestStop();
      const status = await project.status.get();
      console.log(status.stopRequested ? "Graceful stop requested." : "Graceful stop request cleared.");
      return 0;
    }
    if (subcommand === "halt") {
      const status = await project.status.get();
      if (status.state === "halted") {
        console.log(formatAlreadyHalted(status));
        return 0;
      }
      if (!isActiveLoopState(status.state)) {
        throw loopStateError("halt", status);
      }
      if (!(await confirm("Force halt the active JRI loop?"))) {
        console.log("Halt canceled.");
        return 0;
      }
      const rollbackCommit = status.currentIteration?.rollbackCommit;
      const resetGit =
        Boolean(rollbackCommit && status.currentIteration?.trackedTreeCleanAtStart) &&
        (await confirm(`Reset tracked files with git reset --hard ${rollbackCommit}?`));
      for await (const event of project.loop.halt({ resetGit })) {
        console.log(formatLoopEvent(event));
      }
      return 0;
    }
    if (subcommand === "resume") {
      const status = await project.status.get();
      if (!isResumeEligible(status)) {
        throw loopStateError("resume", status);
      }
      for await (const event of project.loop.resume()) {
        console.log(formatLoopEvent(event));
      }
      return 0;
    }
    return usage(`Unsupported loop command: ${subcommand ?? ""}`.trim());
  }

  return usage(`Unsupported command: ${command}`);
}

function isRunnerPhase(value: string | undefined): value is RunnerPhase {
  return value === "auditing" || value === "planning" || value === "building";
}

async function needsInitializationNotice(project: Project): Promise<boolean> {
  const requiredPaths = [
    join(project.projectDir, ".jri", "config.json"),
    join(project.projectDir, ".jri", "status.json"),
    join(project.projectDir, ".jri", "specs"),
    join(project.projectDir, ".jri", "logs"),
    join(project.projectDir, ".jri", "scratchpad.md"),
    join(project.projectDir, "AGENTS.md"),
  ];
  for (const path of requiredPaths) {
    if (!(await pathExists(path))) return true;
  }
  return false;
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

function usage(error?: string): number {
  if (error) console.error(error);
  console.error("Usage: jri | jri auth {status|login|logout} | jri loop {attach|stop|halt|resume}");
  return 1;
}

async function runInteractiveChat(project: Project): Promise<void> {
  const rl = createInterface({
    input: process.stdin,
    output: process.stdout,
    prompt: "jri> ",
  });

  try {
    const reconciliation = await pendingReconciliationMessage(project.projectDir);
    if (reconciliation) console.log(reconciliation);
    for (;;) {
      console.log(formatStatus(await project.status.get()));
      const input = await rl.question("jri> ");
      if (input.trim() === "/exit" || input.trim() === "/quit") return;
      if (!input.trim()) continue;
      for await (const event of project.chat.send({ message: input })) {
        if (event.type === "chatMessageDelta") {
          console.log(event.data.text);
        } else if (event.type === "loopStarted") {
          console.log(formatLoopEvent(event));
        }
      }
    }
  } catch (error) {
    if (isReadlineClosed(error)) return;
    throw error;
  } finally {
    rl.close();
  }
}

function isReadlineClosed(error: unknown): boolean {
  return error instanceof Error && error.message === "readline was closed";
}

async function pendingReconciliationMessage(projectDir: string): Promise<string | null> {
  const startGate = await checkInterrogationStartGate(projectDir);
  if (startGate.ok) return null;
  const pending = startGate.pending[0];
  const summary = pending?.topic.pendingReconciliation?.summary ?? "A pending spec reconciliation must be resolved before Ralph can start.";
  return [
    "Ralph cannot start until pending spec reconciliation is resolved.",
    summary,
    "Clarify the changed requirement in bare jri, then say just ralph it again when the specs are ready.",
  ].join("\n");
}

function formatAuthHelp(): string {
  return [
    "Usage: jri auth {status|login|logout}",
    "",
    "Stable auth commands:",
    "  jri auth status   Show whether the configured provider is authenticated.",
    "  jri auth login    Print or complete the provider auth recovery flow.",
    "  jri auth logout   Remove local Pi-backed OpenAI credentials when possible.",
    "",
    "Advanced passthrough:",
    "  Only auth-related passthrough behavior may be added here. This namespace is not general Pi access.",
  ].join("\n");
}

function isActiveLoopState(state: ProjectState): boolean {
  return state === "auditing" || state === "planning" || state === "building";
}

function isResumeEligible(status: ProjectStatus): boolean {
  return (
    status.state === "stopped" ||
    (status.state === "blocked" && status.blocker?.reason === "needsHumanTask" && status.blocker.resolution?.status === "verified")
  );
}

function loopStateError(action: "attach" | "stop" | "halt" | "resume", status: ProjectStatus): Error {
  const actionLabel = `jri loop ${action}`;
  const logHint = formatLogHint(status);

  if (status.state === "blocked" && status.blocker) {
    const recovery =
      status.blocker.reason === "ambiguousSpecs"
        ? `${status.blocker.resolutionGuide.resumeInstruction}${logHint}`
        : status.blocker.resolution?.status === "verified"
          ? `Run jri loop resume to continue the verified human-task lifecycle.${logHint}`
          : `${status.blocker.resolutionGuide.resumeInstruction}${logHint}`;
    return new JriError(
      `${actionLabel} is not available while JRI is blocked: ${status.blocker.description}`,
      `loop-${action}-blocked`,
      recovery,
    );
  }

  if (status.state === "stopped") {
    return new JriError(
      `${actionLabel} is not available because the loop is stopped.`,
      `loop-${action}-stopped`,
      `Run jri loop resume to continue if specs have not changed, or use bare jri to reconcile requirements.${logHint}`,
    );
  }

  if (status.state === "halted") {
    return new JriError(
      `${actionLabel} is not available because the loop is halted.`,
      `loop-${action}-halted`,
      `Use bare jri to clarify or confirm requirements, then say just ralph it to authorize a new lifecycle.${logHint}`,
    );
  }

  if (status.state === "idle") {
    return new JriError(
      `${actionLabel} is not available because no Ralph loop is running.`,
      `loop-${action}-idle`,
      `Use bare jri to discuss requirements, then say just ralph it when specs are ready.${logHint}`,
    );
  }

  return new JriError(
    `${actionLabel} is not available while JRI is ${status.state}.`,
    `loop-${action}-state`,
    "Reload status and retry the command when the lifecycle reaches an eligible state.",
  );
}

function formatLogHint(status: ProjectStatus): string {
  const loopId = status.activeLoopId ?? status.lastLoopId;
  return loopId ? ` Inspect .jri/logs/${loopId}/stdout.log for prior loop output.` : "";
}

function formatAlreadyHalted(status: ProjectStatus): string {
  return `JRI is already halted.${formatLogHint(status)}`;
}

function formatStatus(status: {
  state: string;
  blocker?: {
    reason: string;
    description: string;
    resolutionGuide: { summary: string; steps: string[]; successCriteria?: string[]; resumeInstruction: string };
  };
  iteration?: number;
  iterations?: number;
  stopRequested: boolean;
}): string {
  if (status.state === "building") {
    return `ralphing${status.iteration ? ` | iteration: ${status.iteration}` : ""} | stop: ${status.stopRequested ? "yes" : "no"}`;
  }
  if (status.state === "blocked" && status.blocker) {
    const guide = status.blocker.resolutionGuide;
    return [
      `blocked | reason: ${status.blocker.reason} | ${status.blocker.description}`,
      guide.summary,
      ...guide.steps.map((step, index) => `${index + 1}. ${step}`),
      ...(guide.successCriteria?.length ? ["Success criteria:", ...guide.successCriteria.map((criterion) => `- ${criterion}`)] : []),
      `Resume: ${guide.resumeInstruction}`,
    ].join("\n");
  }
  if (status.state === "idle" && status.iterations !== undefined) {
    return `idle | iterations: ${status.iterations}`;
  }
  return status.state;
}

function formatLoopEvent(event: { type: string; sequence: number; timestamp: string; message?: string; data?: unknown }): string {
  if (event.type === "loopOutput" && event.message) return event.message.endsWith("\n") ? event.message.slice(0, -1) : event.message;
  return event.message ?? `[${event.sequence}] ${event.timestamp} ${event.type} ${JSON.stringify(event.data ?? {})}`;
}

async function attachLoop(project: Project, initialStatus: ProjectStatus): Promise<void> {
  const input = attachInput(process.stdin);
  const events = project.loop.observe({ includeStdout: true, recentStdoutLines: 100, follow: true })[Symbol.asyncIterator]();
  let status = initialStatus;
  let stop = false;
  let nextEvent = events.next();
  let nextInput = input.next();

  renderAttachFooter(status);
  try {
    while (!stop) {
      const result = await Promise.race([
        nextEvent.then((value) => ({ source: "event" as const, value })),
        nextInput.then((value) => ({ source: "input" as const, value })),
      ]);

      if (result.source === "input") {
        const key = result.value;
        nextInput = input.next();
        if (key === undefined) {
          stop = true;
          break;
        }
        if (key === "d") {
          await flushReadyAttachEvents(events, nextEvent);
          stop = true;
          break;
        }
        if (key === "s") {
          await project.loop.requestStop();
          status = await project.status.get();
          renderAttachFooter(status);
        }
        continue;
      }

      if (result.value.done) break;
      writeAttachEvent(result.value.value);
      nextEvent = events.next();
    }
  } finally {
    input.close();
    await events.return?.();
    clearAttachFooter();
  }
}

type AttachInput = {
  next: () => Promise<"d" | "s" | undefined>;
  close: () => void;
};

function attachInput(stdin: NodeJS.ReadStream): AttachInput {
  const queue: Array<"d" | "s"> = [];
  let pending: ((key: "d" | "s" | undefined) => void) | undefined;
  let ended = false;
  const wasRaw = Boolean(stdin.isRaw);
  const wasPaused = stdin.isPaused();

  const resolvePending = (key: "d" | "s" | undefined): void => {
    if (!pending) return;
    const resolve = pending;
    pending = undefined;
    resolve(key);
  };

  const push = (chunk: Buffer | string): void => {
    for (const char of chunk.toString("utf8")) {
      if (char !== "d" && char !== "s") continue;
      if (pending) {
        resolvePending(char);
      } else {
        queue.push(char);
      }
    }
  };

  const end = (): void => {
    ended = true;
    resolvePending(undefined);
  };

  stdin.on("data", push);
  stdin.once("end", end);
  stdin.once("close", end);
  if (stdin.isTTY) {
    stdin.setRawMode?.(true);
  }
  stdin.resume();

  return {
    next: async (): Promise<"d" | "s" | undefined> => {
      const queued = queue.shift();
      if (queued) return queued;
      if (ended) return undefined;
      return await new Promise((resolve) => {
        pending = resolve;
      });
    },
    close: (): void => {
      stdin.off("data", push);
      stdin.off("end", end);
      stdin.off("close", end);
      if (stdin.isTTY) {
        stdin.setRawMode?.(wasRaw);
      }
      if (wasPaused) stdin.pause();
      resolvePending(undefined);
    },
  };
}

async function flushReadyAttachEvents(
  events: AsyncIterator<CoreEvent>,
  nextEvent: Promise<IteratorResult<CoreEvent>>,
  timeoutMs = 25,
): Promise<void> {
  let current = nextEvent;
  for (;;) {
    const result = await Promise.race([
      current.then((value) => ({ ready: true as const, value })),
      sleep(timeoutMs).then(() => ({ ready: false as const })),
    ]);
    if (!result.ready || result.value.done) return;
    writeAttachEvent(result.value.value);
    current = events.next();
  }
}

function writeAttachEvent(event: CoreEvent): void {
  clearAttachFooter();
  const text = formatLoopEvent(event);
  process.stdout.write(text.endsWith("\n") ? text : `${text}\n`);
}

function renderAttachFooter(status: ProjectStatus): void {
  process.stderr.write(`\r[d]etach [s]top | ${formatStatus(status)}`);
}

function clearAttachFooter(): void {
  process.stderr.write("\r\x1b[2K");
}

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function confirm(question: string): Promise<boolean> {
  process.stderr.write(`${question} y/N `);
  const answer = await new Response(Bun.stdin.stream()).text();
  return answer.trim().toLowerCase() === "y" || answer.trim().toLowerCase() === "yes";
}

main(Bun.argv.slice(2))
  .then((code) => process.exit(code))
  .catch((error) => {
    if (isJriError(error)) {
      console.error(error.message);
      console.error(error.recovery);
    } else {
      console.error(error instanceof Error ? error.message : String(error));
    }
    process.exit(1);
  });
