import { describe, expect, test } from "bun:test";
import { extractLatestBuilderHandoffFromText, extractLatestHandoffFromText, parseHandoff } from "../src/core/handoffs";

const blocker = {
  reason: "needsHumanTask",
  description: "Deployment credentials are not available.",
  resolutionGuide: {
    summary: "Provide credentials outside chat.",
    steps: ["Set the deployment token in the environment."],
    resumeInstruction: "Say done in bare jri after the token is available.",
  },
};

describe("agent handoff contracts", () => {
  test("validates interrogator start requests and spec updates", () => {
    expect(parseHandoff("interrogator", { agent: "interrogator", action: "startRequested", trigger: "ralfealo" })).toEqual({
      agent: "interrogator",
      action: "startRequested",
      trigger: "ralfealo",
    });

    expect(
      parseHandoff("interrogator", {
        agent: "interrogator",
        action: "specsUpdated",
        specFiles: [".jri/specs/app.md"],
        sealedSpecFiles: [".jri/specs/app.md"],
        summary: "App behavior clarified.",
      }),
    ).toMatchObject({ action: "specsUpdated", specFiles: [".jri/specs/app.md"], sealedSpecFiles: [".jri/specs/app.md"] });

    expect(() => parseHandoff("interrogator", { agent: "interrogator", action: "startRequested", trigger: "please just ralph it" })).toThrow(
      "startRequested requires trigger",
    );
  });

  test("validates auditor, planner, builder, validation, and verifier handoffs", () => {
    expect(
      parseHandoff("auditor", {
        agent: "auditor",
        action: "passed",
        specFiles: [".jri/specs/app.md"],
        specsFingerprint: "abc123",
      }),
    ).toMatchObject({ action: "passed", specsFingerprint: "abc123" });

    expect(
      parseHandoff("planner", {
        agent: "planner",
        action: "planned",
        planPath: ".jri/IMPLEMENTATION_PLAN.md",
        summary: "Implement handoff contracts first.",
      }),
    ).toMatchObject({ action: "planned" });

    expect(
      parseHandoff("builder", {
        agent: "builder",
        action: "failedValidation",
        validation: {
          command: "bun run test",
          exitCode: 1,
          passed: false,
          summary: "A handoff parser test failed.",
        },
      }),
    ).toMatchObject({ action: "failedValidation", validation: { passed: false } });

    expect(parseHandoff("verifier", { agent: "verifier", action: "stillBlocked", blocker })).toMatchObject({
      action: "stillBlocked",
      blocker: { reason: "needsHumanTask" },
    });
  });

  test("extracts a bounded handoff and rejects missing handoffs", () => {
    const text = ["free-form prose", 'JRI_HANDOFF_JSON: {"agent":"builder","action":"complete","summary":"Scope complete.","url":"https://example.test"}', ""].join("\n");

    expect(extractLatestBuilderHandoffFromText(text)).toMatchObject({ action: "complete", url: "https://example.test" });
    expect(() => extractLatestHandoffFromText("planner", "planner finished but forgot JSON")).toThrow("did not emit");
  });

  test("rejects ambiguous or trailing malformed handoff records instead of reusing an earlier decision", () => {
    const valid = 'JRI_HANDOFF_JSON: {"agent":"builder","action":"complete","summary":"Scope complete."}';

    expect(() => extractLatestBuilderHandoffFromText([valid, 'JRI_HANDOFF_JSON: {"agent":"builder","action":"continue"', ""].join("\n"))).toThrow(
      "multiple machine-readable JRI handoffs",
    );

    expect(() => extractLatestBuilderHandoffFromText('JRI_HANDOFF_JSON: {"agent":"builder","action":"continue"')).toThrow("not valid JSON");
  });

  test("rejects handoff arrays with whitespace-only entries", () => {
    expect(() =>
      parseHandoff("auditor", {
        agent: "auditor",
        action: "failed",
        feedback: "Specs need sharper acceptance criteria.",
        questions: ["   "],
      }),
    ).toThrow("questions must be a non-empty array of strings");

    expect(() =>
      parseHandoff("builder", {
        agent: "builder",
        action: "blocked",
        blocker: {
          ...blocker,
          resolutionGuide: {
            ...blocker.resolutionGuide,
            steps: ["Set the deployment token in the environment.", "  "],
          },
        },
      }),
    ).toThrow("blocker.resolutionGuide.steps must be a non-empty array of strings");

    expect(() =>
      parseHandoff("interrogator", {
        agent: "interrogator",
        action: "specsUpdated",
        specFiles: ["   "],
        summary: "Updated specs.",
      }),
    ).toThrow("specFiles must be a non-empty array of strings");
  });

  test("accepts legacy builder blocker and replan lines during transition", () => {
    expect(extractLatestBuilderHandoffFromText(`JRI_BLOCKER_JSON: ${JSON.stringify(blocker)}`)).toMatchObject({
      action: "blocked",
      blocker: { reason: "needsHumanTask" },
    });
    expect(extractLatestBuilderHandoffFromText("JRI_NEEDS_REPLAN: plan drifted")).toMatchObject({
      action: "needsReplan",
      reason: "plan drifted",
    });
  });
});
