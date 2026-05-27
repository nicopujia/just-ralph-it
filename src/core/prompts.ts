import { readdir } from "node:fs/promises";
import { join } from "node:path";
import { renderExplorerCapabilityInstructions, renderWebCapabilityInstructions } from "./capabilities";
import type { AgentConfig, AgentName, ProjectConfig, ReasoningLevel } from "./types";

const openAiPreset: Record<AgentName, Required<AgentConfig>> = {
  interrogator: { model: "gpt-5.5", reasoning: "xhigh" },
  explorer: { model: "gpt-5.3-codex-spark", reasoning: "xhigh" },
  auditor: { model: "gpt-5.4", reasoning: "xhigh" },
  planner: { model: "gpt-5.4", reasoning: "xhigh" },
  builder: { model: "gpt-5.5", reasoning: "xhigh" },
};

export function modelForAgent(config: unknown, agent: AgentName): Required<AgentConfig> {
  const parsed = isProjectConfig(config) ? config : undefined;
  return {
    model: parsed?.agents?.[agent]?.model ?? openAiPreset[agent].model,
    reasoning: parsed?.agents?.[agent]?.reasoning ?? openAiPreset[agent].reasoning,
  };
}

export async function buildPiPrompt(
  projectDir: string,
  phase: "interrogation" | "auditing" | "planning" | "building" | "explorer",
  options: { loopId?: string; contextRefs?: string[]; contextInline?: string[]; explorerTask?: string; userMessage?: string } = {},
): Promise<string> {
  const specFiles = await listSpecFiles(projectDir);
  const specs = await Promise.all(
    specFiles.map(async (path) => {
      const text = await Bun.file(join(projectDir, path)).text();
      return `# ${path}\n\n${text.trim()}`;
    }),
  );
  const agents = await readIfExists(join(projectDir, "AGENTS.md"));
  const plan = await readIfExists(join(projectDir, ".jri", "IMPLEMENTATION_PLAN.md"));
  const scratchpad = await readIfExists(join(projectDir, ".jri", "scratchpad.md"));
  const status = await readIfExists(join(projectDir, ".jri", "status.json"));

  if (phase === "interrogation") {
    const selectedContext = await renderSelectedContext(projectDir, options.contextRefs);
    return [
      "You are the JRI interrogator. Help the user turn their idea into durable, unambiguous requirements before Ralph builds.",
      "Use .jri/specs/* as requirements truth. Use .jri/scratchpad.md only for working notes and unresolved questions.",
      "Update specs continuously when decisions become stable. Preserve manual user edits and ask targeted reconciliation questions when needed.",
      "Do not start Ralph from prose. A start is valid only when the user's current message is exactly standalone \"just ralph it\" or \"ralfealo\" after normalization.",
      'At the end, emit exactly one line starting with JRI_HANDOFF_JSON: followed by an interrogator contract JSON with action "messageOnly", "specsUpdated", "scratchpadUpdated", "humanTaskVerified", "humanTaskStillBlocked", or "startRequested".',
      'Use "specsUpdated" when you changed .jri/specs/* and include changed specFiles plus a user-facing summary. Use "scratchpadUpdated" when only scratchpad changed.',
      'Use "messageOnly" when no durable file changed. Never include secrets in handoffs.',
      agents ? `Operational guide:\n${agents}` : "",
      selectedContext ||
        [
          status ? `Current project status JSON:\n${status}` : "",
          scratchpad ? `Current scratchpad:\n${scratchpad}` : "Current scratchpad is empty.",
          specs.length ? specs.join("\n\n") : "No spec files exist yet.",
        ]
          .filter(Boolean)
          .join("\n\n"),
      ...(options.contextInline?.slice(1) ?? []),
      `Current user message:\n${options.userMessage ?? ""}`,
    ]
      .filter(Boolean)
      .join("\n\n");
  }

  if (phase === "auditing") {
    return [
      "You are the JRI auditor. Decide whether the durable specs are ready for Ralph to plan and build safely.",
      "Use only .jri/specs/* as requirements truth. Do not edit files, do not plan, and do not build.",
      "Pass only when the current build scope is sufficiently unambiguous for the planner and builder.",
      'At the end, emit exactly one line starting with JRI_HANDOFF_JSON: followed by JSON: {"agent":"auditor","action":"passed","specFiles":[".jri/specs/example.md"],"specsFingerprint":"...","summary":"..."} or {"agent":"auditor","action":"failed","feedback":"...","ambiguousSpecFiles":[".jri/specs/example.md"],"questions":["..."]}.',
      agents ? `Operational guide:\n${agents}` : "",
      specs.join("\n\n"),
    ]
      .filter(Boolean)
      .join("\n\n");
  }

  if (phase === "planning") {
    return [
      "You are the JRI planner. Create or regenerate .jri/IMPLEMENTATION_PLAN.md from the durable specs and current code.",
      "Keep the plan concise, prioritized, and focused on remaining work. Capture why implementation and tests matter.",
      "Do not commit. Do not edit requirements specs unless you find a direct contradiction that blocks implementation.",
      renderWebCapabilityInstructions(projectDir, options.loopId),
      renderExplorerCapabilityInstructions(projectDir, options.loopId),
      'At the end, emit exactly one line starting with JRI_HANDOFF_JSON: followed by JSON: {"agent":"planner","action":"planned","planPath":".jri/IMPLEMENTATION_PLAN.md","summary":"..."} or {"agent":"planner","action":"blocked","blocker":{...}}.',
      agents ? `Operational guide:\n${agents}` : "",
      specs.join("\n\n"),
    ]
      .filter(Boolean)
      .join("\n\n");
  }

  if (phase === "explorer") {
    return [
      "You are the JRI explorer. Perform one focused, read-only codebase investigation for Ralph.",
      "Use only read-only tools. Do not edit files, run builds, mutate git state, install packages, or change project state.",
      "Return a concise handoff with concrete file references and findings. Prefer exact paths and line numbers when useful.",
      `Task: ${options.explorerTask ?? "Inspect the codebase and report concise findings."}`,
      agents ? `Operational guide:\n${agents}` : "",
      plan ? `Current implementation plan:\n${plan}` : "",
      specs.join("\n\n"),
    ]
      .filter(Boolean)
      .join("\n\n");
  }

  return [
    "You are Ralph, the JRI builder. Complete one coherent outer-loop iteration.",
    "Use .jri/specs/* as requirements truth and ignore .jri/scratchpad.md. Choose the most important remaining plan item.",
    "Implement completely, run relevant validation, update .jri/IMPLEMENTATION_PLAN.md with findings/resolution, update AGENTS.md only for operational learnings, then commit if tracked files changed and validation passes.",
    "If build/test validation has no errors after a successful change commit, create or increment a patch semver git tag.",
    renderWebCapabilityInstructions(projectDir, options.loopId),
    renderExplorerCapabilityInstructions(projectDir, options.loopId),
    'At the end, emit exactly one line starting with JRI_HANDOFF_JSON: followed by a builder contract JSON with agent "builder" and action "continue", "complete", "blocked", "needsReplan", or "failedValidation".',
    'Use "blocked" with blocker.reason "ambiguousSpecs" or "needsHumanTask" when specs are ambiguous or a human task is required; for needsHumanTask include blocker.resumePhase "building"; do not include secrets.',
    'Use "needsReplan" when the current plan is stale or confusing but specs are not blocked. Use "failedValidation" with validation evidence when validation ran and failed.',
    agents ? `Operational guide:\n${agents}` : "",
    plan ? `Current implementation plan:\n${plan}` : "No implementation plan exists yet; inspect specs and code before choosing work.",
    specs.join("\n\n"),
  ]
    .filter(Boolean)
    .join("\n\n");
}

async function listSpecFiles(projectDir: string): Promise<string[]> {
  const specsDir = join(projectDir, ".jri", "specs");
  if (!(await Bun.file(specsDir).exists())) return [];
  return (await readdir(specsDir, { withFileTypes: true }))
    .filter((entry) => entry.isFile() && entry.name.endsWith(".md"))
    .map((entry) => `.jri/specs/${entry.name}`)
    .sort();
}

async function readIfExists(path: string): Promise<string | undefined> {
  if (!(await Bun.file(path).exists())) return undefined;
  return (await Bun.file(path).text()).trim();
}

async function renderSelectedContext(projectDir: string, refs: string[] | undefined): Promise<string> {
  if (!refs) return "";
  const sections = await Promise.all(
    refs.flatMap((ref) => {
      if (ref === ".jri/logs/interrogation.jsonl#recent-unsealed-turns") return [];
      return [renderContextRef(projectDir, ref)];
    }),
  );
  return sections.filter(Boolean).join("\n\n");
}

async function renderContextRef(projectDir: string, ref: string): Promise<string> {
  const path = join(projectDir, ref);
  if (!(await Bun.file(path).exists())) return "";
  const text = (await Bun.file(path).text()).trim();
  if (!text) return `# ${ref}\n\n(empty)`;
  return `# ${ref}\n\n${text}`;
}

function isProjectConfig(value: unknown): value is ProjectConfig {
  if (!value || typeof value !== "object") return false;
  const config = value as Partial<ProjectConfig>;
  return config.schemaVersion === 1 && config.provider === "openai" && config.modelPreset === "openai";
}
