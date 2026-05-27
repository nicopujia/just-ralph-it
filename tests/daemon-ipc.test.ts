import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import {
  daemonObserveLoop,
  daemonRequestStop,
  daemonStatus,
  startDaemonServer,
  type DaemonPaths,
} from "../src/core/daemon-ipc";
import { appendLoopEvent, writeStatusAtomic } from "../src/core/runtime-state";
import { defaultStatus } from "../src/core/schema";

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

      await daemonRequestStop(dir, { paths });
      const stopped = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(stopped.stopRequested).toBe(true);

      const events = await collect(daemonObserveLoop(dir, { paths }));
      expect(events.map((event) => event.type)).toEqual(["loopStarted", "stopRequested"]);
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
});

async function collect<T>(iterable: AsyncIterable<T>): Promise<T[]> {
  const items: T[] = [];
  for await (const item of iterable) items.push(item);
  return items;
}
