import { JriError } from "./errors";
import type { AgentConfig, AgentName, ProjectConfig, ProjectState, ProjectStatus, ReasoningLevel } from "./types";

const agentNames = new Set<AgentName>(["interrogator", "explorer", "auditor", "planner", "builder"]);
const reasoningLevels = new Set<ReasoningLevel>(["low", "medium", "high", "xhigh"]);
const projectStates = new Set<ProjectState>(["idle", "auditing", "planning", "building", "blocked", "stopped", "halted"]);

export const defaultConfig: ProjectConfig = {
  $schema: "https://justralph.it/schemas/config.schema.json",
  schemaVersion: 1,
  provider: "openai",
  modelPreset: "openai",
};

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

  return value as ProjectStatus;
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
