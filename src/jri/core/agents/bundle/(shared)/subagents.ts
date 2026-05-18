import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { basename, join } from "node:path";

export const CHILD_PI_MAX_BUFFER = 64 * 1024 * 1024;
export const EXPLORER_TASK_TIMEOUT_MS = 180_000;
export const VALIDATOR_TIMEOUT_MS = 300_000;
export const WEB_SEARCH_TIMEOUT_MS = 15_000;
export const PYTHON_TOOL_TIMEOUT_MS = 30_000;
export const PYTHON_TOOL_MAX_BUFFER = 4 * 1024 * 1024;

export type ChildProcessResult = {
  status: number;
  stdout: string;
  stderr: string;
  error?: string;
};

function textParts(parts: unknown): string {
  if (!Array.isArray(parts)) return "";
  return parts
    .filter(
      (part): part is { type: string; text: string } =>
        typeof part === "object" &&
        part !== null &&
        "type" in part &&
        "text" in part &&
        part.type === "text" &&
        typeof part.text === "string",
    )
    .map((part) => part.text)
    .join("");
}

export function terminalAssistantText(stdout: string): string | undefined {
  let final = "";
  for (const line of stdout.split("\n")) {
    if (!line.trim()) continue;
    try {
      const event = JSON.parse(line);
      if (event?.type === "message_end" && event?.message?.role === "assistant") {
        const text = textParts(event.message.content);
        if (text) final = text;
        continue;
      }
      if (event?.type === "agent_end" && event?.message?.role === "assistant") {
        const text = textParts(event.message.content);
        if (text) final = text;
        continue;
      }
      if (
        event?.type === "message_update" &&
        event?.assistantMessageEvent?.type === "text_end" &&
        typeof event.assistantMessageEvent.content === "string"
      ) {
        final = event.assistantMessageEvent.content;
      }
    } catch {
      continue;
    }
  }
  return final.trim() || undefined;
}

export function finalAssistantText(stdout: string): string {
  return terminalAssistantText(stdout) ?? stdout.trim();
}

export function configuredModel(packageRoot: string, name: string): string | undefined {
  try {
    const manifest = JSON.parse(readFileSync(join(packageRoot, "package.json"), "utf-8"));
    const model = manifest?.jri?.models?.[name];
    return typeof model === "string" && model ? model : undefined;
  } catch {
    return undefined;
  }
}

export function getPiInvocation(args: string[]): { command: string; args: string[] } {
  const currentScript = process.argv[1];
  const isBunVirtualScript = currentScript?.startsWith("/$bunfs/root/");
  if (currentScript && !isBunVirtualScript && existsSync(currentScript)) {
    return { command: process.execPath, args: [currentScript, ...args] };
  }

  const execName = basename(process.execPath).toLowerCase();
  const isGenericRuntime = /^(node|bun)(\.exe)?$/.test(execName);
  if (!isGenericRuntime) {
    return { command: process.execPath, args };
  }

  return { command: "pi", args };
}

export function runUntilTerminalOutput(
  command: string,
  args: string[],
  options: {
    cwd: string;
    env: NodeJS.ProcessEnv;
    timeoutMs: number;
    maxBuffer?: number;
  },
): Promise<ChildProcessResult> {
  return new Promise((resolve) => {
    const maxBuffer = options.maxBuffer ?? CHILD_PI_MAX_BUFFER;
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      stdio: ["ignore", "pipe", "pipe"],
      detached: process.platform !== "win32",
    });
    let stdout = "";
    let stderr = "";
    let truncated = false;
    let settled = false;
    const killChild = () => {
      try {
        if (process.platform !== "win32" && child.pid !== undefined) {
          process.kill(-child.pid, "SIGTERM");
        } else {
          child.kill("SIGTERM");
        }
      } catch {
        child.kill("SIGTERM");
      }
    };
    const finish = (result: ChildProcessResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    const append = (current: string, data: unknown) => {
      if (current.length >= maxBuffer) {
        truncated = true;
        return current;
      }
      const next = current + String(data);
      if (next.length > maxBuffer) {
        truncated = true;
        return next.slice(0, maxBuffer);
      }
      return next;
    };
    const finishIfTerminal = () => {
      if (!terminalAssistantText(stdout)) return;
      killChild();
      finish({
        status: truncated ? 1 : 0,
        stdout,
        stderr: stderr.trim(),
        error: truncated ? "child process output exceeded buffer limit" : undefined,
      });
    };
    const timer = setTimeout(() => {
      killChild();
      finish({
        status: 1,
        stdout,
        stderr: stderr.trim(),
        error: `child process timed out after ${options.timeoutMs}ms`,
      });
    }, options.timeoutMs);
    child.stdout.on("data", (data) => {
      stdout = append(stdout, data);
      finishIfTerminal();
    });
    child.stderr.on("data", (data) => {
      stderr = append(stderr, data);
    });
    child.on("error", (error) => {
      finish({ status: 1, stdout, stderr: stderr.trim(), error: error.message });
    });
    child.on("close", (code) => {
      finish({
        status: truncated ? 1 : (code ?? 0),
        stdout,
        stderr: stderr.trim(),
        error: truncated ? "child process output exceeded buffer limit" : undefined,
      });
    });
  });
}
