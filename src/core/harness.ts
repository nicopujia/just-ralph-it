import { appendFile, mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { getAuthStatus } from "./auth";
import { explorerCapabilityDescriptor, renderExplorerAgentDescriptor } from "./capabilities";
import { JriError } from "./errors";
import { extractLatestHandoffFromText } from "./handoffs";
import { buildPiPrompt, modelForAgent } from "./prompts";
import { appendLoopEvent } from "./runtime-state";
import { parseJsonObject, validateConfig } from "./schema";
import type { CoreEvent } from "./types";
import type { AgentConfig, AgentHandoff, AgentName, ArtifactRef } from "./types";

export type HarnessPhase = "interrogation" | "auditing" | "planning" | "building" | "explorer";

export type CapabilityDescriptor = {
  name: "web" | "explorer";
  operation?: string;
};

export type HarnessOutputSink = {
  write: (chunk: string) => void | Promise<void>;
};

export type HarnessInvocation = {
  owner: { kind: "chat"; turnId: string } | { kind: "loop"; loopId: string };
  projectDir: string;
  agent: AgentName;
  phase: HarnessPhase;
  model: Required<AgentConfig>;
  context: { refs: string[]; inline: string[] };
  capabilities: CapabilityDescriptor[];
  output: HarnessOutputSink;
  signal: AbortSignal;
};

export type HarnessResult = {
  handoff: AgentHandoff;
  artifacts?: ArtifactRef[];
};

export type HarnessAdapter = (invocation: HarnessInvocation) => Promise<HarnessResult>;

const explorerHandoffLimit = explorerCapabilityDescriptor.limits.handoffChars;
const explorerTimeoutMs = explorerCapabilityDescriptor.limits.timeoutMs;
const explorerConcurrencyLimit = explorerCapabilityDescriptor.limits.concurrency;
const explorerQueues = new Map<string, { active: number; waiters: Array<() => void> }>();

export type HarnessSessionRequest = {
  projectDir: string;
  loopId: string;
  phase: HarnessPhase;
  stdoutPath: string;
  env?: NodeJS.ProcessEnv;
  contextRefs?: string[];
  contextInline?: string[];
  explorerTask?: string;
  userMessage?: string;
  timeoutMs?: number;
};

export type HarnessSessionRunner = (request: HarnessSessionRequest) => Promise<number>;

export type PiHarnessCommand = {
  command: string[];
  env: NodeJS.ProcessEnv;
};

export async function runControlledPiSession(request: HarnessSessionRequest): Promise<number> {
  const built = await buildControlledPiCommand(request);
  const proc = Bun.spawn(built.command, {
    cwd: request.projectDir,
    stdout: "pipe",
    stderr: "pipe",
    stdin: "ignore",
    env: built.env,
  });
  await Promise.all([appendStream(request.stdoutPath, proc.stdout), appendStream(request.stdoutPath, proc.stderr)]);
  return await proc.exited;
}

export async function invokeDefaultHarness(invocation: HarnessInvocation, env: NodeJS.ProcessEnv = process.env): Promise<HarnessResult> {
  if (invocation.signal.aborted) {
    throw new JriError("Harness invocation was cancelled before it started.", "harness-cancelled", "Retry the operation if it is still needed.");
  }

  const loopId = invocation.owner.kind === "loop" ? invocation.owner.loopId : `chat-${invocation.owner.turnId}`;
  const userMessage = invocation.context.inline.at(-1);
  const built = await buildControlledPiCommand({
    projectDir: invocation.projectDir,
    loopId,
    phase: invocation.phase,
    env,
    contextRefs: invocation.context.refs,
    contextInline: invocation.context.inline,
    ...(userMessage ? { userMessage } : {}),
  });
  const output = await runCommandCapture({
    command: built.command,
    cwd: invocation.projectDir,
    env: built.env,
    timeoutMs: 10 * 60 * 1000,
  });
  const assistantText = stripHandoffLines(output.text).trim();
  if (assistantText) await invocation.output.write(assistantText);
  if (output.exitCode !== 0) {
    throw new JriError(
      `${invocation.agent} harness exited with code ${output.exitCode}.`,
      "harness-failed",
      "Inspect the captured output and retry after resolving the harness error.",
    );
  }
  if (invocation.agent === "explorer") {
    throw new JriError(
      "Explorer invocations use the explorer capability wrapper, not the generic handoff harness.",
      "unsupported-harness-agent",
      "Run explorer tasks through the JRI explorer capability.",
    );
  }

  return {
    handoff: extractLatestHandoffFromText(invocation.agent, output.text, invocation.phase),
  };
}

export type ExplorerRunResult = {
  task: string;
  summary: string;
  artifactRef?: string;
  events: CoreEvent[];
};

export async function runExplorerTask(
  request: Omit<HarnessSessionRequest, "phase" | "stdoutPath"> & {
    task: string;
    mode?: "spawn" | "fork";
    handoffLimit?: number;
  },
): Promise<ExplorerRunResult> {
  const mode = request.mode ?? "spawn";
  if (mode !== "spawn") {
    throw new JriError("Explorer only supports spawn mode in the MVP.", "unsupported-explorer-mode", "Run explorer tasks with spawn/fresh context.");
  }
  const task = request.task.trim();
  if (!task) {
    throw new JriError("Explorer task must not be empty.", "invalid-explorer-task", "Pass a focused read-only investigation task.");
  }

  return await withExplorerSlot(`${request.projectDir}:${request.loopId}`, async () => {
    const events: CoreEvent[] = [];
    events.push(
      await appendLoopEvent(request.projectDir, {
        type: "subagentStarted",
        loopId: request.loopId,
        data: { agent: "explorer", task, mode },
      }),
    );

    try {
      const built = await buildControlledPiCommand({
        projectDir: request.projectDir,
        loopId: request.loopId,
        phase: "explorer",
        ...(request.env ? { env: request.env } : {}),
        explorerTask: task,
      });
      const output = await runCommandCapture({
        command: built.command,
        cwd: request.projectDir,
        env: built.env,
        timeoutMs: request.timeoutMs ?? explorerTimeoutMs,
      });
      const artifactRef = await writeExplorerArtifact(request.projectDir, request.loopId, task, output.text);
      if (output.exitCode !== 0) {
        const failed = await appendLoopEvent(request.projectDir, {
          type: "subagentFailed",
          loopId: request.loopId,
          data: { agent: "explorer", error: `Explorer exited with code ${output.exitCode}.`, artifactRef },
        });
        events.push(failed);
        throw new JriError(
          `Explorer exited with code ${output.exitCode}.`,
          "explorer-failed",
          `Inspect ${artifactRef} for the captured explorer output, then retry with a narrower task if needed.`,
        );
      }

      const summary = summarizeExplorerOutput(output.text, request.handoffLimit ?? explorerHandoffLimit);
      const finished = await appendLoopEvent(request.projectDir, {
        type: "subagentFinished",
        loopId: request.loopId,
        data: { agent: "explorer", summary, artifactRef },
      });
      events.push(finished);
      return { task, summary, artifactRef, events };
    } catch (error) {
      if (error instanceof JriError) throw error;
      const failed = await appendLoopEvent(request.projectDir, {
        type: "subagentFailed",
        loopId: request.loopId,
        data: { agent: "explorer", error: error instanceof Error ? error.message : String(error) },
      });
      events.push(failed);
      throw error;
    }
  });
}

export async function buildControlledPiCommand(
  request: Omit<HarnessSessionRequest, "stdoutPath">,
): Promise<PiHarnessCommand> {
  const env = request.env ?? process.env;
  await assertProviderAuth(env);
  await mkdir(join(request.projectDir, ".jri", "logs", request.loopId, "pi-sessions"), { recursive: true });

  const prompt = await buildPiPrompt(request.projectDir, request.phase, {
    loopId: request.loopId,
    ...(request.contextRefs ? { contextRefs: request.contextRefs } : {}),
    ...(request.contextInline ? { contextInline: request.contextInline } : {}),
    ...(request.explorerTask ? { explorerTask: request.explorerTask } : {}),
    ...(request.userMessage ? { userMessage: request.userMessage } : {}),
  });
  const agent = agentForPhase(request.phase);
  const model = modelForAgent(await readProjectConfig(request.projectDir), agent);
  const piPath = env.JRI_PI_COMMAND ?? "pi";
  if (request.phase === "explorer") {
    return await buildControlledExplorerSubagentCommand(request, env, piPath, model, prompt);
  }

  return {
    command: [
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
      allowedToolsForPhase(request.phase).join(","),
      "--session-dir",
      join(request.projectDir, ".jri", "logs", request.loopId, "pi-sessions"),
      "--print",
      prompt,
    ],
    env: {
      ...env,
      PI_CODING_AGENT_SESSION_DIR: join(request.projectDir, ".jri", "logs", request.loopId, "pi-sessions"),
    },
  };
}

async function buildControlledExplorerSubagentCommand(
  request: Omit<HarnessSessionRequest, "stdoutPath">,
  env: NodeJS.ProcessEnv,
  piPath: string,
  model: Required<AgentConfig>,
  prompt: string,
): Promise<PiHarnessCommand> {
  const capabilityDir = join(request.projectDir, ".jri", "logs", request.loopId, "capabilities", "explorer");
  const agentsDir = join(capabilityDir, "agents");
  await mkdir(agentsDir, { recursive: true });
  await writeFile(join(agentsDir, "explorer.md"), `${renderExplorerAgentDescriptor(model)}\n`, "utf8");

  const extension = env.JRI_PI_SUBAGENT_EXTENSION ?? "npm:pi-subagent";
  const task = request.explorerTask ?? "Inspect the codebase and report concise findings.";
  const commandPrompt = [
    "Use the pi-subagent extension to run exactly one foreground JRI explorer delegation.",
    "Use agent name explorer with spawn/fresh context only. Do not run chains or parallel subtasks inside this wrapper.",
    "Return only the explorer's final concise handoff.",
    "",
    `/run explorer ${JSON.stringify(task)}`,
    "",
    "JRI wrapper context for the parent session:",
    prompt,
  ].join("\n");

  return {
    command: [
      piPath,
      "--provider",
      "openai",
      "--model",
      model.model,
      "--thinking",
      model.reasoning,
      "--no-extensions",
      "--extension",
      extension,
      "--no-skills",
      "--no-prompt-templates",
      "--no-themes",
      "--no-context-files",
      "--tools",
      allowedToolsForPhase(request.phase).join(","),
      "--session-dir",
      join(request.projectDir, ".jri", "logs", request.loopId, "pi-sessions"),
      "--print",
      commandPrompt,
    ],
    env: {
      ...env,
      PI_CODING_AGENT_DIR: capabilityDir,
      PI_CODING_AGENT_SESSION_DIR: join(request.projectDir, ".jri", "logs", request.loopId, "pi-sessions"),
    },
  };
}

function agentForPhase(phase: HarnessPhase): AgentName {
  if (phase === "interrogation") return "interrogator";
  if (phase === "explorer") return "explorer";
  if (phase === "auditing") return "auditor";
  return phase === "planning" ? "planner" : "builder";
}

function allowedToolsForPhase(phase: HarnessPhase): string[] {
  if (phase === "interrogation") return ["read", "write", "edit", "grep", "find", "ls"];
  if (phase === "auditing" || phase === "explorer") return ["read", "grep", "find", "ls"];
  return ["read", "bash", "edit", "write", "grep", "find", "ls"];
}

function stripHandoffLines(text: string): string {
  return text
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("JRI_HANDOFF_JSON:"))
    .join("\n");
}

async function assertProviderAuth(env: NodeJS.ProcessEnv): Promise<void> {
  if (env.JRI_PI_COMMAND) return;
  const status = await getAuthStatus(env);
  if (status.authenticated) return;
  throw new JriError(
    "OpenAI authentication is required before JRI can start a controlled Pi session.",
    "auth-required",
    "Run jri auth login, set OPENAI_API_KEY, or complete Pi OpenAI auth, then retry.",
  );
}

export async function readProjectConfig(projectDir: string): Promise<unknown> {
  const path = join(projectDir, ".jri", "config.json");
  if (!(await Bun.file(path).exists())) return undefined;
  return validateConfig(parseJsonObject(await Bun.file(path).text(), path), path);
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

type CapturedCommand = {
  command: string[];
  cwd: string;
  env: NodeJS.ProcessEnv;
  timeoutMs: number;
};

async function runCommandCapture(request: CapturedCommand): Promise<{ exitCode: number; text: string }> {
  const proc = Bun.spawn(request.command, {
    cwd: request.cwd,
    stdout: "pipe",
    stderr: "pipe",
    stdin: "ignore",
    env: request.env,
  });
  const timeout = setTimeout(() => proc.kill("SIGTERM"), request.timeoutMs);
  try {
    const [stdout, stderr, exitCode] = await Promise.all([streamText(proc.stdout), streamText(proc.stderr), proc.exited]);
    return { exitCode, text: `${stdout}${stderr ? `${stdout ? "\n" : ""}${stderr}` : ""}` };
  } finally {
    clearTimeout(timeout);
  }
}

async function streamText(stream: ReadableStream<Uint8Array>): Promise<string> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let text = "";
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    text += decoder.decode(chunk.value, { stream: true });
  }
  return text + decoder.decode();
}

async function writeExplorerArtifact(projectDir: string, loopId: string, task: string, output: string): Promise<string> {
  const safeId = crypto.randomUUID();
  const artifactDir = join(projectDir, ".jri", "logs", loopId, "artifacts");
  await mkdir(artifactDir, { recursive: true });
  const artifactRef = `.jri/logs/${loopId}/artifacts/explorer-${safeId}.txt`;
  await writeFile(
    join(projectDir, artifactRef),
    [`Task: ${task}`, "", output.trimEnd(), ""].join("\n"),
    "utf8",
  );
  return artifactRef;
}

function summarizeExplorerOutput(output: string, handoffLimit: number): string {
  const normalized = output.trim() || "Explorer completed without textual output.";
  if (normalized.length <= handoffLimit) return normalized;
  return `${normalized.slice(0, Math.max(0, handoffLimit - 120)).trimEnd()}\n\n[Explorer output truncated; see artifactRef for the full result.]`;
}

async function withExplorerSlot<T>(key: string, task: () => Promise<T>): Promise<T> {
  const state = explorerQueues.get(key) ?? { active: 0, waiters: [] };
  explorerQueues.set(key, state);
  if (state.active >= explorerConcurrencyLimit) {
    await new Promise<void>((resolve) => state.waiters.push(resolve));
  }
  state.active += 1;
  try {
    return await task();
  } finally {
    state.active -= 1;
    const next = state.waiters.shift();
    if (next) next();
    else if (state.active === 0) explorerQueues.delete(key);
  }
}
