import { appendFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import {
  AuthStorage,
  createAgentSession,
  DefaultResourceLoader,
  ModelRegistry,
  SessionManager,
  SettingsManager,
  type AgentSessionEvent,
  type CreateAgentSessionOptions,
  type CreateAgentSessionResult,
} from "@earendil-works/pi-coding-agent";
import { getAuthStatus } from "./auth";
import { assertExplorerCapabilityAvailable } from "./capability-preflight";
import { explorerCapabilityDescriptor, renderExplorerAgentDescriptor, webCapabilityDescriptor } from "./capabilities";
import type { CapabilityOwner } from "./capability-ownership";
import { JriError } from "./errors";
import { extractLatestHandoffFromText } from "./handoffs";
import { buildPiPrompt, modelForAgent } from "./prompts";
import { readProjectConfig } from "./project-config";
import { buildSdkCapabilityTools } from "./sdk-capability-tools";
import { appendLoopEvent } from "./runtime-state";
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

export type PiSdkSessionFactory = (options: CreateAgentSessionOptions) => Promise<CreateAgentSessionResult>;

const explorerHandoffLimit = explorerCapabilityDescriptor.limits.handoffChars;
const explorerTimeoutMs = explorerCapabilityDescriptor.limits.timeoutMs;
const explorerConcurrencyLimit = explorerCapabilityDescriptor.limits.concurrency;
const explorerQueues = new Map<string, { active: number; waiters: Array<() => void> }>();
const internalInvocationEnv = "JRI_INTERNAL_INVOCATION";
const explorerSummaryFramePrefix = "JRI_EXPLORER_SUMMARY_JSON:";

export type HarnessSessionRequest = {
  projectDir: string;
  loopId: string;
  owner?: CapabilityOwner;
  phase: HarnessPhase;
  stdoutPath: string;
  env?: NodeJS.ProcessEnv;
  signal?: AbortSignal;
  contextRefs?: string[];
  contextInline?: string[];
  explorerTask?: string;
  userMessage?: string;
  capabilities?: CapabilityDescriptor[];
  timeoutMs?: number;
};

export type HarnessSessionRunner = (request: HarnessSessionRequest) => Promise<number>;

export type PiHarnessCommand = {
  command: string[];
  env: NodeJS.ProcessEnv;
};

export async function runControlledPiSession(request: HarnessSessionRequest): Promise<number> {
  const built = await buildControlledPiCommand(request);
  if (request.signal?.aborted) {
    throw harnessCancelledError();
  }
  const proc = Bun.spawn(built.command, {
    cwd: request.projectDir,
    stdout: "pipe",
    stderr: "pipe",
    stdin: "ignore",
    env: built.env,
  });
  const childRegistration = await maybeRegisterLoopChild(request.projectDir, request.loopId, proc.pid, "harness");
  const cancellation = bindProcessCancellation(proc, request.signal);
  try {
    await appendMergedStreams(request.stdoutPath, [proc.stdout, proc.stderr]);
    const exitCode = await proc.exited;
    if (cancellation.cancelled) throw harnessCancelledError();
    return exitCode;
  } finally {
    cancellation.cleanup();
    await childRegistration.cleanup();
  }
}

export async function invokeDefaultHarness(
  invocation: HarnessInvocation,
  env: NodeJS.ProcessEnv = process.env,
  createSession: PiSdkSessionFactory = createAgentSession,
): Promise<HarnessResult> {
  assertHarnessCapabilities(invocation);
  if (invocation.signal.aborted) {
    throw harnessCancelledError();
  }

  const allowChatWrapperFallback = env.JRI_INTERNAL_ALLOW_CHAT_WRAPPER_FALLBACK === "1";
  if ((invocation.owner.kind === "chat" && !allowChatWrapperFallback) || !env.JRI_PI_COMMAND) {
    return await invokePiSdkHarness(invocation, env, createSession);
  }

  const loopId = invocation.owner.kind === "loop" ? invocation.owner.loopId : `chat-${invocation.owner.turnId}`;
  const userMessage = invocation.phase === "interrogation" ? invocation.context.inline[0] : undefined;
  const built = await buildControlledPiCommand({
    projectDir: invocation.projectDir,
    loopId,
    owner: invocation.owner,
    phase: invocation.phase,
    env,
    contextRefs: invocation.context.refs,
    contextInline: invocation.context.inline,
    ...(userMessage ? { userMessage } : {}),
    capabilities: invocation.capabilities,
  });
  const visibleForwarder = invocation.owner.kind === "chat" ? createChatVisibleOutputForwarder(invocation.output.write) : null;
  const output = await runCommandCapture({
    command: built.command,
    cwd: invocation.projectDir,
    env: built.env,
    timeoutMs: 10 * 60 * 1000,
    signal: invocation.signal,
    ...(visibleForwarder
      ? {
          onStdoutChunk: (chunk: string) => visibleForwarder.push(chunk),
          onStderrChunk: (chunk: string) => visibleForwarder.push(chunk),
        }
      : {}),
    ...(invocation.owner.kind === "loop"
      ? { child: { projectDir: invocation.projectDir, loopId: invocation.owner.loopId, capability: "harness" as const } }
      : {}),
  });
  await visibleForwarder?.finish();
  if (invocation.owner.kind === "loop") {
    const visibleOutput = output.text.trimEnd();
    if (visibleOutput) await invocation.output.write(visibleOutput);
  }
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

export async function invokePiSdkHarness(
  invocation: HarnessInvocation,
  env: NodeJS.ProcessEnv = process.env,
  createSession: PiSdkSessionFactory = createAgentSession,
): Promise<HarnessResult> {
  assertHarnessCapabilities(invocation);
  if (invocation.signal.aborted) {
    throw harnessCancelledError();
  }
  if (invocation.agent === "explorer") {
    throw new JriError(
      "Explorer invocations use the explorer capability wrapper, not the generic handoff harness.",
      "unsupported-harness-agent",
      "Run explorer tasks through the JRI explorer capability.",
    );
  }

  const loopId = invocation.owner.kind === "loop" ? invocation.owner.loopId : `chat-${invocation.owner.turnId}`;
  const prompt = await buildPiPrompt(invocation.projectDir, invocation.phase, {
    owner: invocation.owner,
    loopId,
    contextRefs: invocation.context.refs,
    contextInline: invocation.context.inline,
    ...(invocation.phase === "interrogation" && invocation.context.inline[0] ? { userMessage: invocation.context.inline[0] } : {}),
    capabilities: invocation.capabilities,
    capabilityStyle: "sdk-tools",
  });
  const sessionDir = join(invocation.projectDir, ".jri", "logs", loopId, "pi-sessions");
  const agentDir = join(invocation.projectDir, ".jri", "logs", loopId, "pi-agent");
  await mkdir(sessionDir, { recursive: true });
  await mkdir(agentDir, { recursive: true });

  const authStorage = AuthStorage.create(piAuthPath(env));
  if (env.OPENAI_API_KEY?.trim()) {
    authStorage.setRuntimeApiKey("openai", env.OPENAI_API_KEY.trim());
  }
  const modelRegistry = ModelRegistry.create(authStorage);
  const model = modelRegistry.find("openai", invocation.model.model);
  if (!model) {
    throw new JriError(
      `JRI could not resolve OpenAI model ${invocation.model.model}.`,
      "model-not-found",
      "Check .jri/config.json agent model overrides or update the Pi SDK model registry.",
    );
  }
  if (!modelRegistry.hasConfiguredAuth(model)) {
    throw new JriError(
      "OpenAI authentication is required before JRI can start a controlled Pi SDK session.",
      "auth-required",
      "Run jri auth login, set OPENAI_API_KEY, or complete Pi OpenAI auth, then retry.",
    );
  }

  const settingsManager = SettingsManager.inMemory({
    defaultProvider: "openai",
    defaultModel: invocation.model.model,
    defaultThinkingLevel: invocation.model.reasoning,
    sessionDir,
    retry: { enabled: false },
  });
  const nativeCapabilityTools = buildSdkCapabilityTools({
    owner: invocation.owner,
    projectDir: invocation.projectDir,
    agent: invocation.agent,
    phase: invocation.phase,
    capabilities: invocation.capabilities,
    env,
    runExplorerTask,
  });
  const resourceLoader = new DefaultResourceLoader({
    cwd: invocation.projectDir,
    agentDir,
    settingsManager,
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    appendSystemPromptOverride: () => [],
  });
  await resourceLoader.reload();

  const chunks: string[] = [];
  const visibleForwarder = invocation.owner.kind === "chat" ? createChatVisibleOutputForwarder(invocation.output.write) : null;
  let visibleWriteChain = Promise.resolve();
  let completed = false;
  const { session } = await createSession({
    cwd: invocation.projectDir,
    agentDir,
    authStorage,
    modelRegistry,
    model,
    thinkingLevel: invocation.model.reasoning,
    tools: [...new Set([...allowedToolsForPhase(invocation.phase), ...nativeCapabilityTools.activeToolNames])],
    customTools: nativeCapabilityTools.customTools,
    resourceLoader,
    settingsManager,
    sessionManager: SessionManager.create(invocation.projectDir, sessionDir),
  });
  const abort = (): void => {
    void session.abort().catch(() => {});
  };
  invocation.signal.addEventListener("abort", abort, { once: true });
  try {
    const unsubscribe = session.subscribe((event: AgentSessionEvent) => {
      if (event.type !== "message_update" || event.assistantMessageEvent.type !== "text_delta") return;
      const delta = event.assistantMessageEvent.delta;
      chunks.push(delta);
      if (visibleForwarder) {
        visibleWriteChain = visibleWriteChain.then(() => visibleForwarder.push(delta));
      }
    });
    try {
      await session.prompt(prompt, { source: "rpc" });
      await visibleWriteChain;
      await visibleForwarder?.finish();
      completed = true;
    } finally {
      unsubscribe();
    }
  } catch (error) {
    if (invocation.signal.aborted) throw harnessCancelledError();
    throw normalizeSdkHarnessError(error, invocation.agent);
  } finally {
    invocation.signal.removeEventListener("abort", abort);
    session.dispose();
  }

  if (invocation.signal.aborted) throw harnessCancelledError();
  if (!completed) {
    throw new JriError(
      `${invocation.agent} SDK harness ended before completion.`,
      "harness-failed",
      "Retry after checking the captured SDK diagnostics.",
    );
  }

  const outputText = chunks.join("");
  if (invocation.owner.kind === "loop") {
    const visibleOutput = outputText.trimEnd();
    if (visibleOutput) await invocation.output.write(visibleOutput);
  }
  return {
    handoff: extractLatestHandoffFromText(invocation.agent, outputText, invocation.phase),
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
  await assertExplorerCapabilityAvailable(request.projectDir, request.env ?? process.env);

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
        capabilities: [{ name: "web", operation: "search" }, { name: "web", operation: "fetch" }],
      });
      const output = await runCommandCapture({
        command: built.command,
        cwd: request.projectDir,
        env: built.env,
        timeoutMs: request.timeoutMs ?? explorerTimeoutMs,
        ...(request.signal ? { signal: request.signal } : {}),
        child: { projectDir: request.projectDir, loopId: request.loopId, capability: "explorer" },
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

      let summary: string;
      try {
        summary = extractExplorerSummary(output.text, request.handoffLimit ?? explorerHandoffLimit);
      } catch (error) {
        const failed = await appendLoopEvent(request.projectDir, {
          type: "subagentFailed",
          loopId: request.loopId,
          data: { agent: "explorer", error: error instanceof Error ? error.message : String(error), artifactRef },
        });
        events.push(failed);
        throw error;
      }
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
  await assertProviderAuth(request.projectDir, env);
  await mkdir(join(request.projectDir, ".jri", "logs", request.loopId, "pi-sessions"), { recursive: true });

  const promptOwner = request.owner ?? (request.phase === "interrogation" ? { kind: "chat" as const, turnId: request.loopId } : { kind: "loop" as const, loopId: request.loopId });
  const prompt = await buildPiPrompt(request.projectDir, request.phase, {
    owner: promptOwner,
    loopId: request.loopId,
    ...(request.contextRefs ? { contextRefs: request.contextRefs } : {}),
    ...(request.contextInline ? { contextInline: request.contextInline } : {}),
    ...(request.explorerTask ? { explorerTask: request.explorerTask } : {}),
    ...(request.userMessage ? { userMessage: request.userMessage } : {}),
    ...(request.capabilities ? { capabilities: request.capabilities } : {}),
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
      [internalInvocationEnv]: "1",
      PI_CODING_AGENT_SESSION_DIR: join(request.projectDir, ".jri", "logs", request.loopId, "pi-sessions"),
    },
  };
}

export function assertHarnessCapabilities(invocation: Pick<HarnessInvocation, "owner" | "agent" | "phase" | "capabilities">): void {
  for (const capability of invocation.capabilities) {
    if (capability.name === "web") {
      assertWebCapabilityAllowed(invocation, capability.operation);
      continue;
    }
    if (capability.name === "explorer") {
      assertExplorerCapabilityAllowed(invocation);
      continue;
    }
    invalidCapability(String((capability as { name?: unknown }).name ?? "unknown"), invocation.agent);
  }
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
  const delegatedTask = [
    task,
    "",
    "JRI wrapper-provided context and capability instructions:",
    prompt,
    "",
    "Explorer summary contract:",
    `Return exactly one physical line beginning ${explorerSummaryFramePrefix} followed by a JSON object with only a non-empty summary string.`,
    `The summary must be at most ${explorerHandoffLimit} characters and should cite concrete file paths or state no relevant evidence was found.`,
  ].join("\n");
  const commandPrompt = [
    "Use the pi-subagent extension to run exactly one foreground JRI explorer delegation.",
    "Use agent name explorer with spawn/fresh context only. Do not run chains or parallel subtasks inside this wrapper.",
    `Return only the explorer's final ${explorerSummaryFramePrefix} handoff line.`,
    "",
    `/run explorer ${JSON.stringify(delegatedTask)}`,
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
      [internalInvocationEnv]: "1",
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

function assertWebCapabilityAllowed(
  invocation: Pick<HarnessInvocation, "owner" | "agent" | "phase">,
  operation: string | undefined,
): void {
  if (operation !== undefined && operation !== "search" && operation !== "fetch") {
    throw new JriError(
      `JRI web capability received unsupported operation ${JSON.stringify(operation)}.`,
      "capability-declaration-invalid",
      "Declare web capability operations as search or fetch before invoking the harness.",
    );
  }
  if (!webCapabilityDescriptor.allowedAgents.includes(invocation.agent as (typeof webCapabilityDescriptor.allowedAgents)[number])) {
    invalidCapability("web", invocation.agent);
  }
  if (invocation.owner.kind === "chat" && invocation.agent !== "interrogator") {
    throw new JriError(
      "Chat-owned web capability is only available to the interrogator.",
      "capability-owner-unsupported",
      "Use loop-owned capability metadata for Ralph loop agents.",
    );
  }
}

function assertExplorerCapabilityAllowed(invocation: Pick<HarnessInvocation, "owner" | "agent" | "phase">): void {
  if (!explorerCapabilityDescriptor.allowedAgents.includes(invocation.agent as (typeof explorerCapabilityDescriptor.allowedAgents)[number])) {
    invalidCapability("explorer", invocation.agent);
  }
  if (invocation.owner.kind !== "loop") {
    throw new JriError(
      "Explorer capability requires loop ownership.",
      "capability-owner-unsupported",
      "Run explorer tasks from a daemon-managed Ralph loop so artifacts and events attach to the lifecycle.",
    );
  }
  if (invocation.phase !== "planning" && invocation.phase !== "building") {
    throw new JriError(
      `Explorer capability is not available during ${invocation.phase}.`,
      "capability-declaration-invalid",
      "Declare explorer only for planning or building phases.",
    );
  }
}

function invalidCapability(capability: string, agent: AgentName): never {
  throw new JriError(
    `JRI ${capability} capability is not allowed for ${agent}.`,
    "capability-agent-not-allowed",
    "Use the phase's declared JRI capability policy before invoking the harness.",
  );
}

function allowedToolsForPhase(phase: HarnessPhase): string[] {
  if (phase === "interrogation") return ["read", "write", "edit", "grep", "find", "ls"];
  if (phase === "auditing" || phase === "explorer") return ["read", "grep", "find", "ls"];
  return ["read", "bash", "edit", "write", "grep", "find", "ls"];
}

function piAuthPath(env: NodeJS.ProcessEnv): string {
  return join(env.PI_CODING_AGENT_DIR ?? join(homedir(), ".pi", "agent"), "auth.json");
}

function normalizeSdkHarnessError(error: unknown, agent: AgentName): JriError {
  if (error instanceof JriError) return error;
  const message = error instanceof Error ? error.message : String(error);
  return new JriError(
    `${agent} SDK harness failed. ${message}`.trim(),
    "harness-failed",
    "Inspect the SDK diagnostics, verify auth/model configuration, and retry.",
  );
}

async function assertProviderAuth(projectDir: string, env: NodeJS.ProcessEnv): Promise<void> {
  if (env.JRI_PI_COMMAND) return;
  const status = await getAuthStatus(projectDir, env);
  if (status.authenticated) return;
  if (status.recovery) {
    throw new JriError(status.recovery.message, status.recovery.code, status.recovery.instructions);
  }
  throw new JriError(
    "OpenAI authentication is required before JRI can start a controlled Pi session.",
    "auth-required",
    "Run jri auth login, set OPENAI_API_KEY, or complete Pi OpenAI auth, then retry.",
  );
}

async function appendMergedStreams(path: string, streams: ReadableStream<Uint8Array>[]): Promise<void> {
  let writeChain = Promise.resolve();
  const enqueueWrite = (text: string) => {
    if (!text) return;
    writeChain = writeChain.then(() => appendFile(path, text, "utf8"));
  };

  await Promise.all(streams.map((stream) => readStreamIntoWriter(stream, enqueueWrite)));
  await writeChain;
}

async function readStreamIntoWriter(stream: ReadableStream<Uint8Array>, write: (text: string) => void): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    write(decoder.decode(chunk.value, { stream: true }));
  }
  const tail = decoder.decode();
  if (tail) write(tail);
}

type CapturedCommand = {
  command: string[];
  cwd: string;
  env: NodeJS.ProcessEnv;
  timeoutMs: number;
  signal?: AbortSignal;
  onStdoutChunk?: (chunk: string) => void | Promise<void>;
  onStderrChunk?: (chunk: string) => void | Promise<void>;
  child?: {
    projectDir: string;
    loopId: string;
    capability: "harness" | "explorer" | "web";
  };
};

async function runCommandCapture(request: CapturedCommand): Promise<{ exitCode: number; text: string }> {
  if (request.signal?.aborted) {
    throw harnessCancelledError();
  }
  const proc = Bun.spawn(request.command, {
    cwd: request.cwd,
    stdout: "pipe",
    stderr: "pipe",
    stdin: "ignore",
    env: request.env,
  });
  const childRegistration = request.child
    ? await maybeRegisterLoopChild(request.child.projectDir, request.child.loopId, proc.pid, request.child.capability)
    : { cleanup: async () => {} };
  let timedOut = false;
  const cancellation = bindProcessCancellation(proc, request.signal);
  const timeout = setTimeout(() => {
    timedOut = true;
    terminateProcess(proc);
  }, request.timeoutMs);
  try {
    const [stdout, stderr, exitCode] = await Promise.all([
      streamText(proc.stdout, request.onStdoutChunk),
      streamText(proc.stderr, request.onStderrChunk),
      proc.exited,
    ]);
    if (cancellation.cancelled) throw harnessCancelledError();
    if (timedOut) {
      throw new JriError(
        `JRI harness timed out after ${request.timeoutMs}ms.`,
        "harness-timeout",
        "Retry after narrowing the task or resolving the unresponsive harness.",
      );
    }
    return { exitCode, text: `${stdout}${stderr ? `${stdout ? "\n" : ""}${stderr}` : ""}` };
  } finally {
    clearTimeout(timeout);
    cancellation.cleanup();
    await childRegistration.cleanup();
  }
}

function bindProcessCancellation(proc: Bun.Subprocess, signal: AbortSignal | undefined): { readonly cancelled: boolean; cleanup: () => void } {
  let cancelled = false;
  const abort = (): void => {
    cancelled = true;
    terminateProcess(proc);
  };
  signal?.addEventListener("abort", abort, { once: true });
  return {
    get cancelled() {
      return cancelled;
    },
    cleanup: () => {
      signal?.removeEventListener("abort", abort);
    },
  };
}

function terminateProcess(proc: Bun.Subprocess): void {
  proc.kill("SIGTERM");
  setTimeout(() => proc.kill("SIGKILL"), 250).unref?.();
}

function harnessCancelledError(): JriError {
  return new JriError("JRI harness invocation was cancelled.", "harness-cancelled", "Retry the operation if it is still needed.");
}

export type RegisteredLoopChild = {
  id: string;
  pid: number;
  capability: "harness" | "explorer" | "web";
  startedAt: string;
};

type LoopChildRecord =
  | (RegisteredLoopChild & { event: "started" })
  | { event: "finished"; id: string; pid: number; finishedAt: string };

async function maybeRegisterLoopChild(
  projectDir: string,
  loopId: string,
  pid: number | undefined,
  capability: RegisteredLoopChild["capability"],
): Promise<{ cleanup: () => Promise<void> }> {
  if (!pid) return { cleanup: async () => {} };
  const child = await registerLoopChild(projectDir, loopId, { pid, capability });
  return {
    cleanup: async () => {
      await unregisterLoopChild(projectDir, loopId, child);
    },
  };
}

export async function registerLoopChild(
  projectDir: string,
  loopId: string,
  child: Pick<RegisteredLoopChild, "pid" | "capability">,
): Promise<RegisteredLoopChild> {
  const registered: RegisteredLoopChild = {
    id: crypto.randomUUID(),
    pid: child.pid,
    capability: child.capability,
    startedAt: new Date().toISOString(),
  };
  await appendLoopChildRecord(projectDir, loopId, { event: "started", ...registered });
  return registered;
}

export async function unregisterLoopChild(projectDir: string, loopId: string, child: Pick<RegisteredLoopChild, "id" | "pid">): Promise<void> {
  await appendLoopChildRecord(projectDir, loopId, {
    event: "finished",
    id: child.id,
    pid: child.pid,
    finishedAt: new Date().toISOString(),
  });
}

export async function readActiveLoopChildren(projectDir: string, loopId: string): Promise<RegisteredLoopChild[]> {
  let text = "";
  try {
    text = await readFile(loopChildRegistryPath(projectDir, loopId), "utf8");
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") return [];
    throw error;
  }
  const active = new Map<string, RegisteredLoopChild>();
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const parsed = JSON.parse(line) as LoopChildRecord;
    if (parsed.event === "started") {
      active.set(parsed.id, {
        id: parsed.id,
        pid: parsed.pid,
        capability: parsed.capability,
        startedAt: parsed.startedAt,
      });
    } else {
      active.delete(parsed.id);
    }
  }
  return [...active.values()];
}

async function appendLoopChildRecord(projectDir: string, loopId: string, record: LoopChildRecord): Promise<void> {
  const path = loopChildRegistryPath(projectDir, loopId);
  await mkdir(dirname(path), { recursive: true });
  await appendFile(path, `${JSON.stringify(record)}\n`, "utf8");
}

function loopChildRegistryPath(projectDir: string, loopId: string): string {
  return join(projectDir, ".jri", "logs", loopId, "child-processes.jsonl");
}

async function streamText(
  stream: ReadableStream<Uint8Array>,
  onChunk?: (chunk: string) => void | Promise<void>,
): Promise<string> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let text = "";
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    const decoded = decoder.decode(chunk.value, { stream: true });
    if (!decoded) continue;
    text += decoded;
    await onChunk?.(decoded);
  }
  const tail = decoder.decode();
  if (tail) {
    text += tail;
    await onChunk?.(tail);
  }
  return text;
}

function createChatVisibleOutputForwarder(write: HarnessOutputSink["write"]): {
  push: (chunk: string) => Promise<void>;
  finish: () => Promise<void>;
} {
  const handoffPrefix = "JRI_HANDOFF_JSON:";
  let atLineStart = true;
  let candidatePrefix = "";
  let suppressingLine = false;

  const flushVisible = async (text: string): Promise<void> => {
    if (!text) return;
    await write(text);
  };

  return {
    async push(chunk: string): Promise<void> {
      if (!chunk) return;
      let visible = "";
      for (const char of chunk) {
        if (suppressingLine) {
          if (char === "\n") {
            suppressingLine = false;
            atLineStart = true;
          }
          continue;
        }

        if (atLineStart) {
          candidatePrefix += char;
          if (handoffPrefix.startsWith(candidatePrefix)) {
            if (candidatePrefix === handoffPrefix) {
              candidatePrefix = "";
              suppressingLine = true;
              atLineStart = false;
            }
            continue;
          }

          visible += candidatePrefix;
          atLineStart = candidatePrefix.endsWith("\n");
          candidatePrefix = "";
          continue;
        }

        visible += char;
        if (char === "\n") atLineStart = true;
      }
      await flushVisible(visible);
    },
    async finish(): Promise<void> {
      if (suppressingLine || candidatePrefix.length === 0) return;
      const visible = candidatePrefix;
      candidatePrefix = "";
      atLineStart = visible.endsWith("\n");
      await flushVisible(visible);
    },
  };
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

function extractExplorerSummary(output: string, handoffLimit: number): string {
  const frames = output
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.startsWith(explorerSummaryFramePrefix));
  if (frames.length === 0) {
    throw new JriError(
      "Explorer did not return a JRI_EXPLORER_SUMMARY_JSON handoff.",
      "missing-explorer-summary",
      "Retry the explorer task through the JRI explorer capability so the parent receives a structured summary frame.",
    );
  }
  if (frames.length > 1) {
    throw new JriError(
      "Explorer returned multiple JRI_EXPLORER_SUMMARY_JSON handoffs.",
      "multiple-explorer-summaries",
      "Retry with one final explorer summary frame.",
    );
  }

  const payload = frames[0]!.slice(explorerSummaryFramePrefix.length).trim();
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch (error) {
    throw new JriError(
      "Explorer returned invalid summary JSON.",
      "invalid-explorer-summary",
      error instanceof Error ? error.message : "Return one JSON object after JRI_EXPLORER_SUMMARY_JSON.",
    );
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw invalidExplorerSummary("summary handoff must be a JSON object.");
  }
  const record = parsed as Record<string, unknown>;
  const keys = Object.keys(record);
  const unsupported = keys.filter((key) => key !== "summary");
  if (unsupported.length > 0) {
    throw invalidExplorerSummary(`summary handoff has unsupported key(s): ${unsupported.join(", ")}.`);
  }
  if (typeof record.summary !== "string" || record.summary.trim().length === 0) {
    throw invalidExplorerSummary("summary handoff needs a non-empty summary string.");
  }
  const summary = record.summary.trim();
  if (summary.length > handoffLimit) {
    throw invalidExplorerSummary(`summary exceeds ${handoffLimit} characters.`);
  }
  return summary;
}

function invalidExplorerSummary(detail: string): JriError {
  return new JriError(
    `Explorer returned an invalid summary handoff: ${detail}`,
    "invalid-explorer-summary",
    "Return exactly one JRI_EXPLORER_SUMMARY_JSON line with a bounded summary string.",
  );
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
