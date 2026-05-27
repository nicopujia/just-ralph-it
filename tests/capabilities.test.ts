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

    const instructions = renderWebCapabilityInstructions("/tmp/project", "20260527T184210Z");
    expect(instructions).toContain("jri --run-web search");
    expect(instructions).toContain("jri --run-web fetch");
    expect(instructions).toContain(String(webCapabilityDescriptor.limits.searchResults));
    expect(instructions).toContain(String(webCapabilityDescriptor.limits.fetchMarkdownChars));
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
});
