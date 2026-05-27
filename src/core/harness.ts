import { appendFile } from "node:fs/promises";
import { join } from "node:path";
import { getAuthStatus } from "./auth";
import { JriError } from "./errors";
import { buildPiPrompt, modelForAgent } from "./prompts";
import { parseJsonObject, validateConfig } from "./schema";
import type { AgentName } from "./types";

export type HarnessPhase = "auditing" | "planning" | "building";

export type HarnessSessionRequest = {
  projectDir: string;
  loopId: string;
  phase: HarnessPhase;
  stdoutPath: string;
  env?: NodeJS.ProcessEnv;
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

export async function buildControlledPiCommand(request: Omit<HarnessSessionRequest, "stdoutPath">): Promise<PiHarnessCommand> {
  const env = request.env ?? process.env;
  await assertProviderAuth(env);

  const prompt = await buildPiPrompt(request.projectDir, request.phase);
  const agent = agentForPhase(request.phase);
  const model = modelForAgent(await readProjectConfig(request.projectDir), agent);
  const piPath = env.JRI_PI_COMMAND ?? "pi";

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
      "--print",
      prompt,
    ],
    env: {
      ...env,
      PI_CODING_AGENT_SESSION_DIR: join(request.projectDir, ".jri", "logs", request.loopId, "pi-sessions"),
    },
  };
}

function agentForPhase(phase: HarnessPhase): AgentName {
  if (phase === "auditing") return "auditor";
  return phase === "planning" ? "planner" : "builder";
}

function allowedToolsForPhase(phase: HarnessPhase): string[] {
  if (phase === "auditing") return ["read", "grep", "find", "ls"];
  return ["read", "bash", "edit", "write", "grep", "find", "ls"];
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

async function readProjectConfig(projectDir: string): Promise<unknown> {
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
