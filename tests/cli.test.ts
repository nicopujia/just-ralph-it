import { appendFile, chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "bun:test";
import { startDaemonServer, type DaemonPaths } from "../src/core/daemon-ipc";
import { defaultStatus } from "../src/core/schema";
import { appendLoopEvent, updateStatus, writeStatusAtomic } from "../src/core/runtime-state";
import { fingerprintSpecFile, writeInterrogationState } from "../src/core/interrogation-state";

const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const cliPath = join(repoRoot, "src", "cli", "index.ts");
const daemonEnvKeys = ["JRI_DAEMON_RUNTIME_DIR", "JRI_DAEMON_STATE_DIR", "JRI_DAEMON_SOCKET_PATH", "JRI_DAEMON_REGISTRY_PATH"];
const internalInvocationEnv = { JRI_INTERNAL_INVOCATION: "1" };

async function tempProject(): Promise<string> {
  return await mkdtemp(join(tmpdir(), "jri-cli-test-"));
}

async function tempInitializedProject(): Promise<string> {
  const dir = await tempProject();
  await mkdir(join(dir, ".jri", "logs"), { recursive: true });
  await writeStatusAtomic(dir, defaultStatus(dir));
  return dir;
}

async function activateLoop(dir: string, loopId: string, state: "auditing" | "planning" | "building" = "building"): Promise<void> {
  await mkdir(join(dir, ".jri", "logs", loopId), { recursive: true });
  await writeStatusAtomic(dir, {
    ...defaultStatus(dir),
    state,
    activeLoopId: loopId,
    lastLoopId: loopId,
    lock: activeTestLock(state === "auditing" ? "audit" : state === "planning" ? "plan" : "build"),
  });
}

function activeTestLock(operation: "audit" | "plan" | "build") {
  return {
    owner: "daemon" as const,
    pid: process.pid,
    operation,
    acquiredAt: "2026-05-27T18:42:10.000Z",
    heartbeatAt: "2026-05-27T18:42:10.000Z",
    expiresAt: "2999-01-01T00:00:00.000Z",
  };
}

describe("CLI", () => {
  test("public package bin path exposes help without internal entrypoints", async () => {
    const packageJson = JSON.parse(await readFile(join(repoRoot, "package.json"), "utf8")) as { bin?: { jri?: string } };
    const jriBin = packageJson.bin?.jri;
    expect(jriBin).toBe("./src/cli/index.ts");

    const proc = Bun.spawn([join(repoRoot, jriBin ?? ""), "auth", "--help"], {
      cwd: repoRoot,
      stdout: "pipe",
      stderr: "pipe",
    });

    const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

    expect(exitCode).toBe(0);
    expect(stderr).toBe("");
    expect(stdout).toContain("Usage: jri auth {status|login|logout}");
    expect(stdout).not.toContain("--run-web");
    expect(stdout).not.toContain("--run-explorer");
  });

  test("direct internal entrypoints are rejected from the public CLI", async () => {
    const proc = Bun.spawn(["bun", cliPath, "--run-web", "search", "{}", "docs"], {
      cwd: repoRoot,
      stdout: "pipe",
      stderr: "pipe",
    });

    const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

    expect(exitCode).toBe(1);
    expect(stdout).toBe("");
    expect(stderr).toContain("--run-web is an internal JRI entrypoint");
    expect(stderr).toContain("Use the public MVP commands");
  });

  test("bare jri creates the required scaffold commit in a brand-new repository", async () => {
    const dir = await tempProject();
    try {
      const proc = Bun.spawn(["bun", cliPath], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          GIT_AUTHOR_NAME: "JRI",
          GIT_AUTHOR_EMAIL: "jri@example.com",
          GIT_COMMITTER_NAME: "JRI",
          GIT_COMMITTER_EMAIL: "jri@example.com",
        },
      });
      proc.stdin.end();

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stderr).toContain(`Initialized JRI in ${dir}`);
      expect(stdout).toContain("idle");
      expect(await gitOutput(dir, ["log", "-1", "--pretty=%s"])).toBe("Initialize JRI project\n");
      expect((await gitOutput(dir, ["status", "--short"])).trim()).toBe("");
      expect((await gitOutput(dir, ["ls-tree", "--name-only", "-r", "HEAD"])).trim().split("\n").sort()).toEqual([
        ".jri/config.json",
        ".jri/logs/interrogation.jsonl",
        ".jri/scratchpad.md",
        ".jri/status.json",
        "AGENTS.md",
      ]);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("bare jri does not auto-commit scaffold files inside an existing repository", async () => {
    const dir = await tempProject();
    try {
      await git(dir, ["init"]);
      await writeFile(join(dir, "README.md"), "# Existing repo\n", "utf8");
      await git(dir, ["add", "README.md"]);
      await git(dir, ["commit", "-m", "Initial repo commit"]);
      const headBefore = await gitOutput(dir, ["rev-parse", "--verify", "HEAD"]);

      const proc = Bun.spawn(["bun", cliPath], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
      });
      proc.stdin.end();

      const [exitCode, stderr] = await Promise.all([proc.exited, new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stderr).toContain(`Initialized JRI in ${dir}`);
      expect(await gitOutput(dir, ["rev-parse", "--verify", "HEAD"])).toBe(headBefore);
      expect((await gitOutput(dir, ["status", "--short"])).trim().split("\n").sort()).toEqual([
        "?? .jri/",
        "?? AGENTS.md",
      ]);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("bare jri records a piped interrogation message", async () => {
    const dir = await tempProject();
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/usr/bin/env bash",
          "printf 'Which deployment environment should Ralph target?\\n'",
          "printf 'JRI_HANDOFF_JSON: {\"agent\":\"interrogator\",\"action\":\"messageOnly\",\"summary\":\"Asked about deployment environment.\"}\\n'",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);

      const proc = Bun.spawn(["bun", cliPath], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          JRI_PI_COMMAND: fakePi,
        },
      });
      proc.stdin.write("Need a deployment workflow.\n");
      proc.stdin.end();

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stderr).toContain(`Initialized JRI in ${dir}`);
      expect(stdout).toContain("Which deployment environment should Ralph target?");

      const log = await readFile(join(dir, ".jri", "logs", "interrogation.jsonl"), "utf8");
      expect(log).toContain("Need a deployment workflow.");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("bare jri rejects restarting a stopped loop when the authorized specs fingerprint is missing", async () => {
    const dir = await tempInitializedProject();
    const env = isolatedDaemonEnv(dir);
    const daemon = await startDaemonServer({
      paths: daemonPathsForEnv(env),
      idleTimeoutMs: 10_000,
      runtimeOptions: {
        spawnRunner: ({ loopId }) => {
          scheduleLoopCompletion(dir, loopId);
          return { pid: process.pid, command: "runner auditing" };
        },
      },
    });
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T200000Z",
        lastLoopId: "20260527T200000Z",
      });

      const proc = Bun.spawn(["bun", cliPath], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
        env,
      });
      proc.stdin.write("just ralph it\n");
      proc.stdin.end();

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(1);
      expect(stdout).toContain("Start request accepted (just ralph it). Running the specs auditor now.");
      expect(stderr).toContain("Cannot start because the stopped loop is missing its authorized specs fingerprint.");
      expect(stderr).toContain("Return to bare jri, confirm the requirements, then say just ralph it so audit and planning authorize a fresh lifecycle.");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T200000Z",
        lastLoopId: "20260527T200000Z",
      });
      expect(status.authorizedSpecsFingerprint).toBeUndefined();
    } finally {
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("bare jri renders non-message chat events from the fallback stream", async () => {
    const dir = await tempProject();
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/usr/bin/env bash",
          "mkdir -p .jri/specs",
          "printf '# App\\n\\nBuild a focused CLI.\\n' > .jri/specs/app.md",
          "printf 'I wrote the CLI spec.\\n'",
          "printf 'JRI_HANDOFF_JSON: {\"agent\":\"interrogator\",\"action\":\"specsUpdated\",\"specFiles\":[\".jri/specs/app.md\"],\"summary\":\"Captured CLI requirements.\",\"sealedSpecFiles\":[\".jri/specs/app.md\"]}\\n'",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);

      const proc = Bun.spawn(["bun", cliPath], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          JRI_PI_COMMAND: fakePi,
        },
      });
      proc.stdin.write("Capture the CLI scope.\n");
      proc.stdin.end();

      const [exitCode, stdout] = await Promise.all([proc.exited, new Response(proc.stdout).text()]);

      expect(exitCode).toBe(0);
      expect(stdout).toContain("I wrote the CLI spec.");
      expect(stdout).toContain("Specs updated: Captured CLI requirements. (.jri/specs/app.md)");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("bare jri shows full blocked resolution guide in fallback status output", async () => {
    const dir = await tempInitializedProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        blocker: {
          reason: "needsHumanTask",
          description: "Cloudflare billing access is missing.",
          resolutionGuide: {
            summary: "A billing-capable account must be connected before deployment.",
            steps: ["Sign in to Cloudflare.", "Enable billing on the target account."],
            successCriteria: ["JRI can deploy without an account setup error."],
            resumeInstruction: "Say done in bare jri after billing is enabled.",
          },
        },
      });

      const proc = Bun.spawn(["bun", cliPath], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
      });
      proc.stdin.end();
      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stderr).toContain(`Initialized JRI in ${dir}`);
      expect(stdout).toContain("blocked | reason: needsHumanTask | Cloudflare billing access is missing.");
      expect(stdout).toContain("1. Sign in to Cloudflare.");
      expect(stdout).toContain("2. Enable billing on the target account.");
      expect(stdout).toContain("Success criteria:");
      expect(stdout).toContain("Resume: Say done in bare jri after billing is enabled.");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("bare jri done accepts an interrogator humanTaskVerified handoff and records blocker resolution", async () => {
    const dir = await tempInitializedProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        blocker: {
          reason: "needsHumanTask",
          description: "Deployment credentials are missing.",
          resolutionGuide: {
            summary: "Credentials are required.",
            steps: ["Set the deployment token outside chat."],
            resumeInstruction: "Say done in bare jri after the token is available.",
          },
          resumePhase: "building",
        },
      });
      const fakePi = join(dir, "fake-verify-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/usr/bin/env bash",
          "printf 'Verified the deployment token without exposing it.\\n'",
          "printf 'JRI_HANDOFF_JSON: {\"agent\":\"interrogator\",\"action\":\"humanTaskVerified\",\"verificationSummary\":\"Deployment token is present.\"}\\n'",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);

      const proc = Bun.spawn(["bun", cliPath], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          JRI_PI_COMMAND: fakePi,
        },
      });
      proc.stdin.write("done\n");
      proc.stdin.end();

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(exitCode).toBe(0);
      expect(stderr).toContain(`Initialized JRI in ${dir}`);
      expect(stdout).toContain("Run jri loop resume");
      expect(status).toMatchObject({
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        blocker: {
          reason: "needsHumanTask",
          resumePhase: "building",
          resolution: {
            status: "verified",
            verificationSummary: "Deployment token is present.",
          },
        },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("bare jri with no input surfaces idle last result details", async () => {
    const dir = await tempInitializedProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        lastResult: {
          outcome: "completed",
          summary: "Deployed the app.",
          url: "https://example.com",
          validationPassed: true,
          commit: "abc123",
          tag: "0.0.1",
        },
      });

      const proc = Bun.spawn(["bun", cliPath], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
      });
      proc.stdin.end();
      const [exitCode, stdout] = await Promise.all([proc.exited, new Response(proc.stdout).text()]);

      expect(exitCode).toBe(0);
      expect(stdout).toContain("idle | result: completed | Deployed the app.");
      expect(stdout).toContain("URL: https://example.com");
      expect(stdout).toContain("Validation: passed");
      expect(stdout).toContain("Commit: abc123");
      expect(stdout).toContain("Tag: 0.0.1");
      expect(stdout).toContain("Next: Run bare jri to inspect the result or authorize another Ralph loop.");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("bare jri with no input renders stopped status with resume guidance", async () => {
    const dir = await tempInitializedProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        iteration: 2,
        lastResult: {
          outcome: "stopped",
          summary: "Graceful stop completed after iteration 2.",
        },
      });

      const proc = Bun.spawn(["bun", cliPath], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
      });
      proc.stdin.end();
      const [exitCode, stdout] = await Promise.all([proc.exited, new Response(proc.stdout).text()]);

      expect(exitCode).toBe(0);
      expect(stdout).toContain("stopped | iteration: 2 | Graceful stop completed after iteration 2.");
      expect(stdout).toContain("Next: Run jri loop resume to continue, or bare jri to revise requirements before authorizing a new loop.");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("bare jri with no input renders halted status with recovery guidance", async () => {
    const dir = await tempInitializedProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "halted",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lastResult: {
          outcome: "halted",
          summary: "Force halt completed. Rollback reset skipped.",
        },
      });

      const proc = Bun.spawn(["bun", cliPath], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
      });
      proc.stdin.end();
      const [exitCode, stdout] = await Promise.all([proc.exited, new Response(proc.stdout).text()]);

      expect(exitCode).toBe(0);
      expect(stdout).toContain("halted | Force halt completed. Rollback reset skipped.");
      expect(stdout).toContain("Next: Inspect the working tree, then run bare jri to reconcile requirements and authorize fresh work.");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("bare jri with no input detects pending spec reconciliation instead of only printing status", async () => {
    const dir = await tempInitializedProject();
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI.\n");
      const fingerprint = await fingerprintSpecFile(dir, ".jri/specs/app.md");
      await writeInterrogationState(dir, {
        schemaVersion: 1,
        topics: {
          app: {
            specFile: ".jri/specs/app.md",
            status: "sealed",
            lastReconciledSpecFingerprint: fingerprint,
          },
        },
      });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI and web UI.\n");

      const proc = Bun.spawn(["bun", cliPath], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
      });
      proc.stdin.end();
      const [exitCode, stdout] = await Promise.all([proc.exited, new Response(proc.stdout).text()]);
      const state = JSON.parse(await readFile(join(dir, ".jri", "interrogation-state.json"), "utf8"));

      expect(exitCode).toBe(0);
      expect(stdout).toContain("pending spec reconciliation");
      expect(stdout).toContain(".jri/specs/app.md changed after this topic was sealed");
      expect(state.topics.app.pendingReconciliation).toMatchObject({ reason: "manualSpecEdit" });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auth help lists stable commands before auth-only passthrough note", async () => {
    const dir = await tempProject();
    try {
      const proc = Bun.spawn(["bun", cliPath, "auth", "--help"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
      });
      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stderr).toBe("");
      expect(stdout).toContain("Stable auth commands:");
      expect(stdout.indexOf("jri auth status")).toBeLessThan(stdout.indexOf("Advanced passthrough:"));
      expect(stdout).toContain("This namespace is not general Pi access.");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auth forwards advanced auth-only passthrough commands to Pi", async () => {
    const dir = await tempProject();
    try {
      const fakePi = join(dir, "fake-pi.sh");
      const argvPath = join(dir, "pi-argv.txt");
      await writeFile(
        fakePi,
        [
          "#!/usr/bin/env bash",
          `printf '%s\\n' "$@" > ${JSON.stringify(argvPath)}`,
          "printf 'pi-auth-ok\\n'",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);

      const proc = Bun.spawn(["bun", cliPath, "auth", "providers", "list"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          JRI_PI_COMMAND: fakePi,
        },
      });
      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stdout).toContain("pi-auth-ok");
      expect(stderr).toBe("");
      expect(await readFile(argvPath, "utf8")).toBe("auth\nproviders\nlist\n");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auth normalizes failed advanced passthrough commands", async () => {
    const dir = await tempProject();
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/usr/bin/env bash",
          "printf 'unknown auth operation\\n' >&2",
          "exit 12",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);

      const proc = Bun.spawn(["bun", cliPath, "auth", "made-up"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          JRI_PI_COMMAND: fakePi,
        },
      });
      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(1);
      expect(stdout).toBe("");
      expect(stderr).toContain('Pi auth passthrough failed for "made-up".');
      expect(stderr).toContain("unknown auth operation");
      expect(stderr).toContain("jri auth --help");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auth login runs Pi-backed login and verifies JRI auth state", async () => {
    const dir = await tempProject();
    try {
      const piDir = join(dir, "pi-agent");
      const fakePi = join(dir, "fake-pi.sh");
      const argvPath = join(dir, "pi-argv.txt");
      await writeFile(
        fakePi,
        [
          "#!/usr/bin/env bash",
          "mkdir -p \"$PI_CODING_AGENT_DIR\"",
          `printf '%s\\n' "$@" > ${JSON.stringify(argvPath)}`,
          "printf '{\"openai\":{\"access\":\"access-token\"}}\\n' > \"$PI_CODING_AGENT_DIR/auth.json\"",
          "printf 'pi-login-ok\\n'",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);

      const proc = Bun.spawn(["bun", cliPath, "auth", "login"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          OPENAI_API_KEY: "",
          PI_CODING_AGENT_DIR: piDir,
          JRI_PI_COMMAND: fakePi,
        },
      });
      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stdout).toContain("pi-login-ok");
      expect(stdout).toContain("Authenticated.");
      expect(stderr).toContain("OpenAI authentication is required");
      expect(await readFile(argvPath, "utf8")).toBe("auth\nlogin\n");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auth login reports incomplete Pi-backed login when no usable credentials appear", async () => {
    const dir = await tempProject();
    try {
      const piDir = join(dir, "pi-agent");
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(fakePi, "#!/usr/bin/env bash\nprintf 'login finished without credentials\\n'\n", "utf8");
      await chmod(fakePi, 0o755);

      const proc = Bun.spawn(["bun", cliPath, "auth", "login"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          OPENAI_API_KEY: "",
          PI_CODING_AGENT_DIR: piDir,
          JRI_PI_COMMAND: fakePi,
        },
      });
      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(1);
      expect(stdout).toContain("login finished without credentials");
      expect(stderr).toContain("auth login completed but JRI still cannot find usable OpenAI credentials");
      expect(stderr).toContain("jri auth login");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("interactive bare jri runs inline auth and continues when credentials become available", async () => {
    const dir = await tempProject();
    try {
      const piDir = join(dir, "pi-agent");
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/usr/bin/env bash",
          "mkdir -p \"$PI_CODING_AGENT_DIR\"",
          "printf '{\"openai\":{\"access\":\"access-token\"}}\\n' > \"$PI_CODING_AGENT_DIR/auth.json\"",
          "printf 'pi-login-ok\\n'",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);

      const proc = Bun.spawn(["script", "-q", "-e", "-c", `bun ${cliPath}`, "/dev/null"], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          OPENAI_API_KEY: "",
          PI_CODING_AGENT_DIR: piDir,
          JRI_PI_COMMAND: fakePi,
        },
      });
      setTimeout(() => {
        proc.stdin.write("/exit\n");
        proc.stdin.end();
      }, 250);

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(`${stdout}\n${stderr}`).toContain("OpenAI authentication is required");
      expect(stdout).toContain("pi-login-ok");
      expect(stdout).toContain("Authenticated.");
      expect(stdout).toContain("jri>");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("interactive bare jri exits with recovery guidance when inline auth cannot run", async () => {
    const dir = await tempProject();
    try {
      const proc = Bun.spawn(["script", "-q", "-e", "-c", `bun ${cliPath}`, "/dev/null"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          OPENAI_API_KEY: "",
          PI_CODING_AGENT_DIR: join(dir, "pi-agent"),
          JRI_PI_COMMAND: join(dir, "missing-pi"),
        },
      });

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(1);
      expect(`${stdout}\n${stderr}`).toContain("OpenAI authentication is required");
      expect(`${stdout}\n${stderr}`).toContain("Pi auth passthrough is unavailable");
      expect(`${stdout}\n${stderr}`).toContain("jri auth --help");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auth status reports corrupt Pi auth cache without hard failure", async () => {
    const dir = await tempProject();
    try {
      const piDir = join(dir, "pi-agent");
      await mkdir(piDir, { recursive: true });
      await writeFile(join(piDir, "auth.json"), "{not json", "utf8");

      const proc = Bun.spawn(["bun", cliPath, "auth", "status"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          OPENAI_API_KEY: "",
          PI_CODING_AGENT_DIR: piDir,
        },
      });
      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stderr).toBe("");
      expect(stdout).toContain("openai: not authenticated");
      expect(stdout).toContain("Fix or remove");
      expect(stdout).toContain("jri auth status");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auth logout recovers corrupt Pi auth cache", async () => {
    const dir = await tempProject();
    try {
      const piDir = join(dir, "pi-agent");
      await mkdir(piDir, { recursive: true });
      const authPath = join(piDir, "auth.json");
      await writeFile(authPath, "{not json", "utf8");

      const logout = Bun.spawn(["bun", cliPath, "auth", "logout"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          OPENAI_API_KEY: "",
          PI_CODING_AGENT_DIR: piDir,
        },
      });
      const [logoutCode, logoutStdout, logoutStderr] = await Promise.all([
        logout.exited,
        new Response(logout.stdout).text(),
        new Response(logout.stderr).text(),
      ]);

      expect(logoutCode).toBe(0);
      expect(logoutStdout).toContain("Logged out.");
      expect(logoutStderr).toBe("");
      expect(await Bun.file(authPath).exists()).toBe(false);

      const status = Bun.spawn(["bun", cliPath, "auth", "status"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          OPENAI_API_KEY: "",
          PI_CODING_AGENT_DIR: piDir,
        },
      });
      const [statusCode, statusStdout, statusStderr] = await Promise.all([
        status.exited,
        new Response(status.stdout).text(),
        new Response(status.stderr).text(),
      ]);

      expect(statusCode).toBe(0);
      expect(statusStdout).toContain("openai: not authenticated");
      expect(statusStderr).toBe("");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("interactive bare jri opens a fallback interrogator REPL", async () => {
    const dir = await tempProject();
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/usr/bin/env bash",
          "printf 'Which CLI commands should stay public?\\n'",
          "printf 'JRI_HANDOFF_JSON: {\"agent\":\"interrogator\",\"action\":\"messageOnly\",\"summary\":\"Asked about public CLI scope.\"}\\n'",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);

      const proc = Bun.spawn(["script", "-q", "-e", "-c", `bun ${cliPath}`, "/dev/null"], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          OPENAI_API_KEY: "test-key",
          JRI_PI_COMMAND: fakePi,
        },
      });
      setTimeout(() => {
        proc.stdin.write("Need a CLI.\n/exit\n");
        proc.stdin.end();
      }, 1000);

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(`${stdout}\n${stderr}`).toContain(`Initialized JRI in ${dir}`);
      expect(stdout).toContain("jri>");
      expect(stdout).toContain("idle");
      expect(stdout).toContain("Which CLI commands should stay public?");

      const log = await readFile(join(dir, ".jri", "logs", "interrogation.jsonl"), "utf8");
      expect(log).toContain("Need a CLI.");
      expect(log).toContain("Which CLI commands should stay public?");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("hidden run-explorer command prints a bounded handoff and artifact ref", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await activateLoop(dir, "20260527T184210Z", "building");
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        "#!/usr/bin/env bash\nprintf 'JRI_EXPLORER_SUMMARY_JSON: {\"summary\":\"CLI explorer result\"}\\n'\n",
        "utf8",
      );
      await chmod(fakePi, 0o755);

      const proc = Bun.spawn(["bun", cliPath, "--run-explorer", dir, "20260527T184210Z", "Inspect CLI dispatch."], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          ...internalInvocationEnv,
          JRI_PI_COMMAND: fakePi,
        },
      });

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stderr).toBe("");
      expect(stdout).toContain("CLI explorer result");
      expect(stdout).toContain("artifactRef: .jri/logs/20260527T184210Z/artifacts/explorer-");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("hidden web commands print bounded JSON results", async () => {
    const dir = await tempProject();
    try {
      await activateLoop(dir, "20260527T184210Z", "planning");
      const fakeWeb = join(dir, "fake-web.sh");
      await writeFile(
        fakeWeb,
        [
          "#!/usr/bin/env bash",
          "if [ \"$1\" = \"search\" ]; then",
          "  printf '{\"retrievedAt\":\"2026-05-27T00:00:00.000Z\",\"results\":[{\"title\":\"Docs\",\"url\":\"https://example.com/docs\",\"snippet\":\"Read docs\"}]}'",
          "else",
          "  printf '{\"url\":\"https://example.com/docs\",\"fetchedAt\":\"2026-05-27T00:00:00.000Z\",\"markdown\":\"# Docs\"}'",
          "fi",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakeWeb, 0o755);
      const metadata = JSON.stringify({
        projectDir: dir,
        owner: { kind: "loop", loopId: "20260527T184210Z" },
        capability: "web",
        operation: "search",
      });

      const search = Bun.spawn(["bun", cliPath, "--run-web", "search", metadata, "docs"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: { ...process.env, ...internalInvocationEnv, JRI_PI_WEB_COMMAND: fakeWeb },
      });
      const searchStdout = await new Response(search.stdout).text();
      expect(await search.exited).toBe(0);
      expect(JSON.parse(searchStdout)[0].title).toBe("Docs");

      const fetchMetadata = JSON.stringify({
        projectDir: dir,
        owner: { kind: "loop", loopId: "20260527T184210Z" },
        capability: "web",
        operation: "fetch",
      });
      const fetch = Bun.spawn(["bun", cliPath, "--run-web", "fetch", fetchMetadata, "https://example.com/docs"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: { ...process.env, ...internalInvocationEnv, JRI_PI_WEB_COMMAND: fakeWeb },
      });
      const fetchStdout = await new Response(fetch.stdout).text();
      expect(await fetch.exited).toBe(0);
      expect(JSON.parse(fetchStdout).markdown).toBe("# Docs");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("hidden web commands reject mismatched operation metadata", async () => {
    const dir = await tempProject();
    try {
      await activateLoop(dir, "20260527T184210Z", "planning");
      const metadata = JSON.stringify({
        projectDir: dir,
        owner: { kind: "loop", loopId: "20260527T184210Z" },
        capability: "web",
        operation: "search",
      });

      const proc = Bun.spawn(["bun", cliPath, "--run-web", "fetch", metadata, "https://example.com/docs"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: { ...process.env, ...internalInvocationEnv, JRI_PI_WEB_COMMAND: "/bin/false" },
      });

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(1);
      expect(stdout).toBe("");
      expect(stderr).toContain("mismatched operation metadata");
      expect(stderr).toContain("specific operation");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("hidden capability commands reject stale loop ownership", async () => {
    const dir = await tempProject();
    try {
      await activateLoop(dir, "active-loop", "building");
      const metadata = JSON.stringify({
        projectDir: dir,
        owner: { kind: "loop", loopId: "stale-loop" },
        capability: "web",
        operation: "search",
      });
      const proc = Bun.spawn(["bun", cliPath, "--run-web", "search", metadata, "docs"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: { ...process.env, ...internalInvocationEnv, JRI_PI_WEB_COMMAND: "/bin/false" },
      });

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(1);
      expect(stdout).toBe("");
      expect(stderr).toContain("stale or mismatched loop");
      expect(stderr).toContain("currently running Ralph loop");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("hidden web command accepts chat-owned metadata and writes interrogation artifacts", async () => {
    const dir = await tempInitializedProject();
    try {
      const fakeWeb = join(dir, "fake-web-chat.sh");
      await writeFile(
        fakeWeb,
        [
          "#!/usr/bin/env bash",
          "printf '{\"url\":\"https://example.com/docs\",\"fetchedAt\":\"2026-05-27T00:00:00.000Z\",\"markdown\":\"'",
          "printf 'chat docs %.0s' {1..2000}",
          "printf '\"}'",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakeWeb, 0o755);
      const metadata = JSON.stringify({
        projectDir: dir,
        owner: { kind: "chat", turnId: "turn-1" },
        capability: "web",
        operation: "fetch",
      });

      const proc = Bun.spawn(["bun", cliPath, "--run-web", "fetch", metadata, "https://example.com/docs"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: { ...process.env, ...internalInvocationEnv, JRI_PI_WEB_COMMAND: fakeWeb },
      });

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stderr).toBe("");
      expect(JSON.parse(stdout).artifactRef).toMatch(/^\.jri\/logs\/interrogation-artifacts\/web-turn-1-/);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("hidden capability commands reject inactive loop ownership", async () => {
    const dir = await tempProject();
    try {
      const loopId = "20260527T184210Z";
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: loopId,
        lastLoopId: loopId,
      });
      const proc = Bun.spawn(["bun", cliPath, "--run-explorer", dir, loopId, "Inspect CLI dispatch."], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: { ...process.env, ...internalInvocationEnv, JRI_PI_COMMAND: "/bin/false" },
      });

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(1);
      expect(stdout).toBe("");
      expect(stderr).toContain("cannot run while the loop is stopped");
      expect(stderr).toContain("active auditing, planning, or building loop");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("loop attach rejects non-active states with next action and log path", async () => {
    const dir = await tempInitializedProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        blocker: {
          reason: "ambiguousSpecs",
          description: "Deployment target is unclear.",
          resolutionGuide: {
            summary: "Clarify deployment.",
            steps: ["Choose the deployment target."],
            resumeInstruction: "Clarify the target in bare jri, then say just ralph it.",
          },
        },
      });

      const proc = Bun.spawn(["bun", cliPath, "loop", "attach"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
      });
      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(1);
      expect(stdout).toBe("");
      expect(stderr).toContain("jri loop attach is not available while JRI is blocked");
      expect(stderr).toContain("Clarify the target in bare jri");
      expect(stderr).toContain(".jri/logs/20260527T184210Z/stdout.log");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("loop attach renders recent output and milestone events with detach and stop controls outside stdout log", async () => {
    const dir = await tempInitializedProject();
    const loopId = "20260527T184210Z";
    try {
      await mkdir(join(dir, ".jri", "logs", loopId), { recursive: true });
      await writeFile(join(dir, ".jri", "logs", loopId, "stdout.log"), "first line\nsecond line\n", "utf8");
      await appendLoopEvent(dir, {
        type: "iterationStarted",
        loopId,
        iteration: 1,
        data: { trackedTreeCleanAtStart: true },
      });
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: loopId,
        lastLoopId: loopId,
        iteration: 1,
        lock: activeTestLock("build"),
      });

      const proc = Bun.spawn(["bun", cliPath, "loop", "attach"], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
        env: isolatedDaemonEnv(dir),
      });
      setTimeout(() => {
        proc.stdin.write("sd");
        proc.stdin.end();
      }, 150);

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stdout).toContain("Attached to JRI loop 20260527T184210Z");
      expect(stdout).toContain("Recent context:");
      expect(stdout).toContain("first line");
      expect(stdout).toContain("second line");
      expect(stdout).toContain("iterationStarted");
      expect(stderr).toContain("[d]etach [s]top");
      expect(stderr).toContain("ralphing | iteration: 1 | stop: yes");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status.stopRequested).toBe(true);
      expect(await readFile(join(dir, ".jri", "logs", loopId, "stdout.log"), "utf8")).toBe("first line\nsecond line\n");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  }, 10_000);

  test("loop attach redraws the detach and stop footer after live output arrives", async () => {
    const dir = await tempInitializedProject();
    const loopId = "20260527T184210Z";
    try {
      await mkdir(join(dir, ".jri", "logs", loopId), { recursive: true });
      await writeFile(join(dir, ".jri", "logs", loopId, "stdout.log"), "before\n", "utf8");
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: loopId,
        lastLoopId: loopId,
        iteration: 1,
        lock: activeTestLock("build"),
      });

      const proc = Bun.spawn(["bun", cliPath, "loop", "attach"], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
        env: isolatedDaemonEnv(dir),
      });

      setTimeout(async () => {
        await appendFile(join(dir, ".jri", "logs", loopId, "stdout.log"), "after\n", "utf8");
      }, 50);
      setTimeout(() => {
        proc.stdin.write("d");
        proc.stdin.end();
      }, 150);

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stdout).toContain("before");
      expect(stdout).toContain("after");
      expect(stderr.match(/\[d\]etach \[s\]top/g)?.length).toBeGreaterThanOrEqual(2);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  }, 10_000);

  test("loop stop rejects stopped state before mutating stop request", async () => {
    const dir = await tempInitializedProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
      });

      const proc = Bun.spawn(["bun", cliPath, "loop", "stop"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
      });
      const [exitCode, stderr] = await Promise.all([proc.exited, new Response(proc.stderr).text()]);

      expect(exitCode).toBe(1);
      expect(stderr).toContain("jri loop stop is not available because the loop is stopped");
      expect(stderr).toContain("jri loop resume");
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status.stopRequested).toBe(false);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("loop halt is idempotent for halted state and does not prompt", async () => {
    const dir = await tempInitializedProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "halted",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
      });

      const proc = Bun.spawn(["bun", cliPath, "loop", "halt"], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
      });
      proc.stdin.end();
      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stdout).toContain("JRI is already halted.");
      expect(stdout).toContain(".jri/logs/20260527T184210Z/stdout.log");
      expect(stderr).not.toContain("Force halt");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("loop halt reads separate confirmations for force halt and rollback reset", async () => {
    const dir = await tempInitializedProject();
    const loopId = "20260527T184210Z";
    try {
      await mkdir(join(dir, ".jri", "logs", loopId), { recursive: true });
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: loopId,
        lastLoopId: loopId,
        lock: activeTestLock("build"),
        currentIteration: {
          iteration: 1,
          rollbackCommit: "abc123",
          trackedTreeCleanAtStart: true,
        },
      });

      const proc = Bun.spawn(["bun", cliPath, "loop", "halt"], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
        env: isolatedDaemonEnv(dir),
      });
      proc.stdin.write("y\ny\n");
      proc.stdin.end();

      const [exitCode, stderr] = await Promise.all([proc.exited, new Response(proc.stderr).text()]);
      const events = (await readFile(join(dir, ".jri", "logs", loopId, "events.jsonl"), "utf8"))
        .trim()
        .split("\n")
        .map((line) => JSON.parse(line));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(exitCode).toBe(0);
      expect(stderr).toContain("Force halt the active JRI loop?");
      expect(stderr).toContain("Reset tracked files with git reset --hard abc123?");
      expect(events.at(-1)).toMatchObject({
        type: "loopHalted",
        data: { resetOffered: true, resetAccepted: true, rollbackCommit: "abc123" },
      });
      expect(status.state).toBe("halted");
      expect(status.lastResult.summary).toContain("Rollback reset failed");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("loop resume rejects idle state with bare jri recovery", async () => {
    const dir = await tempInitializedProject();
    try {
      const proc = Bun.spawn(["bun", cliPath, "loop", "resume"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
      });
      const [exitCode, stderr] = await Promise.all([proc.exited, new Response(proc.stderr).text()]);

      expect(exitCode).toBe(1);
      expect(stderr).toContain("jri loop resume is not available because no Ralph loop is running");
      expect(stderr).toContain("Use bare jri");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});

function isolatedDaemonEnv(dir: string, overrides: Record<string, string | undefined> = {}): Record<string, string | undefined> {
  const env: Record<string, string | undefined> = { ...process.env, ...overrides };
  for (const key of daemonEnvKeys) delete env[key];
  env.JRI_DAEMON_RUNTIME_DIR = join(dir, "daemon-runtime");
  env.JRI_DAEMON_STATE_DIR = join(dir, "daemon-state");
  env.JRI_DAEMON_SOCKET_PATH =
    process.platform === "win32" ? `\\\\.\\pipe\\jri-cli-test-${crypto.randomUUID()}` : join(dir, "daemon-runtime", "daemon.sock");
  env.JRI_DAEMON_REGISTRY_PATH = join(dir, "daemon-state", "daemon-registry.json");
  return env;
}

function daemonPathsForEnv(env: Record<string, string | undefined>): DaemonPaths {
  return {
    runtimeDir: env.JRI_DAEMON_RUNTIME_DIR!,
    stateDir: env.JRI_DAEMON_STATE_DIR!,
    socketPath: env.JRI_DAEMON_SOCKET_PATH!,
    registryPath: env.JRI_DAEMON_REGISTRY_PATH!,
  };
}

function scheduleLoopCompletion(projectDir: string, loopId: string): void {
  setTimeout(() => {
    void (async () => {
      await appendLoopEvent(projectDir, {
        type: "loopFinished",
        loopId,
        data: { outcome: "completed", summary: "Fake loop completed." },
      });
      await updateStatus(projectDir, (current) => {
        const { process, lock, ...withoutOwnership } = current;
        void process;
        void lock;
        return {
          ...withoutOwnership,
          state: "idle",
          activeLoopId: null,
          lastLoopId: loopId,
          finishedAt: "2026-05-27T20:00:01.000Z",
          stopRequested: false,
          lastResult: { outcome: "completed", summary: "Fake loop completed." },
        };
      });
    })();
  }, 25);
}

async function gitOutput(cwd: string, args: string[]): Promise<string> {
  const proc = Bun.spawn(["git", ...args], {
    cwd,
    stdout: "pipe",
    stderr: "pipe",
    stdin: "ignore",
    env: gitTestEnv(),
  });
  const [stdout, stderr] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text()]);
  const exitCode = await proc.exited;
  if (exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${stderr}`);
  return stdout;
}

async function git(cwd: string, args: string[]): Promise<void> {
  const proc = Bun.spawn(["git", ...args], {
    cwd,
    stdout: "ignore",
    stderr: "pipe",
    stdin: "ignore",
    env: gitTestEnv(),
  });
  const stderr = await new Response(proc.stderr).text();
  const exitCode = await proc.exited;
  if (exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${stderr}`);
}

function gitTestEnv(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    GIT_AUTHOR_NAME: process.env.GIT_AUTHOR_NAME ?? "JRI Test",
    GIT_AUTHOR_EMAIL: process.env.GIT_AUTHOR_EMAIL ?? "jri-test@example.com",
    GIT_COMMITTER_NAME: process.env.GIT_COMMITTER_NAME ?? process.env.GIT_AUTHOR_NAME ?? "JRI Test",
    GIT_COMMITTER_EMAIL: process.env.GIT_COMMITTER_EMAIL ?? process.env.GIT_AUTHOR_EMAIL ?? "jri-test@example.com",
  };
}
