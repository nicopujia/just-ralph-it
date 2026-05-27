export type CapabilityDescriptor = {
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

export const webCapabilityDescriptor: CapabilityDescriptor = {
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

export function renderWebCapabilityInstructions(projectDir: string, loopId: string | undefined): string {
  if (!loopId) return "";
  const limits = webCapabilityDescriptor.limits;
  return [
    "JRI web capability:",
    `- For current external facts, use the JRI-owned web wrapper commands: jri --run-web search ${JSON.stringify(projectDir)} ${JSON.stringify(loopId)} "<query>" and jri --run-web fetch ${JSON.stringify(projectDir)} ${JSON.stringify(loopId)} "<url>".`,
    `- Search results are capped at ${limits.searchResults} and include retrieval timestamps; fetched markdown is capped at ${limits.fetchMarkdownChars} characters with artifact refs for omitted content.`,
    "- Cite sources in user-visible summaries when web facts affect a decision.",
    "- If required web access is unavailable, return an actionable capability blocker or a clearly labeled degraded answer; do not guess current facts.",
    "- Do not use ad hoc shell curl, browser automation, package CLIs, or raw HTML dumps for facts the JRI web capability can retrieve.",
  ].join("\n");
}
