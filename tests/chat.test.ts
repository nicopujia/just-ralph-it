import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import { normalizeStartTrigger, sendChat } from "../src/core/chat";
import { open } from "../src/core";
import { startDaemonServer, type DaemonPaths } from "../src/core/daemon-ipc";
import { defaultStatus } from "../src/core/schema";
import { appendLoopEvent, updateStatus, writeStatusAtomic } from "../src/core/runtime-state";
import { fingerprintSpecFile, writeInterrogationState } from "../src/core/interrogation-state";
import type { CoreEvent } from "../src/core";
import type { HarnessAdapter } from "../src/core/harness";

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
    const previousPiCommand = process.env.JRI_PI_COMMAND;
    try {
      process.env.JRI_PI_COMMAND = await writeFakePi(
        dir,
        "fake-interrogator.sh",
        [
          "Which CLI commands should be in scope?",
          'JRI_HANDOFF_JSON: {"agent":"interrogator","action":"messageOnly","summary":"Asked about CLI scope."}',
        ].join("\n"),
      );
      const project = await open(dir);
      const events = await collect(project.chat.send({ message: "We need a CLI." }));

      expect(events.map((event) => event.type)).toEqual([
        "chatTurnRecorded",
        "chatMessageStarted",
        "chatMessageDelta",
        "chatMessageFinished",
        "chatTurnRecorded",
      ]);
      expect(events[2]).toMatchObject({ type: "chatMessageDelta", data: { text: expect.stringContaining("Which CLI commands") } });

      const log = await readJsonl(join(dir, ".jri", "logs", "interrogation.jsonl"));
      expect(log).toHaveLength(5);
      expect(log[0]).toMatchObject({ type: "chatTurnRecorded", data: { role: "user", content: "We need a CLI." } });
      expect(log[4]).toMatchObject({ type: "chatTurnRecorded", data: { role: "assistant" } });
      expect(log.map((event) => event.sequence)).toEqual([1, 2, 3, 4, 5]);
    } finally {
      restoreEnv("JRI_PI_COMMAND", previousPiCommand);
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("ordinary chat can run an interrogator harness and persist its handoff-backed response", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));
      const requests: Array<{ agent: string; phase: string; message: string }> = [];
      const harness: HarnessAdapter = async (invocation) => {
        requests.push({
          agent: invocation.agent,
          phase: invocation.phase,
          message: invocation.context.inline[0] ?? "",
        });
        await invocation.output.write("Which deployment target should Ralph use?");
        return {
          handoff: {
            agent: "interrogator",
            action: "messageOnly",
            summary: "Asked about deployment target.",
          },
        };
      };

      const events = await collect(sendChat(dir, { message: "Build a deployment flow." }, { interrogatorHarness: harness }));

      expect(requests).toEqual([{ agent: "interrogator", phase: "interrogation", message: "Build a deployment flow." }]);
      expect(events.map((event) => event.type)).toEqual([
        "chatTurnRecorded",
        "chatMessageStarted",
        "chatMessageDelta",
        "chatMessageFinished",
        "chatTurnRecorded",
      ]);
      expect(events[2]).toMatchObject({ data: { text: "Which deployment target should Ralph use?" } });

      const log = await readJsonl(join(dir, ".jri", "logs", "interrogation.jsonl"));
      expect(log.at(-1)).toMatchObject({ type: "chatTurnRecorded", data: { role: "assistant", content: "Which deployment target should Ralph use?" } });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("ordinary chat passes selected durable context refs to the interrogator", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "scratchpad.md"), "Open question: billing tier.\n");
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI.\n");
      await writeStatusAtomic(dir, defaultStatus(dir));
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
      await writeFile(
        join(dir, ".jri", "logs", "interrogation.jsonl"),
        [
          JSON.stringify({
            type: "chatTurnRecorded",
            data: { role: "user", content: "The CLI needs a billing command." },
          }),
          "",
        ].join("\n"),
      );

      let refs: string[] = [];
      let inline: string[] = [];
      await collect(
        sendChat(dir, { message: "Billing is still open." }, {
          interrogatorHarness: async (invocation) => {
            refs = invocation.context.refs;
            inline = invocation.context.inline;
            return {
              handoff: {
                agent: "interrogator",
                action: "messageOnly",
                summary: "Asked about billing.",
              },
            };
          },
        }),
      );

      expect(refs).toEqual([
        ".jri/status.json",
        ".jri/interrogation-state.json",
        ".jri/specs/app.md",
        ".jri/scratchpad.md",
        ".jri/logs/interrogation.jsonl#recent-unsealed-turns",
      ]);
      expect(refs).not.toContain(".jri/specs");
      expect(refs).not.toContain(".jri/logs/interrogation.jsonl");
      expect(inline[0]).toBe("Billing is still open.");
      expect(inline[1]).toContain("The CLI needs a billing command.");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("sealed topics omit old interrogation turns from selected context", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "scratchpad.md"), "Old scratchpad note.\n");
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI.\n");
      await writeStatusAtomic(dir, defaultStatus(dir));
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
      await writeFile(
        join(dir, ".jri", "logs", "interrogation.jsonl"),
        [
          JSON.stringify({
            type: "chatTurnRecorded",
            data: { role: "user", content: "Old sealed-topic discussion." },
          }),
          "",
        ].join("\n"),
      );

      let refs: string[] = [];
      let inline: string[] = [];
      await collect(
        sendChat(dir, { message: "Can we discuss another topic?" }, {
          interrogatorHarness: async (invocation) => {
            refs = invocation.context.refs;
            inline = invocation.context.inline;
            return {
              handoff: {
                agent: "interrogator",
                action: "messageOnly",
                summary: "Asked for the next topic.",
              },
            };
          },
        }),
      );

      expect(refs).toContain(".jri/specs/app.md");
      expect(refs).not.toContain(".jri/logs/interrogation.jsonl");
      expect(refs).not.toContain(".jri/logs/interrogation.jsonl#recent-unsealed-turns");
      expect(inline).toEqual(["Can we discuss another topic?"]);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("interrogator specsUpdated handoff emits a durable specsUpdated event", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));

      const events = await collect(
        sendChat(dir, { message: "The app should deploy to Cloudflare." }, {
          interrogatorHarness: async (invocation) => {
            await writeFile(join(invocation.projectDir, ".jri", "specs", "deployment.md"), "# Deployment\n\nDeploy to Cloudflare.\n");
            await invocation.output.write("I updated the deployment spec.");
            return {
              handoff: {
                agent: "interrogator",
                action: "specsUpdated",
                specFiles: [".jri/specs/deployment.md"],
                summary: "Deployment target clarified.",
              },
            };
          },
        }),
      );

      expect(events.at(-1)).toMatchObject({
        type: "specsUpdated",
        data: { specFiles: [".jri/specs/deployment.md"], summary: "Deployment target clarified." },
      });
      const state = JSON.parse(await readFile(join(dir, ".jri", "interrogation-state.json"), "utf8"));
      expect(state.topics.deployment).toMatchObject({
        specFile: ".jri/specs/deployment.md",
        status: "open",
      });
      expect(typeof state.topics.deployment.lastReconciledSpecFingerprint).toBe("string");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("interrogator specsUpdated handoff can seal completed topics", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));

      const events = await collect(
        sendChat(dir, { message: "Deployment is fully decided." }, {
          interrogatorHarness: async (invocation) => {
            await writeFile(join(invocation.projectDir, ".jri", "specs", "deployment.md"), "# Deployment\n\nDeploy to Cloudflare.\n");
            return {
              handoff: {
                agent: "interrogator",
                action: "specsUpdated",
                specFiles: [".jri/specs/deployment.md"],
                sealedSpecFiles: [".jri/specs/deployment.md"],
                summary: "Deployment target is complete.",
              },
            };
          },
        }),
      );

      expect(events.at(-1)).toMatchObject({
        type: "specsUpdated",
        data: {
          specFiles: [".jri/specs/deployment.md"],
          sealedSpecFiles: [".jri/specs/deployment.md"],
          summary: "Deployment target is complete.",
        },
      });
      const state = JSON.parse(await readFile(join(dir, ".jri", "interrogation-state.json"), "utf8"));
      expect(state.topics.deployment).toMatchObject({
        specFile: ".jri/specs/deployment.md",
        status: "sealed",
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("interrogator scratchpadUpdated handoff emits durable scratchpad evidence", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await mkdir(join(dir, ".jri"), { recursive: true });
      await writeFile(join(dir, ".jri", "scratchpad.md"), "Open question: deployment credentials.\n");
      await writeStatusAtomic(dir, defaultStatus(dir));

      const events = await collect(
        sendChat(dir, { message: "Credentials are unresolved." }, {
          interrogatorHarness: async (invocation) => {
            await writeFile(join(invocation.projectDir, ".jri", "scratchpad.md"), "Open question: deployment credentials.\n", "utf8");
            return {
              handoff: {
                agent: "interrogator",
                action: "scratchpadUpdated",
                summary: "Recorded unresolved credential question.",
              },
            };
          },
        }),
      );

      expect(events.at(-1)).toMatchObject({
        type: "scratchpadUpdated",
        data: {
          scratchpadPath: ".jri/scratchpad.md",
          summary: "Recorded unresolved credential question.",
        },
      });
      const scratchpad = await readFile(join(dir, ".jri", "scratchpad.md"), "utf8");
      expect(scratchpad).toContain("deployment credentials");
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

  test("standalone start trigger bypasses the interrogator harness", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));

      let harnessCalled = false;
      let startCalled = false;
      const events = await collect(
        sendChat(dir, { message: "Just Ralph It!" }, {
          interrogatorHarness: async () => {
            harnessCalled = true;
            return {
              handoff: {
                agent: "interrogator",
                action: "messageOnly",
                summary: "Should not run.",
              },
            };
          },
          startLoop: async function* (_projectDir, trigger) {
            startCalled = true;
            expect(trigger).toBe("just ralph it");
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

      expect(harnessCalled).toBe(false);
      expect(startCalled).toBe(true);
      expect(events.map((event) => event.type)).toContain("loopStarted");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("interrogator startRequested handoff from non-trigger prose cannot start a loop", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));

      let startCalled = false;
      const events = await collect(
        sendChat(dir, { message: "please start when the specs look ready" }, {
          interrogatorHarness: async () => ({
            handoff: {
              agent: "interrogator",
              action: "startRequested",
              trigger: "just ralph it",
            },
          }),
          startLoop: async function* () {
            startCalled = true;
          },
        }),
      );
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(startCalled).toBe(false);
      expect(status.state).toBe("idle");
      expect(events.map((event) => event.type)).not.toContain("loopStarted");
      expect(events.filter((event) => event.type === "chatMessageDelta").at(-1)).toMatchObject({
        type: "chatMessageDelta",
        data: { text: expect.stringContaining("standalone just ralph it or ralfealo") },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("active loop chat reports observation guidance without invoking interrogator", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        startedAt: "2026-05-27T18:42:10.000Z",
        process: {
          pid: 12345,
          command: "runner building",
          startedAt: "2026-05-27T18:42:10.000Z",
        },
      });

      let harnessCalled = false;
      let startCalled = false;
      const events = await collect(
        sendChat(dir, { message: "just ralph it" }, {
          interrogatorHarness: async () => {
            harnessCalled = true;
            return {
              handoff: {
                agent: "interrogator",
                action: "startRequested",
                trigger: "just ralph it",
              },
            };
          },
          startLoop: async function* () {
            startCalled = true;
          },
        }),
      );

      expect(harnessCalled).toBe(false);
      expect(startCalled).toBe(false);
      expect(events.map((event) => event.type)).not.toContain("loopStarted");
      expect(events[2]).toMatchObject({
        type: "chatMessageDelta",
        data: { text: expect.stringContaining("jri loop attach") },
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

  test("standalone start trigger blocks when sealed specs need manual reconciliation", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));
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
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI and web UI.\n");

      let startCalled = false;
      const events = await collect(
        sendChat(dir, { message: "just ralph it" }, {
          now: new Date("2026-05-27T20:00:00.000Z"),
          startLoop: async function* () {
            startCalled = true;
          },
        }),
      );
      const state = JSON.parse(await readFile(join(dir, ".jri", "interrogation-state.json"), "utf8"));

      expect(startCalled).toBe(false);
      expect(events.map((event) => event.type)).toEqual([
        "chatTurnRecorded",
        "chatMessageStarted",
        "chatMessageDelta",
        "chatMessageFinished",
        "chatTurnRecorded",
      ]);
      expect(events[2]).toMatchObject({ type: "chatMessageDelta", data: { text: expect.stringContaining("pending spec reconciliation") } });
      expect(state.topics.app.pendingReconciliation).toMatchObject({ reason: "manualSpecEdit" });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("empty chat open reports pending manual reconciliation before user input", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));
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
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI and web UI.\n");

      const events = await collect(sendChat(dir, { message: "" }, { now: new Date("2026-05-27T20:00:00.000Z") }));
      const state = JSON.parse(await readFile(join(dir, ".jri", "interrogation-state.json"), "utf8"));

      expect(events.map((event) => event.type)).toEqual(["chatMessageStarted", "chatMessageDelta", "chatMessageFinished", "chatTurnRecorded"]);
      expect(events[1]).toMatchObject({
        type: "chatMessageDelta",
        data: { text: expect.stringContaining("pending spec reconciliation") },
      });
      expect(state.topics.app.pendingReconciliation).toMatchObject({ reason: "manualSpecEdit" });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("ordinary chat surfaces pending reconciliation before invoking interrogator", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));
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
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI and web UI.\n");

      let harnessCalled = false;
      const events = await collect(
        sendChat(dir, { message: "The web UI edit is intentional." }, {
          now: new Date("2026-05-27T20:00:00.000Z"),
          interrogatorHarness: async () => {
            harnessCalled = true;
            return {
              handoff: {
                agent: "interrogator",
                action: "messageOnly",
                summary: "Recorded reconciliation answer.",
              },
            };
          },
        }),
      );

      expect(harnessCalled).toBe(true);
      expect(events.filter((event) => event.type === "chatMessageDelta")[0]).toMatchObject({
        type: "chatMessageDelta",
        data: { text: expect.stringContaining("pending spec reconciliation") },
      });
      expect(events.filter((event) => event.type === "chatMessageDelta")[1]).toMatchObject({
        type: "chatMessageDelta",
        data: { text: "Recorded reconciliation answer." },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("project chat starts accepted triggers through daemon IPC and updates the registry", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const previousEnv = captureDaemonEnv();
    const previousPiCommand = process.env.JRI_PI_COMMAND;
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
      applyDaemonEnv(paths);
      process.env.JRI_PI_COMMAND = await writeFakePi(
        dir,
        "fake-start-interrogator.sh",
        [
          "Start request accepted.",
          'JRI_HANDOFF_JSON: {"agent":"interrogator","action":"startRequested","trigger":"just ralph it"}',
        ].join("\n"),
      );
      const project = await open(dir);
      const events = await collect(project.chat.send({ message: "just ralph it" }));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const registry = JSON.parse(await readFile(paths.registryPath, "utf8"));
      const loopEvents = await readJsonl(join(dir, ".jri", "logs", "20260527T200000Z", "events.jsonl"));

      expect(events.map((event) => event.type)).toContain("loopStarted");
      expect(events.map((event) => event.type)).toContain("loopFinished");
      expect(events.at(-1)).toMatchObject({ type: "loopFinished", loopId: "20260527T200000Z" });
      expect(status).toMatchObject({
        state: "idle",
        activeLoopId: null,
        lastLoopId: "20260527T200000Z",
        lastResult: { outcome: "completed", summary: "Fake loop completed." },
      });
      expect(registry.projects[0]).toMatchObject({ projectDir: dir, activeLoopId: null });
      expect(loopEvents[0]).toMatchObject({ type: "loopStarted", loopId: "20260527T200000Z" });
      expect(loopEvents.at(-1)).toMatchObject({ type: "loopFinished", loopId: "20260527T200000Z" });
    } finally {
      restoreEnv("JRI_PI_COMMAND", previousPiCommand);
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

async function writeFakePi(dir: string, name: string, output: string): Promise<string> {
  const path = join(dir, name);
  const escaped = output.replace(/'/g, "'\\''");
  await writeFile(path, `#!/usr/bin/env bash\nprintf '%s\\n' '${escaped}'\n`, "utf8");
  await chmod(path, 0o755);
  return path;
}

function restoreEnv(key: string, value: string | undefined): void {
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
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
