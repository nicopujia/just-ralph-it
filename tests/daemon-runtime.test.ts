import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import { getRecoveredStatus, haltLoop, observeLoop, requestGracefulStop } from "../src/core/daemon-runtime";
import { appendLoopEvent, writeStatusAtomic } from "../src/core/runtime-state";
import { defaultStatus } from "../src/core/schema";

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

  test("observe replays persisted loop events in sequence", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
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

  test("requestGracefulStop toggles active loop stop state and logs each request", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "planning",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
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
    const killed: number[] = [];
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        stopRequested: true,
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
          killProcess: (pid) => killed.push(pid),
        }),
      );
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(killed).toEqual([67890]);
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
});

async function collect<T>(iterable: AsyncIterable<T>): Promise<T[]> {
  const items: T[] = [];
  for await (const item of iterable) items.push(item);
  return items;
}
