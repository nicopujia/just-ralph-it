import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "bun:test";

const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const cliPath = join(repoRoot, "src", "cli", "index.ts");

async function tempProject(): Promise<string> {
  return await mkdtemp(join(tmpdir(), "jri-cli-test-"));
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
});
