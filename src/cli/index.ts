#!/usr/bin/env bun
import { open, isJriError, JriError } from "../core";
import type { ProjectStatus, ProjectState } from "../core";
import { runDaemon } from "../core/daemon-ipc";
import { runLoopProcess, type RunnerPhase } from "../core/daemon-runtime";
import { runExplorerTask } from "../core/harness";
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
    await project.lifecycle.ensureInitialized();
    if (process.stdin.isTTY) {
      const auth = await project.auth.login();
      if (auth.status === "userActionRequired") {
        console.error(auth.instructions);
        return 1;
      }
    }
    const input = process.stdin.isTTY ? "" : await new Response(Bun.stdin.stream()).text();
    if (!input.trim()) {
      const status = await project.status.get();
      console.log(formatStatus(status));
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
      for await (const event of project.loop.observe({ includeStdout: true, recentStdoutLines: 100, follow: true })) {
        console.log(formatLoopEvent(event));
      }
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

function usage(error?: string): number {
  if (error) console.error(error);
  console.error("Usage: jri | jri auth {status|login|logout} | jri loop {attach|stop|halt|resume}");
  return 1;
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

function formatStatus(status: { state: string; blocker?: { reason: string; description: string; resolutionGuide: { resumeInstruction: string } }; iteration?: number; iterations?: number; stopRequested: boolean }): string {
  if (status.state === "building") {
    return `ralphing${status.iteration ? ` | iteration: ${status.iteration}` : ""} | stop: ${status.stopRequested ? "yes" : "no"}`;
  }
  if (status.state === "blocked" && status.blocker) {
    return `blocked | reason: ${status.blocker.reason} | ${status.blocker.description} | ${status.blocker.resolutionGuide.resumeInstruction}`;
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
