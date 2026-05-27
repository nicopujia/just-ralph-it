import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import { defaultStatus } from "../src/core/schema";
import type { ProjectStatus } from "../src/core/types";
import {
  acquireLock,
  appendLoopEvent,
  generateLoopId,
  heartbeatLock,
  nextEventSequence,
  releaseLock,
  transitionStatus,
  writeStatusAtomic,
} from "../src/core/runtime-state";

async function tempInitializedProject(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "jri-runtime-test-"));
  await mkdir(join(dir, ".jri", "logs"), { recursive: true });
  await writeStatusAtomic(dir, defaultStatus(dir));
  return dir;
}

describe("runtime state primitives", () => {
  test("transitionStatus enforces legal state transitions and loop id requirements", async () => {
    const dir = await tempInitializedProject();
    try {
      await expect(transitionStatus(dir, "building", { loopId: "20260527T184210Z" })).rejects.toThrow("Illegal JRI status transition");

      const auditing = await transitionStatus(dir, "auditing", { loopId: "20260527T184210Z" });
      expect(auditing.state).toBe("auditing");
      expect(auditing.activeLoopId).toBe("20260527T184210Z");
      expect(auditing.lastLoopId).toBe("20260527T184210Z");

      const planning = await transitionStatus(dir, "planning");
      expect(planning.state).toBe("planning");
      expect(planning.activeLoopId).toBe("20260527T184210Z");

      await expect(transitionStatus(dir, "idle")).rejects.toThrow("Illegal JRI status transition");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("blocked transition rules depend on blocker reason", async () => {
    const dir = await tempInitializedProject();
    try {
      const base: ProjectStatus = {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        blocker: {
          reason: "ambiguousSpecs",
          description: "Need clearer requirements.",
          resolutionGuide: {
            summary: "Clarify the scope.",
            steps: ["Update specs."],
            resumeInstruction: "Say just ralph it again.",
          },
        },
      };
      await writeStatusAtomic(dir, base);
      await expect(transitionStatus(dir, "building")).rejects.toThrow("Illegal JRI status transition");
      await expect(transitionStatus(dir, "auditing")).resolves.toMatchObject({ state: "auditing" });

      await writeStatusAtomic(dir, {
        ...base,
        blocker: { ...base.blocker!, reason: "needsHumanTask" },
      });
      await expect(transitionStatus(dir, "auditing")).rejects.toThrow("Illegal JRI status transition");
      await expect(transitionStatus(dir, "building")).resolves.toMatchObject({ state: "building" });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("lock acquire, heartbeat, and release update status atomically", async () => {
    const dir = await tempInitializedProject();
    try {
      const acquired = await acquireLock(dir, "build", {
        pid: 123,
        now: new Date("2026-05-27T18:42:10.000Z"),
        ttlMs: 10_000,
        isProcessAlive: () => true,
      });
      expect(acquired).toMatchObject({
        owner: "daemon",
        pid: 123,
        operation: "build",
        heartbeatAt: "2026-05-27T18:42:10.000Z",
        expiresAt: "2026-05-27T18:42:20.000Z",
      });

      await expect(
        acquireLock(dir, "halt", {
          pid: 456,
          now: new Date("2026-05-27T18:42:11.000Z"),
          isProcessAlive: () => true,
        }),
      ).rejects.toThrow("already running build");

      const heartbeat = await heartbeatLock(dir, acquired, {
        now: new Date("2026-05-27T18:42:15.000Z"),
        ttlMs: 20_000,
      });
      expect(heartbeat?.heartbeatAt).toBe("2026-05-27T18:42:15.000Z");
      expect(heartbeat?.expiresAt).toBe("2026-05-27T18:42:35.000Z");

      await releaseLock(dir, heartbeat!);
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status.lock).toBeUndefined();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("stale locks can be replaced only after expiry and dead process check", async () => {
    const dir = await tempInitializedProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        lock: {
          owner: "daemon",
          pid: 123,
          operation: "build",
          acquiredAt: "2026-05-27T18:41:00.000Z",
          heartbeatAt: "2026-05-27T18:41:00.000Z",
          expiresAt: "2026-05-27T18:41:30.000Z",
        },
      });

      const replacement = await acquireLock(dir, "resume", {
        pid: 456,
        now: new Date("2026-05-27T18:42:00.000Z"),
        isProcessAlive: () => false,
      });
      expect(replacement.operation).toBe("resume");
      expect(replacement.pid).toBe(456);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("loop ids use UTC timestamp slugs and avoid existing log directories", async () => {
    const dir = await tempInitializedProject();
    try {
      await mkdir(join(dir, ".jri", "logs", "20260527T184210Z"), { recursive: true });
      await mkdir(join(dir, ".jri", "logs", "20260527T184210Z-2"), { recursive: true });

      await expect(generateLoopId(dir, new Date("2026-05-27T18:42:10.123Z"))).resolves.toBe("20260527T184210Z-3");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("appendLoopEvent allocates monotonically across loop and interrogation logs", async () => {
    const dir = await tempInitializedProject();
    try {
      await mkdir(join(dir, ".jri", "logs", "20260527T184210Z"), { recursive: true });
      await writeFile(join(dir, ".jri", "logs", "interrogation.jsonl"), `${JSON.stringify({ sequence: 41, type: "chatTurnRecorded" })}\n`);
      await writeFile(join(dir, ".jri", "logs", "20260527T184210Z", "events.jsonl"), `${JSON.stringify({ sequence: 42, type: "loopStarted" })}\n`);

      expect(await nextEventSequence(dir)).toBe(43);
      const event = await appendLoopEvent(dir, {
        type: "auditStarted",
        loopId: "20260527T184210Z",
        data: {},
      });

      expect(event.sequence).toBe(43);
      const persisted = await readFile(join(dir, ".jri", "logs", "20260527T184210Z", "events.jsonl"), "utf8");
      expect(persisted).toContain('"sequence":43');
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("concurrent event appends allocate unique monotonic sequences", async () => {
    const dir = await tempInitializedProject();
    try {
      await mkdir(join(dir, ".jri", "logs", "20260527T184210Z"), { recursive: true });

      const events = await Promise.all(
        Array.from({ length: 25 }, (_, index) =>
          appendLoopEvent(dir, {
            type: "validationStarted",
            loopId: "20260527T184210Z",
            iteration: 1,
            data: { command: `command-${index}` },
          }),
        ),
      );

      expect(new Set(events.map((event) => event.sequence)).size).toBe(25);
      expect([...events.map((event) => event.sequence)].sort((left, right) => left - right)).toEqual(
        Array.from({ length: 25 }, (_, index) => index + 1),
      );

      const persisted = await readFile(join(dir, ".jri", "logs", "20260527T184210Z", "events.jsonl"), "utf8");
      const persistedSequences = persisted
        .trim()
        .split("\n")
        .map((line) => JSON.parse(line) as { sequence: number })
        .map((event) => event.sequence);
      expect(persistedSequences).toEqual(Array.from({ length: 25 }, (_, index) => index + 1));
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
