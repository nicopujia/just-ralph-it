import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import {
  checkInterrogationStartGate,
  fingerprintSpecFile,
  readInterrogationState,
  writeInterrogationState,
} from "../src/core/interrogation-state";

async function tempProject(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "jri-interrogation-state-test-"));
  await mkdir(join(dir, ".jri", "specs"), { recursive: true });
  return dir;
}

describe("interrogation state", () => {
  test("is generated lazily and absent before interrogation state exists", async () => {
    const dir = await tempProject();
    try {
      await expect(readInterrogationState(dir)).resolves.toBeNull();
      await expect(checkInterrogationStartGate(dir)).resolves.toMatchObject({ ok: true, state: null });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("detects manual edits to sealed spec topics before start", async () => {
    const dir = await tempProject();
    try {
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

      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI and a TUI.\n");
      const result = await checkInterrogationStartGate(dir, { now: new Date("2026-05-27T20:00:00.000Z") });
      const persisted = JSON.parse(await readFile(join(dir, ".jri", "interrogation-state.json"), "utf8"));

      expect(result).toMatchObject({
        ok: false,
        pending: [
          {
            topicId: "app",
            topic: {
              status: "open",
              pendingReconciliation: {
                reason: "manualSpecEdit",
                detectedAt: "2026-05-27T20:00:00.000Z",
              },
            },
          },
        ],
      });
      expect(persisted.topics.app).toMatchObject({
        status: "open",
        pendingReconciliation: { reason: "manualSpecEdit" },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
