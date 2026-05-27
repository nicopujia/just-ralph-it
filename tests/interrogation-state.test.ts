import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import {
  checkInterrogationStartGate,
  fingerprintSpecFile,
  recordInterrogatorSpecUpdate,
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

  test("detects manual edits to open spec topics before start", async () => {
    const dir = await tempProject();
    try {
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI.\n");
      await recordInterrogatorSpecUpdate(dir, [".jri/specs/app.md"]);

      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI and a web UI.\n");
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

  test("detects deleted open spec topics before start", async () => {
    const dir = await tempProject();
    try {
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI.\n");
      await recordInterrogatorSpecUpdate(dir, [".jri/specs/app.md"]);
      await rm(join(dir, ".jri", "specs", "app.md"));

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
                reason: "specFileDeleted",
                detectedAt: "2026-05-27T20:00:00.000Z",
              },
            },
          },
        ],
      });
      expect(persisted.topics.app).toMatchObject({
        status: "open",
        pendingReconciliation: { reason: "specFileDeleted" },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("detects sealed spec topics missing reconciled fingerprints before start", async () => {
    const dir = await tempProject();
    try {
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI.\n");
      await writeInterrogationState(dir, {
        schemaVersion: 1,
        topics: {
          app: {
            specFile: ".jri/specs/app.md",
            status: "sealed",
          },
        },
      });

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

  test("detects added spec files before start", async () => {
    const dir = await tempProject();
    try {
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI.\n");
      await recordInterrogatorSpecUpdate(dir, [".jri/specs/app.md"], {
        sealedSpecFiles: [".jri/specs/app.md"],
      });

      await writeFile(join(dir, ".jri", "specs", "billing.md"), "# Billing\n\nAdd paid plans.\n");
      const result = await checkInterrogationStartGate(dir, { now: new Date("2026-05-27T20:00:00.000Z") });
      const persisted = JSON.parse(await readFile(join(dir, ".jri", "interrogation-state.json"), "utf8"));

      expect(result).toMatchObject({
        ok: false,
        pending: [
          {
            topicId: "billing",
            topic: {
              specFile: ".jri/specs/billing.md",
              status: "open",
              pendingReconciliation: {
                reason: "specFileAdded",
                detectedAt: "2026-05-27T20:00:00.000Z",
              },
            },
          },
        ],
      });
      expect(persisted.topics.billing).toMatchObject({
        specFile: ".jri/specs/billing.md",
        status: "open",
        pendingReconciliation: { reason: "specFileAdded" },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("records interrogator spec updates as reconciled topic fingerprints", async () => {
    const dir = await tempProject();
    try {
      await writeFile(join(dir, ".jri", "specs", "deployment.md"), "# Deployment\n\nDeploy with Wrangler.\n");

      const state = await recordInterrogatorSpecUpdate(dir, [".jri/specs/deployment.md"]);
      const fingerprint = await fingerprintSpecFile(dir, ".jri/specs/deployment.md");
      const persisted = await readInterrogationState(dir);

      expect(state.topics.deployment).toMatchObject({
        specFile: ".jri/specs/deployment.md",
        status: "open",
        lastReconciledSpecFingerprint: fingerprint,
      });
      expect(persisted?.topics.deployment).toEqual(state.topics.deployment);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("records sealed spec topics with reconciled fingerprints", async () => {
    const dir = await tempProject();
    try {
      await writeFile(join(dir, ".jri", "specs", "deployment.md"), "# Deployment\n\nDeploy with Wrangler.\n");

      const state = await recordInterrogatorSpecUpdate(dir, [".jri/specs/deployment.md"], {
        sealedSpecFiles: [".jri/specs/deployment.md"],
      });

      expect(state.topics.deployment).toMatchObject({
        specFile: ".jri/specs/deployment.md",
        status: "sealed",
        lastReconciledSpecFingerprint: await fingerprintSpecFile(dir, ".jri/specs/deployment.md"),
      });

      await writeFile(join(dir, ".jri", "specs", "deployment.md"), "# Deployment\n\nDeploy with Wrangler and preview URLs.\n");
      await expect(checkInterrogationStartGate(dir, { now: new Date("2026-05-27T20:00:00.000Z") })).resolves.toMatchObject({
        ok: false,
        pending: [{ topicId: "deployment", topic: { pendingReconciliation: { reason: "manualSpecEdit" } } }],
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("clears pending manual reconciliation when the interrogator accepts the updated spec", async () => {
    const dir = await tempProject();
    try {
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI.\n");
      const originalFingerprint = await fingerprintSpecFile(dir, ".jri/specs/app.md");
      await writeInterrogationState(dir, {
        schemaVersion: 1,
        topics: {
          app: {
            specFile: ".jri/specs/app.md",
            status: "sealed",
            lastReconciledSpecFingerprint: originalFingerprint,
          },
        },
      });

      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI and a TUI.\n");
      await checkInterrogationStartGate(dir, { now: new Date("2026-05-27T20:00:00.000Z") });
      await recordInterrogatorSpecUpdate(dir, [".jri/specs/app.md"]);
      const result = await checkInterrogationStartGate(dir, { now: new Date("2026-05-27T20:01:00.000Z") });
      const persisted = await readInterrogationState(dir);

      expect(result).toMatchObject({ ok: true });
      expect(persisted?.topics.app).toBeDefined();
      expect(persisted!.topics.app!.pendingReconciliation).toBeUndefined();
      expect(persisted?.topics.app).toMatchObject({
        status: "open",
        lastReconciledSpecFingerprint: await fingerprintSpecFile(dir, ".jri/specs/app.md"),
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("rejects traversal-like persisted spec paths", async () => {
    const dir = await tempProject();
    try {
      await writeFile(
        join(dir, ".jri", "interrogation-state.json"),
        `${JSON.stringify({
          schemaVersion: 1,
          topics: {
            app: {
              specFile: ".jri/specs/app/../other.md",
              status: "open",
            },
          },
        })}\n`,
      );

      await expect(readInterrogationState(dir)).rejects.toThrow("invalid specFile");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
