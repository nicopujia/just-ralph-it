import { JriError } from "./errors";
import type { AgentConfig, AgentName, BlockerReason, LockOperation, ProjectConfig, ProjectState, ProjectStatus, ReasoningLevel } from "./types";

const agentNames = new Set<AgentName>(["interrogator", "explorer", "auditor", "planner", "builder"]);
const reasoningLevels = new Set<ReasoningLevel>(["low", "medium", "high", "xhigh"]);
const projectStates = new Set<ProjectState>(["idle", "auditing", "planning", "building", "blocked", "stopped", "halted"]);
const blockerReasons = new Set<BlockerReason>(["ambiguousSpecs", "needsHumanTask"]);
const lockOperations = new Set<LockOperation>(["audit", "plan", "build", "halt", "resume"]);

export const defaultConfig: ProjectConfig = {
  $schema: "https://justralph.it/schemas/config.schema.json",
  schemaVersion: 1,
  provider: "openai",
  modelPreset: "openai",
};

export const configJsonSchema = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "https://justralph.it/schemas/config.schema.json",
  type: "object",
  additionalProperties: false,
  required: ["schemaVersion", "provider", "modelPreset"],
  properties: {
    $schema: { type: "string" },
    schemaVersion: { const: 1 },
    provider: { enum: ["openai"] },
    modelPreset: { enum: ["openai"] },
    agents: {
      type: "object",
      additionalProperties: false,
      properties: {
        interrogator: { $ref: "#/$defs/agentConfig" },
        explorer: { $ref: "#/$defs/agentConfig" },
        auditor: { $ref: "#/$defs/agentConfig" },
        planner: { $ref: "#/$defs/agentConfig" },
        builder: { $ref: "#/$defs/agentConfig" },
      },
    },
  },
  $defs: {
    agentConfig: {
      type: "object",
      additionalProperties: false,
      properties: {
        model: { type: "string", minLength: 1 },
        reasoning: { enum: ["low", "medium", "high", "xhigh"] },
      },
      anyOf: [{ required: ["model"] }, { required: ["reasoning"] }],
    },
  },
} as const;

export function defaultStatus(projectDir: string): ProjectStatus {
  return {
    schemaVersion: 1,
    projectDir,
    state: "idle",
    activeLoopId: null,
    stopRequested: false,
  };
}

export function parseJsonObject(raw: string, filePath: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new JriError(
      `${filePath} is not valid JSON.`,
      "invalid-json",
      `Fix the JSON syntax in ${filePath}, then run jri again. ${error instanceof Error ? error.message : ""}`.trim(),
    );
  }

  if (!isRecord(parsed) || Array.isArray(parsed)) {
    throw new JriError(`${filePath} must contain a JSON object.`, "invalid-json-shape", `Replace ${filePath} with a JSON object.`);
  }

  return parsed;
}

export function validateConfig(value: Record<string, unknown>, filePath: string): ProjectConfig {
  const allowed = new Set(["$schema", "schemaVersion", "provider", "modelPreset", "agents"]);
  rejectUnknownKeys(value, allowed, filePath);

  if (value.schemaVersion !== 1) {
    throw new JriError(`${filePath} uses an unsupported schemaVersion.`, "unsupported-config-version", "Use schemaVersion 1.");
  }
  if (value.provider !== "openai") {
    throw new JriError(`${filePath} has an unsupported provider.`, "invalid-config", "Set provider to \"openai\".");
  }
  if (value.modelPreset !== "openai") {
    throw new JriError(`${filePath} has an unsupported modelPreset.`, "invalid-config", "Set modelPreset to \"openai\".");
  }
  if ("$schema" in value && typeof value.$schema !== "string") {
    throw new JriError(`${filePath} has an invalid $schema value.`, "invalid-config", "$schema must be a string when present.");
  }

  const agents = validateAgents(value.agents, filePath);
  return {
    ...(typeof value.$schema === "string" ? { $schema: value.$schema } : {}),
    schemaVersion: 1,
    provider: "openai",
    modelPreset: "openai",
    ...(agents ? { agents } : {}),
  };
}

export function validateStatus(value: Record<string, unknown>, filePath: string): ProjectStatus {
  rejectUnknownKeys(
    value,
    new Set([
      "schemaVersion",
      "projectDir",
      "state",
      "activeLoopId",
      "lastLoopId",
      "authorizedSpecsFingerprint",
      "iteration",
      "iterations",
      "startedAt",
      "finishedAt",
      "stopRequested",
      "process",
      "blocker",
      "currentIteration",
      "lastResult",
      "recoveryNote",
      "lock",
    ]),
    filePath,
  );

  if (value.schemaVersion !== 1) {
    throw new JriError(`${filePath} uses an unsupported schemaVersion.`, "unsupported-status-version", "Use schemaVersion 1.");
  }
  if (typeof value.projectDir !== "string" || value.projectDir.length === 0) {
    throw new JriError(`${filePath} must include projectDir.`, "invalid-status", "Set projectDir to the absolute project root.");
  }
  if (typeof value.state !== "string" || !projectStates.has(value.state as ProjectState)) {
    throw new JriError(`${filePath} has an invalid state.`, "invalid-status", "Use a supported JRI state.");
  }
  if (value.activeLoopId !== null && typeof value.activeLoopId !== "string") {
    throw new JriError(`${filePath} has an invalid activeLoopId.`, "invalid-status", "Set activeLoopId to null or a loop id string.");
  }
  if (typeof value.stopRequested !== "boolean") {
    throw new JriError(`${filePath} must include stopRequested as a boolean.`, "invalid-status", "Set stopRequested to true or false.");
  }
  validateOptionalString(value.lastLoopId, "lastLoopId", filePath);
  validateOptionalString(value.authorizedSpecsFingerprint, "authorizedSpecsFingerprint", filePath);
  validateOptionalInteger(value.iteration, "iteration", filePath);
  validateOptionalInteger(value.iterations, "iterations", filePath);
  validateOptionalString(value.startedAt, "startedAt", filePath);
  validateOptionalString(value.finishedAt, "finishedAt", filePath);
  validateProcess(value.process, filePath);
  validateBlocker(value.blocker, filePath);
  validateCurrentIteration(value.currentIteration, filePath);
  validateLastResult(value.lastResult, filePath);
  validateRecoveryNote(value.recoveryNote, filePath);
  validateLock(value.lock, filePath);

  return value as ProjectStatus;
}

function validateProcess(value: unknown, filePath: string): void {
  if (value === undefined) return;
  if (!isRecord(value) || Array.isArray(value)) {
    throw new JriError(`${filePath} process must be an object.`, "invalid-status", "Set process to an object with pid and startedAt.");
  }
  rejectUnknownKeys(value, new Set(["pid", "command", "startedAt"]), filePath);
  validateInteger(value.pid, "process.pid", filePath);
  validateOptionalString(value.command, "process.command", filePath);
  validateString(value.startedAt, "process.startedAt", filePath);
}

function validateBlocker(value: unknown, filePath: string): void {
  if (value === undefined) return;
  if (!isRecord(value) || Array.isArray(value)) {
    throw new JriError(`${filePath} blocker must be an object.`, "invalid-status", "Set blocker to a valid blocker object.");
  }
  rejectUnknownKeys(value, new Set(["reason", "description", "resolutionGuide", "changedFiles", "validationRan", "resumePhase", "resolution"]), filePath);
  if (typeof value.reason !== "string" || !blockerReasons.has(value.reason as BlockerReason)) {
    throw new JriError(`${filePath} blocker has an invalid reason.`, "invalid-status", "Use ambiguousSpecs or needsHumanTask.");
  }
  validateString(value.description, "blocker.description", filePath);
  validateResolutionGuide(value.resolutionGuide, filePath);
  validateOptionalStringArray(value.changedFiles, "blocker.changedFiles", filePath);
  if (value.validationRan !== undefined && typeof value.validationRan !== "boolean") {
    throw new JriError(`${filePath} blocker.validationRan must be a boolean.`, "invalid-status", "Set blocker.validationRan to true or false.");
  }
  if (value.resumePhase !== undefined && value.resumePhase !== "planning" && value.resumePhase !== "building") {
    throw new JriError(`${filePath} blocker.resumePhase must be planning or building.`, "invalid-status", "Set blocker.resumePhase to planning or building.");
  }
  validateBlockerResolution(value.resolution, filePath);
}

function validateResolutionGuide(value: unknown, filePath: string): void {
  if (!isRecord(value) || Array.isArray(value)) {
    throw new JriError(`${filePath} blocker.resolutionGuide must be an object.`, "invalid-status", "Set a resolution guide with summary, steps, and resumeInstruction.");
  }
  rejectUnknownKeys(value, new Set(["summary", "steps", "successCriteria", "resumeInstruction", "sensitive"]), filePath);
  validateString(value.summary, "blocker.resolutionGuide.summary", filePath);
  validateStringArray(value.steps, "blocker.resolutionGuide.steps", filePath);
  validateOptionalStringArray(value.successCriteria, "blocker.resolutionGuide.successCriteria", filePath);
  validateString(value.resumeInstruction, "blocker.resolutionGuide.resumeInstruction", filePath);
  if (value.sensitive !== undefined && typeof value.sensitive !== "boolean") {
    throw new JriError(`${filePath} blocker.resolutionGuide.sensitive must be a boolean.`, "invalid-status", "Set sensitive to true or false.");
  }
}

function validateBlockerResolution(value: unknown, filePath: string): void {
  if (value === undefined) return;
  if (!isRecord(value) || Array.isArray(value)) {
    throw new JriError(`${filePath} blocker.resolution must be an object.`, "invalid-status", "Set blocker.resolution to a valid resolution object.");
  }
  rejectUnknownKeys(value, new Set(["status", "verifiedAt", "verificationSummary"]), filePath);
  if (value.status !== "verified") {
    throw new JriError(`${filePath} blocker.resolution has an invalid status.`, "invalid-status", "Set blocker.resolution.status to verified.");
  }
  validateString(value.verifiedAt, "blocker.resolution.verifiedAt", filePath);
  validateOptionalString(value.verificationSummary, "blocker.resolution.verificationSummary", filePath);
}

function validateCurrentIteration(value: unknown, filePath: string): void {
  if (value === undefined) return;
  if (!isRecord(value) || Array.isArray(value)) {
    throw new JriError(`${filePath} currentIteration must be an object.`, "invalid-status", "Set currentIteration to valid iteration metadata.");
  }
  rejectUnknownKeys(value, new Set(["iteration", "rollbackCommit", "trackedTreeCleanAtStart", "dirtySummary"]), filePath);
  validateInteger(value.iteration, "currentIteration.iteration", filePath);
  validateOptionalString(value.rollbackCommit, "currentIteration.rollbackCommit", filePath);
  if (typeof value.trackedTreeCleanAtStart !== "boolean") {
    throw new JriError(`${filePath} currentIteration.trackedTreeCleanAtStart must be a boolean.`, "invalid-status", "Set trackedTreeCleanAtStart to true or false.");
  }
  validateOptionalString(value.dirtySummary, "currentIteration.dirtySummary", filePath);
}

function validateLastResult(value: unknown, filePath: string): void {
  if (value === undefined) return;
  if (!isRecord(value) || Array.isArray(value)) {
    throw new JriError(`${filePath} lastResult must be an object.`, "invalid-status", "Set lastResult to a valid result object.");
  }
  rejectUnknownKeys(value, new Set(["outcome", "summary", "url", "validationPassed", "commit", "tag"]), filePath);
  if (typeof value.outcome !== "string" || !new Set(["completed", "stopped", "halted", "blocked", "failed"]).has(value.outcome)) {
    throw new JriError(`${filePath} lastResult has an invalid outcome.`, "invalid-status", "Use a supported lastResult outcome.");
  }
  validateOptionalString(value.summary, "lastResult.summary", filePath);
  validateOptionalString(value.url, "lastResult.url", filePath);
  if (value.validationPassed !== undefined && typeof value.validationPassed !== "boolean") {
    throw new JriError(`${filePath} lastResult.validationPassed must be a boolean.`, "invalid-status", "Set validationPassed to true or false.");
  }
  validateOptionalString(value.commit, "lastResult.commit", filePath);
  validateOptionalString(value.tag, "lastResult.tag", filePath);
}

function validateRecoveryNote(value: unknown, filePath: string): void {
  if (value === undefined) return;
  if (!isRecord(value) || Array.isArray(value)) {
    throw new JriError(`${filePath} recoveryNote must be an object.`, "invalid-status", "Set recoveryNote to a valid recovery note.");
  }
  rejectUnknownKeys(value, new Set(["timestamp", "message", "repairedFrom"]), filePath);
  validateString(value.timestamp, "recoveryNote.timestamp", filePath);
  validateString(value.message, "recoveryNote.message", filePath);
  validateOptionalString(value.repairedFrom, "recoveryNote.repairedFrom", filePath);
}

function validateLock(value: unknown, filePath: string): void {
  if (value === undefined) return;
  if (!isRecord(value) || Array.isArray(value)) {
    throw new JriError(`${filePath} lock must be an object.`, "invalid-status", "Set lock to a valid daemon lock object.");
  }
  rejectUnknownKeys(value, new Set(["owner", "pid", "operation", "acquiredAt", "heartbeatAt", "expiresAt"]), filePath);
  if (value.owner !== "daemon") {
    throw new JriError(`${filePath} lock has an invalid owner.`, "invalid-status", "Set lock.owner to daemon.");
  }
  validateInteger(value.pid, "lock.pid", filePath);
  if (typeof value.operation !== "string" || !lockOperations.has(value.operation as LockOperation)) {
    throw new JriError(`${filePath} lock has an invalid operation.`, "invalid-status", "Use audit, plan, build, halt, or resume.");
  }
  validateString(value.acquiredAt, "lock.acquiredAt", filePath);
  validateString(value.heartbeatAt, "lock.heartbeatAt", filePath);
  validateString(value.expiresAt, "lock.expiresAt", filePath);
}

function validateAgents(value: unknown, filePath: string): ProjectConfig["agents"] | undefined {
  if (value === undefined) return undefined;
  if (!isRecord(value) || Array.isArray(value)) {
    throw new JriError(`${filePath} agents must be an object.`, "invalid-config", "Set agents to an object keyed by JRI agent name.");
  }

  const agents: Partial<Record<AgentName, AgentConfig>> = {};
  for (const [name, config] of Object.entries(value)) {
    if (!agentNames.has(name as AgentName)) {
      throw new JriError(`${filePath} contains an unknown agent override: ${name}.`, "invalid-config", "Remove unknown agent keys.");
    }
    if (!isRecord(config) || Array.isArray(config)) {
      throw new JriError(`${filePath} agent override ${name} must be an object.`, "invalid-config", "Use model and/or reasoning fields.");
    }
    rejectUnknownKeys(config, new Set(["model", "reasoning"]), filePath);
    if (config.model !== undefined && (typeof config.model !== "string" || config.model.length === 0)) {
      throw new JriError(`${filePath} agent override ${name} has an invalid model.`, "invalid-config", "Use a non-empty model string.");
    }
    if (config.reasoning !== undefined && (typeof config.reasoning !== "string" || !reasoningLevels.has(config.reasoning as ReasoningLevel))) {
      throw new JriError(`${filePath} agent override ${name} has an invalid reasoning level.`, "invalid-config", "Use low, medium, high, or xhigh.");
    }
    if (config.model === undefined && config.reasoning === undefined) {
      throw new JriError(`${filePath} agent override ${name} must set model or reasoning.`, "invalid-config", "Set at least one override field.");
    }

    agents[name as AgentName] = {
      ...(typeof config.model === "string" ? { model: config.model } : {}),
      ...(typeof config.reasoning === "string" ? { reasoning: config.reasoning as ReasoningLevel } : {}),
    };
  }

  return agents;
}

function rejectUnknownKeys(value: Record<string, unknown>, allowed: Set<string>, filePath: string): void {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new JriError(`${filePath} contains unknown field ${key}.`, "invalid-schema-field", `Remove ${key} from ${filePath}.`);
    }
  }
}

function validateString(value: unknown, field: string, filePath: string): void {
  if (typeof value !== "string" || value.length === 0) {
    throw new JriError(`${filePath} has an invalid ${field}.`, "invalid-status", `${field} must be a non-empty string.`);
  }
}

function validateOptionalString(value: unknown, field: string, filePath: string): void {
  if (value !== undefined) validateString(value, field, filePath);
}

function validateInteger(value: unknown, field: string, filePath: string): void {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    throw new JriError(`${filePath} has an invalid ${field}.`, "invalid-status", `${field} must be a non-negative integer.`);
  }
}

function validateOptionalInteger(value: unknown, field: string, filePath: string): void {
  if (value !== undefined) validateInteger(value, field, filePath);
}

function validateStringArray(value: unknown, field: string, filePath: string): void {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || item.length === 0)) {
    throw new JriError(`${filePath} has an invalid ${field}.`, "invalid-status", `${field} must be an array of non-empty strings.`);
  }
}

function validateOptionalStringArray(value: unknown, field: string, filePath: string): void {
  if (value !== undefined) validateStringArray(value, field, filePath);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
