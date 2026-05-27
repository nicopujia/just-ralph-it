import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import { normalizeStartTrigger, sendChat } from "../src/core/chat";
import { open } from "../src/core";
import { startDaemonServer, type DaemonPaths } from "../src/core/daemon-ipc";
import { defaultStatus } from "../src/core/schema";
import { appendLoopEvent, updateStatus, writeStatusAtomic } from "../src/core/runtime-state";
import { fingerprintSpecFile, recordInterrogatorSpecUpdate, writeInterrogationState } from "../src/core/interrogation-state";
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
    const controller = new AbortController();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));
      const requests: Array<{ agent: string; phase: string; message: string; capabilities: string[]; signal: AbortSignal }> = [];
      const harness: HarnessAdapter = async (invocation) => {
        requests.push({
          agent: invocation.agent,
          phase: invocation.phase,
          message: invocation.context.inline[0] ?? "",
          capabilities: invocation.capabilities.map((capability) => `${capability.name}:${capability.operation ?? "*"}`),
          signal: invocation.signal,
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

      const events = await collect(sendChat(dir, { message: "Build a deployment flow." }, { signal: controller.signal, interrogatorHarness: harness }));

      expect(requests).toEqual([
        {
          agent: "interrogator",
          phase: "interrogation",
          message: "Build a deployment flow.",
          capabilities: ["web:search", "web:fetch"],
          signal: controller.signal,
        },
      ]);
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
      await recordInterrogatorSpecUpdate(dir, [".jri/specs/app.md"], { sealedSpecFiles: [".jri/specs/app.md"] });
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

  test("standalone start trigger defaults to daemon-owned streaming start", async () => {
    const dir = await tempProject();
    const paths = tempDaemonPaths(dir);
    const previousEnv = captureDaemonEnv();
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
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));
      applyDaemonEnv(paths);

      const events = await collect(sendChat(dir, { message: "just ralph it" }));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const registry = JSON.parse(await readFile(paths.registryPath, "utf8"));

      expect(events.map((event) => event.type)).toEqual([
        "chatTurnRecorded",
        "chatMessageStarted",
        "chatMessageDelta",
        "chatMessageFinished",
        "chatTurnRecorded",
        "loopStarted",
        "loopFinished",
      ]);
      expect(events[5]).toMatchObject({ type: "loopStarted", data: { pid: process.pid } });
      expect(status).toMatchObject({
        state: "idle",
        activeLoopId: null,
        lastLoopId: "20260527T200000Z",
        lastResult: { outcome: "completed", summary: "Fake loop completed." },
      });
      expect(status.process).toBeUndefined();
      expect(status.lock).toBeUndefined();
      expect(registry.projects[0]).toMatchObject({ projectDir: dir, activeLoopId: null });
    } finally {
      restoreDaemonEnv(previousEnv);
      await daemon.close();
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

  test("active loop chat invokes interrogator in observation mode with loop context", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs", "20260527T184210Z"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeFile(join(dir, ".jri", "IMPLEMENTATION_PLAN.md"), "# Plan\n\n- Keep building.\n", "utf8");
      await writeFile(join(dir, ".jri", "logs", "20260527T184210Z", "events.jsonl"), '{"type":"iterationStarted"}\n', "utf8");
      await writeFile(join(dir, ".jri", "logs", "20260527T184210Z", "stdout.log"), "Builder output.\n", "utf8");
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

      let refs: string[] = [];
      let inline: string[] = [];
      let startCalled = false;
      const events = await collect(
        sendChat(dir, { message: "What is Ralph doing?" }, {
          interrogatorHarness: async (invocation) => {
            refs = invocation.context.refs;
            inline = invocation.context.inline;
            await invocation.output.write("Ralph is currently building from the authorized specs.");
            return {
              handoff: {
                agent: "interrogator",
                action: "messageOnly",
                summary: "Explained active loop status.",
              },
            };
          },
          startLoop: async function* () {
            startCalled = true;
          },
        }),
      );

      expect(startCalled).toBe(false);
      expect(events.map((event) => event.type)).not.toContain("loopStarted");
      expect(refs).toContain(".jri/status.json");
      expect(refs).toContain(".jri/IMPLEMENTATION_PLAN.md");
      expect(refs).toContain(".jri/logs/20260527T184210Z/events.jsonl");
      expect(refs).toContain(".jri/logs/20260527T184210Z/stdout.log");
      expect(inline[0]).toBe("What is Ralph doing?");
      expect(inline[1]).toContain("Observation mode restrictions:");
      expect(inline[1]).toContain("must not mutate .jri/specs/*");
      expect(events[2]).toMatchObject({
        type: "chatMessageDelta",
        data: { text: "Ralph is currently building from the authorized specs." },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("active loop observation mode rejects lifecycle-changing interrogator handoffs", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "planning",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        startedAt: "2026-05-27T18:42:10.000Z",
        process: {
          pid: 12345,
          command: "runner planning",
          startedAt: "2026-05-27T18:42:10.000Z",
        },
      });

      await expect(
        collect(
          sendChat(dir, { message: "Change the requirements now." }, {
            interrogatorHarness: async () => ({
              handoff: {
                agent: "interrogator",
                action: "specsUpdated",
                specFiles: [".jri/specs/app.md"],
                summary: "Changed requirements.",
              },
            }),
          }),
        ),
      ).rejects.toMatchObject({ code: "invalid-observation-handoff" });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("active loop chat exact stop request asks daemon for graceful stop without invoking interrogator", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
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

      let requestedStop = false;
      let harnessCalled = false;
      const events = await collect(
        sendChat(dir, { message: "jri loop stop" }, {
          requestStop: async () => {
            requestedStop = true;
            await updateStatus(dir, (current) => ({ ...current, stopRequested: true }));
          },
          interrogatorHarness: async () => {
            harnessCalled = true;
            return { handoff: { agent: "interrogator", action: "messageOnly", summary: "Should not run." } };
          },
        }),
      );

      expect(requestedStop).toBe(true);
      expect(harnessCalled).toBe(false);
      expect(events.map((event) => event.type)).toEqual([
        "chatTurnRecorded",
        "chatMessageStarted",
        "chatMessageDelta",
        "chatMessageFinished",
        "chatTurnRecorded",
      ]);
      expect(events[2]).toMatchObject({
        type: "chatMessageDelta",
        data: { text: expect.stringContaining("Graceful stop requested for loop 20260527T184210Z") },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("active loop chat stop request is idempotent when graceful stop is already requested", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "building",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        startedAt: "2026-05-27T18:42:10.000Z",
        stopRequested: true,
        process: {
          pid: 12345,
          command: "runner building",
          startedAt: "2026-05-27T18:42:10.000Z",
        },
      });

      let requestedStop = false;
      const events = await collect(
        sendChat(dir, { message: "stop" }, {
          requestStop: async () => {
            requestedStop = true;
          },
        }),
      );

      expect(requestedStop).toBe(false);
      expect(events[2]).toMatchObject({
        type: "chatMessageDelta",
        data: { text: expect.stringContaining("A graceful stop is already requested for loop 20260527T184210Z") },
      });
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      expect(status.stopRequested).toBe(true);
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

  test("standalone start trigger bootstraps missing interrogation state from existing specs before starting", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));
      await writeFile(join(dir, ".jri", "specs", "app.md"), "# App\n\nBuild a CLI.\n");

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
      expect(events[2]).toMatchObject({
        type: "chatMessageDelta",
        data: { text: expect.stringContaining("pending spec reconciliation") },
      });
      expect(state.topics.app).toMatchObject({
        specFile: ".jri/specs/app.md",
        status: "open",
        pendingReconciliation: {
          reason: "specFileAdded",
          detectedAt: "2026-05-27T20:00:00.000Z",
        },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("standalone start trigger blocks on unresolved scratchpad notes when interrogation state is missing", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));
      await writeFile(join(dir, ".jri", "scratchpad.md"), "Open question: should the app have a TUI?\n");

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
      expect(events[2]).toMatchObject({
        type: "chatMessageDelta",
        data: { text: expect.stringContaining("scratchpad") },
      });
      expect(state).toEqual({
        schemaVersion: 1,
        topics: {},
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("interrogator specsUpdated handoff can accept intentional deletion of a sealed spec", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));
      await writeFile(join(dir, ".jri", "specs", "legacy.md"), "# Legacy\n\nBuild the legacy mode.\n");
      const fingerprint = await fingerprintSpecFile(dir, ".jri/specs/legacy.md");
      await writeInterrogationState(dir, {
        schemaVersion: 1,
        topics: {
          legacy: {
            specFile: ".jri/specs/legacy.md",
            status: "sealed",
            lastReconciledSpecFingerprint: fingerprint,
          },
        },
      });
      await rm(join(dir, ".jri", "specs", "legacy.md"));

      let startCalled = false;
      await collect(sendChat(dir, { message: "" }, { now: new Date("2026-05-27T20:00:00.000Z") }));
      const events = await collect(
        sendChat(dir, { message: "Legacy mode is intentionally removed." }, {
          interrogatorHarness: async () => ({
            handoff: {
              agent: "interrogator",
              action: "specsUpdated",
              specFiles: [".jri/specs/legacy.md"],
              summary: "Removed legacy mode from scope.",
            },
          }),
          startLoop: async function* () {
            startCalled = true;
          },
        }),
      );
      const state = JSON.parse(await readFile(join(dir, ".jri", "interrogation-state.json"), "utf8"));
      await collect(
        sendChat(dir, { message: "just ralph it" }, {
          startLoop: async function* () {
            startCalled = true;
          },
        }),
      );

      expect(events.at(-1)).toMatchObject({
        type: "specsUpdated",
        data: { specFiles: [".jri/specs/legacy.md"], summary: "Removed legacy mode from scope." },
      });
      expect(state.topics.legacy).toBeUndefined();
      expect(startCalled).toBe(true);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("interrogator specsUpdated handoff can restore a deleted sealed spec", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, ".jri", "specs"), { recursive: true });
      await writeStatusAtomic(dir, defaultStatus(dir));
      await writeFile(join(dir, ".jri", "specs", "legacy.md"), "# Legacy\n\nBuild the legacy mode.\n");
      const fingerprint = await fingerprintSpecFile(dir, ".jri/specs/legacy.md");
      await writeInterrogationState(dir, {
        schemaVersion: 1,
        topics: {
          legacy: {
            specFile: ".jri/specs/legacy.md",
            status: "sealed",
            lastReconciledSpecFingerprint: fingerprint,
          },
        },
      });
      await rm(join(dir, ".jri", "specs", "legacy.md"));
      await collect(sendChat(dir, { message: "" }, { now: new Date("2026-05-27T20:00:00.000Z") }));
      await writeFile(join(dir, ".jri", "specs", "legacy.md"), "# Legacy\n\nBuild the restored legacy mode.\n");

      const events = await collect(
        sendChat(dir, { message: "The legacy spec is restored and should stay sealed." }, {
          interrogatorHarness: async () => ({
            handoff: {
              agent: "interrogator",
              action: "specsUpdated",
              specFiles: [".jri/specs/legacy.md"],
              sealedSpecFiles: [".jri/specs/legacy.md"],
              summary: "Restored legacy mode.",
            },
          }),
        }),
      );
      const state = JSON.parse(await readFile(join(dir, ".jri", "interrogation-state.json"), "utf8"));

      expect(events.at(-1)).toMatchObject({
        type: "specsUpdated",
        data: {
          specFiles: [".jri/specs/legacy.md"],
          sealedSpecFiles: [".jri/specs/legacy.md"],
          summary: "Restored legacy mode.",
        },
      });
      expect(state.topics.legacy).toMatchObject({
        specFile: ".jri/specs/legacy.md",
        status: "sealed",
      });
      expect(state.topics.legacy.pendingReconciliation).toBeUndefined();
      expect(state.topics.legacy.lastReconciledSpecFingerprint).not.toBe(fingerprint);
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

      expect(events.some((event) => event.type === "blockerResolved")).toBe(false);
      expect(status.blocker.resolution).toMatchObject({ status: "verified", verificationSummary: "Deployment token is present." });
      expect(status.lock).toBeUndefined();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("ordinary interrogator humanTaskVerified handoff does not verify a blocker", async () => {
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
        sendChat(dir, { message: "I think the token is probably ready." }, {
          interrogatorHarness: async () => ({
            handoff: {
              agent: "interrogator",
              action: "humanTaskVerified",
              verificationSummary: "Deployment token is present.",
            },
          }),
        }),
      );
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const assistantText = events.find((event) => event.type === "chatMessageDelta")?.data.text;

      expect(status.blocker.resolution).toBeUndefined();
      expect(status.lock).toBeUndefined();
      expect(assistantText).toContain("only recorded after you say done");
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
      expect(status.blocker.resolutionGuide.summary).toContain("no machine-checkable success criteria");
      expect(events.find((event) => event.type === "chatMessageDelta")).toMatchObject({
        data: { text: expect.stringContaining("remains blocked") },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("done verifies machine-checkable human-task success criteria without exposing secrets", async () => {
    const dir = await tempProject();
    const previousToken = process.env.JRI_CHAT_TEST_DEPLOY_TOKEN;
    try {
      process.env.JRI_CHAT_TEST_DEPLOY_TOKEN = "present";
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
            steps: ["Set the deployment token outside chat."],
            successCriteria: ["env JRI_CHAT_TEST_DEPLOY_TOKEN is set"],
            resumeInstruction: "Say done in bare jri after the token is available.",
            sensitive: true,
          },
        },
      });

      const events = await collect(sendChat(dir, { message: "done" }));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const assistantText = events.find((event) => event.type === "chatMessageDelta")?.data.text;

      expect(events.some((event) => event.type === "blockerResolved")).toBe(false);
      expect(status.blocker.resolution).toMatchObject({
        status: "verified",
        verificationSummary: "Verified 1 machine-checkable success criterion.",
      });
      expect(assistantText).not.toContain("present");
    } finally {
      restoreEnv("JRI_CHAT_TEST_DEPLOY_TOKEN", previousToken);
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("done verifies project-relative path-exists human-task success criteria", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await mkdir(join(dir, "deploy"), { recursive: true });
      await writeFile(join(dir, "deploy", "token-ready.txt"), "ready\n", "utf8");
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        blocker: {
          reason: "needsHumanTask",
          description: "Deployment proof file is missing.",
          resolutionGuide: {
            summary: "Create the deployment proof file.",
            steps: ["Create deploy/token-ready.txt after configuring deployment access."],
            successCriteria: ["path exists: deploy/token-ready.txt"],
            resumeInstruction: "Say done in bare jri after the proof file exists.",
          },
        },
      });

      const events = await collect(sendChat(dir, { message: "done" }));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(events.some((event) => event.type === "blockerResolved")).toBe(false);
      expect(status.blocker.resolution).toMatchObject({
        status: "verified",
        verificationSummary: "Verified 1 machine-checkable success criterion.",
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("done keeps a human-task blocker for unsupported machine-checkable criteria", async () => {
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
          description: "Cloudflare account setup needs confirmation.",
          resolutionGuide: {
            summary: "Confirm the Cloudflare account manually.",
            steps: ["Finish account setup outside chat."],
            successCriteria: ["Cloudflare dashboard shows account active"],
            resumeInstruction: "Say done in bare jri after the account is active.",
          },
        },
      });

      const events = await collect(sendChat(dir, { message: "done" }));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));
      const assistantText = events.find((event) => event.type === "chatMessageDelta")?.data.text;

      expect(events.some((event) => event.type === "blockerResolved")).toBe(false);
      expect(status).toMatchObject({
        state: "blocked",
        blocker: {
          reason: "needsHumanTask",
          description: "Cloudflare account setup needs confirmation.",
        },
      });
      expect(status.blocker.resolution).toBeUndefined();
      expect(status.blocker.resolutionGuide.summary).toContain("could not verify");
      expect(status.blocker.resolutionGuide.steps.at(-1)).toContain("does not know how to verify");
      expect(assistantText).toContain("remains blocked");
      expect(assistantText).toContain("does not know how to verify");
      expect(assistantText).toContain("Resume: Say done in bare jri after the account is active.");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("blocked project chat emits the full resolution guide", async () => {
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
          description: "Cloudflare billing access is missing.",
          resolutionGuide: {
            summary: "A billing-capable account must be connected before deployment.",
            steps: ["Sign in to Cloudflare.", "Enable billing on the target account."],
            successCriteria: ["JRI can deploy without an account setup error."],
            resumeInstruction: "Say done in bare jri after billing is enabled.",
          },
        },
      });

      const events = await collect(sendChat(dir, { message: "what is blocked?" }));
      const assistantText = events.find((event) => event.type === "chatMessageDelta")?.message;

      expect(assistantText).toContain("JRI is blocked: Cloudflare billing access is missing.");
      expect(assistantText).toContain("1. Sign in to Cloudflare.");
      expect(assistantText).toContain("2. Enable billing on the target account.");
      expect(assistantText).toContain("Success criteria:");
      expect(assistantText).toContain("- JRI can deploy without an account setup error.");
      expect(assistantText).toContain("Resume: Say done in bare jri after billing is enabled.");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("done for ambiguous-spec blockers gives spec-resolution guidance instead of human-task verification", async () => {
    const dir = await tempProject();
    try {
      await mkdir(join(dir, ".jri", "logs"), { recursive: true });
      await writeStatusAtomic(dir, {
        ...defaultStatus(dir),
        state: "blocked",
        activeLoopId: "20260527T184210Z",
        lastLoopId: "20260527T184210Z",
        blocker: {
          reason: "ambiguousSpecs",
          description: "The deployment target is unclear.",
          resolutionGuide: {
            summary: "Clarify the deployment target.",
            steps: ["Choose Cloudflare or another deployment target.", "Confirm the production hostname."],
            successCriteria: ["The target and hostname are both explicit in specs."],
            resumeInstruction: "Clarify the target in bare jri, then say just ralph it.",
          },
        },
      });

      const events = await collect(sendChat(dir, { message: "done" }));
      const status = JSON.parse(await readFile(join(dir, ".jri", "status.json"), "utf8"));

      expect(events.some((event) => event.type === "blockerResolved")).toBe(false);
      expect(status.blocker.resolution).toBeUndefined();
      expect(events.find((event) => event.type === "chatMessageDelta")).toMatchObject({
        data: { text: expect.stringContaining("ambiguous specs") },
      });
      const assistantText = events.find((event) => event.type === "chatMessageDelta")?.message;
      expect(assistantText).toContain("1. Choose Cloudflare or another deployment target.");
      expect(assistantText).toContain("2. Confirm the production hostname.");
      expect(assistantText).toContain("- The target and hostname are both explicit in specs.");
      expect(assistantText).toContain("Resume: Clarify the target in bare jri, then say just ralph it.");
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
