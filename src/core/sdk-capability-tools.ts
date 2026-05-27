import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import type { CapabilityOwner } from "./capability-ownership";
import { runWebSearch } from "./web-capability";
import type { AgentName } from "./types";

type HarnessPhase = "interrogation" | "auditing" | "planning" | "building" | "explorer";
type CapabilityDescriptor = {
  name: "web" | "explorer";
  operation?: string;
};

export type SdkCapabilityToolsRequest = {
  owner: CapabilityOwner;
  projectDir: string;
  agent: AgentName;
  phase: HarnessPhase;
  capabilities: CapabilityDescriptor[];
  env?: NodeJS.ProcessEnv;
};

export type SdkCapabilityTools = {
  customTools: ToolDefinition[];
  activeToolNames: string[];
};

const webSearchToolName = "jri_web_search";
const webSearchParameters = Type.Object({
  query: Type.String({
    minLength: 1,
    description: "Focused search query for current external facts.",
  }),
});

export function buildSdkCapabilityTools(request: SdkCapabilityToolsRequest): SdkCapabilityTools {
  if (!shouldRegisterInterrogatorWebSearch(request)) {
    return { customTools: [], activeToolNames: [] };
  }

  const searchTool = defineTool({
    name: webSearchToolName,
    label: "JRI Web Search",
    description: "Search current external sources through JRI-owned bounded web capability.",
    promptSnippet: "Search current external sources through the JRI-owned web capability",
    promptGuidelines: ["Use jri_web_search when current external facts are required. Do not guess current facts."],
    parameters: webSearchParameters,
    async execute(_toolCallId, params, signal) {
      const results = await runWebSearch({
        projectDir: request.projectDir,
        owner: request.owner,
        query: params.query,
        ...(request.env ? { env: request.env } : {}),
        ...(signal ? { signal } : {}),
      });
      return {
        content: [{ type: "text" as const, text: JSON.stringify(results, null, 2) }],
        details: { capability: "web.search", results },
      };
    },
  });

  return {
    customTools: [searchTool],
    activeToolNames: [searchTool.name],
  };
}

function shouldRegisterInterrogatorWebSearch(request: SdkCapabilityToolsRequest): boolean {
  return (
    request.owner.kind === "chat" &&
    request.agent === "interrogator" &&
    request.phase === "interrogation" &&
    request.capabilities.some((capability) => capability.name === "web" && capability.operation === "search")
  );
}
