import { mkdir, rename, stat, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { getAuthStatus, login, logout } from "./auth";
import { sendChat } from "./chat";
import { JriError } from "./errors";
import { getRecoveredStatus, observeLoop } from "./daemon-runtime";
import { daemonHaltLoop, daemonObserveLoop, daemonRequestStop, daemonResumeLoop, daemonStartLoop, daemonStatus } from "./daemon-ipc";
import { invokeDefaultHarness } from "./harness";
import { readInterrogationState } from "./interrogation-state";
import { assertStatusProjectDir, defaultConfig, defaultStatus, parseJsonObject, validateConfig, validateStatus } from "./schema";
import type { AuthResult, AuthState, ChatInput, CoreEvent, HaltOptions, LoopObserveOptions, ProjectConfig, ProjectStatus } from "./types";

const agentsTemplate = `## Build & Run

Succinct rules for how to BUILD the project:

## Validation

Project-specific validation commands are not known yet. Replace each item with
the exact command after it has been verified for this project.

- Tests: not documented yet
- Typecheck: not documented yet
- Lint: not documented yet

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
    status: async (): Promise<AuthState> => getAuthStatus(),
    login: async (): Promise<AuthResult> => login(),
    logout: async (): Promise<void> => logout(),
  };

  readonly chat = {
    send: (input: ChatInput): AsyncIterable<CoreEvent> => this.sendChat(input),
  };

  readonly loop = {
    observe: (options: LoopObserveOptions = {}): AsyncIterable<CoreEvent> => {
      return observeWithFallback(this.projectDir, options);
    },
    requestStop: async (): Promise<void> => {
      await daemonRequestStop(this.projectDir);
    },
    halt: (options: HaltOptions = {}): AsyncIterable<CoreEvent> => {
      return daemonHaltLoop(this.projectDir, options);
    },
    resume: (): AsyncIterable<CoreEvent> => {
      return daemonResumeLoop(this.projectDir);
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
    return assertStatusProjectDir(validateStatus(parseJsonObject(await Bun.file(path).text(), path), path), this.projectDir, path);
  }

  private async *sendChat(input: ChatInput): AsyncIterable<CoreEvent> {
    await this.ensureInitialized();
    yield* sendChat(this.projectDir, input, { startLoop: startLoopWithFallback, interrogatorHarness: invokeDefaultHarness });
  }

  private async ensureInitialized(): Promise<void> {
    const createdFiles: string[] = [];
    const initializedGit = this.needsGitInit && !(await pathExists(join(this.projectDir, ".git")));
    if (initializedGit) {
      await runGit(this.projectDir, ["init"], "Failed to initialize git repository.", "git-init-failed", "Run git init manually and retry.");
    }

    await mkdir(join(this.projectDir, ".jri", "specs"), { recursive: true });
    await mkdir(join(this.projectDir, ".jri", "logs"), { recursive: true });
    await writeIfMissing(join(this.projectDir, ".jri", "config.json"), `${JSON.stringify(defaultConfig, null, 2)}\n`, createdFiles, ".jri/config.json");
    await writeIfMissing(join(this.projectDir, ".jri", "status.json"), `${JSON.stringify(defaultStatus(this.projectDir), null, 2)}\n`, createdFiles, ".jri/status.json");
    await writeIfMissing(join(this.projectDir, ".jri", "scratchpad.md"), "", createdFiles, ".jri/scratchpad.md");
    await writeIfMissing(join(this.projectDir, ".jri", "logs", "interrogation.jsonl"), "", createdFiles, ".jri/logs/interrogation.jsonl");
    await writeIfMissing(join(this.projectDir, "AGENTS.md"), agentsTemplate, createdFiles, "AGENTS.md");

    await this.readConfig();
    await this.getStatus();
    if (initializedGit && createdFiles.length > 0) {
      await createInitialScaffoldCommit(this.projectDir, createdFiles);
    }
  }
}

async function* observeWithFallback(projectDir: string, options: LoopObserveOptions): AsyncIterable<CoreEvent> {
  try {
    yield* daemonObserveLoop(projectDir, options);
  } catch (error) {
    if (!isDaemonUnavailable(error)) throw error;
    yield* observeLoop(projectDir, options);
  }
}

async function* startLoopWithFallback(projectDir: string, trigger: Parameters<typeof daemonStartLoop>[1]): AsyncIterable<CoreEvent> {
  try {
    yield* daemonStartLoop(projectDir, trigger);
  } catch (error) {
    if (!isDaemonUnavailable(error)) throw error;
    throw error;
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
    assertStatusProjectDir(validateStatus(parseJsonObject(await Bun.file(statusPath).text(), statusPath), statusPath), projectDir, statusPath);
  }

  await readInterrogationState(projectDir);
}

async function writeIfMissing(path: string, contents: string, createdFiles?: string[], relativePath?: string): Promise<void> {
  if (await pathExists(path)) return;
  await mkdir(dirname(path), { recursive: true });
  await atomicWrite(path, contents);
  if (createdFiles && relativePath) createdFiles.push(relativePath);
}

async function atomicWrite(path: string, contents: string): Promise<void> {
  const tmpPath = `${path}.${process.pid}.${Date.now()}.tmp`;
  await writeFile(tmpPath, contents, "utf8");
  await rename(tmpPath, path);
}

async function createInitialScaffoldCommit(projectDir: string, scaffoldFiles: string[]): Promise<void> {
  await runGit(
    projectDir,
    ["add", "--", ...scaffoldFiles],
    "Failed to stage the initial JRI scaffold.",
    "git-init-failed",
    "Inspect the new repository state, then stage the scaffold files or remove the repo and retry bare jri.",
  );
  await runGit(
    projectDir,
    ["commit", "-m", "Initialize JRI project"],
    "Failed to create the initial JRI scaffold commit.",
    "git-init-failed",
    "Set git author information or repair the repository, then retry bare jri.",
  );
}

async function runGit(projectDir: string, args: string[], message: string, code: string, recovery: string): Promise<void> {
  const proc = Bun.spawn(["git", ...args], {
    cwd: projectDir,
    stdout: "pipe",
    stderr: "pipe",
    env: gitEnv(),
  });
  const exitCode = await proc.exited;
  if (exitCode !== 0) {
    const stderr = await new Response(proc.stderr).text();
    throw new JriError(message, code, stderr.trim() || recovery);
  }
}

function gitEnv(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    GIT_AUTHOR_NAME: process.env.GIT_AUTHOR_NAME ?? "JRI",
    GIT_AUTHOR_EMAIL: process.env.GIT_AUTHOR_EMAIL ?? "jri@local.invalid",
    GIT_COMMITTER_NAME: process.env.GIT_COMMITTER_NAME ?? process.env.GIT_AUTHOR_NAME ?? "JRI",
    GIT_COMMITTER_EMAIL: process.env.GIT_COMMITTER_EMAIL ?? process.env.GIT_AUTHOR_EMAIL ?? "jri@local.invalid",
  };
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
