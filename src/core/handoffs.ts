import { JriError } from "./errors";
import type {
  AgentHandoff,
  ArtifactRef,
  AuditorHandoff,
  Blocker,
  BlockerReason,
  BuilderHandoff,
  HumanTaskVerificationHandoff,
  InterrogatorHandoff,
  PlannerHandoff,
  ValidationHandoff,
} from "./types";

export const handoffPrefix = "JRI_HANDOFF_JSON:";
export const legacyBlockerPrefix = "JRI_BLOCKER_JSON:";
export const legacyReplanPrefix = "JRI_NEEDS_REPLAN:";

export type HandoffAgent = AgentHandoff["agent"];

export function parseHandoff(agent: "interrogator", value: unknown): InterrogatorHandoff;
export function parseHandoff(agent: "auditor", value: unknown): AuditorHandoff;
export function parseHandoff(agent: "planner", value: unknown): PlannerHandoff;
export function parseHandoff(agent: "builder", value: unknown): BuilderHandoff;
export function parseHandoff(agent: "verifier", value: unknown): HumanTaskVerificationHandoff;
export function parseHandoff(agent: HandoffAgent, value: unknown): AgentHandoff {
  if (!isRecord(value)) {
    throw invalidHandoff(agent, "The handoff must be a JSON object.");
  }
  if (value.agent !== agent) {
    throw invalidHandoff(agent, `The handoff agent must be "${agent}".`);
  }
  if (typeof value.action !== "string") {
    throw invalidHandoff(agent, "The handoff action must be a string.");
  }

  switch (agent) {
    case "interrogator":
      return parseInterrogator(value);
    case "auditor":
      return parseAuditor(value);
    case "planner":
      return parsePlanner(value);
    case "builder":
      return parseBuilder(value);
    case "verifier":
      return parseVerifier(value);
  }
}

export function parseHandoffJson(agent: HandoffAgent, rawJson: string, phaseLabel: string = agent): AgentHandoff {
  try {
    const parsed = JSON.parse(rawJson);
    switch (agent) {
      case "interrogator":
        return parseHandoff("interrogator", parsed);
      case "auditor":
        return parseHandoff("auditor", parsed);
      case "planner":
        return parseHandoff("planner", parsed);
      case "builder":
        return parseHandoff("builder", parsed);
      case "verifier":
        return parseHandoff("verifier", parsed);
    }
  } catch (error) {
    if (error instanceof JriError) throw error;
    throw new JriError(
      `The ${phaseLabel} handoff is not valid JSON.`,
      "invalid-agent-handoff",
      `Emit ${handoffPrefix} followed by valid JSON for the ${phaseLabel} contract. ${error instanceof Error ? error.message : ""}`.trim(),
    );
  }
}

export function extractLatestHandoffFromText(agent: HandoffAgent, text: string, phaseLabel: string = agent): AgentHandoff {
  const rawJson = extractSinglePrefixedRecord(text, handoffPrefix, phaseLabel);
  return parseHandoffJson(agent, rawJson, phaseLabel);
}

export function extractLatestBuilderHandoffFromText(text: string, phaseLabel = "builder"): BuilderHandoff {
  const handoffRecords = extractPrefixedRecords(text, handoffPrefix);
  if (handoffRecords.length > 0) {
    assertSingleRecord(handoffRecords, phaseLabel, handoffPrefix);
    return parseHandoff("builder", parseRawJson(onlyRecord(handoffRecords), phaseLabel)) as BuilderHandoff;
  }

  const blockerRecords = extractPrefixedRecords(text, legacyBlockerPrefix);
  if (blockerRecords.length > 0) {
    assertSingleRecord(blockerRecords, "builder blocker", legacyBlockerPrefix);
    const rawJson = onlyRecord(blockerRecords);
    return { agent: "builder", action: "blocked", blocker: parseBlocker(parseRawJson(rawJson, "builder blocker")) };
  }

  const replanRecords = extractPrefixedRecords(text, legacyReplanPrefix);
  if (replanRecords.length > 0) {
    assertSingleRecord(replanRecords, "builder replan", legacyReplanPrefix);
    const reason = onlyRecord(replanRecords).trim();
    return { agent: "builder", action: "needsReplan", reason: reason || "Builder requested plan regeneration." };
  }

  throw new JriError(
    `The ${phaseLabel} phase did not emit a machine-readable JRI handoff.`,
    "missing-agent-handoff",
    `Emit exactly one line that starts with ${handoffPrefix} followed by the ${phaseLabel} JSON contract.`,
  );
}

function extractSinglePrefixedRecord(text: string, prefix: string, phaseLabel: string): string {
  const records = extractPrefixedRecords(text, prefix);
  if (records.length === 0) {
    throw new JriError(
      `The ${phaseLabel} phase did not emit a machine-readable JRI handoff.`,
      "missing-agent-handoff",
      `Emit exactly one line that starts with ${handoffPrefix} followed by the ${phaseLabel} JSON contract.`,
    );
  }
  assertSingleRecord(records, phaseLabel, prefix);
  return onlyRecord(records);
}

function extractPrefixedRecords(text: string, prefix: string): string[] {
  const records: string[] = [];
  for (const line of text.split("\n")) {
    if (line.trimStart().startsWith(prefix)) {
      records.push(line.slice(line.indexOf(prefix) + prefix.length).trim());
    }
  }
  return records;
}

function assertSingleRecord(records: string[], phaseLabel: string, prefix: string): void {
  if (records.length === 1) return;
  throw new JriError(
    `The ${phaseLabel} phase emitted multiple machine-readable JRI handoffs.`,
    "multiple-agent-handoffs",
    `Emit exactly one line that starts with ${prefix} for the ${phaseLabel} contract.`,
  );
}

function onlyRecord(records: string[]): string {
  const record = records[0];
  if (record === undefined) {
    throw new JriError(
      "The handoff record was unexpectedly missing after validation.",
      "missing-agent-handoff",
      `Emit exactly one line that starts with ${handoffPrefix} followed by the JSON contract.`,
    );
  }
  return record;
}

function parseInterrogator(value: Record<string, unknown>): InterrogatorHandoff {
  switch (value.action) {
    case "messageOnly":
      assertKnownKeys(value, "interrogator handoff", ["agent", "action", "summary"], "interrogator");
      return { agent: "interrogator", action: "messageOnly", ...optionalSummary(value, "interrogator") };
    case "specsUpdated":
      assertKnownKeys(value, "interrogator handoff", ["agent", "action", "specFiles", "summary", "sealedSpecFiles"], "interrogator");
      return {
        agent: "interrogator",
        action: "specsUpdated",
        specFiles: parseSpecFiles(value.specFiles, "interrogator"),
        summary: requiredString(value.summary, "summary", "interrogator"),
        ...(value.sealedSpecFiles === undefined ? {} : { sealedSpecFiles: parseSpecFiles(value.sealedSpecFiles, "interrogator") }),
      };
    case "scratchpadUpdated":
      assertKnownKeys(value, "interrogator handoff", ["agent", "action", "summary"], "interrogator");
      return { agent: "interrogator", action: "scratchpadUpdated", summary: requiredString(value.summary, "summary", "interrogator") };
    case "humanTaskVerified":
      assertKnownKeys(value, "interrogator handoff", ["agent", "action", "verificationSummary"], "interrogator");
      return { agent: "interrogator", action: "humanTaskVerified", ...optionalVerificationSummary(value, "interrogator") };
    case "humanTaskStillBlocked":
      assertKnownKeys(value, "interrogator handoff", ["agent", "action", "blocker"], "interrogator");
      return { agent: "interrogator", action: "humanTaskStillBlocked", blocker: parseBlocker(value.blocker) };
    case "startRequested": {
      assertKnownKeys(value, "interrogator handoff", ["agent", "action", "trigger"], "interrogator");
      const trigger = value.trigger;
      if (trigger !== "just ralph it" && trigger !== "ralfealo") {
        throw invalidHandoff("interrogator", "startRequested requires trigger just ralph it or ralfealo.");
      }
      return { agent: "interrogator", action: "startRequested", trigger };
    }
    default:
      throw invalidHandoff("interrogator", "Unsupported interrogator handoff action.");
  }
}

function parseAuditor(value: Record<string, unknown>): AuditorHandoff {
  switch (value.action) {
    case "passed":
      assertKnownKeys(value, "auditor handoff", ["agent", "action", "specFiles", "specsFingerprint", "summary"], "auditor");
      return {
        agent: "auditor",
        action: "passed",
        specFiles: parseSpecFiles(value.specFiles, "auditor"),
        specsFingerprint: requiredString(value.specsFingerprint, "specsFingerprint", "auditor"),
        ...optionalSummary(value, "auditor"),
      };
    case "failed":
      assertKnownKeys(value, "auditor handoff", ["agent", "action", "feedback", "ambiguousSpecFiles", "questions"], "auditor");
      return {
        agent: "auditor",
        action: "failed",
        feedback: requiredString(value.feedback, "feedback", "auditor"),
        ...(value.ambiguousSpecFiles === undefined ? {} : { ambiguousSpecFiles: parseSpecFiles(value.ambiguousSpecFiles, "auditor") }),
        questions: requiredStringArray(value.questions, "questions", "auditor"),
      };
    default:
      throw invalidHandoff("auditor", "Unsupported auditor handoff action.");
  }
}

function parsePlanner(value: Record<string, unknown>): PlannerHandoff {
  switch (value.action) {
    case "planned":
      assertKnownKeys(value, "planner handoff", ["agent", "action", "planPath", "summary"], "planner");
      if (value.planPath !== ".jri/IMPLEMENTATION_PLAN.md") {
        throw invalidHandoff("planner", "planned requires planPath .jri/IMPLEMENTATION_PLAN.md.");
      }
      return {
        agent: "planner",
        action: "planned",
        planPath: ".jri/IMPLEMENTATION_PLAN.md",
        summary: requiredString(value.summary, "summary", "planner"),
      };
    case "blocked":
      assertKnownKeys(value, "planner handoff", ["agent", "action", "blocker"], "planner");
      return { agent: "planner", action: "blocked", blocker: parseBlocker(value.blocker) };
    default:
      throw invalidHandoff("planner", "Unsupported planner handoff action.");
  }
}

function parseBuilder(value: Record<string, unknown>): BuilderHandoff {
  const validation = optionalValidationList(value.validation, "builder");
  switch (value.action) {
    case "continue":
      assertKnownKeys(value, "builder handoff", ["agent", "action", "summary", "url", "artifacts", "validation"], "builder");
      return {
        agent: "builder",
        action: "continue",
        summary: requiredString(value.summary, "summary", "builder"),
        ...optionalUrl(value, "builder"),
        ...optionalArtifacts(value, "builder"),
        ...(validation ? { validation } : {}),
      };
    case "complete":
      assertKnownKeys(value, "builder handoff", ["agent", "action", "summary", "url", "artifacts", "validation"], "builder");
      return {
        agent: "builder",
        action: "complete",
        summary: requiredString(value.summary, "summary", "builder"),
        ...optionalUrl(value, "builder"),
        ...optionalArtifacts(value, "builder"),
        ...(validation ? { validation } : {}),
      };
    case "blocked":
      assertKnownKeys(value, "builder handoff", ["agent", "action", "blocker", "validation"], "builder");
      return { agent: "builder", action: "blocked", blocker: parseBlocker(value.blocker), ...(validation ? { validation } : {}) };
    case "needsReplan":
      assertKnownKeys(value, "builder handoff", ["agent", "action", "reason", "summary", "validation"], "builder");
      return {
        agent: "builder",
        action: "needsReplan",
        reason: requiredString(value.reason, "reason", "builder"),
        ...optionalSummary(value, "builder"),
        ...(validation ? { validation } : {}),
      };
    case "failedValidation":
      assertKnownKeys(value, "builder handoff", ["agent", "action", "validation", "summary"], "builder");
      return {
        agent: "builder",
        action: "failedValidation",
        validation: parseValidation(value.validation, "builder"),
        ...optionalSummary(value, "builder"),
      };
    default:
      throw invalidHandoff("builder", "Unsupported builder handoff action.");
  }
}

function parseVerifier(value: Record<string, unknown>): HumanTaskVerificationHandoff {
  switch (value.action) {
    case "verified":
      assertKnownKeys(value, "verifier handoff", ["agent", "action", "verificationSummary"], "verifier");
      return { agent: "verifier", action: "verified", ...optionalVerificationSummary(value, "verifier") };
    case "stillBlocked":
      assertKnownKeys(value, "verifier handoff", ["agent", "action", "blocker"], "verifier");
      return { agent: "verifier", action: "stillBlocked", blocker: parseBlocker(value.blocker) };
    default:
      throw invalidHandoff("verifier", "Unsupported verifier handoff action.");
  }
}

export function parseBlocker(value: unknown): Blocker {
  if (!isRecord(value)) {
    throw invalidHandoff("builder", "The blocker must be a JSON object.");
  }
  assertKnownKeys(value, "builder blocker", ["reason", "description", "resolutionGuide", "changedFiles", "validationRan", "resumePhase"], "builder");
  const reason = value.reason;
  if (reason !== "ambiguousSpecs" && reason !== "needsHumanTask") {
    throw invalidHandoff("builder", "The blocker reason must be ambiguousSpecs or needsHumanTask.");
  }
  const guide = value.resolutionGuide;
  if (!isRecord(guide)) {
    throw invalidHandoff("builder", "The blocker needs a resolutionGuide object.");
  }
  assertKnownKeys(guide, "builder blocker.resolutionGuide", ["summary", "steps", "successCriteria", "resumeInstruction", "sensitive"], "builder");
  return {
    reason: reason as BlockerReason,
    description: requiredString(value.description, "blocker.description", "builder"),
    resolutionGuide: {
      summary: requiredString(guide.summary, "blocker.resolutionGuide.summary", "builder"),
      steps: requiredStringArray(guide.steps, "blocker.resolutionGuide.steps", "builder"),
      ...(guide.successCriteria === undefined
        ? {}
        : { successCriteria: requiredStringArray(guide.successCriteria, "blocker.resolutionGuide.successCriteria", "builder") }),
      resumeInstruction: requiredString(guide.resumeInstruction, "blocker.resolutionGuide.resumeInstruction", "builder"),
      ...(guide.sensitive === undefined ? {} : { sensitive: requiredBoolean(guide.sensitive, "blocker.resolutionGuide.sensitive", "builder") }),
    },
    ...(value.changedFiles === undefined ? {} : { changedFiles: requiredStringArray(value.changedFiles, "blocker.changedFiles", "builder") }),
    ...(value.validationRan === undefined ? {} : { validationRan: requiredBoolean(value.validationRan, "blocker.validationRan", "builder") }),
    ...(value.resumePhase === undefined ? {} : { resumePhase: requiredResumePhase(value.resumePhase, "blocker.resumePhase", "builder") }),
  };
}

function parseValidation(value: unknown, agent: HandoffAgent): ValidationHandoff {
  if (!isRecord(value)) {
    throw invalidHandoff(agent, "The validation handoff must be an object.");
  }
  assertKnownKeys(value, `${agent} validation`, ["command", "exitCode", "passed", "summary", "artifacts"], agent);
  return {
    command: requiredString(value.command, "validation.command", agent),
    exitCode: requiredInteger(value.exitCode, "validation.exitCode", agent),
    passed: requiredBoolean(value.passed, "validation.passed", agent),
    summary: requiredString(value.summary, "validation.summary", agent),
    ...optionalArtifacts(value, agent),
  };
}

function optionalValidationList(value: unknown, agent: HandoffAgent): ValidationHandoff[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value)) return [parseValidation(value, agent)];
  return value.map((item) => parseValidation(item, agent));
}

function parseRawJson(rawJson: string, phaseLabel: string): unknown {
  try {
    return JSON.parse(rawJson);
  } catch (error) {
    throw new JriError(
      `The ${phaseLabel} handoff is not valid JSON.`,
      "invalid-agent-handoff",
      `Emit ${handoffPrefix} followed by valid JSON for the ${phaseLabel} contract. ${error instanceof Error ? error.message : ""}`.trim(),
    );
  }
}

function parseSpecFiles(value: unknown, agent: HandoffAgent): string[] {
  const files = requiredStringArray(value, "specFiles", agent);
  for (const file of files) {
    if (!file.startsWith(".jri/specs/") || file.includes("..")) {
      throw invalidHandoff(agent, "Spec files must be stable .jri/specs/* paths.");
    }
  }
  return files;
}

function optionalSummary(value: Record<string, unknown>, agent: HandoffAgent): { summary?: string } {
  return value.summary === undefined ? {} : { summary: requiredString(value.summary, "summary", agent) };
}

function optionalVerificationSummary(value: Record<string, unknown>, agent: HandoffAgent): { verificationSummary?: string } {
  return value.verificationSummary === undefined
    ? {}
    : { verificationSummary: requiredString(value.verificationSummary, "verificationSummary", agent) };
}

function optionalUrl(value: Record<string, unknown>, agent: HandoffAgent): { url?: string } {
  return value.url === undefined ? {} : { url: requiredString(value.url, "url", agent) };
}

function optionalArtifacts(value: Record<string, unknown>, agent: HandoffAgent): { artifacts?: ArtifactRef[] } {
  if (value.artifacts === undefined) return {};
  if (!Array.isArray(value.artifacts)) {
    throw invalidHandoff(agent, "artifacts must be an array.");
  }
  return { artifacts: value.artifacts.map((artifact) => parseArtifact(artifact, agent)) };
}

function parseArtifact(value: unknown, agent: HandoffAgent): ArtifactRef {
  if (!isRecord(value)) {
    throw invalidHandoff(agent, "Artifact references must be objects.");
  }
  assertKnownKeys(value, `${agent} artifact`, ["path", "summary"], agent);
  const path = requiredString(value.path, "artifact.path", agent);
  if (!path.startsWith(".jri/logs/") || path.includes("..")) {
    throw invalidHandoff(agent, "Artifact paths must be stable .jri/logs/* paths.");
  }
  return {
    path: path as `.jri/logs/${string}`,
    ...(value.summary === undefined ? {} : { summary: requiredString(value.summary, "artifact.summary", agent) }),
  };
}

function requiredString(value: unknown, field: string, agent: HandoffAgent): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw invalidHandoff(agent, `${field} must be a non-empty string.`);
  }
  return value.trim();
}

function requiredStringArray(value: unknown, field: string, agent: HandoffAgent): string[] {
  if (
    !Array.isArray(value) ||
    value.length === 0 ||
    !value.every((item) => typeof item === "string" && item.trim().length > 0)
  ) {
    throw invalidHandoff(agent, `${field} must be a non-empty array of strings.`);
  }
  return value.map((item) => item.trim());
}

function requiredBoolean(value: unknown, field: string, agent: HandoffAgent): boolean {
  if (typeof value !== "boolean") {
    throw invalidHandoff(agent, `${field} must be boolean.`);
  }
  return value;
}

function requiredInteger(value: unknown, field: string, agent: HandoffAgent): number {
  if (!Number.isInteger(value)) {
    throw invalidHandoff(agent, `${field} must be an integer.`);
  }
  return value as number;
}

function requiredResumePhase(value: unknown, field: string, agent: HandoffAgent): "planning" | "building" {
  if (value !== "planning" && value !== "building") {
    throw invalidHandoff(agent, `${field} must be planning or building.`);
  }
  return value;
}

function invalidHandoff(agent: HandoffAgent, message: string): JriError {
  return new JriError(message, "invalid-agent-handoff", `Emit ${handoffPrefix} followed by valid JSON for the ${agent} contract.`);
}

function assertKnownKeys(value: Record<string, unknown>, label: string, allowed: readonly string[], agent: HandoffAgent): void {
  const allowedKeys = new Set(allowed);
  for (const key of Object.keys(value)) {
    if (!allowedKeys.has(key)) {
      throw invalidHandoff(agent, `Unknown ${label} key: ${key}.`);
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}
