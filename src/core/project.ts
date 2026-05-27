import { mkdir, rename, stat, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { JriError } from "./errors";
import { getRecoveredStatus, haltLoop, observeLoop, requestGracefulStop, resumeLoop } from "./daemon-runtime";
import { daemonHaltLoop, daemonObserveLoop, daemonRequestStop, daemonResumeLoop, daemonStatus } from "./daemon-ipc";
import { defaultConfig, defaultStatus, parseJsonObject, validateConfig, validateStatus } from "./schema";
import type { AuthResult, AuthState, ChatInput, CoreEvent, ProjectConfig, ProjectStatus } from "./types";

const agentsTemplate = `## Build & Run

Succinct rules for how to BUILD the project:

## Validation

Run these after implementing to get immediate feedback:

- Tests: \`[test command]\`
- Typecheck: \`[typecheck command]\`
- Lint: \`[lint command]\`

## Operational Notes

Succinct learnings about how to RUN the project:

...

### Codebase Patterns

...
`;

export class Project {
  constructor(readonly projectDir: string, private readonly needsGitInit: boolean) {}

  readonly lifecycle = {
    ensureInitialized: async (): Promise<void> => {
      await this.ensureInitialized();
    },
  };

  readonly status = {
    get: async (): Promise<ProjectStatus> => {
      try {
        return await daemonStatus(this.projectDir);
      } catch (error) {
        if (!isDaemonUnavailable(error)) throw error;
        return await getRecoveredStatus(this.projectDir);
      }
    },
  };

  readonly auth = {
    status: async (): Promise<AuthState> => ({ provider: "openai", authenticated: false }),
    login: async (): Promise<AuthResult> => ({
      status: "userActionRequired",
      instructions: "Pi-backed authentication is not implemented yet. Use the future JRI auth provider flow when available.",
    }),
    logout: async (): Promise<void> => {},
  };

  readonly chat = {
    send: async function* (input: ChatInput): AsyncIterable<CoreEvent> {
      void input;
      throw new JriError("The JRI interrogator is not implemented yet.", "not-implemented", "Run this command again after the interrogator P0 is implemented.");
    },
  };

  readonly loop = {
    observe: (): AsyncIterable<CoreEvent> => {
      return observeWithFallback(this.projectDir);
    },
    requestStop: async (): Promise<void> => {
      try {
        await daemonRequestStop(this.projectDir);
      } catch (error) {
        if (!isDaemonUnavailable(error)) throw error;
        await requestGracefulStop(this.projectDir);
      }
    },
    halt: (): AsyncIterable<CoreEvent> => {
      return haltWithFallback(this.projectDir);
    },
    resume: (): AsyncIterable<CoreEvent> => {
      return resumeWithFallback(this.projectDir);
    },
  };

  async readConfig(): Promise<ProjectConfig | null> {
    const path = join(this.projectDir, ".jri", "config.json");
    if (!(await Bun.file(path).exists())) return null;
    return validateConfig(parseJsonObject(await Bun.file(path).text(), path), path);
  }

  private async getStatus(): Promise<ProjectStatus> {
    const path = join(this.projectDir, ".jri", "status.json");
    if (!(await Bun.file(path).exists())) {
      throw new JriError("JRI status does not exist yet.", "uninitialized", "Run bare jri or call ensureInitialized() to create the scaffold.");
    }
    return validateStatus(parseJsonObject(await Bun.file(path).text(), path), path);
  }

  private async ensureInitialized(): Promise<void> {
    if (this.needsGitInit && !(await pathExists(join(this.projectDir, ".git")))) {
      const proc = Bun.spawn(["git", "init"], { cwd: this.projectDir, stdout: "pipe", stderr: "pipe" });
      const exitCode = await proc.exited;
      if (exitCode !== 0) {
        const stderr = await new Response(proc.stderr).text();
        throw new JriError("Failed to initialize git repository.", "git-init-failed", stderr.trim() || "Run git init manually and retry.");
      }
    }

    await mkdir(join(this.projectDir, ".jri", "specs"), { recursive: true });
    await mkdir(join(this.projectDir, ".jri", "logs"), { recursive: true });
    await writeIfMissing(join(this.projectDir, ".jri", "config.json"), `${JSON.stringify(defaultConfig, null, 2)}\n`);
    await writeIfMissing(join(this.projectDir, ".jri", "status.json"), `${JSON.stringify(defaultStatus(this.projectDir), null, 2)}\n`);
    await writeIfMissing(join(this.projectDir, ".jri", "scratchpad.md"), "");
    await writeIfMissing(join(this.projectDir, ".jri", "logs", "interrogation.jsonl"), "");
    await writeIfMissing(join(this.projectDir, "AGENTS.md"), agentsTemplate);

    await this.readConfig();
    await this.getStatus();
  }
}

async function* observeWithFallback(projectDir: string): AsyncIterable<CoreEvent> {
  try {
    yield* daemonObserveLoop(projectDir);
  } catch (error) {
    if (!isDaemonUnavailable(error)) throw error;
    yield* observeLoop(projectDir);
  }
}

async function* haltWithFallback(projectDir: string): AsyncIterable<CoreEvent> {
  try {
    yield* daemonHaltLoop(projectDir);
  } catch (error) {
    if (!isDaemonUnavailable(error)) throw error;
    yield* haltLoop(projectDir);
  }
}

async function* resumeWithFallback(projectDir: string): AsyncIterable<CoreEvent> {
  try {
    yield* daemonResumeLoop(projectDir);
  } catch (error) {
    if (!isDaemonUnavailable(error)) throw error;
    yield* resumeLoop(projectDir);
  }
}

function isDaemonUnavailable(error: unknown): boolean {
  return error instanceof JriError && error.code === "daemon-unavailable";
}

export async function validateExistingProject(projectDir: string): Promise<void> {
  const jriDir = join(projectDir, ".jri");
  if (!(await pathExists(jriDir))) return;

  const configPath = join(jriDir, "config.json");
  if (await Bun.file(configPath).exists()) {
    validateConfig(parseJsonObject(await Bun.file(configPath).text(), configPath), configPath);
  }

  const statusPath = join(jriDir, "status.json");
  if (await Bun.file(statusPath).exists()) {
    validateStatus(parseJsonObject(await Bun.file(statusPath).text(), statusPath), statusPath);
  }
}

async function writeIfMissing(path: string, contents: string): Promise<void> {
  if (await pathExists(path)) return;
  await mkdir(dirname(path), { recursive: true });
  await atomicWrite(path, contents);
}

async function atomicWrite(path: string, contents: string): Promise<void> {
  const tmpPath = `${path}.${process.pid}.${Date.now()}.tmp`;
  await writeFile(tmpPath, contents, "utf8");
  await rename(tmpPath, path);
}

async function pathExists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
      return false;
    }
    throw error;
  }
}
