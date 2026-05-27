import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import { buildControlledPiCommand } from "../src/core/harness";
import { writeStatusAtomic } from "../src/core/runtime-state";
import { defaultConfig, defaultStatus } from "../src/core/schema";

async function tempProject(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "jri-harness-test-"));
  await mkdir(join(dir, ".jri", "logs"), { recursive: true });
  await mkdir(join(dir, ".jri", "specs"), { recursive: true });
  await writeFile(join(dir, ".jri", "config.json"), `${JSON.stringify(defaultConfig, null, 2)}\n`, "utf8");
  await writeStatusAtomic(dir, defaultStatus(dir));
  return dir;
}

describe("controlled Pi harness", () => {
  test("builds an isolated auditing command with read-only tools and session directory", async () => {
    const dir = await tempProject();
    try {
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild it.\n", "utf8");

      const built = await buildControlledPiCommand({
        projectDir: dir,
        loopId: "20260527T184210Z",
        phase: "auditing",
        env: {
          JRI_PI_COMMAND: "/tmp/fake-pi",
        },
      });

      expect(built.command.slice(0, 10)).toEqual([
        "/tmp/fake-pi",
        "--provider",
        "openai",
        "--model",
        "gpt-5.4",
        "--thinking",
        "xhigh",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
      ]);
      expect(built.command).toContain("--no-themes");
      expect(built.command).toContain("--no-context-files");
      expect(built.command).toContain("--print");
      expect(built.command[built.command.indexOf("--tools") + 1]).toBe("read,grep,find,ls");
      expect(built.env.PI_CODING_AGENT_SESSION_DIR).toBe(join(dir, ".jri", "logs", "20260527T184210Z", "pi-sessions"));
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("uses agent model overrides for the current phase", async () => {
    const dir = await tempProject();
    try {
      await writeFile(
        join(dir, ".jri", "config.json"),
        `${JSON.stringify(
          {
            ...defaultConfig,
            agents: {
              builder: {
                model: "custom-builder",
                reasoning: "high",
              },
            },
          },
          null,
          2,
        )}\n`,
        "utf8",
      );

      const built = await buildControlledPiCommand({
        projectDir: dir,
        loopId: "20260527T184210Z",
        phase: "building",
        env: {
          JRI_PI_COMMAND: "/tmp/fake-pi",
        },
      });

      expect(built.command[built.command.indexOf("--model") + 1]).toBe("custom-builder");
      expect(built.command[built.command.indexOf("--thinking") + 1]).toBe("high");
      expect(built.command[built.command.indexOf("--tools") + 1]).toBe("read,bash,edit,write,grep,find,ls");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("requires provider auth for the real Pi command", async () => {
    const dir = await tempProject();
    try {
      await expect(
        buildControlledPiCommand({
          projectDir: dir,
          loopId: "20260527T184210Z",
          phase: "building",
          env: {
            PI_CODING_AGENT_DIR: join(dir, "missing-pi-auth"),
          },
        }),
      ).rejects.toThrow("OpenAI authentication is required");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
