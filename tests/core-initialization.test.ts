import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { describe, expect, test } from "bun:test";
import { JriError, open, resolveProjectRoot } from "../src/core";

async function tempProject(): Promise<string> {
  return await mkdtemp(join(tmpdir(), "jri-test-"));
}

describe("project initialization", () => {
  test("open binds an uninitialized directory without mutating it", async () => {
    const dir = await tempProject();
    try {
      const project = await open(dir);
      expect(project.projectDir).toBe(dir);
      expect(await Bun.file(join(dir, ".jri")).exists()).toBe(false);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("ensureInitialized creates only the durable scaffold, not the implementation plan", async () => {
    const dir = await tempProject();
    try {
      const project = await open(dir);
      await project.lifecycle.ensureInitialized();

      expect(await pathExists(join(dir, ".git"))).toBe(true);
      expect(await pathExists(join(dir, ".jri", "config.json"))).toBe(true);
      expect(await pathExists(join(dir, ".jri", "status.json"))).toBe(true);
      expect(await pathExists(join(dir, ".jri", "specs"))).toBe(true);
      expect(await pathExists(join(dir, ".jri", "logs", "interrogation.jsonl"))).toBe(true);
      expect(await pathExists(join(dir, ".jri", "scratchpad.md"))).toBe(true);
      expect(await pathExists(join(dir, "AGENTS.md"))).toBe(true);
      expect(await Bun.file(join(dir, ".jri", "IMPLEMENTATION_PLAN.md")).exists()).toBe(false);

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status.projectDir).toBe(dir);
      expect(status.state).toBe("idle");
      expect(status.activeLoopId).toBeNull();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("ensureInitialized does not overwrite existing durable files", async () => {
    const dir = await tempProject();
    try {
      await writeFile(join(dir, "AGENTS.md"), "custom agents\n");
      const project = await open(dir);
      await project.lifecycle.ensureInitialized();
      await writeFile(join(dir, ".jri", "scratchpad.md"), "custom notes\n");

      await project.lifecycle.ensureInitialized();

      expect(await readFile(join(dir, "AGENTS.md"), "utf8")).toBe("custom agents\n");
      expect(await readFile(join(dir, ".jri", "scratchpad.md"), "utf8")).toBe("custom notes\n");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("open rejects malformed existing config with recovery guidance", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri"), { recursive: true });
      await writeFile(join(dir, ".jri", "config.json"), "{ nope", "utf8");

      await expect(open(dir)).rejects.toThrow(JriError);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("root resolution prefers nearest .jri ancestor before git root", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri"), { recursive: true });
      await mkdir(join(dir, "nested", ".jri"), { recursive: true });
      await mkdir(join(dir, "nested", "child"), { recursive: true });
      const proc = Bun.spawn(["git", "init"], { cwd: dir, stdout: "ignore", stderr: "ignore" });
      expect(await proc.exited).toBe(0);

      const resolved = await resolveProjectRoot(join(dir, "nested", "child"));
      expect(resolved.root).toBe(join(dir, "nested"));
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});

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
