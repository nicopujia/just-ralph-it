import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { appendFile, chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import { getRecoveredStatus, haltLoop, observeLoop, requestGracefulStop, resumeLoop, runLoopProcess, startRalphLoop } from "../src/core/daemon-runtime";
import { appendLoopEvent, writeStatusAtomic } from "../src/core/runtime-state";
import { defaultStatus } from "../src/core/schema";
import { fingerprintSpecFile, recordInterrogatorSpecUpdate, writeInterrogationState } from "../src/core/interrogation-state";
import { registerLoopChild } from "../src/core/harness";

const emptySpecsFingerprint = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

async function tempProject(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "jri-daemon-test-"));
  await mkdir(join(dir, ".jri", "logs"), { recursive: true });
  await writeStatusAtomic(dir, defaultStatus(dir));
  return dir;
}

describe("daemon/runtime scaffolding", () => {
  test("recovery clears dead process ownership and records a repair event", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        stopRequested: true,
        process: {
          pid: 12345,
          command: "jri-daemon",
          startedAt: "2026-05-27T18:42:10.000Z",
        },
      });

      const status = await getRecoveredStatus(dir, {
        now: new Date("2026-05-27T18:45:00.000Z"),
        isProcessAlive: () => false,
      });

      expect(status.state).toBe("stopped");
      expect(status.process).toBeUndefined();
      expect(status.stopRequested).toBe(false);
      expect(status.recoveryNote?.repairedFrom).toBe("building");

      const events = await collect(observeLoop(dir, { isProcessAlive: () => false }));
      expect(events.some((event) => event.type === "statusRepaired")).toBe(true);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("recovery repairs active status with no process or lock from terminal loop events", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        stopRequested: true,
      });
      await appendLoopEvent(dir, {
        type: "loopFinished",
        loopId: "20260527T184210Z",
        data: { outcome: "completed", summary: "Build complete.", url: "https://example.test", commit: "abc123", tag: "0.0.1" },
      });

      const status = await getRecoveredStatus(dir, {
        now: new Date("2026-05-27T18:45:00.000Z"),
      });

      expect(status).toMatchObject({
        state: "idle",
        activeLoopId: null,
        lastLoopId: "20260527T184210Z",
        stopRequested: false,
        lastResult: {
          outcome: "completed",
          summary: "Build complete.",
          url: "https://example.test",
          commit: "abc123",
          tag: "0.0.1",
        },
        recoveryNote: {
          repairedFrom: "building",
        },
      });

      const events = await collect(observeLoop(dir));
      expect(events.map((event) => event.type)).toEqual(["loopFinished", "statusRepaired"]);
      expect(events[1]).toMatchObject({ type: "statusRepaired", data: { repairedFrom: "building", repairedTo: "idle" } });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("recovery prefers terminal loop event over stale process ownership", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        stopRequested: true,
        process: {
          pid: 12345,
          command: "runner building",
          startedAt: "2026-05-27T18:42:10.000Z",
        },
        lock: activeTestLock("build", 12345),
      });
      await appendLoopEvent(dir, {
        type: "loopFinished",
        loopId: "20260527T184210Z",
        data: { outcome: "completed", summary: "Build complete.", commit: "abc123", tag: "0.0.1" },
      });

      const status = await getRecoveredStatus(dir, {
        now: new Date("2026-05-27T18:45:00.000Z"),
        isProcessAlive: () => false,
      });

      expect(status).toMatchObject({
        state: "idle",
        activeLoopId: null,
        stopRequested: false,
        lastResult: {
          outcome: "completed",
          summary: "Build complete.",
          commit: "abc123",
          tag: "0.0.1",
        },
        recoveryNote: {
          repairedFrom: "building",
          message: expect.stringContaining("Latest durable loop event completed the loop."),
        },
      });
      expect(status.process).toBeUndefined();
      expect(status.lock).toBeUndefined();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("recovery stops orphaned active status when no terminal event exists", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "planning",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        stopRequested: true,
      });
      await appendLoopEvent(dir, {
        type: "planningStarted",
        loopId: "20260527T184210Z",
        data: {},
      });

      const status = await getRecoveredStatus(dir, {
        now: new Date("2026-05-27T18:45:00.000Z"),
      });

      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        stopRequested: false,
        lastResult: {
          outcome: "failed",
          summary: expect.stringContaining("no recorded process or lock"),
        },
        recoveryNote: {
          repairedFrom: "planning",
        },
      });

      const events = await collect(observeLoop(dir));
      expect(events.map((event) => event.type)).toEqual(["planningStarted", "statusRepaired"]);
      expect(events[1]).toMatchObject({ type: "statusRepaired", data: { repairedFrom: "planning", repairedTo: "stopped" } });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("observe replays persisted loop events in sequence", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("build"),
      });
      await appendLoopEvent(dir, {
        type: "loopStarted",
        loopId: "20260527T184210Z",
        data: { projectDir: dir },
      });
      await appendLoopEvent(dir, {
        type: "auditStarted",
        loopId: "20260527T184210Z",
        data: {},
      });

      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual(["loopStarted", "auditStarted"]);
      expect(events.map((event) => event.sequence)).toEqual([1, 2]);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("recovery appends missing loopStarted milestone for live startup ownership", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "auditing",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        process: {
          pid: 24680,
          command: "runner auditing",
          startedAt: "2026-05-27T18:42:10.000Z",
        },
        lock: activeTestLock("audit", 24680),
      });

      const status = await getRecoveredStatus(dir, {
        isProcessAlive: () => true,
      });
      const events = await collect(observeLoop(dir, { isProcessAlive: () => true }));

      expect(status).toMatchObject({
        state: "auditing",
        activeLoopId: "20260527T184210Z",
        process: { pid: 24680, command: "runner auditing" },
      });
      expect(events.map((event) => event.type)).toEqual(["loopStarted"]);
      expect(events[0]).toMatchObject({
        type: "loopStarted",
        loopId: "20260527T184210Z",
        data: { projectDir: dir, pid: 24680 },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("observe can include recent stdout context with byte offset before milestone events", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("build"),
      });
      await mkdir(join(dir, ".jri", "logs", "20260527T184210Z"), { recursive: true });
      await writeFile(join(dir, ".jri", "logs", "20260527T184210Z", "stdout.log"), "préface café\nsecond line\nthird line\n", "utf8");
      await appendLoopEvent(dir, {
        type: "iterationStarted",
        loopId: "20260527T184210Z",
        iteration: 1,
        data: { trackedTreeCleanAtStart: true },
      });

      const events = await collect(observeLoop(dir, { includeStdout: true, recentStdoutLines: 2 }));

      expect(events.map((event) => event.type)).toEqual(["loopOutput", "iterationStarted"]);
      const output = events[0];
      if (output?.type !== "loopOutput") throw new Error("Expected loopOutput event.");
      expect(output.data).toEqual({ text: "second line\nthird line\n", replayed: true });
      expect(output.stdoutOffset).toBe(Buffer.byteLength("préface café\n", "utf8"));
      expect(output.sequence).toBe(-(output.stdoutOffset + 1));
      expect(events[1]).toMatchObject({ type: "iterationStarted", sequence: 1 });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("observe can follow newly appended stdout and loop events without duplicating replayed output", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("build"),
      });
      await mkdir(join(dir, ".jri", "logs", "20260527T184210Z"), { recursive: true });
      await writeFile(join(dir, ".jri", "logs", "20260527T184210Z", "stdout.log"), "before\n", "utf8");
      await appendLoopEvent(dir, {
        type: "iterationStarted",
        loopId: "20260527T184210Z",
        iteration: 1,
        data: { trackedTreeCleanAtStart: true },
      });

      const iterator = observeLoop(dir, { includeStdout: true, recentStdoutLines: 1, follow: true, observePollIntervalMs: 5 })[Symbol.asyncIterator]();

      await expect(iterator.next()).resolves.toMatchObject({
        done: false,
        value: { type: "loopOutput", sequence: -1, stdoutOffset: 0, data: { text: "before\n", replayed: true } },
      });
      await expect(iterator.next()).resolves.toMatchObject({
        done: false,
        value: { type: "iterationStarted", sequence: 1 },
      });

      await appendFile(join(dir, ".jri", "logs", "20260527T184210Z", "stdout.log"), "after\n", "utf8");
      await appendLoopEvent(dir, {
        type: "validationStarted",
        loopId: "20260527T184210Z",
        iteration: 1,
        data: { command: "bun run test" },
      });
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "idle",
        activeLoopId: null,
        lastLoopId: "20260527T184210Z",
      });

      await expect(iterator.next()).resolves.toMatchObject({
        done: false,
        value: {
          type: "loopOutput",
          sequence: -(Buffer.byteLength("before\n", "utf8") + 1),
          stdoutOffset: Buffer.byteLength("before\n", "utf8"),
          data: { text: "after\n", replayed: false },
        },
      });
      await expect(iterator.next()).resolves.toMatchObject({
        done: false,
        value: { type: "validationStarted", sequence: 2 },
      });
      await expect(iterator.next()).resolves.toMatchObject({ done: true });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("requestGracefulStop toggles active loop stop state and logs each request", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "planning",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("plan"),
      });

      const requested = await requestGracefulStop(dir);
      const cleared = await requestGracefulStop(dir);
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(requested).toMatchObject({ type: "stopRequested", data: { requested: true } });
      expect(cleared).toMatchObject({ type: "stopRequested", data: { requested: false } });
      expect(status.stopRequested).toBe(false);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("halt kills recorded process, clears ownership, and moves active loop to halted", async () => {
    const dir = await tempProject();
    const killed: Array<{ pid: number; signal: string | undefined }> = [];
    const lockOperationsDuringKill: string[] = [];
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        stopRequested: true,
        lock: activeTestLock("build", 67890),
        process: {
          pid: 67890,
          command: "jri-daemon",
          startedAt: "2026-05-27T18:42:10.000Z",
        },
        currentIteration: {
          iteration: 3,
          rollbackCommit: "abc123",
          trackedTreeCleanAtStart: true,
        },
      });

      const events = await collect(
        haltLoop(dir, {
          now: new Date("2026-05-27T18:50:00.000Z"),
          isProcessAlive: () => true,
          killProcess: (pid, signal) => {
            const status = JSON.parse(readFileSync(join(dir, ".jri", "status.json"), "utf8"));
            lockOperationsDuringKill.push(status.lock?.operation);
            killed.push({ pid, signal });
          },
          childKillGraceMs: 1,
        }),
      );
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(killed).toEqual([
        { pid: 67890, signal: "SIGTERM" },
        { pid: 67890, signal: "SIGKILL" },
      ]);
      expect(lockOperationsDuringKill).toEqual(["halt", "halt"]);
      expect(events[0]).toMatchObject({
        type: "loopHalted",
        data: { killedPid: 67890, resetOffered: true, resetAccepted: false, rollbackCommit: "abc123" },
      });
      expect(status.state).toBe("halted");
      expect(status.process).toBeUndefined();
      expect(status.lock).toBeUndefined();
      expect(status.stopRequested).toBe(false);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("halt kills registered loop child processes before clearing ownership", async () => {
    const dir = await tempProject();
    const killed: Array<{ pid: number; signal: string | undefined }> = [];
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("build", 67890),
        process: {
          pid: 67890,
          command: "jri-runner",
          startedAt: "2026-05-27T18:42:10.000Z",
        },
      });
      await registerLoopChild(dir, "20260527T184210Z", { pid: 11111, capability: "web" });
      await registerLoopChild(dir, "20260527T184210Z", { pid: 22222, capability: "explorer" });

      const events = await collect(
        haltLoop(dir, {
          isProcessAlive: () => true,
          killProcess: (pid, signal) => killed.push({ pid, signal }),
          childKillGraceMs: 1,
        }),
      );

      expect(killed).toEqual([
        { pid: 11111, signal: "SIGTERM" },
        { pid: 22222, signal: "SIGTERM" },
        { pid: 67890, signal: "SIGTERM" },
        { pid: 11111, signal: "SIGKILL" },
        { pid: 22222, signal: "SIGKILL" },
        { pid: 67890, signal: "SIGKILL" },
      ]);
      expect(events[0]).toMatchObject({
        type: "loopHalted",
        data: { killedPid: 67890, killedChildPids: [11111, 22222] },
      });
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status.state).toBe("halted");
      expect(status.process).toBeUndefined();
      expect(status.lock).toBeUndefined();
      expect(status.lastResult.summary).toContain("child pids 11111, 22222");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("halt accepts eligible rollback reset and records success", async () => {
    const dir = await tempProject();
    const resets: Array<{ projectDir: string; rollbackCommit: string }> = [];
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("build"),
        currentIteration: {
          iteration: 1,
          rollbackCommit: "abc123",
          trackedTreeCleanAtStart: true,
        },
      });

      const events = await collect(
        haltLoop(dir, {
          resetGit: true,
          gitResetRunner: async (projectDir, rollbackCommit) => {
            resets.push({ projectDir, rollbackCommit });
            return { succeeded: true };
          },
        }),
      );
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(resets).toEqual([{ projectDir: dir, rollbackCommit: "abc123" }]);
      expect(events[0]).toMatchObject({
        type: "loopHalted",
        data: { resetOffered: true, resetAccepted: true, resetSucceeded: true, rollbackCommit: "abc123" },
      });
      expect(status.lastResult.summary).toContain("Rollback reset completed.");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("halt does not reset when rollback is ineligible", async () => {
    const dir = await tempProject();
    let resetCalled = false;
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("build"),
        currentIteration: {
          iteration: 1,
          rollbackCommit: "abc123",
          trackedTreeCleanAtStart: false,
        },
      });

      const events = await collect(
        haltLoop(dir, {
          resetGit: true,
          gitResetRunner: async () => {
            resetCalled = true;
            return { succeeded: true };
          },
        }),
      );

      expect(resetCalled).toBe(false);
      expect(events[0]).toMatchObject({
        type: "loopHalted",
        data: { resetOffered: false, resetAccepted: false, rollbackCommit: "abc123" },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("start acquires lock and enters auditing before spawning the runner", async () => {
    const dir = await tempProject();
    const observedStates: unknown[] = [];
    try {
      const event = await startRalphLoop(dir, {
        now: new Date("2026-05-27T20:00:00.000Z"),
        isProcessAlive: () => false,
        spawnRunner: ({ phase }) => {
          observedStates.push(JSON.parse(readFileSync(join(dir, ".jri", "status.json"), "utf8")));
          return { pid: 24680, command: `runner ${phase}` };
        },
      });
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(event).toMatchObject({
        type: "loopStarted",
        loopId: "20260527T200000Z",
        data: { projectDir: dir, pid: 24680 },
      });
      expect(observedStates).toHaveLength(1);
      expect(observedStates[0]).toMatchObject({
        state: "auditing",
        activeLoopId: "20260527T200000Z",
        lock: { owner: "daemon", pid: process.pid, operation: "audit" },
      });
      expect((observedStates[0] as { process?: unknown }).process).toBeUndefined();
      expect(status).toMatchObject({
        state: "auditing",
        process: { pid: 24680, command: "runner auditing" },
        lock: { owner: "daemon", pid: 24680, operation: "audit" },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("start rejects pending spec reconciliation before acquiring a lock", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI.\n");
      await writeInterrogationState(dir, {
        schemaVersion: 1,
        topics: {
          app: {
            specFile: ".jri/specs/app.md",
            status: "sealed",
            lastReconciledSpecFingerprint: await fingerprintSpecFile(dir, ".jri/specs/app.md"),
          },
        },
      });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI and TUI.\n");

      await expect(
        startRalphLoop(dir, {
          now: new Date("2026-05-27T20:00:00.000Z"),
          spawnRunner: () => {
            throw new Error("runner should not start");
          },
        }),
      ).rejects.toThrow("Cannot start while spec reconciliation is pending.");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status).toEqual(defaultStatus(dir));
      const state = JSON.parse(await readFile(join(dir, ".jri", "interrogation-state.json"), "utf8"));
      expect(state.topics.app).toMatchObject({
        status: "open",
        pendingReconciliation: {
          reason: "manualSpecEdit",
          detectedAt: "2026-05-27T20:00:00.000Z",
        },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("start restores prior status when runner spawn fails after lock acquisition", async () => {
    const dir = await tempProject();
    try {
      await expect(
        startRalphLoop(dir, {
          now: new Date("2026-05-27T20:00:00.000Z"),
          isProcessAlive: () => false,
          spawnRunner: () => {
            throw new Error("spawn unavailable");
          },
        }),
      ).rejects.toThrow("spawn unavailable");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status).toEqual(defaultStatus(dir));
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("start rejects unchanged stopped loops and points to resume", async () => {
    const dir = await tempProject();
    let spawnCalled = false;
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: emptySpecsFingerprint,
      });

      await expect(
        startRalphLoop(dir, {
          isProcessAlive: () => false,
          spawnRunner: () => {
            spawnCalled = true;
            return { pid: 24680, command: "runner auditing" };
          },
        }),
      ).rejects.toThrow("stopped loop has unchanged specs");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(spawnCalled).toBe(false);
      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: emptySpecsFingerprint,
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("start reauthorizes stopped loops when specs changed", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nChanged requirements.\n", "utf8");
      await recordInterrogatorSpecUpdate(dir, [".jri/specs/app.md"]);
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: "previous-authorized-fingerprint",
      });

      const event = await startRalphLoop(dir, {
        isProcessAlive: () => false,
        spawnRunner: ({ phase }) => ({ pid: 24681, command: `runner ${phase}` }),
      });
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(event).toMatchObject({
        type: "loopStarted",
        loopId: "20260527T184210Z",
        data: { projectDir: dir, pid: 24681 },
      });
      expect(status).toMatchObject({
        state: "auditing",
        activeLoopId: "20260527T184210Z",
        process: { pid: 24681, command: "runner auditing" },
        lock: { operation: "audit" },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("start rejects stopped loops when authorized fingerprint is missing", async () => {
    const dir = await tempProject();
    let spawnCalled = false;
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
      });

      await expect(
        startRalphLoop(dir, {
          isProcessAlive: () => false,
          spawnRunner: ({ phase }) => {
            spawnCalled = true;
            return { pid: 24682, command: `runner ${phase}` };
          },
        }),
      ).rejects.toMatchObject({ code: "specs-fingerprint-missing" });

      expect(spawnCalled).toBe(false);
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
      });
      expect(status.authorizedSpecsFingerprint).toBeUndefined();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("start preserves ambiguous-spec blocker until the new audit passes", async () => {
    const dir = await tempProject();
    const observedStates: unknown[] = [];
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
            summary: "Clarify deployment target.",
            steps: ["Choose the deployment target."],
            resumeInstruction: "Clarify specs in bare jri, then say just ralph it.",
          },
        },
      });

      await startRalphLoop(dir, {
        isProcessAlive: () => false,
        spawnRunner: ({ phase }) => {
          observedStates.push(JSON.parse(readFileSync(join(dir, ".jri", "status.json"), "utf8")));
          return { pid: 24683, command: `runner ${phase}` };
        },
      });
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(observedStates[0]).toMatchObject({
        state: "auditing",
        blocker: {
          reason: "ambiguousSpecs",
          description: "Deployment target is unclear.",
        },
      });
      expect(status).toMatchObject({
        state: "auditing",
        blocker: {
          reason: "ambiguousSpecs",
          description: "Deployment target is unclear.",
        },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("resume from stopped starts a controlled runner and records ownership", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        stopRequested: true,
        authorizedSpecsFingerprint: emptySpecsFingerprint,
      });
      await appendLoopEvent(dir, {
        type: "auditPassed",
        loopId: "20260527T184210Z",
        data: { specFiles: [], specsFingerprint: emptySpecsFingerprint },
      });
      await appendLoopEvent(dir, {
        type: "loopStopped",
        loopId: "20260527T184210Z",
        data: { reason: "gracefulStopRequested", nextPhase: "planning", specsFingerprint: emptySpecsFingerprint },
      });

      const events = await collect(
        resumeLoop(dir, {
          now: new Date("2026-05-27T19:00:00.000Z"),
          isProcessAlive: () => false,
          spawnRunner: ({ phase }) => ({ pid: 24680, command: `runner ${phase}` }),
        }),
      );
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(events).toHaveLength(1);
      expect(events[0]).toMatchObject({
        type: "loopStarted",
        loopId: "20260527T184210Z",
        data: { projectDir: dir, pid: 24680 },
      });
      expect(status).toMatchObject({
        state: "planning",
        activeLoopId: "20260527T184210Z",
        stopRequested: false,
        process: { pid: 24680, command: "runner planning" },
        lock: { owner: "daemon", pid: 24680, operation: "plan" },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("resume uses the persisted stopped event next phase instead of plan-file existence", async () => {
    const dir = await tempProject();
    try {
      await Bun.write(join(dir, ".jri", "IMPLEMENTATION_PLAN.md"), "# Plan\n");
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: emptySpecsFingerprint,
      });
      await appendLoopEvent(dir, {
        type: "auditPassed",
        loopId: "20260527T184210Z",
        data: { specFiles: [], specsFingerprint: emptySpecsFingerprint },
      });
      await appendLoopEvent(dir, {
        type: "loopStopped",
        loopId: "20260527T184210Z",
        data: { reason: "gracefulStopRequested", nextPhase: "planning", specsFingerprint: emptySpecsFingerprint },
      });

      await collect(
        resumeLoop(dir, {
          spawnRunner: ({ phase }) => ({ pid: 13579, command: `runner ${phase}` }),
        }),
      );
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(status).toMatchObject({
        state: "planning",
        process: { pid: 13579, command: "runner planning" },
        lock: { operation: "plan" },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("resume rejects stopped loops without durable next-phase evidence", async () => {
    const dir = await tempProject();
    let spawnCalled = false;
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: emptySpecsFingerprint,
      });

      await expect(
        collect(
          resumeLoop(dir, {
            spawnRunner: ({ phase }) => {
              spawnCalled = true;
              return { pid: 13579, command: `runner ${phase}` };
            },
          }),
        ),
      ).rejects.toThrow("next safe phase was not recorded");

      expect(spawnCalled).toBe(false);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("resume rejects stopped loops when the stop event has no valid prior phase milestone", async () => {
    const dir = await tempProject();
    let spawnCalled = false;
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: emptySpecsFingerprint,
      });
      await appendLoopEvent(dir, {
        type: "loopStopped",
        loopId: "20260527T184210Z",
        data: { reason: "gracefulStopRequested", nextPhase: "planning", specsFingerprint: emptySpecsFingerprint },
      });

      await expect(
        collect(
          resumeLoop(dir, {
            spawnRunner: ({ phase }) => {
              spawnCalled = true;
              return { pid: 13579, command: `runner ${phase}` };
            },
          }),
        ),
      ).rejects.toThrow("stop event is missing its prior phase milestone");

      expect(spawnCalled).toBe(false);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("resume rejects stopped loops when later lifecycle events supersede the stop event", async () => {
    const dir = await tempProject();
    let spawnCalled = false;
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: emptySpecsFingerprint,
      });
      await appendLoopEvent(dir, {
        type: "auditPassed",
        loopId: "20260527T184210Z",
        data: { specFiles: [], specsFingerprint: emptySpecsFingerprint },
      });
      await appendLoopEvent(dir, {
        type: "loopStopped",
        loopId: "20260527T184210Z",
        data: { reason: "gracefulStopRequested", nextPhase: "planning", specsFingerprint: emptySpecsFingerprint },
      });
      await appendLoopEvent(dir, {
        type: "planningStarted",
        loopId: "20260527T184210Z",
        data: {},
      });

      await expect(
        collect(
          resumeLoop(dir, {
            spawnRunner: ({ phase }) => {
              spawnCalled = true;
              return { pid: 13580, command: `runner ${phase}` };
            },
          }),
        ),
      ).rejects.toThrow("recorded stop is not the latest loop event");

      expect(spawnCalled).toBe(false);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("resume from stopped rejects direct continuation when specs changed", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nChanged requirements.\n", "utf8");
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: "previous-authorized-fingerprint",
      });

      await expect(
        collect(
          resumeLoop(dir, {
            spawnRunner: ({ phase }) => ({ pid: 11111, command: `runner ${phase}` }),
          }),
        ),
      ).rejects.toThrow("specs changed");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("resume from verified needs-human-task blocker clears blocker and records resolution", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: emptySpecsFingerprint,
        blocker: {
          reason: "needsHumanTask",
          description: "Provide deployment credentials.",
          resolutionGuide: {
            summary: "Credentials are required.",
            steps: ["Provide the deployment token."],
            resumeInstruction: "Say done in bare jri after the token is available.",
          },
          resumePhase: "building",
          resolution: {
            status: "verified",
            verifiedAt: "2026-05-27T19:10:00.000Z",
            verificationSummary: "Deployment token is present.",
          },
        },
      });

      const events = await collect(
        resumeLoop(dir, {
          spawnRunner: ({ phase }) => ({ pid: 22222, command: `runner ${phase}` }),
        }),
      );
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(events.map((event) => event.type)).toEqual(["blockerResolved", "loopStarted"]);
      expect(status).toMatchObject({
        state: "building",
        process: { pid: 22222, command: "runner building" },
        lock: { operation: "build" },
      });
      expect(status.blocker).toBeUndefined();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("resume from verified planner human-task blocker returns to planning", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: emptySpecsFingerprint,
        blocker: {
          reason: "needsHumanTask",
          description: "Choose a deployment account.",
          resolutionGuide: {
            summary: "Account choice is required.",
            steps: ["Choose the account outside chat."],
            resumeInstruction: "Say done in bare jri after choosing the account.",
          },
          resumePhase: "planning",
          resolution: {
            status: "verified",
            verifiedAt: "2026-05-27T19:10:00.000Z",
            verificationSummary: "Account is selected.",
          },
        },
      });

      await collect(
        resumeLoop(dir, {
          spawnRunner: ({ phase }) => ({ pid: 22223, command: `runner ${phase}` }),
        }),
      );
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(status).toMatchObject({
        state: "planning",
        process: { pid: 22223, command: "runner planning" },
        lock: { operation: "plan" },
      });
      expect(status.blocker).toBeUndefined();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("resume from verified human-task blocker rejects missing resume phase", async () => {
    const dir = await tempProject();
    let spawnCalled = false;
    try {
      await mkdir(join(dir, ".jri"), { recursive: true });
      await writeFile(
        join(dir, ".jri", "status.json"),
        `${JSON.stringify(
          {
            ...defaultStatus(dir),
            state: "blocked",
            activeLoopId: "20260527T184210Z",
            lastLoopId: "20260527T184210Z",
            authorizedSpecsFingerprint: emptySpecsFingerprint,
            blocker: {
              reason: "needsHumanTask",
              description: "Provide deployment credentials.",
              resolutionGuide: {
                summary: "Credentials are required.",
                steps: ["Provide the deployment token."],
                resumeInstruction: "Say done in bare jri after the token is available.",
              },
              resolution: {
                status: "verified",
                verifiedAt: "2026-05-27T19:10:00.000Z",
                verificationSummary: "Deployment token is present.",
              },
            },
          },
          null,
          2,
        )}\n`,
        "utf8",
      );

      await expect(
        collect(
          resumeLoop(dir, {
            spawnRunner: ({ phase }) => {
              spawnCalled = true;
              return { pid: 22222, command: `runner ${phase}` };
            },
          }),
        ),
      ).rejects.toThrow("must record blocker.resumePhase");

      expect(spawnCalled).toBe(false);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("resume does not duplicate an already recorded human-task resolution event", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: emptySpecsFingerprint,
        blocker: {
          reason: "needsHumanTask",
          description: "Provide deployment credentials.",
          resolutionGuide: {
            summary: "Credentials are required.",
            steps: ["Provide the deployment token."],
            resumeInstruction: "Say done in bare jri after the token is available.",
          },
          resumePhase: "building",
          resolution: {
            status: "verified",
            verifiedAt: "2026-05-27T19:10:00.000Z",
            verificationSummary: "Deployment token is present.",
          },
        },
      });
      await appendLoopEvent(dir, {
        type: "blockerResolved",
        loopId: "20260527T184210Z",
        data: { reason: "needsHumanTask" },
      });

      const events = await collect(
        resumeLoop(dir, {
          spawnRunner: ({ phase }) => ({ pid: 22224, command: `runner ${phase}` }),
        }),
      );

      expect(events.map((event) => event.type)).toEqual(["loopStarted"]);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("resume from verified needs-human-task blocker rejects changed specs", async () => {
    const dir = await tempProject();
    let spawnCalled = false;
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nChanged requirements.\n", "utf8");
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: "previous-authorized-fingerprint",
        blocker: {
          reason: "needsHumanTask",
          description: "Provide deployment credentials.",
          resolutionGuide: {
            summary: "Credentials are required.",
            steps: ["Provide the deployment token."],
            resumeInstruction: "Say done in bare jri after the token is available.",
          },
          resumePhase: "building",
          resolution: {
            status: "verified",
            verifiedAt: "2026-05-27T19:10:00.000Z",
            verificationSummary: "Deployment token is present.",
          },
        },
      });

      await expect(
        collect(
          resumeLoop(dir, {
            spawnRunner: ({ phase }) => {
              spawnCalled = true;
              return { pid: 22222, command: `runner ${phase}` };
            },
          }),
        ),
      ).rejects.toThrow("specs changed");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(spawnCalled).toBe(false);
      expect(status).toMatchObject({
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: "previous-authorized-fingerprint",
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner executes an isolated Pi command, records stdout, and completes the loop", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild the app.\n", "utf8");
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        "#!/bin/sh\necho fake-pi-ran\necho 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Fake build completed.\"}'\n",
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });
      await recordExplorerProof(dir);
      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const stdout = await readFile(join(dir, ".jri", "logs", "20260527T184210Z", "stdout.log"), "utf8");
      const events = await collect(observeLoop(dir));

      expect(stdout).toContain("fake-pi-ran");
      expect(events.map((event) => event.type)).toEqual(["subagentFinished", "iterationStarted", "iterationFinished", "loopFinished"]);
      expect(status).toMatchObject({
        state: "idle",
        activeLoopId: null,
        iterations: 1,
        lastResult: { outcome: "completed", explorer: { used: true, summary: "Explorer found the relevant code path." } },
      });
      expect(status.process).toBeUndefined();
      expect(status.lock).toBeUndefined();
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner records malformed builder handoffs as durable failure evidence", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      const fakePi = join(dir, "fake-pi-invalid-handoff.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "echo build-output-before-invalid-handoff",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\"'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });
      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "iterationFinished", "loopFinished"]);
      expect(events[1]).toMatchObject({ type: "iterationFinished", data: { outcome: "validationFailed" } });
      expect(events[2]).toMatchObject({ type: "loopFinished", data: { outcome: "failed" } });
      const loopFinished = events[2];
      if (!loopFinished || loopFinished.type !== "loopFinished") throw new Error("Expected loopFinished event.");
      expect(loopFinished.message).toContain("Emit JRI_HANDOFF_JSON:");
      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastResult: {
          outcome: "failed",
        },
      });
      expect(status.lastResult.summary).toContain("Building failed: The building handoff is not valid JSON.");
      expect(status.lock).toBeUndefined();
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner rejects legacy builder handoff frames as durable failure evidence", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      const fakePi = join(dir, "fake-pi-legacy-handoff.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "echo build-output-before-legacy-handoff",
          'echo \'JRI_BLOCKER_JSON: {"reason":"needsHumanTask","description":"old blocker","resolutionGuide":{"summary":"old","steps":["old"],"resumeInstruction":"old"}}\'',
          "echo 'JRI_NEEDS_REPLAN: plan drifted'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });
      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "iterationFinished", "loopFinished"]);
      expect(events[1]).toMatchObject({ type: "iterationFinished", data: { outcome: "validationFailed" } });
      const loopFinished = events[2];
      if (!loopFinished || loopFinished.type !== "loopFinished") throw new Error("Expected loopFinished event.");
      expect(loopFinished).toMatchObject({ type: "loopFinished", data: { outcome: "failed" } });
      expect(loopFinished.message).toContain("Emit exactly one line that starts with JRI_HANDOFF_JSON:");
      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastResult: {
          outcome: "failed",
        },
      });
      expect(status.lastResult.summary).toContain("Building failed: The building phase did not emit a machine-readable JRI handoff.");
      expect(status.lock).toBeUndefined();
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner rejects an already-cancelled runtime signal before starting phase work", async () => {
    const dir = await tempProject();
    const controller = new AbortController();
    controller.abort();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await expect(runLoopProcess(dir, "20260527T184210Z", "building", { signal: controller.signal })).rejects.toThrow("cancelled");

      const events = await collect(observeLoop(dir));
      expect(events).toEqual([]);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner records startup lock loss as durable failure evidence", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: 99999,
          operation: "plan",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
        process: {
          pid: 99999,
          command: "runner planning",
          startedAt: "2026-05-27T19:00:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "planning", {
        observePollIntervalMs: 1,
        isProcessAlive: () => true,
      });

      const events = await collect(observeLoop(dir, { isProcessAlive: () => true }));
      expect(events.map((event) => event.type)).toEqual(["loopStarted", "loopFinished"]);
      expect(events[1]).toMatchObject({
        type: "loopFinished",
        data: {
          outcome: "failed",
          summary: expect.stringContaining("The JRI runner startup state changed before ownership was confirmed."),
        },
      });

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastResult: {
          outcome: "failed",
          summary: expect.stringContaining("The JRI runner startup state changed before ownership was confirmed."),
        },
      });
      expect(status.process).toBeUndefined();
      expect(status.lock).toBeUndefined();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner cancellation fans out to registered loop children before recording failure", async () => {
    const dir = await tempProject();
    const controller = new AbortController();
    const killed: Array<{ pid: number; signal: string | undefined; state: string }> = [];
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("build", process.pid),
      });
      await registerLoopChild(dir, "20260527T184210Z", { pid: 11111, capability: "web" });
      await registerLoopChild(dir, "20260527T184210Z", { pid: 22222, capability: "explorer" });

      await runLoopProcess(dir, "20260527T184210Z", "building", {
        signal: controller.signal,
        childKillGraceMs: 1,
        killProcess: (pid, signal) => {
          const status = JSON.parse(readFileSync(join(dir, ".jri", "status.json"), "utf8"));
          killed.push({ pid, signal, state: status.state });
        },
        harnessAdapter: async (invocation) => {
          controller.abort();
          expect(invocation.signal.aborted).toBe(true);
          return {
            handoff: {
              agent: "builder",
              action: "continue",
              summary: "Cancelled after adapter return.",
              validation: [{ command: "bun run test", exitCode: 0, passed: true, summary: "Tests passed." }],
            },
          };
        },
      });

      const events = await collect(observeLoop(dir));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(killed).toEqual([
        { pid: 11111, signal: "SIGTERM", state: "building" },
        { pid: 22222, signal: "SIGTERM", state: "building" },
        { pid: 11111, signal: "SIGKILL", state: "building" },
        { pid: 22222, signal: "SIGKILL", state: "building" },
      ]);
      expect(events.at(-1)).toMatchObject({ type: "loopFinished", data: { outcome: "failed" } });
      expect(status).toMatchObject({
        state: "stopped",
        lastResult: { outcome: "failed", summary: expect.stringContaining("cancelled") },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner timeout aborts phase work and fans out to registered loop children", async () => {
    const dir = await tempProject();
    const killed: Array<{ pid: number; signal: string | undefined; state: string }> = [];
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("build", process.pid),
      });
      await registerLoopChild(dir, "20260527T184210Z", { pid: 11111, capability: "web" });
      await registerLoopChild(dir, "20260527T184210Z", { pid: 22222, capability: "explorer" });

      await runLoopProcess(dir, "20260527T184210Z", "building", {
        runnerTimeoutMs: 1,
        childKillGraceMs: 1,
        killProcess: (pid, signal) => {
          const status = JSON.parse(readFileSync(join(dir, ".jri", "status.json"), "utf8"));
          killed.push({ pid, signal, state: status.state });
        },
        harnessAdapter: async (invocation) => {
          await new Promise<void>((resolve) => invocation.signal.addEventListener("abort", () => resolve(), { once: true }));
          return {
            handoff: {
              agent: "builder",
              action: "continue",
              summary: "Timed out after adapter return.",
              validation: [{ command: "bun run test", exitCode: 0, passed: true, summary: "Tests passed." }],
            },
          };
        },
      });

      const events = await collect(observeLoop(dir));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(killed).toEqual([
        { pid: 11111, signal: "SIGTERM", state: "building" },
        { pid: 22222, signal: "SIGTERM", state: "building" },
        { pid: 11111, signal: "SIGKILL", state: "building" },
        { pid: 22222, signal: "SIGKILL", state: "building" },
      ]);
      expect(events.at(-1)).toMatchObject({ type: "loopFinished", data: { outcome: "failed" } });
      expect(status).toMatchObject({
        state: "stopped",
        lastResult: { outcome: "failed", summary: expect.stringContaining("cancelled") },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner invokes loop phases through the harness adapter contract", async () => {
    const dir = await tempProject();
    const controller = new AbortController();
    const invocations: Array<{
      owner: unknown;
      agent: string;
      phase: string;
      model: unknown;
      refs: string[];
      capabilities: string[];
      signal: AbortSignal;
    }> = [];
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild the app.\n", "utf8");
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "building", {
        signal: controller.signal,
        harnessAdapter: async (invocation) => {
          invocations.push({
            owner: invocation.owner,
            agent: invocation.agent,
            phase: invocation.phase,
            model: invocation.model,
            refs: invocation.context.refs,
            capabilities: invocation.capabilities.map((capability) => capability.name),
            signal: invocation.signal,
          });
          await invocation.output.write("builder display output");
          await recordExplorerProof(dir);
          return {
            handoff: {
              agent: "builder",
              action: "complete",
              summary: "Adapter build complete.",
            },
          };
        },
      });

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const stdout = await readFile(join(dir, ".jri", "logs", "20260527T184210Z", "stdout.log"), "utf8");
      const events = await collect(observeLoop(dir));

      expect(invocations).toEqual([
        {
          owner: { kind: "loop", loopId: "20260527T184210Z" },
          agent: "builder",
          phase: "building",
          model: { model: "gpt-5.5", reasoning: "xhigh" },
          refs: expect.arrayContaining([".jri/status.json", ".jri/specs/app.md"]),
          capabilities: ["web", "web", "explorer"],
          signal: controller.signal,
        },
      ]);
      expect(stdout).toBe("builder display output\n");
      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "subagentFinished", "iterationFinished", "loopFinished"]);
      expect(status).toMatchObject({
        state: "idle",
        activeLoopId: null,
        lastResult: {
          outcome: "completed",
          summary: "Adapter build complete.",
          explorer: { used: true, summary: "Explorer found the relevant code path." },
        },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auditing harness context includes interrogation scratchpad scope", async () => {
    const dir = await tempProject();
    let refs: string[] = [];
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild the app.\n", "utf8");
      await writeFile(join(dir, ".jri", "scratchpad.md"), "Open question: deployment owner.\n", "utf8");
      await writeInterrogationState(dir, {
        schemaVersion: 1,
        topics: {
          app: {
            specFile: ".jri/specs/app.md",
            status: "open",
            lastReconciledSpecFingerprint: await fingerprintSpecFile(dir, ".jri/specs/app.md"),
          },
        },
      });
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "auditing",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("audit"),
      });

      await runLoopProcess(dir, "20260527T184210Z", "auditing", {
        harnessAdapter: async (invocation) => {
          refs = invocation.context.refs;
          return {
            handoff: {
              agent: "auditor",
              action: "failed",
              feedback: "Scratchpad scope still needs review.",
              affectedTopics: ["app"],
              questions: ["Should deployment owner be in scope?"],
            },
          };
        },
      });

      expect(refs).toEqual(expect.arrayContaining([".jri/interrogation-state.json", ".jri/scratchpad.md", ".jri/specs/app.md"]));
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner rejects completion without durable explorer proof", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild the app.\n", "utf8");
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("build"),
      });

      await runLoopProcess(dir, "20260527T184210Z", "building", {
        harnessAdapter: async () => ({
          handoff: {
            agent: "builder",
            action: "complete",
            summary: "Adapter build complete.",
          },
        }),
      });

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "iterationFinished", "loopFinished"]);
      expect(events[2]).toMatchObject({
        type: "loopFinished",
        data: {
          outcome: "failed",
          summary: expect.stringContaining("durable successful explorer delegation evidence"),
        },
      });
      expect(events[2]?.message).toContain("subagentStarted and subagentFinished");
      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastResult: {
          outcome: "failed",
          summary: expect.stringContaining("durable successful explorer delegation evidence"),
        },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("planning fails durably when the planner handoff omits the implementation plan file", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "planning",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("plan"),
      });

      await runLoopProcess(dir, "20260527T184210Z", "planning", {
        harnessAdapter: async () => ({
          handoff: {
            agent: "planner",
            action: "planned",
            planPath: ".jri/IMPLEMENTATION_PLAN.md",
            summary: "Plan ready.",
          },
        }),
      });

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual(["planningStarted", "loopFinished"]);
      expect(events[1]).toMatchObject({
        type: "loopFinished",
        data: {
          outcome: "failed",
          summary: expect.stringContaining("did not create .jri/IMPLEMENTATION_PLAN.md"),
        },
      });
      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastResult: {
          outcome: "failed",
          summary: expect.stringContaining("Planning failed: The planner reported success"),
        },
      });
      expect(status.lock).toBeUndefined();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("plan regeneration fails durably when the regenerated plan file is missing", async () => {
    const dir = await tempProject();
    let invocation = 0;
    try {
      await writeFile(join(dir, ".jri", "IMPLEMENTATION_PLAN.md"), "# Existing plan\n", "utf8");
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("build"),
      });

      await runLoopProcess(dir, "20260527T184210Z", "building", {
        harnessAdapter: async () => {
          invocation += 1;
          if (invocation === 1) {
            await rm(join(dir, ".jri", "IMPLEMENTATION_PLAN.md"), { force: true });
            return {
              handoff: {
                agent: "builder",
                action: "needsReplan",
                reason: "plan is stale",
              },
            };
          }
          return {
            handoff: {
              agent: "planner",
              action: "planned",
              planPath: ".jri/IMPLEMENTATION_PLAN.md",
              summary: "Plan regenerated.",
            },
          };
        },
      });

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual([
        "iterationStarted",
        "iterationFinished",
        "planRegenerationRequested",
        "planRegenerationStarted",
        "loopFinished",
      ]);
      expect(events[4]).toMatchObject({
        type: "loopFinished",
        data: {
          outcome: "failed",
          summary: expect.stringContaining("did not create .jri/IMPLEMENTATION_PLAN.md"),
        },
      });
      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastResult: { outcome: "failed" },
      });
      expect(status.lastResult.summary).toContain("Planning failed: The planner reported success");
      expect(status.lock).toBeUndefined();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auditing runner passes specs before planning and building", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "count_file=.jri/fake-pi-count",
          "count=0",
          "[ -f \"$count_file\" ] && count=$(cat \"$count_file\")",
          "count=$((count + 1))",
          "printf '%s' \"$count\" > \"$count_file\"",
          "if [ \"$count\" = 1 ]; then",
          `  echo 'JRI_HANDOFF_JSON: {"agent":"auditor","action":"passed","specFiles":[".jri/specs/app.md"],"specsFingerprint":"${emptySpecsFingerprint}","summary":"Specs ready."}'`,
          "elif [ \"$count\" = 2 ]; then",
          "  echo '# Plan' > .jri/IMPLEMENTATION_PLAN.md",
          "  echo 'JRI_HANDOFF_JSON: {\"agent\":\"planner\",\"action\":\"planned\",\"planPath\":\".jri/IMPLEMENTATION_PLAN.md\",\"summary\":\"Plan ready.\"}'",
          "else",
          "  echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Build complete.\"}'",
          "fi",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "auditing",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "audit",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });
      await recordExplorerProof(dir);

      await runLoopProcess(dir, "20260527T184210Z", "auditing");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual([
        "subagentFinished",
        "auditStarted",
        "auditPassed",
        "planningStarted",
        "planningFinished",
        "iterationStarted",
        "iterationFinished",
        "loopFinished",
      ]);
      expect(status).toMatchObject({
        state: "idle",
        activeLoopId: null,
        authorizedSpecsFingerprint: emptySpecsFingerprint,
        iterations: 1,
        lastResult: { outcome: "completed", explorer: { used: true, summary: "Explorer found the relevant code path." } },
      });
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auditing runner persists the core-computed specs fingerprint", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      const specContent = "# App\n\nBuild the app.\n";
      await writeFile(join(dir, ".jri", "specs", "app.md"), specContent, "utf8");
      const specsFingerprint = specsFingerprintForFiles({ "app.md": specContent });
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "auditing",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("audit"),
      });

      await runLoopProcess(dir, "20260527T184210Z", "auditing", {
        harnessRunner: async ({ phase, stdoutPath }) => {
          if (phase === "auditing") {
            await appendFile(
              stdoutPath,
              `JRI_HANDOFF_JSON: {"agent":"auditor","action":"passed","specFiles":[".jri/specs/app.md"],"specsFingerprint":"${specsFingerprint}","summary":"Specs ready."}\n`,
            );
            return 0;
          }
          if (phase === "planning") {
            await writeFile(join(dir, ".jri", "IMPLEMENTATION_PLAN.md"), "# Plan\n", "utf8");
            await appendFile(
              stdoutPath,
              'JRI_HANDOFF_JSON: {"agent":"planner","action":"planned","planPath":".jri/IMPLEMENTATION_PLAN.md","summary":"Plan ready."}\n',
            );
            return 0;
          }
          await appendFile(stdoutPath, 'JRI_HANDOFF_JSON: {"agent":"builder","action":"complete","summary":"Build complete."}\n');
          return 0;
        },
      });

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));
      expect(events.find((event) => event.type === "auditPassed")).toMatchObject({
        type: "auditPassed",
        data: { specsFingerprint },
      });
      expect(status.authorizedSpecsFingerprint).toBe(specsFingerprint);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auditing runner fails when the auditor fingerprint does not match current specs", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild the app.\n", "utf8");
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "auditing",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("audit"),
      });

      await runLoopProcess(dir, "20260527T184210Z", "auditing", {
        harnessRunner: async ({ stdoutPath }) => {
          await appendFile(
            stdoutPath,
            'JRI_HANDOFF_JSON: {"agent":"auditor","action":"passed","specFiles":[".jri/specs/app.md"],"specsFingerprint":"not-current","summary":"Specs ready."}\n',
          );
          return 0;
        },
      });

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));
      expect(events.map((event) => event.type)).toEqual(["auditStarted", "loopFinished"]);
      expect(events[1]).toMatchObject({
        type: "loopFinished",
        data: { outcome: "failed", summary: expect.stringContaining("Auditing failed") },
        message: expect.stringContaining("core-computed specs fingerprint"),
      });
      expect(status).toMatchObject({
        state: "stopped",
        lastResult: { outcome: "failed" },
      });
      expect(status.authorizedSpecsFingerprint).toBeUndefined();
      expect(status.lastResult.summary).toContain("does not match the current specs");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auditing runner rejects legacy specs fingerprints without path and byte-length framing", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      const specContent = "# App\n\nBuild the app.\n";
      await writeFile(join(dir, ".jri", "specs", "app.md"), specContent, "utf8");
      const legacyFingerprint = legacySpecsFingerprintForFiles({ "app.md": specContent });
      expect(legacyFingerprint).not.toBe(specsFingerprintForFiles({ "app.md": specContent }));
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "auditing",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("audit"),
      });

      await runLoopProcess(dir, "20260527T184210Z", "auditing", {
        harnessRunner: async ({ stdoutPath }) => {
          await appendFile(
            stdoutPath,
            `JRI_HANDOFF_JSON: {"agent":"auditor","action":"passed","specFiles":[".jri/specs/app.md"],"specsFingerprint":"${legacyFingerprint}","summary":"Specs ready."}\n`,
          );
          return 0;
        },
      });

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));
      expect(events.map((event) => event.type)).toEqual(["auditStarted", "loopFinished"]);
      expect(status).toMatchObject({
        state: "stopped",
        lastResult: { outcome: "failed" },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auditing runner resolves an ambiguous-spec blocker only after audit passes", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "count_file=.jri/fake-pi-count",
          "count=0",
          "[ -f \"$count_file\" ] && count=$(cat \"$count_file\")",
          "count=$((count + 1))",
          "printf '%s' \"$count\" > \"$count_file\"",
          "if [ \"$count\" = 1 ]; then",
          `  echo 'JRI_HANDOFF_JSON: {"agent":"auditor","action":"passed","specFiles":[".jri/specs/app.md"],"specsFingerprint":"${emptySpecsFingerprint}","summary":"Specs ready."}'`,
          "elif [ \"$count\" = 2 ]; then",
          "  echo '# Plan' > .jri/IMPLEMENTATION_PLAN.md",
          "  echo 'JRI_HANDOFF_JSON: {\"agent\":\"planner\",\"action\":\"planned\",\"planPath\":\".jri/IMPLEMENTATION_PLAN.md\",\"summary\":\"Plan ready.\"}'",
          "else",
          "  echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Build complete.\"}'",
          "fi",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "auditing",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        blocker: {
          reason: "ambiguousSpecs",
          description: "Deployment target is unclear.",
          resolutionGuide: {
            summary: "Clarify deployment target.",
            steps: ["Choose the deployment target."],
            resumeInstruction: "Clarify specs in bare jri, then say just ralph it.",
          },
        },
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "audit",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });
      await recordExplorerProof(dir);

      await runLoopProcess(dir, "20260527T184210Z", "auditing");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual([
        "subagentFinished",
        "auditStarted",
        "auditPassed",
        "blockerResolved",
        "planningStarted",
        "planningFinished",
        "iterationStarted",
        "iterationFinished",
        "loopFinished",
      ]);
      expect(events[3]).toMatchObject({
        type: "blockerResolved",
        data: { reason: "ambiguousSpecs" },
      });
      expect(status.state).toBe("idle");
      expect(status.blocker).toBeUndefined();
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("auditing runner blocks on ambiguous specs without planning", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"auditor\",\"action\":\"failed\",\"feedback\":\"Deployment target is ambiguous.\",\"ambiguousSpecFiles\":[\".jri/specs/app.md\"],\"questions\":[\"Which host should receive the deployment?\"]}'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "auditing",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "audit",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "auditing");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual(["auditStarted", "auditFailed", "blockerReported"]);
      expect(events[1]).toMatchObject({
        type: "auditFailed",
        data: {
          feedback: "Deployment target is ambiguous.",
          ambiguousSpecFiles: [".jri/specs/app.md"],
          questions: ["Which host should receive the deployment?"],
        },
      });
      expect(events[2]).toMatchObject({
        type: "blockerReported",
        data: {
          reason: "ambiguousSpecs",
          description: "Deployment target is ambiguous.",
          changedFiles: [".jri/specs/app.md"],
          validationRan: false,
          resolutionGuide: {
            summary: "The current specs are not ready for Ralph to build safely.",
            steps: ["Which host should receive the deployment?"],
            resumeInstruction: "Answer the audit questions in bare jri, then say just ralph it.",
          },
        },
      });
      expect(status).toMatchObject({
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        blocker: {
          reason: "ambiguousSpecs",
          description: "Deployment target is ambiguous.",
          resolutionGuide: {
            resumeInstruction: "Answer the audit questions in bare jri, then say just ralph it.",
          },
        },
        lastResult: { outcome: "blocked" },
      });
      expect(status.blocker.resolutionGuide.steps).toEqual(["Which host should receive the deployment?"]);
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner honors graceful stop after planning before building starts", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "echo planning-done",
          "echo '# Plan' > .jri/IMPLEMENTATION_PLAN.md",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"planner\",\"action\":\"planned\",\"planPath\":\".jri/IMPLEMENTATION_PLAN.md\",\"summary\":\"Plan ready.\"}'",
          "bun -e 'const fs = require(\"node:fs\"); const path = \".jri/status.json\"; const status = JSON.parse(fs.readFileSync(path, \"utf8\")); status.stopRequested = true; fs.writeFileSync(path, `${JSON.stringify(status, null, 2)}\\n`);'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "planning",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "plan",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "planning");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual(["planningStarted", "planningFinished", "loopStopped"]);
      expect(events[2]).toMatchObject({ type: "loopStopped", data: { reason: "gracefulStopRequested", nextPhase: "building" } });
      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        stopRequested: false,
        lastResult: { outcome: "stopped", summary: "Graceful stop completed after planning finished." },
      });
      expect(status.process).toBeUndefined();
      expect(status.lock).toBeUndefined();
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner honors graceful stop after the current build iteration", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "echo build-done",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Build done.\"}'",
          "bun -e 'const fs = require(\"node:fs\"); const path = \".jri/status.json\"; const status = JSON.parse(fs.readFileSync(path, \"utf8\")); status.stopRequested = true; fs.writeFileSync(path, `${JSON.stringify(status, null, 2)}\\n`);'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });
      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "iterationFinished", "loopStopped"]);
      expect(events[2]).toMatchObject({ type: "loopStopped", data: { reason: "gracefulStopRequested", nextPhase: "building", iteration: 1 } });
      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        stopRequested: false,
        iteration: 1,
        lastResult: { outcome: "stopped", summary: "Graceful stop completed after iteration 1." },
      });
      expect(status.process).toBeUndefined();
      expect(status.lock).toBeUndefined();
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner preserves validation artifact refs in durable events", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"No changes needed.\",\"validation\":[{\"command\":\"bun run test\",\"exitCode\":0,\"passed\":true,\"summary\":\"Tests passed.\",\"artifacts\":[{\"path\":\".jri/logs/20260527T184210Z/artifacts/test-output.txt\",\"summary\":\"Full test output.\"}]}]}'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const events = await collect(observeLoop(dir));
      expect(events.find((event) => event.type === "validationFinished")).toMatchObject({
        type: "validationFinished",
        data: {
          artifacts: [{ path: ".jri/logs/20260527T184210Z/artifacts/test-output.txt", summary: "Full test output." }],
        },
      });
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner records commits and tags created by a successful build iteration", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      await git(dir, ["init"]);
      await git(dir, ["config", "user.email", "ralph@example.test"]);
      await git(dir, ["config", "user.name", "Ralph"]);
      await writeFile(join(dir, "README.md"), "initial\n", "utf8");
      await git(dir, ["add", "README.md"]);
      await git(dir, ["commit", "-m", "initial"]);

      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "printf 'built\\n' > built.txt",
          "git add built.txt",
          "git commit -m 'build iteration'",
          "git tag 0.0.1",
          "echo build-committed",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Build iteration committed.\",\"validation\":[{\"command\":\"bun run test\",\"exitCode\":0,\"passed\":true,\"summary\":\"Tests passed.\"}]}'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });
      await recordExplorerProof(dir);

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));
      const commit = events.find((event) => event.type === "commitCreated");
      const tag = events.find((event) => event.type === "tagCreated");
      const iterationFinished = events.find((event) => event.type === "iterationFinished");

      expect(events.map((event) => event.type)).toEqual([
        "subagentFinished",
        "iterationStarted",
        "validationStarted",
        "validationFinished",
        "commitCreated",
        "tagCreated",
        "iterationFinished",
        "loopFinished",
      ]);
      expect(events[1]).toMatchObject({ type: "iterationStarted", data: { trackedTreeCleanAtStart: true } });
      expect(commit).toMatchObject({ type: "commitCreated", iteration: 1, data: { subject: "build iteration" } });
      expect(tag).toMatchObject({ type: "tagCreated", iteration: 1, data: { tag: "0.0.1" } });
      expect(iterationFinished).toMatchObject({
        type: "iterationFinished",
        data: { outcome: "committed", tag: "0.0.1" },
      });
      expect(status).toMatchObject({
        state: "idle",
        activeLoopId: null,
        iterations: 1,
        lastResult: { outcome: "completed", validationPassed: true, tag: "0.0.1", explorer: { used: true } },
      });
      expect(status.lastResult.commit).toBe(commit?.data.sha);
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner fails successful git-changing handoffs with multiple commits in one iteration", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      await git(dir, ["init"]);
      await git(dir, ["config", "user.email", "ralph@example.test"]);
      await git(dir, ["config", "user.name", "Ralph"]);
      await writeFile(join(dir, "README.md"), "initial\n", "utf8");
      await git(dir, ["add", "README.md"]);
      await git(dir, ["commit", "-m", "initial"]);

      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "printf 'one\\n' > one.txt",
          "git add one.txt",
          "git commit -m 'first build commit'",
          "printf 'two\\n' > two.txt",
          "git add two.txt",
          "git commit -m 'second build commit'",
          "git tag 0.0.1",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Build iteration committed.\",\"validation\":[{\"command\":\"bun run test\",\"exitCode\":0,\"passed\":true,\"summary\":\"Tests passed.\"}]}'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });
      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.some((event) => event.type === "commitCreated")).toBe(false);
      expect(events.some((event) => event.type === "tagCreated")).toBe(false);
      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "validationStarted", "validationFinished", "iterationFinished", "loopFinished"]);
      expect(status).toMatchObject({
        state: "stopped",
        lastResult: { outcome: "failed", validationPassed: true },
      });
      expect(status.lastResult.summary).toContain("created 2 commits during one iteration");
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner fails successful git-changing handoffs without the expected semantic-version tag", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      await git(dir, ["init"]);
      await git(dir, ["config", "user.email", "ralph@example.test"]);
      await git(dir, ["config", "user.name", "Ralph"]);
      await writeFile(join(dir, "README.md"), "initial\n", "utf8");
      await git(dir, ["add", "README.md"]);
      await git(dir, ["commit", "-m", "initial"]);

      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "printf 'built\\n' > built.txt",
          "git add built.txt",
          "git commit -m 'build iteration'",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Build iteration committed.\",\"validation\":[{\"command\":\"bun run test\",\"exitCode\":0,\"passed\":true,\"summary\":\"Tests passed.\"}]}'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.some((event) => event.type === "tagCreated")).toBe(false);
      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "validationStarted", "validationFinished", "commitCreated", "iterationFinished", "loopFinished"]);
      expect(events[4]).toMatchObject({
        type: "iterationFinished",
        data: { outcome: "validationFailed" },
      });
      expect(status).toMatchObject({
        state: "stopped",
        lastResult: { outcome: "failed", validationPassed: true },
      });
      expect(status.lastResult.summary).toContain("without creating expected semantic-version tag 0.0.1");
      expect(status.lastResult.tag).toBeUndefined();
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner fails successful git-changing handoffs with ambiguous new semantic-version tags", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      await git(dir, ["init"]);
      await git(dir, ["config", "user.email", "ralph@example.test"]);
      await git(dir, ["config", "user.name", "Ralph"]);
      await writeFile(join(dir, "README.md"), "initial\n", "utf8");
      await git(dir, ["add", "README.md"]);
      await git(dir, ["commit", "-m", "initial"]);

      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "printf 'built\\n' > built.txt",
          "git add built.txt",
          "git commit -m 'build iteration'",
          "git tag 0.0.1",
          "git tag 0.0.2",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Build iteration committed.\",\"validation\":[{\"command\":\"bun run test\",\"exitCode\":0,\"passed\":true,\"summary\":\"Tests passed.\"}]}'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.some((event) => event.type === "tagCreated")).toBe(false);
      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "validationStarted", "validationFinished", "commitCreated", "iterationFinished", "loopFinished"]);
      expect(status).toMatchObject({
        state: "stopped",
        lastResult: { outcome: "failed", validationPassed: true },
      });
      expect(status.lastResult.summary).toContain("multiple new semantic-version tags");
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner fails successful git-changing handoffs when the expected tag points at the wrong commit", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      await git(dir, ["init"]);
      await git(dir, ["config", "user.email", "ralph@example.test"]);
      await git(dir, ["config", "user.name", "Ralph"]);
      await writeFile(join(dir, "README.md"), "initial\n", "utf8");
      await git(dir, ["add", "README.md"]);
      await git(dir, ["commit", "-m", "initial"]);

      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "printf 'built\\n' > built.txt",
          "git add built.txt",
          "git commit -m 'build iteration'",
          "git tag 0.0.1 HEAD~1",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Build iteration committed.\",\"validation\":[{\"command\":\"bun run test\",\"exitCode\":0,\"passed\":true,\"summary\":\"Tests passed.\"}]}'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.some((event) => event.type === "tagCreated")).toBe(false);
      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "validationStarted", "validationFinished", "commitCreated", "iterationFinished", "loopFinished"]);
      expect(status).toMatchObject({
        state: "stopped",
        lastResult: { outcome: "failed", validationPassed: true },
      });
      expect(status.lastResult.summary).toContain("does not point at the iteration commit");
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner fails successful handoffs that mutate tags without an iteration commit", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      await git(dir, ["init"]);
      await git(dir, ["config", "user.email", "ralph@example.test"]);
      await git(dir, ["config", "user.name", "Ralph"]);
      await writeFile(join(dir, "README.md"), "initial\n", "utf8");
      await git(dir, ["add", "README.md"]);
      await git(dir, ["commit", "-m", "initial"]);

      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "git tag 0.0.1",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Tagged without commit.\",\"validation\":[{\"command\":\"bun run test\",\"exitCode\":0,\"passed\":true,\"summary\":\"Tests passed.\"}]}'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.some((event) => event.type === "commitCreated")).toBe(false);
      expect(events.some((event) => event.type === "tagCreated")).toBe(false);
      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "validationStarted", "validationFinished", "iterationFinished", "loopFinished"]);
      expect(status).toMatchObject({
        state: "stopped",
        lastResult: { outcome: "failed", validationPassed: true },
      });
      expect(status.lastResult.summary).toContain("changed git tags without creating an iteration commit");
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner fails successful handoffs that leave tracked changes without an iteration commit", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      await git(dir, ["init"]);
      await git(dir, ["config", "user.email", "ralph@example.test"]);
      await git(dir, ["config", "user.name", "Ralph"]);
      await writeFile(join(dir, "README.md"), "initial\n", "utf8");
      await git(dir, ["add", "README.md"]);
      await git(dir, ["commit", "-m", "initial"]);

      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "printf 'dirty\\n' > README.md",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Changed without commit.\",\"validation\":[{\"command\":\"bun run test\",\"exitCode\":0,\"passed\":true,\"summary\":\"Tests passed.\"}]}'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.some((event) => event.type === "commitCreated")).toBe(false);
      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "validationStarted", "validationFinished", "iterationFinished", "loopFinished"]);
      expect(status).toMatchObject({
        state: "stopped",
        lastResult: { outcome: "failed", validationPassed: true },
      });
      expect(status.lastResult.summary).toContain("tracked working-tree changes without creating an iteration commit");
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner fails git-changing successful handoffs without passing validation evidence", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      await git(dir, ["init"]);
      await git(dir, ["config", "user.email", "ralph@example.test"]);
      await git(dir, ["config", "user.name", "Ralph"]);
      await writeFile(join(dir, "README.md"), "initial\n", "utf8");
      await git(dir, ["add", "README.md"]);
      await git(dir, ["commit", "-m", "initial"]);

      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "printf 'built\\n' > built.txt",
          "git add built.txt",
          "git commit -m 'build iteration'",
          "git tag 0.0.1",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Build iteration committed without validation.\"}'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.some((event) => event.type === "commitCreated")).toBe(false);
      expect(events.some((event) => event.type === "tagCreated")).toBe(false);
      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "iterationFinished", "loopFinished"]);
      expect(status).toMatchObject({
        state: "stopped",
        lastResult: {
          outcome: "failed",
          validationPassed: false,
        },
      });
      expect(status.lastResult.summary).toContain("without passing validation evidence");
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner fails successful handoffs with failed validation evidence even without git changes", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Claimed success despite failed validation.\",\"validation\":[{\"command\":\"bun run test\",\"exitCode\":1,\"passed\":false,\"summary\":\"Tests failed.\"}]}'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "validationStarted", "validationFinished", "iterationFinished", "loopFinished"]);
      expect(events[3]).toMatchObject({ type: "iterationFinished", data: { outcome: "validationFailed" } });
      expect(events[4]).toMatchObject({ type: "loopFinished", data: { outcome: "failed" } });
      expect(status).toMatchObject({
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastResult: {
          outcome: "failed",
          validationPassed: false,
        },
      });
      expect(status.lastResult.summary).toContain("successful handoff with failed validation evidence");
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner fails failed-validation handoffs that committed git changes", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      await git(dir, ["init"]);
      await git(dir, ["config", "user.email", "ralph@example.test"]);
      await git(dir, ["config", "user.name", "Ralph"]);
      await writeFile(join(dir, "README.md"), "initial\n", "utf8");
      await git(dir, ["add", "README.md"]);
      await git(dir, ["commit", "-m", "initial"]);

      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "printf 'broken\\n' > broken.txt",
          "git add broken.txt",
          "git commit -m 'unexpected validation commit'",
          "git tag 0.0.1",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"failedValidation\",\"summary\":\"Tests failed.\",\"validation\":{\"command\":\"bun run test\",\"exitCode\":1,\"passed\":false,\"summary\":\"Unit tests failed.\"}}'",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.some((event) => event.type === "commitCreated")).toBe(false);
      expect(events.some((event) => event.type === "tagCreated")).toBe(false);
      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "validationStarted", "validationFinished", "iterationFinished", "loopFinished"]);
      expect(events[3]).toMatchObject({ type: "iterationFinished", data: { outcome: "validationFailed" } });
      expect(status).toMatchObject({
        state: "stopped",
        lastResult: {
          outcome: "failed",
        },
      });
      expect(status.lastResult.summary).toContain("git commits or tags changed");
      expect(status.lastResult.validationPassed).toBe(false);
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner records a builder blocker and leaves changed files uncommitted", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      await git(dir, ["init"]);
      await git(dir, ["config", "user.email", "ralph@example.test"]);
      await git(dir, ["config", "user.name", "Ralph"]);
      await writeFile(join(dir, "README.md"), "initial\n", "utf8");
      await git(dir, ["add", "README.md"]);
      await git(dir, ["commit", "-m", "initial"]);

      const fakePi = join(dir, "fake-pi.sh");
      const blocker = JSON.stringify({
        agent: "builder",
        action: "blocked",
        blocker: {
        reason: "ambiguousSpecs",
        description: "The deployment target is unclear.",
        resolutionGuide: {
          summary: "Clarify the deployment target.",
          steps: ["Ask which host should receive the app."],
          resumeInstruction: "Answer the question in bare jri, then say just ralph it.",
        },
        changedFiles: ["partial.txt"],
        validationRan: false,
        },
      });
      await writeFile(
        fakePi,
        ["#!/bin/sh", "printf 'partial\\n' > partial.txt", `echo 'JRI_HANDOFF_JSON: ${blocker}'`, ""].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });
      await mkdir(join(dir, ".jri", "logs", "20260527T184210Z"), { recursive: true });
      await writeFile(
        join(dir, ".jri", "logs", "20260527T184210Z", "stdout.log"),
        [
          'JRI_HANDOFF_JSON: {"agent":"builder","action":"complete","summary":"old"}',
          "",
        ].join("\n"),
        "utf8",
      );
      const headBefore = await gitOutput(dir, ["rev-parse", "--verify", "HEAD"]);

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));
      const head = await gitOutput(dir, ["rev-parse", "--verify", "HEAD"]);

      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "blockerReported", "iterationFinished"]);
      const blockerEvent = events[1];
      if (blockerEvent?.type !== "blockerReported") throw new Error("Expected blockerReported event.");
      expect(blockerEvent).toMatchObject({
        type: "blockerReported",
        data: {
          reason: "ambiguousSpecs",
          description: "The deployment target is unclear.",
          validationRan: false,
        },
      });
      expect(blockerEvent.data.changedFiles).toContain("partial.txt");
      const iterationFinished = events[2];
      if (iterationFinished?.type !== "iterationFinished") throw new Error("Expected iterationFinished event.");
      expect(iterationFinished).toMatchObject({ type: "iterationFinished", data: { outcome: "blocked" } });
      expect(iterationFinished.data.changedFiles).toContain("partial.txt");
      expect(status).toMatchObject({
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        blocker: {
          reason: "ambiguousSpecs",
          description: "The deployment target is unclear.",
        },
        lastResult: { outcome: "blocked", summary: "The deployment target is unclear." },
      });
      expect(status.blocker.changedFiles).toContain("partial.txt");
      expect(status.process).toBeUndefined();
      expect(status.lock).toBeUndefined();
      expect(head.trim()).toBe(headBefore.trim());
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("runner regenerates the plan when builder requests replan", async () => {
    const dir = await tempProject();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      const fakePi = join(dir, "fake-pi.sh");
      await writeFile(
        fakePi,
        [
          "#!/bin/sh",
          "count_file=.jri/fake-pi-count",
          "count=0",
          "[ -f \"$count_file\" ] && count=$(cat \"$count_file\")",
          "count=$((count + 1))",
          "printf '%s' \"$count\" > \"$count_file\"",
          "if [ \"$count\" = 1 ]; then",
          "  echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"needsReplan\",\"reason\":\"plan is stale\"}'",
          "elif [ \"$count\" = 2 ]; then",
          "  echo 'planner-regenerated'",
          "  echo '# Plan regenerated' > .jri/IMPLEMENTATION_PLAN.md",
          "  echo 'JRI_HANDOFF_JSON: {\"agent\":\"planner\",\"action\":\"planned\",\"planPath\":\".jri/IMPLEMENTATION_PLAN.md\",\"summary\":\"Plan regenerated.\"}'",
          "else",
          "  echo 'builder-finished'",
          "  echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Builder finished.\"}'",
          "fi",
          "",
        ].join("\n"),
        "utf8",
      );
      await chmod(fakePi, 0o755);
      process.env.JRI_PI_COMMAND = fakePi;
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: {
          owner: "daemon",
          pid: process.pid,
          operation: "build",
          acquiredAt: "2026-05-27T19:00:00.000Z",
          heartbeatAt: "2026-05-27T19:00:00.000Z",
          expiresAt: "2026-05-27T19:01:00.000Z",
        },
      });
      await recordExplorerProof(dir);

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual([
        "subagentFinished",
        "iterationStarted",
        "iterationFinished",
        "planRegenerationRequested",
        "planRegenerationStarted",
        "planRegenerationFinished",
        "iterationStarted",
        "iterationFinished",
        "loopFinished",
      ]);
      expect(status).toMatchObject({
        state: "idle",
        activeLoopId: null,
        iterations: 2,
        lastResult: { outcome: "completed", explorer: { used: true } },
      });
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
      await rm(dir, { recursive: true, force: true });
    }
  });
});

async function collect<T>(iterable: AsyncIterable<T>): Promise<T[]> {
  const items: T[] = [];
  for await (const item of iterable) items.push(item);
  return items;
}

async function recordExplorerProof(dir: string): Promise<void> {
  await appendLoopEvent(dir, {
    type: "subagentFinished",
    loopId: "20260527T184210Z",
    data: {
      agent: "explorer",
      summary: "Explorer found the relevant code path.",
      artifactRef: ".jri/logs/20260527T184210Z/artifacts/explorer-proof.txt",
    },
  });
}

function activeTestLock(operation: "audit" | "plan" | "build", pid = process.pid) {
  return {
    owner: "daemon" as const,
    pid,
    operation,
    acquiredAt: "2026-05-27T18:42:10.000Z",
    heartbeatAt: "2026-05-27T18:42:10.000Z",
    expiresAt: "2999-01-01T00:00:00.000Z",
  };
}

function specsFingerprintForFiles(files: Record<string, string>): string {
  const hash = createHash("sha256");
  for (const name of Object.keys(files).sort()) {
    const contents = files[name] ?? "";
    const bytes = Buffer.from(contents, "utf8");
    hash.update(`.jri/specs/${name}`);
    hash.update("\0");
    hash.update(String(bytes.byteLength));
    hash.update("\0");
    hash.update(bytes);
    hash.update("\n");
  }
  return hash.digest("hex");
}

function legacySpecsFingerprintForFiles(files: Record<string, string>): string {
  const hash = createHash("sha256");
  for (const name of Object.keys(files).sort()) {
    hash.update(name);
    hash.update("\0");
    hash.update(files[name] ?? "");
    hash.update("\0");
  }
  return hash.digest("hex");
}

async function git(cwd: string, args: string[]): Promise<void> {
  const proc = Bun.spawn(["git", ...args], {
    cwd,
    stdout: "ignore",
    stderr: "pipe",
    stdin: "ignore",
  });
  const stderr = await new Response(proc.stderr).text();
  const exitCode = await proc.exited;
  if (exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${stderr}`);
}

async function gitOutput(cwd: string, args: string[]): Promise<string> {
  const proc = Bun.spawn(["git", ...args], {
    cwd,
    stdout: "pipe",
    stderr: "pipe",
    stdin: "ignore",
  });
  const [stdout, stderr] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text()]);
  const exitCode = await proc.exited;
  if (exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${stderr}`);
  return stdout;
}
