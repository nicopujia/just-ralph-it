import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import { normalizeStartTrigger, sendChat } from "../src/core/chat";
import { open } from "../src/core";
import { startDaemonServer, type DaemonPaths } from "../src/core/daemon-ipc";
import { defaultStatus } from "../src/core/schema";
import { writeStatusAtomic } from "../src/core/runtime-state";
import type { CoreEvent } from "../src/core";

async function tempProject(): Promise<string> {
  return await mkdtemp(join(tmpdir(), "jri-chat-test-"));
}

function tempDaemonPaths(dir: string): DaemonPaths {
  return {
    runtimeDir: join(dir, "runtime"),
    stateDir: join(dir, "state"),
    socketPath: process.platform === "win32" ? `\\\\.\\pipe\\jri-chat-test-${crypto.randomUUID()}` : join(dir, "runtime", "daemon.sock"),
    registryPath: join(dir, "state", "daemon-registry.json"),
  };
}

describe("interrogation chat", () => {
  test("records user and assistant turns in interrogation history", async () => {
    const dir = await tempProject();
    try {
      const project = await open(dir);
      const events = await collect(project.chat.send({ message: "We need a CLI." }));

      expect(events.map((event) => event.type)).toEqual([
        "chatTurnRecorded",
        "chatMessageStarted",
        "chatMessageDelta",
        "chatMessageFinished",
        "chatTurnRecorded",
      ]);
      expect(events[2]).toMatchObject({ type: "chatMessageDelta", data: { text: expect.stringContaining("recorded") } });

      const log = await readJsonl(join(dir, ".jri", "logs", "interrogation.jsonl"));
      expect(log).toHaveLength(5);
      expect(log[0]).toMatchObject({ type: "chatTurnRecorded", data: { role: "user", content: "We need a CLI." } });
      expect(log[4]).toMatchObject({ type: "chatTurnRecorded", data: { role: "assistant" } });
      expect(log.map((event) => event.sequence)).toEqual([1, 2, 3, 4, 5]);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("normalizes only standalone start triggers", () => {
    expect(normalizeStartTrigger("just ralph it")).toBe("just ralph it");
    expect(normalizeStartTrigger("Just Ralph It!")).toBe("just ralph it");
    expect(normalizeStartTrigger("ralfealo.")).toBe("ralfealo");
    expect(normalizeStartTrigger("please just ralph it")).toBeNull();
    expect(normalizeStartTrigger("just ralph it now")).toBeNull();
  });

  test("standalone start trigger enters auditing and starts a controlled runner", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));

      const events = await collect(
        sendChat(
          dir,
          { message: "just ralph it" },
          {
            now: new Date("2026-05-27T20:00:00.000Z"),
            spawnRunner: ({ phase }) => ({ pid: 33333, command: `runner ${phase}` }),
          },
        ),
      );
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(events.map((event) => event.type)).toEqual([
        "chatTurnRecorded",
        "chatMessageStarted",
        "chatMessageDelta",
        "chatMessageFinished",
        "chatTurnRecorded",
        "loopStarted",
      ]);
      expect(events[5]).toMatchObject({ type: "loopStarted", data: { pid: 33333 } });
      expect(status).toMatchObject({
        state: "auditing",
        activeLoopId: "20260527T200000Z",
        process: { pid: 33333, command: "runner auditing" },
        lock: { operation: "audit", pid: 33333 },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("standalone start trigger can stream an injected daemon-owned start event", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));

      const events = await collect(
        sendChat(dir, { message: "ralfealo" }, {
          startLoop: async function* (_projectDir, trigger) {
            expect(trigger).toBe("ralfealo");
            yield {
              id: "event-1",
              sequence: 1,
              timestamp: "2026-05-27T20:00:00.000Z",
              type: "loopStarted",
              loopId: "20260527T200000Z",
              data: { projectDir: dir, pid: 44444 },
            };
          },
        }),
      );

      expect(events.map((event) => event.type)).toEqual([
        "chatTurnRecorded",
        "chatMessageStarted",
        "chatMessageDelta",
        "chatMessageFinished",
        "chatTurnRecorded",
        "loopStarted",
      ]);
      expect(events[5]).toMatchObject({ type: "loopStarted", data: { pid: 44444 } });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("project chat starts accepted triggers through daemon IPC and updates the registry", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const previousEnv = captureDaemonEnv();
    const daemon = await startDaemonServer({
      paths,
      idleTimeoutMs: 10_000,
      runtimeOptions: {
        now: new Date("2026-05-27T20:00:00.000Z"),
        spawnRunner: ({ phase }) => ({ pid: process.pid, command: `runner ${phase}` }),
      },
    });
    try {
      applyDaemonEnv(paths);
      const project = await open(dir);
      const events = await collect(project.chat.send({ message: "just ralph it" }));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const registry = JSON.parse(await readFile(paths.registryPath, "utf8"));
      const loopEvents = await readJsonl(join(dir, ".jri", "logs", "20260527T200000Z", "events.jsonl"));

      expect(events.map((event) => event.type)).toEqual([
        "chatTurnRecorded",
        "chatMessageStarted",
        "chatMessageDelta",
        "chatMessageFinished",
        "chatTurnRecorded",
        "loopStarted",
      ]);
      expect(events[5]).toMatchObject({ type: "loopStarted", loopId: "20260527T200000Z", data: { pid: process.pid } });
      expect(status).toMatchObject({
        state: "auditing",
        activeLoopId: "20260527T200000Z",
        process: { pid: process.pid, command: "runner auditing" },
      });
      expect(registry.projects[0]).toMatchObject({ projectDir: dir, activeLoopId: "20260527T200000Z" });
      expect(loopEvents[0]).toMatchObject({ type: "loopStarted", loopId: "20260527T200000Z" });
    } finally {
      restoreDaemonEnv(previousEnv);
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("done verifies an existing needs-human-task blocker only after verifier approval", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        blocker: {
          reason: "needsHumanTask",
          description: "Deployment credentials are missing.",
          resolutionGuide: {
            summary: "Credentials are required.",
            steps: ["Set the deployment token."],
            resumeInstruction: "Say done in bare jri after the token is available.",
          },
        },
      });

      const events = await collect(
        sendChat(
          dir,
          { message: "done" },
          {
            verifyHumanTask: () => ({
              agent: "verifier",
              action: "verified",
              verificationSummary: "Deployment token is present.",
            }),
          },
        ),
      );
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const loopEvents = await readJsonl(join(dir, ".jri", "logs", "20260527T184210Z", "events.jsonl"));

      expect(events.some((event) => event.type === "blockerResolved")).toBe(true);
      expect(status.blocker.resolution).toMatchObject({ status: "verified", verificationSummary: "Deployment token is present." });
      expect(loopEvents[0]).toMatchObject({ type: "blockerResolved", data: { reason: "needsHumanTask" } });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("done preserves a needs-human-task blocker when verification is inconclusive", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        blocker: {
          reason: "needsHumanTask",
          description: "Deployment credentials are missing.",
          resolutionGuide: {
            summary: "Credentials are required.",
            steps: ["Set the deployment token."],
            resumeInstruction: "Say done in bare jri after the token is available.",
          },
        },
      });

      const project = await open(dir);
      const events = await collect(project.chat.send({ message: "done" }));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(events.some((event) => event.type === "blockerResolved")).toBe(false);
      expect(status).toMatchObject({
        state: "blocked",
        blocker: {
          reason: "needsHumanTask",
          description: "Deployment credentials are missing.",
        },
      });
      expect(status.blocker.resolution).toBeUndefined();
      expect(status.blocker.resolutionGuide.summary).toContain("needs a verifier");
      expect(events.find((event) => event.type === "chatMessageDelta")).toMatchObject({
        data: { text: expect.stringContaining("remains blocked") },
      });
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

async function readJsonl(path: string): Promise<CoreEvent[]> {
  return (await readFile(path, "utf8"))
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line) as CoreEvent);
}

function captureDaemonEnv(): Record<string, string | undefined> {
  return {
    JRI_DAEMON_RUNTIME_DIR: process.env.JRI_DAEMON_RUNTIME_DIR,
    JRI_DAEMON_STATE_DIR: process.env.JRI_DAEMON_STATE_DIR,
    JRI_DAEMON_SOCKET_PATH: process.env.JRI_DAEMON_SOCKET_PATH,
    JRI_DAEMON_REGISTRY_PATH: process.env.JRI_DAEMON_REGISTRY_PATH,
  };
}

function applyDaemonEnv(paths: DaemonPaths): void {
  process.env.JRI_DAEMON_RUNTIME_DIR = paths.runtimeDir;
  process.env.JRI_DAEMON_STATE_DIR = paths.stateDir;
  process.env.JRI_DAEMON_SOCKET_PATH = paths.socketPath;
  process.env.JRI_DAEMON_REGISTRY_PATH = paths.registryPath;
}

function restoreDaemonEnv(previous: Record<string, string | undefined>): void {
  for (const [key, value] of Object.entries(previous)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
}
