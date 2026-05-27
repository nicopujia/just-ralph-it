import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import {
  daemonHaltLoop,
  daemonObserveLoop,
  daemonRequestStop,
  daemonStartLoop,
  daemonStatus,
  startDaemonServer,
  type DaemonPaths,
} from "../src/core/daemon-ipc";
import { appendLoopEvent, writeStatusAtomic } from "../src/core/runtime-state";
import { defaultStatus } from "../src/core/schema";
import { fingerprintSpecFile, writeInterrogationState } from "../src/core/interrogation-state";

async function tempProject(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "jri-daemon-ipc-test-"));
  await mkdir(join(dir, ".jri", "logs"), { recursive: true });
  await writeStatusAtomic(dir, defaultStatus(dir));
  return dir;
}

function tempDaemonPaths(dir: string): DaemonPaths {
  return {
    runtimeDir: join(dir, "runtime"),
    stateDir: join(dir, "state"),
    socketPath: process.platform === "win32" ? `\\\\.\\pipe\\jri-test-${crypto.randomUUID()}` : join(dir, "runtime", "daemon.sock"),
    registryPath: join(dir, "state", "daemon-registry.json"),
  };
}

describe("daemon IPC", () => {
  test("serves status requests and records the project registry", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    try {
      const status = await daemonStatus(dir, { paths });
      expect(status).toMatchObject({ state: "idle", activeLoopId: null });

      const registry = JSON.parse(await readFile(paths.registryPath, "utf8"));
      expect(registry).toMatchObject({
        protocolVersion: 1,
        projects: [{ projectDir: dir, activeLoopId: null }],
      });
    } finally {
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("routes loop controls and event streams over JSON socket IPC", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "planning",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
      });
      await appendLoopEvent(dir, {
        type: "loopStarted",
        loopId: "20260527T184210Z",
        data: { projectDir: dir },
      });
      await writeFile(join(dir, ".jri", "logs", "20260527T184210Z", "stdout.log"), "agent output\n", "utf8");

      await daemonRequestStop(dir, { paths });
      const stopped = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(stopped.stopRequested).toBe(true);

      const events = await collect(daemonObserveLoop(dir, { paths, includeStdout: true }));
      expect(events.map((event) => event.type)).toEqual(["loopOutput", "loopStarted", "stopRequested"]);
      expect(events[0]).toMatchObject({ type: "loopOutput", data: { text: "agent output\n", replayed: true } });
    } finally {
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("routes daemon-owned loop start over streaming IPC", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({
      paths,
      idleTimeoutMs: 10_000,
      runtimeOptions: {
        now: new Date("2026-05-27T20:00:00.000Z"),
        spawnRunner: ({ phase }) => ({ pid: process.pid, command: `runner ${phase}` }),
      },
    });
    try {
      const events = await collect(daemonStartLoop(dir, "just ralph it", { paths }));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const registry = JSON.parse(await readFile(paths.registryPath, "utf8"));

      expect(events).toHaveLength(1);
      expect(events[0]).toMatchObject({
        type: "loopStarted",
        loopId: "20260527T200000Z",
        data: { projectDir: dir, pid: process.pid },
      });
      expect(status).toMatchObject({
        state: "auditing",
        activeLoopId: "20260527T200000Z",
        process: { pid: process.pid, command: "runner auditing" },
        lock: { owner: "daemon", pid: process.pid, operation: "audit" },
      });
      expect(registry.projects[0]).toMatchObject({
        projectDir: dir,
        activeLoopId: "20260527T200000Z",
      });
    } finally {
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("rejects loop start requests without a standalone accepted trigger", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    try {
      await expect(collect(daemonStartLoop(dir, "please just ralph it" as "just ralph it", { paths }))).rejects.toThrow("invalid start trigger");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status).toMatchObject({ state: "idle", activeLoopId: null });
    } finally {
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("rejects loop start while sealed specs need reconciliation", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI.\n", "utf8");
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
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI and web UI.\n", "utf8");

      await expect(collect(daemonStartLoop(dir, "just ralph it", { paths }))).rejects.toThrow("spec reconciliation is pending");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const interrogationState = JSON.parse(await readFile(join(dir, ".jri", "interrogation-state.json"), "utf8"));
      expect(status).toMatchObject({ state: "idle", activeLoopId: null });
      expect(interrogationState.topics.app.pendingReconciliation).toMatchObject({ reason: "manualSpecEdit" });
    } finally {
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("rejects loop start while a loop is already active", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
      });

      await expect(collect(daemonStartLoop(dir, "just ralph it", { paths }))).rejects.toThrow("while JRI is building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status).toMatchObject({ state: "building", activeLoopId: "20260527T184210Z" });
    } finally {
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("rejects loop start while a human-task blocker is unresolved", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    try {
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

      await expect(collect(daemonStartLoop(dir, "just ralph it", { paths }))).rejects.toThrow("human-task blocker is unresolved");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status).toMatchObject({ state: "blocked", activeLoopId: "20260527T184210Z" });
    } finally {
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("rejects loop start for verified human-task blockers with resume guidance", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        blocker: {
          reason: "needsHumanTask",
          description: "Deployment credentials were verified.",
          resolutionGuide: {
            summary: "Credentials are available.",
            steps: ["Resume the loop."],
            resumeInstruction: "Run jri loop resume.",
          },
          resolution: {
            status: "verified",
            verifiedAt: "2026-05-27T19:10:00.000Z",
            verificationSummary: "Deployment token is present.",
          },
        },
      });

      await expect(collect(daemonStartLoop(dir, "just ralph it", { paths }))).rejects.toThrow("waiting to resume");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status).toMatchObject({ state: "blocked", activeLoopId: "20260527T184210Z" });
    } finally {
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("routes halt reset option over daemon IPC", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        currentIteration: {
          iteration: 1,
          rollbackCommit: "not-a-real-commit",
          trackedTreeCleanAtStart: true,
        },
      });

      const events = await collect(daemonHaltLoop(dir, { paths, resetGit: true }));
      const halted = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(events[0]).toMatchObject({
        type: "loopHalted",
        data: { resetOffered: true, resetAccepted: true, resetSucceeded: false, rollbackCommit: "not-a-real-commit" },
      });
      expect(events[0]?.type === "loopHalted" ? events[0].data.resetError : "").toContain("not a git repository");
      expect(halted.state).toBe("halted");
    } finally {
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("idle shutdown waits until registered loops are no longer active", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 20 });
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
      });

      await daemonStatus(dir, { paths });
      await Bun.sleep(60);

      const activeStatus = await daemonStatus(dir, { paths });
      expect(activeStatus.state).toBe("building");

      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        lastLoopId: "20260527T184210Z",
      });
      await Bun.sleep(80);

      await expect(daemonStatus(dir, { paths })).rejects.toThrow("The JRI daemon is unavailable.");
    } finally {
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("rejects an incompatible active daemon with safe guidance", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const fakeDaemon = await startFakeDaemon(paths, 0);
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
      });
      await writeFile(
        paths.registryPath,
        `${JSON.stringify(
          {
            protocolVersion: 1,
            projects: [{ projectDir: dir, lastSeenAt: new Date().toISOString(), activeLoopId: "20260527T184210Z" }],
          },
          null,
          2,
        )}\n`,
        "utf8",
      );

      await expect(daemonStatus(dir, { paths })).rejects.toThrow("incompatible protocol while a loop may still be active");
      expect(fakeDaemon.requests).toContain("handshake");
      expect(fakeDaemon.requests).not.toContain("status.get");
      expect(fakeDaemon.requests).not.toContain("daemon.shutdown");
    } finally {
      await fakeDaemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("stops an incompatible idle daemon before asking for a compatible daemon", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const fakeDaemon = await startFakeDaemon(paths, 0);
    try {
      await expect(daemonStatus(dir, { paths })).rejects.toThrow("was stopped because it was idle");
      expect(fakeDaemon.requests).toEqual(["handshake", "daemon.shutdown"]);
    } finally {
      await fakeDaemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });
});

async function collect<T>(iterable: AsyncIterable<T>): Promise<T[]> {
  const items: T[] = [];
  for await (const item of iterable) items.push(item);
  return items;
}

async function startFakeDaemon(paths: DaemonPaths, protocolVersion: number): Promise<{ requests: string[]; close(): Promise<void> }> {
  await mkdir(paths.runtimeDir, { recursive: true });
  await mkdir(paths.stateDir, { recursive: true });
  if (process.platform !== "win32") await rm(paths.socketPath, { force: true });
  const requests: string[] = [];
  const sockets = new Set<Socket>();
  const server = createServer((socket) => {
    sockets.add(socket);
    socket.setEncoding("utf8");
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk;
      for (;;) {
        const newline = buffer.indexOf("\n");
        if (newline === -1) break;
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        const request = JSON.parse(line) as { id: string; method: string };
        requests.push(request.method);
        if (request.method === "handshake") {
          socket.write(`${JSON.stringify({ id: request.id, ok: true, result: { protocolVersion } })}\n`);
          continue;
        }
        if (request.method === "daemon.shutdown") {
          socket.write(`${JSON.stringify({ id: request.id, ok: true, result: { exiting: true } })}\n`);
          void closeServer(server, sockets);
          continue;
        }
        socket.write(`${JSON.stringify({ id: request.id, ok: false, error: { code: "unsupported-daemon-method", message: "unsupported" } })}\n`);
      }
    });
    socket.on("close", () => sockets.delete(socket));
  });
  await new Promise<void>((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.once("listening", resolveListen);
    server.listen(paths.socketPath);
  });
  return {
    requests,
    close: async () => {
      await closeServer(server, sockets);
      if (process.platform !== "win32") await rm(paths.socketPath, { force: true });
    },
  };
}

async function closeServer(server: Server, sockets: Set<Socket>): Promise<void> {
  for (const socket of sockets) socket.destroy();
  if (!server.listening) return;
  await new Promise<void>((resolveClose, rejectClose) => {
    server.close((error) => (error ? rejectClose(error) : resolveClose()));
  });
}
