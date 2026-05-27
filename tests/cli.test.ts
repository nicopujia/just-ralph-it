import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "bun:test";
import { defaultStatus } from "../src/core/schema";
import { appendLoopEvent, writeStatusAtomic } from "../src/core/runtime-state";
import { fingerprintSpecFile, writeInterrogationState } from "../src/core/interrogation-state";

const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const cliPath = join(repoRoot, "src", "cli", "index.ts");
const daemonEnvKeys = ["JRI_DAEMON_RUNTIME_DIR", "JRI_DAEMON_STATE_DIR", "JRI_DAEMON_SOCKET_PATH", "JRI_DAEMON_REGISTRY_PATH"];

async function tempProject(): Promise<string> {
  return await mkdtemp(join(tmpdir(), "jri-cli-test-"));
}

async function tempInitializedProject(): Promise<string> {
  const dir = await tempProject();
  await mkdir(join(dir, ".jri", "logs"), { recursive: true });
  await writeStatusAtomic(dir, defaultStatus(dir));
  return dir;
}

describe("CLI", () => {
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

  test("interactive bare jri blocks with auth recovery guidance when auth is missing", async () => {
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
        },
      });

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(1);
      expect(`${stdout}\n${stderr}`).toContain("OpenAI authentication is required");
      expect(`${stdout}\n${stderr}`).toContain("jri auth login");
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
      }, 250);

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
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(fakePi, "#!/usr/bin/env bash\nprintf 'CLI explorer result\\n'\n", "utf8");
      await chmod(fakePi, 0o755);

      const proc = Bun.spawn(["bun", cliPath, "--run-explorer", dir, "20260527T184210Z", "Inspect CLI dispatch."], {
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

      const search = Bun.spawn(["bun", cliPath, "--run-web", "search", dir, "20260527T184210Z", "docs"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: { ...process.env, JRI_PI_WEB_COMMAND: fakeWeb },
      });
      const searchStdout = await new Response(search.stdout).text();
      expect(await search.exited).toBe(0);
      expect(JSON.parse(searchStdout)[0].title).toBe("Docs");

      const fetch = Bun.spawn(["bun", cliPath, "--run-web", "fetch", dir, "20260527T184210Z", "https://example.com/docs"], {
        cwd: dir,
        stdout: "pipe",
        stderr: "pipe",
        env: { ...process.env, JRI_PI_WEB_COMMAND: fakeWeb },
      });
      const fetchStdout = await new Response(fetch.stdout).text();
      expect(await fetch.exited).toBe(0);
      expect(JSON.parse(fetchStdout).markdown).toBe("# Docs");
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
  });

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
