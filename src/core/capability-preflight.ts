import { access, stat } from "node:fs/promises";
import { constants } from "node:fs";
import { delimiter, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { JriError } from "./errors";

type CapabilityName = "web" | "explorer";

export async function assertCapabilityImplementationsAvailable(
  projectDir: string,
  capabilities: Array<{ name: CapabilityName }>,
  env: NodeJS.ProcessEnv = process.env,
): Promise<void> {
  const names = new Set(capabilities.map((capability) => capability.name));
  if (names.has("web")) await assertWebCapabilityAvailable(projectDir, env);
  if (names.has("explorer")) await assertExplorerCapabilityAvailable(projectDir, env);
}

export async function assertWebCapabilityAvailable(projectDir: string, env: NodeJS.ProcessEnv = process.env): Promise<void> {
  const command = env.JRI_PI_WEB_COMMAND;
  if (!command) return;
  const executable = await resolveExecutable(command, env.PATH, projectDir);
  if (executable) return;

  throw new JriError(
    "JRI web capability is not available: the configured JRI_PI_WEB_COMMAND executable was not found.",
    "capability-web-unavailable",
    "Configure the JRI web capability implementation, or set JRI_PI_WEB_COMMAND to a working executable before retrying.",
  );
}

export async function assertExplorerCapabilityAvailable(projectDir: string, env: NodeJS.ProcessEnv = process.env): Promise<void> {
  const extension = env.JRI_PI_SUBAGENT_EXTENSION;
  if (!extension) return;
  const trimmed = extension.trim();
  if (!trimmed) {
    throw new JriError(
      "JRI explorer capability is not available: JRI_PI_SUBAGENT_EXTENSION is empty.",
      "capability-explorer-unavailable",
      "Configure the JRI explorer capability implementation, or set JRI_PI_SUBAGENT_EXTENSION to a working extension before retrying.",
    );
  }

  const extensionPath = extensionFilePath(projectDir, trimmed);
  if (!extensionPath) return;

  try {
    await stat(extensionPath);
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
      throw new JriError(
        "JRI explorer capability is not available: the configured JRI_PI_SUBAGENT_EXTENSION was not found.",
        "capability-explorer-unavailable",
        "Configure the JRI explorer capability implementation, or set JRI_PI_SUBAGENT_EXTENSION to a working extension before retrying.",
      );
    }
    throw error;
  }
}

async function resolveExecutable(command: string, pathValue: string | undefined, projectDir: string): Promise<string | null> {
  const candidates = executableCandidates(command, pathValue, projectDir);
  for (const candidate of candidates) {
    try {
      await access(candidate, constants.X_OK);
      return candidate;
    } catch {
      continue;
    }
  }
  return null;
}

function executableCandidates(command: string, pathValue: string | undefined, projectDir: string): string[] {
  if (isPathLike(command)) {
    return [resolve(projectDir, command)];
  }

  const pathEntries = (pathValue ?? process.env.PATH ?? "").split(delimiter).filter(Boolean);
  return pathEntries.map((entry) => join(entry, command));
}

function extensionFilePath(projectDir: string, extension: string): string | null {
  if (extension.startsWith("file://")) return fileURLToPath(extension);
  if (isPathLike(extension)) return resolve(projectDir, extension);
  return null;
}

function isPathLike(value: string): boolean {
  return isAbsolute(value) || value.startsWith("./") || value.startsWith("../") || value.includes("/") || value.includes("\\");
}
