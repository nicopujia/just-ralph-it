import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createConnection, createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import {
  daemonHaltLoop,
  daemonObserveLoop,
  daemonRequestStop,
  daemonStartLoop,
  daemonStatus,
  MAX_DAEMON_FRAME_BYTES,
  startDaemonServer,
  type DaemonPaths,
} from "../src/core/daemon-ipc";
import { appendLoopEvent, updateStatus, writeStatusAtomic } from "../src/core/runtime-state";
import { defaultStatus } from "../src/core/schema";
import { fingerprintSpecFile, writeInterrogationState } from "../src/core/interrogation-state";

async function tempProject(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "jri-daemon-ipc-test-"));
  await mkdir(join(dir, ".jri", "logs"), { recursive: true });
  await writeStatusAtomic(dir, defaultStatus(dir));
  return dir;
}

function activeTestLock(operation: "audit" | "plan" | "build") {
  return {
    owner: "daemon" as const,
    pid: process.pid,
    operation,
    acquiredAt: "2026-05-27T18:42:10.000Z",
    heartbeatAt: "2026-05-27T18:42:10.000Z",
    expiresAt: "2999-01-01T00:00:00.000Z",
  };
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

  test("drops malformed registry entries before recording projects", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    await mkdir(paths.stateDir, { recursive: true });
    await writeFile(
      paths.registryPath,
      `${JSON.stringify(
        {
          protocolVersion: 1,
          projects: [
            { projectDir: "relative-project", lastSeenAt: new Date().toISOString(), activeLoopId: null },
            { projectDir: dir, lastSeenAt: "not a date", activeLoopId: null },
            { projectDir: dir, lastSeenAt: new Date().toISOString(), activeLoopId: 42 },
          ],
        },
        null,
        2,
      )}\n`,
      "utf8",
    );
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    try {
      await daemonStatus(dir, { paths });

      const registry = JSON.parse(await readFile(paths.registryPath, "utf8"));
      expect(registry.projects).toHaveLength(1);
      expect(registry.projects[0]).toMatchObject({ projectDir: dir, activeLoopId: null });
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
        lock: activeTestLock("plan"),
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
        observePollIntervalMs: 10,
        spawnRunner: ({ loopId, phase }) => {
          scheduleLoopCompletion(dir, loopId);
          return { pid: process.pid, command: `runner ${phase}` };
        },
      },
    });
    try {
      const events = await collect(daemonStartLoop(dir, "just ralph it", { paths }));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const registry = JSON.parse(await readFile(paths.registryPath, "utf8"));

      expect(events[0]).toMatchObject({
        type: "loopStarted",
        loopId: "20260527T200000Z",
        data: { projectDir: dir, pid: process.pid },
      });
      expect(events.map((event) => event.type)).toEqual(["loopStarted", "loopFinished"]);
      expect(status).toMatchObject({
        state: "idle",
        activeLoopId: null,
        lastLoopId: "20260527T200000Z",
        lastResult: { outcome: "completed", summary: "Fake loop completed." },
      });
      expect(registry.projects[0]).toMatchObject({
        projectDir: dir,
        activeLoopId: null,
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
        lock: activeTestLock("build"),
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
        lock: activeTestLock("build"),
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
        lock: activeTestLock("build"),
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
        lock: activeTestLock("build"),
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

  test("refuses direct daemon shutdown while a registered loop is active", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    const socket = await connectRawSocket(paths.socketPath);
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        lock: activeTestLock("build"),
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

      socket.write(`${JSON.stringify({ id: "shutdown", method: "daemon.shutdown" })}\n`);
      const response = JSON.parse(await readRawSocketLine(socket));

      expect(response).toMatchObject({
        id: "shutdown",
        ok: false,
        error: {
          code: "daemon-active-loop",
          message: "Cannot shut down the JRI daemon while a loop is active.",
        },
      });

      const activeStatus = await daemonStatus(dir, { paths });
      expect(activeStatus.state).toBe("building");
    } finally {
      socket.destroy();
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("maps malformed daemon unary responses to protocol errors", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const fakeDaemon = await startBadResponseDaemon(paths, "status.get", "{malformed daemon response\n");
    try {
      await expect(daemonStatus(dir, { paths })).rejects.toMatchObject({
        code: "daemon-protocol-error",
        message: "Daemon response is not valid JSON.",
      });
      expect(fakeDaemon.requests).toEqual(["handshake", "status.get"]);
    } finally {
      await fakeDaemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("maps malformed daemon stream responses to protocol errors", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const fakeDaemon = await startBadResponseDaemon(paths, "loop.observe", "{malformed daemon response\n");
    try {
      await expect(collect(daemonObserveLoop(dir, { paths }))).rejects.toMatchObject({
        code: "daemon-protocol-error",
        message: "Daemon response is not valid JSON.",
      });
      expect(fakeDaemon.requests).toEqual(["handshake", "loop.observe"]);
    } finally {
      await fakeDaemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("maps structurally invalid daemon responses to protocol errors", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const fakeDaemon = await startBadResponseDaemon(paths, "status.get", (id) => `${JSON.stringify({ id, ok: true, unexpected: true })}\n`);
    try {
      await expect(daemonStatus(dir, { paths })).rejects.toMatchObject({
        code: "daemon-protocol-error",
        message: "Daemon returned an invalid response.",
      });
      expect(fakeDaemon.requests).toEqual(["handshake", "status.get"]);
    } finally {
      await fakeDaemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("rejects oversized daemon responses before parsing payloads", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const hugeResponse = `${"x".repeat(MAX_DAEMON_FRAME_BYTES + 1)}\n`;
    const fakeDaemon = await startBadResponseDaemon(paths, "status.get", hugeResponse);
    try {
      await expect(daemonStatus(dir, { paths })).rejects.toMatchObject({
        code: "daemon-frame-too-large",
        message: "Daemon response exceeded the maximum IPC frame size.",
      });
      expect(fakeDaemon.requests).toEqual(["handshake", "status.get"]);
    } finally {
      await fakeDaemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("rejects oversized daemon requests before parsing payloads", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    const socket = await connectRawSocket(paths.socketPath);
    try {
      socket.write(`${JSON.stringify({ id: "oversized", method: "status.get", params: { projectDir: dir, padding: "x".repeat(MAX_DAEMON_FRAME_BYTES) } })}\n`);
      const response = JSON.parse(await readRawSocketLine(socket));

      expect(response).toMatchObject({
        id: "unknown",
        ok: false,
        error: {
          code: "daemon-frame-too-large",
          message: "Daemon request exceeded the maximum IPC frame size.",
        },
      });
    } finally {
      socket.destroy();
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("rejects daemon requests with missing or invalid projectDir", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    const socket = await connectRawSocket(paths.socketPath);
    try {
      socket.write(`${JSON.stringify({ id: "missing-project", method: "status.get", params: {} })}\n`);
      const missing = JSON.parse(await readRawSocketLine(socket));
      expect(missing).toMatchObject({
        id: "missing-project",
        ok: false,
        error: {
          code: "invalid-daemon-request",
          message: "Daemon request is missing projectDir.",
        },
      });

      socket.write(`${JSON.stringify({ id: "relative-project", method: "status.get", params: { projectDir: "relative-project" } })}\n`);
      const relative = JSON.parse(await readRawSocketLine(socket));
      expect(relative).toMatchObject({
        id: "relative-project",
        ok: false,
        error: {
          code: "invalid-daemon-request",
          message: "Daemon request projectDir must be an absolute path.",
        },
      });
    } finally {
      socket.destroy();
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("rejects malformed loop halt payloads", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const daemon = await startDaemonServer({ paths, idleTimeoutMs: 10_000 });
    const socket = await connectRawSocket(paths.socketPath);
    try {
      socket.write(`${JSON.stringify({ id: "halt-missing-options", method: "loop.halt", params: { projectDir: dir } })}\n`);
      const missing = JSON.parse(await readRawSocketLine(socket));
      expect(missing).toMatchObject({
        id: "halt-missing-options",
        ok: false,
        error: {
          code: "invalid-daemon-request",
          message: "Daemon loop.halt is missing halt options.",
        },
      });

      socket.write(`${JSON.stringify({ id: "halt-bad-reset", method: "loop.halt", params: { projectDir: dir, halt: { resetGit: "yes" } } })}\n`);
      const badReset = JSON.parse(await readRawSocketLine(socket));
      expect(badReset).toMatchObject({
        id: "halt-bad-reset",
        ok: false,
        error: {
          code: "invalid-daemon-request",
          message: "Daemon loop.halt resetGit option must be a boolean.",
        },
      });
    } finally {
      socket.destroy();
      await daemon.close();
      await rm(dir, { recursive: true, force: true });
    }
  });
});

async function collect<T>(iterable: AsyncIterable<T>): Promise<T[]> {
  const items: T[] = [];
  for await (const item of iterable) items.push(item);
  return items;
}

async function connectRawSocket(socketPath: string): Promise<Socket> {
  return await new Promise<Socket>((resolveConnect, rejectConnect) => {
    const socket = createConnection(socketPath);
    const onError = (error: Error) => {
      socket.destroy();
      rejectConnect(error);
    };
    socket.once("error", onError);
    socket.once("connect", () => {
      socket.off("error", onError);
      resolveConnect(socket);
    });
  });
}

async function readRawSocketLine(socket: Socket): Promise<string> {
  return await new Promise<string>((resolveLine, rejectLine) => {
    let buffer = "";
    socket.setEncoding("utf8");
    socket.on("data", onData);
    socket.once("error", onError);
    socket.once("close", onClose);

    function cleanup(): void {
      socket.off("data", onData);
      socket.off("error", onError);
      socket.off("close", onClose);
    }

    function onData(chunk: string): void {
      buffer += chunk;
      const newline = buffer.indexOf("\n");
      if (newline === -1) return;
      cleanup();
      resolveLine(buffer.slice(0, newline));
    }

    function onError(error: Error): void {
      cleanup();
      rejectLine(error);
    }

    function onClose(): void {
      cleanup();
      rejectLine(new Error("Socket closed before a response line was received."));
    }
  });
}

function scheduleLoopCompletion(projectDir: string, loopId: string): void {
  setTimeout(() => {
    void (async () => {
      await appendLoopEvent(projectDir, {
        type: "loopFinished",
        loopId,
        data: { outcome: "completed", summary: "Fake loop completed." },
      });
      await updateStatus(projectDir, (current) => {
        const { process, lock, ...withoutOwnership } = current;
        void process;
        void lock;
        return {
          ...withoutOwnership,
          state: "idle",
          activeLoopId: null,
          lastLoopId: loopId,
          finishedAt: "2026-05-27T20:00:01.000Z",
          stopRequested: false,
          lastResult: { outcome: "completed", summary: "Fake loop completed." },
        };
      });
    })();
  }, 25);
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

async function startBadResponseDaemon(
  paths: DaemonPaths,
  malformedMethod: string,
  badResponse: string | ((id: string) => string),
): Promise<{ requests: string[]; close(): Promise<void> }> {
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
          socket.write(`${JSON.stringify({ id: request.id, ok: true, result: { protocolVersion: 1 } })}\n`);
          continue;
        }
        if (request.method === malformedMethod) {
          socket.write(typeof badResponse === "function" ? badResponse(request.id) : badResponse);
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
