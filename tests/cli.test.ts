import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "bun:test";
import { defaultStatus } from "../src/core/schema";
import { writeStatusAtomic } from "../src/core/runtime-state";

const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const cliPath = join(repoRoot, "src", "cli", "index.ts");

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
      const proc = Bun.spawn(["bun", cliPath], {
        cwd: dir,
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
      });
      proc.stdin.write("Need a deployment workflow.\n");
      proc.stdin.end();

      const [exitCode, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);

      expect(exitCode).toBe(0);
      expect(stderr).toBe("");
      expect(stdout).toContain("I recorded your note.");

      const log = await readFile(join(dir, ".jri", "logs", "interrogation.jsonl"), "utf8");
      expect(log).toContain("Need a deployment workflow.");
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
