import { readFileSync } from "node:fs";
import { appendFile, chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import { getRecoveredStatus, haltLoop, observeLoop, requestGracefulStop, resumeLoop, runLoopProcess, startRalphLoop } from "../src/core/daemon-runtime";
import { appendLoopEvent, writeStatusAtomic } from "../src/core/runtime-state";
import { defaultStatus } from "../src/core/schema";

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

  test("observe can include recent stdout context with byte offset before milestone events", async () => {
    const dir = await tempProject();
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
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
        value: { type: "loopOutput", stdoutOffset: 0, data: { text: "before\n", replayed: true } },
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

  test("halt accepts eligible rollback reset and records success", async () => {
    const dir = await tempProject();
    const resets: Array<{ projectDir: string; rollbackCommit: string }> = [];
    try {
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
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

  test("resume with an existing implementation plan starts in building", async () => {
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

      await collect(
        resumeLoop(dir, {
          spawnRunner: ({ phase }) => ({ pid: 13579, command: `runner ${phase}` }),
        }),
      );
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(status).toMatchObject({
        state: "building",
        process: { pid: 13579, command: "runner building" },
        lock: { operation: "build" },
      });
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
      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const stdout = await readFile(join(dir, ".jri", "logs", "20260527T184210Z", "stdout.log"), "utf8");
      const events = await collect(observeLoop(dir));

      expect(stdout).toContain("fake-pi-ran");
      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "iterationFinished", "loopFinished"]);
      expect(status).toMatchObject({
        state: "idle",
        activeLoopId: null,
        iterations: 1,
        lastResult: { outcome: "completed" },
      });
      expect(status.process).toBeUndefined();
      expect(status.lock).toBeUndefined();
    } finally {
      if (previousPiCommand === undefined) delete process.env.JRI_PI_COMMAND;
      else process.env.JRI_PI_COMMAND = previousPiCommand;
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

      await runLoopProcess(dir, "20260527T184210Z", "auditing");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual([
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
        lastResult: { outcome: "completed" },
      });
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

      expect(events.map((event) => event.type)).toEqual(["auditStarted", "auditFailed"]);
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
      expect(events[2]).toMatchObject({ type: "loopStopped", data: { reason: "gracefulStopRequested", iteration: 1 } });
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
          "git tag 0.0.99",
          "echo build-committed",
          "echo 'JRI_HANDOFF_JSON: {\"agent\":\"builder\",\"action\":\"complete\",\"summary\":\"Build iteration committed.\"}'",
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
      const commit = events.find((event) => event.type === "commitCreated");
      const tag = events.find((event) => event.type === "tagCreated");
      const iterationFinished = events.find((event) => event.type === "iterationFinished");

      expect(events.map((event) => event.type)).toEqual(["iterationStarted", "commitCreated", "tagCreated", "iterationFinished", "loopFinished"]);
      expect(events[0]).toMatchObject({ type: "iterationStarted", data: { trackedTreeCleanAtStart: true } });
      expect(commit).toMatchObject({ type: "commitCreated", iteration: 1, data: { subject: "build iteration" } });
      expect(tag).toMatchObject({ type: "tagCreated", iteration: 1, data: { tag: "0.0.99" } });
      expect(iterationFinished).toMatchObject({
        type: "iterationFinished",
        data: { outcome: "committed", tag: "0.0.99" },
      });
      expect(status).toMatchObject({
        state: "idle",
        activeLoopId: null,
        iterations: 1,
        lastResult: { outcome: "completed", tag: "0.0.99" },
      });
      expect(status.lastResult.commit).toBe(commit?.data.sha);
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
          'JRI_BLOCKER_JSON: {"reason":"needsHumanTask","description":"old blocker","resolutionGuide":{"summary":"old","steps":["old"],"resumeInstruction":"old"}}',
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

      await runLoopProcess(dir, "20260527T184210Z", "building");

      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const events = await collect(observeLoop(dir));

      expect(events.map((event) => event.type)).toEqual([
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
        lastResult: { outcome: "completed" },
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
