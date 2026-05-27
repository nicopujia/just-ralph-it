import type { AgentConfig } from "./types";
import { encodeCapabilityMetadata, type CapabilityOwner } from "./capability-ownership";

export type WebCapabilityDescriptor = {
  name: "web";
  allowedAgents: readonly ("interrogator" | "explorer" | "auditor" | "planner" | "builder")[];
  limits: {
    searchResults: number;
    fetchMarkdownChars: number;
    fetchTimeoutMs: number;
    redirects: number;
    artifactBytes: number;
  };
};

export type ExplorerCapabilityDescriptor = {
  name: "explorer";
  allowedAgents: readonly ("planner" | "builder")[];
  mode: "spawn";
  limits: {
    concurrency: number;
    timeoutMs: number;
    handoffChars: number;
  };
  tools: readonly ("read" | "grep" | "find" | "ls")[];
};

export type CapabilityDescriptor = WebCapabilityDescriptor | ExplorerCapabilityDescriptor;

export const webCapabilityDescriptor: WebCapabilityDescriptor = {
  name: "web",
  allowedAgents: ["interrogator", "explorer", "auditor", "planner", "builder"],
  limits: {
    searchResults: 5,
    fetchMarkdownChars: 12_000,
    fetchTimeoutMs: 20_000,
    redirects: 5,
    artifactBytes: 5 * 1024 * 1024,
  },
};

export const explorerCapabilityDescriptor: ExplorerCapabilityDescriptor = {
  name: "explorer",
  allowedAgents: ["planner", "builder"],
  mode: "spawn",
  limits: {
    concurrency: 6,
    timeoutMs: 10 * 60 * 1_000,
    handoffChars: 4_000,
  },
  tools: ["read", "grep", "find", "ls"],
};

export function renderWebCapabilityInstructions(projectDir: string, owner: CapabilityOwner | undefined): string {
  if (!owner) return "";
  const limits = webCapabilityDescriptor.limits;
  const metadata = encodeCapabilityMetadata({ projectDir, owner, capability: "web" });
  const artifactDir =
    owner.kind === "chat" ? ".jri/logs/interrogation-artifacts/" : `.jri/logs/${owner.loopId}/artifacts/`;
  return [
    "JRI web capability:",
    `- For current external facts, use the JRI-owned web wrapper commands: jri --run-web search ${JSON.stringify(metadata)} "<query>" and jri --run-web fetch ${JSON.stringify(metadata)} "<url>".`,
    `- Search results are capped at ${limits.searchResults} and include retrieval timestamps; fetched markdown is capped at ${limits.fetchMarkdownChars} characters with artifact refs under ${artifactDir} for omitted content.`,
    "- Cite sources in user-visible summaries when web facts affect a decision.",
    "- If required web access is unavailable, return an actionable capability blocker or a clearly labeled degraded answer; do not guess current facts.",
    "- Do not use ad hoc shell curl, browser automation, package CLIs, or raw HTML dumps for facts the JRI web capability can retrieve.",
  ].join("\n");
}

export function renderExplorerCapabilityInstructions(projectDir: string, loopId: string | undefined): string {
  if (!loopId) return "";
  const limits = explorerCapabilityDescriptor.limits;
  return [
    "JRI explorer capability:",
    `- For read-only codebase investigation, delegate through the JRI-owned explorer wrapper: jri --run-explorer ${JSON.stringify(projectDir)} ${JSON.stringify(loopId)} "<focused task>".`,
    `- Explorer runs use spawn/fresh context by default, read-only tools only, ${limits.timeoutMs / 60_000}-minute timeout, ${limits.concurrency}-way concurrency, and ${limits.handoffChars}-character parent handoffs with artifact refs for longer output.`,
    "- Use focused explorer tasks for codebase search or investigation before making risky changes.",
    "- Do not call pi-subagent, pi-subagents, or other raw Pi package commands directly; JRI owns capability isolation and logging.",
  ].join("\n");
}

export function renderExplorerAgentDescriptor(model: Required<AgentConfig>): string {
  return [
    "---",
    "name: explorer",
    "description: JRI-owned read-only codebase investigator for focused Ralph planning and build questions.",
    `model: ${JSON.stringify(model.model)}`,
    `thinking: ${JSON.stringify(model.reasoning)}`,
    "tools:",
    ...explorerCapabilityDescriptor.tools.map((tool) => `  - ${tool}`),
    "inheritProjectContext: false",
    "inheritSkills: false",
    "defaultContext: false",
    "---",
    "",
    "You are the JRI explorer. Perform one focused, read-only codebase investigation for Ralph.",
    "Use only read-only tools. Do not edit files, run builds, mutate git state, install packages, or change project state.",
    "Return a concise handoff with concrete file references and findings. Prefer exact paths and line numbers when useful.",
  ].join("\n");
}
