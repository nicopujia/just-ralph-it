import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import {
  explorerCapabilityDescriptor,
  renderExplorerCapabilityInstructions,
  renderWebCapabilityInstructions,
  webCapabilityDescriptor,
} from "../src/core/capabilities";
import { buildPiPrompt } from "../src/core/prompts";

async function tempProject(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "jri-capabilities-test-"));
  await Bun.$`mkdir -p ${join(dir, ".jri", "specs")}`.quiet();
  await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nUse current docs when needed.\n", "utf8");
  return dir;
}

describe("capability descriptors", () => {
  test("web descriptor renders concrete hidden CLI commands only with a loop id", () => {
    expect(renderWebCapabilityInstructions("/tmp/project", undefined)).toBe("");

    const instructions = renderWebCapabilityInstructions("/tmp/project", { kind: "loop", loopId: "20260527T184210Z" });
    expect(instructions).toContain("jri --run-web search");
    expect(instructions).toContain("jri --run-web fetch");
    expect(instructions).toContain(String(webCapabilityDescriptor.limits.searchResults));
    expect(instructions).toContain(String(webCapabilityDescriptor.limits.fetchMarkdownChars));
    expect(instructions).toContain('\\"owner\\":{\\"kind\\":\\"loop\\",\\"loopId\\":\\"20260527T184210Z\\"}');
  });

  test("web descriptor supports chat-owned interrogation artifacts", () => {
    const instructions = renderWebCapabilityInstructions("/tmp/project", { kind: "chat", turnId: "turn-1" });
    expect(instructions).toContain('\\"owner\\":{\\"kind\\":\\"chat\\",\\"turnId\\":\\"turn-1\\"}');
    expect(instructions).toContain(".jri/logs/interrogation-artifacts/");
  });

  test("explorer descriptor renders concrete hidden CLI commands only with a loop id", () => {
    expect(renderExplorerCapabilityInstructions("/tmp/project", undefined)).toBe("");

    const instructions = renderExplorerCapabilityInstructions("/tmp/project", "20260527T184210Z");
    expect(instructions).toContain("jri --run-explorer");
    expect(instructions).toContain(String(explorerCapabilityDescriptor.limits.concurrency));
    expect(instructions).toContain(String(explorerCapabilityDescriptor.limits.timeoutMs / 60_000));
    expect(instructions).toContain(String(explorerCapabilityDescriptor.limits.handoffChars));
    expect(instructions).toContain("spawn/fresh");
    expect(instructions).toContain("Do not call pi-subagent");
  });

  test("planner and builder prompts include concrete capability instructions", async () => {
    const dir = await tempProject();
    try {
      const planner = await buildPiPrompt(dir, "planning", { loopId: "20260527T184210Z" });
      const builder = await buildPiPrompt(dir, "building", { loopId: "20260527T184210Z" });

      expect(planner).toContain("jri --run-web search");
      expect(builder).toContain("jri --run-web fetch");
      expect(planner).toContain("jri --run-explorer");
      expect(builder).toContain("jri --run-explorer");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("explorer prompt includes loop-owned web capability instructions", async () => {
    const dir = await tempProject();
    try {
      const prompt = await buildPiPrompt(dir, "explorer", {
        loopId: "20260527T184210Z",
        explorerTask: "Check current framework docs before reporting findings.",
      });

      expect(prompt).toContain("jri --run-web search");
      expect(prompt).toContain("jri --run-web fetch");
      expect(prompt).toContain('\\"owner\\":{\\"kind\\":\\"loop\\",\\"loopId\\":\\"20260527T184210Z\\"}');
      expect(prompt).toContain("Check current framework docs before reporting findings.");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("interrogator prompt includes chat-owned web capability instructions", async () => {
    const dir = await tempProject();
    try {
      const prompt = await buildPiPrompt(dir, "interrogation", {
        owner: { kind: "chat", turnId: "turn-1" },
        userMessage: "Which docs are current?",
      });

      expect(prompt).toContain("jri --run-web search");
      expect(prompt).toContain('\\"owner\\":{\\"kind\\":\\"chat\\",\\"turnId\\":\\"turn-1\\"}');
      expect(prompt).toContain(".jri/logs/interrogation-artifacts/");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
