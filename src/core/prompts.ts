import { join } from "node:path";
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

export async function buildPiPrompt(projectDir: string, phase: "planning" | "building"): Promise<string> {
  const specFiles = await listSpecFiles(projectDir);
  const specs = await Promise.all(
    specFiles.map(async (path) => {
      const text = await Bun.file(join(projectDir, path)).text();
      return `# ${path}\n\n${text.trim()}`;
    }),
  );
  const agents = await readIfExists(join(projectDir, "AGENTS.md"));
  const plan = await readIfExists(join(projectDir, ".jri", "IMPLEMENTATION_PLAN.md"));

  if (phase === "planning") {
    return [
      "You are the JRI planner. Create or regenerate .jri/IMPLEMENTATION_PLAN.md from the durable specs and current code.",
      "Keep the plan concise, prioritized, and focused on remaining work. Capture why implementation and tests matter.",
      "Do not commit. Do not edit requirements specs unless you find a direct contradiction that blocks implementation.",
      agents ? `Operational guide:\n${agents}` : "",
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
    "If specs are ambiguous or a human task is required, record that clearly in .jri/IMPLEMENTATION_PLAN.md and leave work uncommitted.",
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
  const proc = Bun.spawn(["find", ".jri/specs", "-maxdepth", "1", "-type", "f", "-name", "*.md", "-print"], {
    cwd: projectDir,
    stdout: "pipe",
    stderr: "ignore",
  });
  const output = await new Response(proc.stdout).text();
  await proc.exited;
  return output
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .sort();
}

async function readIfExists(path: string): Promise<string | undefined> {
  if (!(await Bun.file(path).exists())) return undefined;
  return (await Bun.file(path).text()).trim();
}

function isProjectConfig(value: unknown): value is ProjectConfig {
  if (!value || typeof value !== "object") return false;
  const config = value as Partial<ProjectConfig>;
  return config.schemaVersion === 1 && config.provider === "openai" && config.modelPreset === "openai";
}
