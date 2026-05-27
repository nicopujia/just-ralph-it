import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import { buildControlledPiCommand, invokeDefaultHarness, runExplorerTask } from "../src/core/harness";
import { writeStatusAtomic } from "../src/core/runtime-state";
import { defaultConfig, defaultStatus } from "../src/core/schema";
import { runWebFetch, runWebSearch } from "../src/core/web-capability";

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

  test("builds an isolated interrogation command with the interrogator model and prompt", async () => {
    const dir = await tempProject();
    try {
      await writeFile(join(dir, ".jri", "scratchpad.md"), "Selected scratchpad note.\n", "utf8");
      await writeFile(join(dir, ".jri", "logs", "interrogation.jsonl"), "old full transcript\n", "utf8");
      const built = await buildControlledPiCommand({
        projectDir: dir,
        loopId: "chat-turn-1",
        phase: "interrogation",
        contextRefs: [".jri/scratchpad.md"],
        contextInline: ["We need a deployment workflow.", "Recent unsealed interrogation turns:\nuser: selected recent turn"],
        userMessage: "We need a deployment workflow.",
        env: {
          JRI_PI_COMMAND: "/tmp/fake-pi",
        },
      });

      expect(built.command[built.command.indexOf("--model") + 1]).toBe("gpt-5.5");
      expect(built.command[built.command.indexOf("--thinking") + 1]).toBe("xhigh");
      expect(built.command[built.command.indexOf("--tools") + 1]).toBe("read,write,edit,grep,find,ls");
      expect(built.command.at(-1)).toContain("You are the JRI interrogator");
      expect(built.command.at(-1)).toContain("# .jri/scratchpad.md\n\nSelected scratchpad note.");
      expect(built.command.at(-1)).toContain("Recent unsealed interrogation turns:\nuser: selected recent turn");
      expect(built.command.at(-1)).toContain("Current user message:\nWe need a deployment workflow.");
      expect(built.command.at(-1)).not.toContain("old full transcript");
      expect(built.env.PI_CODING_AGENT_SESSION_DIR).toBe(join(dir, ".jri", "logs", "chat-turn-1", "pi-sessions"));
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("default interrogator harness keeps the current message separate from recent context", async () => {
    const dir = await tempProject();
    try {
      const capturedPromptPath = join(dir, "captured-prompt.txt");
      const fakePi = join(dir, "fake-pi-capture.sh");
      await writeFile(
        fakePi,
        [
          "#!/usr/bin/env bash",
          "prompt=\"${@: -1}\"",
          `printf '%s' "$prompt" > ${JSON.stringify(capturedPromptPath)}`,
          "printf 'Assistant answer.\\n'",
          "printf 'JRI_HANDOFF_JSON: {\"agent\":\"interrogator\",\"action\":\"messageOnly\",\"summary\":\"Answered.\"}\\n'",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);

      const chunks: string[] = [];
      await invokeDefaultHarness(
        {
          owner: { kind: "chat", turnId: "turn-1" },
          projectDir: dir,
          agent: "interrogator",
          phase: "interrogation",
          model: { model: "gpt-5.5", reasoning: "xhigh" },
          context: {
            refs: [],
            inline: ["Current trimmed message.", "Recent unsealed interrogation turns:\nuser: older context"],
          },
          capabilities: [],
          output: {
            write: (chunk) => {
              chunks.push(chunk);
            },
          },
          signal: new AbortController().signal,
        },
        {
          JRI_PI_COMMAND: fakePi,
        },
      );

      const prompt = await readFile(capturedPromptPath, "utf8");
      expect(prompt).toContain("Recent unsealed interrogation turns:\nuser: older context");
      expect(prompt).toContain("Current user message:\nCurrent trimmed message.");
      expect(prompt).not.toContain("Current user message:\nRecent unsealed interrogation turns");
      expect(chunks.join("")).toContain("Assistant answer.");
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

  test("builds an isolated read-only pi-subagent explorer command with the explorer model", async () => {
    const dir = await tempProject();
    try {
      const built = await buildControlledPiCommand({
        projectDir: dir,
        loopId: "20260527T184210Z",
        phase: "explorer",
        explorerTask: "Find the CLI dispatch code.",
        env: {
          JRI_PI_COMMAND: "/tmp/fake-pi",
        },
      });

      expect(built.command[built.command.indexOf("--model") + 1]).toBe("gpt-5.3-codex-spark");
      expect(built.command[built.command.indexOf("--thinking") + 1]).toBe("xhigh");
      expect(built.command).toContain("--no-extensions");
      expect(built.command[built.command.indexOf("--extension") + 1]).toBe("npm:pi-subagent");
      expect(built.command[built.command.indexOf("--tools") + 1]).toBe("read,grep,find,ls");
      expect(built.command[built.command.indexOf("--session-dir") + 1]).toBe(
        join(dir, ".jri", "logs", "20260527T184210Z", "pi-sessions"),
      );
      expect(built.env.PI_CODING_AGENT_DIR).toBe(join(dir, ".jri", "logs", "20260527T184210Z", "capabilities", "explorer"));
      expect(built.command.at(-1)).toContain('/run explorer "Find the CLI dispatch code."');
      const descriptor = await readFile(join(dir, ".jri", "logs", "20260527T184210Z", "capabilities", "explorer", "agents", "explorer.md"), "utf8");
      expect(descriptor).toContain("name: explorer");
      expect(descriptor).toContain("inheritProjectContext: false");
      expect(descriptor).toContain("tools:\n  - read\n  - grep\n  - find\n  - ls");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runExplorerTask records subagent events and caps handoff with an artifact", async () => {
    const dir = await tempProject();
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        ["#!/usr/bin/env bash", "printf 'finding %.0s' {1..700}", "printf '\\nfinal line\\n'"].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);

      const result = await runExplorerTask({
        projectDir: dir,
        loopId: "20260527T184210Z",
        task: "Inspect CLI behavior.",
        handoffLimit: 120,
        env: {
          JRI_PI_COMMAND: fakePi,
        },
      });

      expect(result.summary.length).toBeLessThanOrEqual(160);
      expect(result.summary).toContain("Explorer output truncated");
      expect(result.artifactRef).toMatch(/^\.jri\/logs\/20260527T184210Z\/artifacts\/explorer-/);
      const artifact = await readFile(join(dir, result.artifactRef!), "utf8");
      expect(artifact).toContain("Task: Inspect CLI behavior.");
      expect(artifact).toContain("final line");

      const events = await readFile(join(dir, ".jri", "logs", "20260527T184210Z", "events.jsonl"), "utf8");
      expect(events).toContain('"type":"subagentStarted"');
      expect(events).toContain('"type":"subagentFinished"');
      expect(events).toContain('"artifactRef"');
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runWebSearch wraps pi-web-access with bounded timestamped results", async () => {
    const dir = await tempProject();
    try {
      const fakeWeb = join(dir, "fake-web.sh");
      await writeFile(
        fakeWeb,
        [
          "#!/usr/bin/env bash",
          "printf '{\"retrievedAt\":\"2026-05-27T00:00:00.000Z\",\"results\":['",
          "for i in 1 2 3 4 5 6; do",
          "  if [ \"$i\" != \"1\" ]; then printf ','; fi",
          "  printf '{\"title\":\"Result %s\",\"url\":\"https://example.com/%s\",\"snippet\":\"Snippet %s\"}' \"$i\" \"$i\" \"$i\"",
          "done",
          "printf ']}'",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakeWeb, 0o755);

      const results = await runWebSearch({
        projectDir: dir,
        owner: { kind: "loop", loopId: "20260527T184210Z" },
        query: "current docs",
        limit: 99,
        env: {
          JRI_PI_WEB_COMMAND: fakeWeb,
        },
      });

      expect(results).toHaveLength(5);
      expect(results[0]).toEqual({
        title: "Result 1",
        url: "https://example.com/1",
        snippet: "Snippet 1",
        retrievedAt: "2026-05-27T00:00:00.000Z",
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runWebFetch caps markdown and stores omitted content as an artifact", async () => {
    const dir = await tempProject();
    try {
      const fakeWeb = join(dir, "fake-web-fetch.sh");
      await writeFile(
        fakeWeb,
        [
          "#!/usr/bin/env bash",
          "printf '{\"url\":\"https://example.com/docs\",\"title\":\"Docs\",\"fetchedAt\":\"2026-05-27T00:00:00.000Z\",\"markdown\":\"'",
          "printf 'section %.0s' {1..3000}",
          "printf '\"}'",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakeWeb, 0o755);

      const result = await runWebFetch({
        projectDir: dir,
        owner: { kind: "loop", loopId: "20260527T184210Z" },
        url: "https://example.com/docs",
        env: {
          JRI_PI_WEB_COMMAND: fakeWeb,
        },
      });

      expect(result.url).toBe("https://example.com/docs");
      expect(result.title).toBe("Docs");
      expect(result.markdown.length).toBeLessThanOrEqual(12_000);
      expect(result.artifactRef).toMatch(/^\.jri\/logs\/20260527T184210Z\/artifacts\/web-/);
      expect(result.omittedBytes).toBeGreaterThan(0);
      const artifact = await readFile(join(dir, result.artifactRef!), "utf8");
      expect(artifact).toContain("Source: https://example.com/docs");
      expect(artifact).toContain("section");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runWebFetch stores chat-owned artifacts under interrogation artifacts", async () => {
    const dir = await tempProject();
    try {
      const fakeWeb = join(dir, "fake-web-chat-fetch.sh");
      await writeFile(
        fakeWeb,
        [
          "#!/usr/bin/env bash",
          "printf '{\"url\":\"https://example.com/chat-docs\",\"fetchedAt\":\"2026-05-27T00:00:00.000Z\",\"markdown\":\"'",
          "printf 'chat section %.0s' {1..2000}",
          "printf '\"}'",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakeWeb, 0o755);

      const result = await runWebFetch({
        projectDir: dir,
        owner: { kind: "chat", turnId: "turn-1" },
        url: "https://example.com/chat-docs",
        env: {
          JRI_PI_WEB_COMMAND: fakeWeb,
        },
      });

      expect(result.artifactRef).toMatch(/^\.jri\/logs\/interrogation-artifacts\/web-turn-1-/);
      const artifact = await readFile(join(dir, result.artifactRef!), "utf8");
      expect(artifact).toContain("Source: https://example.com/chat-docs");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runWebFetch truncates unicode cleanly and reports exact omitted bytes", async () => {
    const dir = await tempProject();
    try {
      const fakeWeb = join(dir, "fake-web-unicode.ts");
      await writeFile(
        fakeWeb,
        [
          `#!${process.execPath}`,
          "const markdown = 'intro ' + '😀'.repeat(13000) + ' done';",
          "process.stdout.write(JSON.stringify({ url: 'https://example.com/unicode', fetchedAt: '2026-05-27T00:00:00.000Z', markdown }));",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakeWeb, 0o755);

      const result = await runWebFetch({
        projectDir: dir,
        owner: { kind: "loop", loopId: "20260527T184210Z" },
        url: "https://example.com/unicode",
        env: {
          JRI_PI_WEB_COMMAND: fakeWeb,
        },
      });

      const original = "intro " + "😀".repeat(13000) + " done";
      expect(result.markdown).not.toContain("\uFFFD");
      expect(result.markdown.endsWith("😀")).toBe(true);
      expect(result.omittedBytes).toBe(new TextEncoder().encode(original).length - new TextEncoder().encode(result.markdown).length);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runWebFetch enforces process timeout and reports actionable cleanup", async () => {
    const dir = await tempProject();
    try {
      const fakeWeb = join(dir, "fake-web-timeout.ts");
      await writeFile(
        fakeWeb,
        [
          `#!${process.execPath}`,
          "process.on('SIGTERM', () => {});",
          "await new Promise((resolve) => setTimeout(resolve, 5000));",
          "process.stdout.write(JSON.stringify({ markdown: 'late' }));",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakeWeb, 0o755);

      await expect(
        runWebFetch({
          projectDir: dir,
          owner: { kind: "loop", loopId: "20260527T184210Z" },
          url: "https://example.com/docs",
          timeoutMs: 1_000,
          env: {
            JRI_PI_WEB_COMMAND: fakeWeb,
          },
        }),
      ).rejects.toThrow("JRI web capability timed out after 1000ms");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runWebFetch reports actionable capability errors", async () => {
    const dir = await tempProject();
    try {
      const fakeWeb = join(dir, "fake-web-fail.sh");
      await writeFile(fakeWeb, "#!/usr/bin/env bash\nprintf 'missing web package' >&2\nexit 9\n", "utf8");
      await chmod(fakeWeb, 0o755);

      await expect(
        runWebFetch({
          projectDir: dir,
          owner: { kind: "loop", loopId: "20260527T184210Z" },
          url: "https://example.com/docs",
          env: {
            JRI_PI_WEB_COMMAND: fakeWeb,
          },
        }),
      ).rejects.toThrow("JRI web capability failed with exit code 9");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
