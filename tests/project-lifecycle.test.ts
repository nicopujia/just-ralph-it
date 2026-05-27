import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, test } from "bun:test";
import { open } from "../src/core";
import { writeStatusAtomic } from "../src/core/runtime-state";
import { defaultStatus } from "../src/core/schema";

const daemonEnvKeys = ["JRI_DAEMON_RUNTIME_DIR", "JRI_DAEMON_STATE_DIR", "JRI_DAEMON_SOCKET_PATH", "JRI_DAEMON_REGISTRY_PATH"];
const savedDaemonEnv = new Map<string, string | undefined>();

for (const key of daemonEnvKeys) savedDaemonEnv.set(key, process.env[key]);

afterEach(() => {
  for (const key of daemonEnvKeys) {
    const value = savedDaemonEnv.get(key);
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
});

async function tempInitializedProject(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "jri-project-lifecycle-test-"));
  await mkdir(join(dir, ".jri", "logs"), { recursive: true });
  await writeStatusAtomic(dir, defaultStatus(dir));
  return dir;
}

async function activateLoop(dir: string, loopId: string, state: "planning" | "building" = "building"): Promise<void> {
  await mkdir(join(dir, ".jri", "logs", loopId), { recursive: true });
  await writeStatusAtomic(dir, {
    ...defaultStatus(dir),
    state,
    activeLoopId: loopId,
    lastLoopId: loopId,
    process: { pid: process.pid, command: "runner", startedAt: new Date("2026-05-27T00:00:00.000Z").toISOString() },
  });
}

async function collect<T>(iterable: AsyncIterable<T>): Promise<T[]> {
  const values: T[] = [];
  for await (const value of iterable) values.push(value);
  return values;
}

function forceDaemonUnavailable(dir: string): void {
  const socketDirectory = join(dir, "daemon-socket-as-directory");
  process.env.JRI_DAEMON_RUNTIME_DIR = join(dir, "daemon-runtime");
  process.env.JRI_DAEMON_STATE_DIR = join(dir, "daemon-state");
  process.env.JRI_DAEMON_SOCKET_PATH = socketDirectory;
  process.env.JRI_DAEMON_REGISTRY_PATH = join(dir, "daemon-state", "daemon-registry.json");
}

describe("project lifecycle daemon ownership", () => {
  test("requestStop does not locally mutate status when the daemon is unavailable", async () => {
    const dir = await tempInitializedProject();
    try {
      await mkdir(join(dir, "daemon-socket-as-directory"), { recursive: true });
      await activateLoop(dir, "20260527T184210Z");
      forceDaemonUnavailable(dir);

      const project = await open(dir);
      await expect(project.loop.requestStop()).rejects.toThrow("daemon is unavailable");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status.stopRequested).toBe(false);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("halt does not locally mutate status when the daemon is unavailable", async () => {
    const dir = await tempInitializedProject();
    try {
      await mkdir(join(dir, "daemon-socket-as-directory"), { recursive: true });
      await activateLoop(dir, "20260527T184210Z");
      forceDaemonUnavailable(dir);

      const project = await open(dir);
      await expect(collect(project.loop.halt())).rejects.toThrow("daemon is unavailable");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status.state).toBe("building");
      expect(status.lastResult).toBeUndefined();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("resume does not locally mutate status when the daemon is unavailable", async () => {
    const dir = await tempInitializedProject();
    try {
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild the CLI.\n", "utf8");
      await mkdir(join(dir, "daemon-socket-as-directory"), { recursive: true });
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "stopped",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        authorizedSpecsFingerprint: "would-have-been-checked-by-daemon",
      });
      forceDaemonUnavailable(dir);

      const project = await open(dir);
      await expect(collect(project.loop.resume())).rejects.toThrow("daemon is unavailable");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status.state).toBe("stopped");
      expect(status.process).toBeUndefined();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
