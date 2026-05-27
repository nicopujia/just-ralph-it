import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { jriExplorerToolName, jriWebFetchToolName, jriWebSearchToolName } from "./capability-tool-names";
import type { CapabilityOwner } from "./capability-ownership";
import { runWebFetch, runWebSearch } from "./web-capability";
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
  runExplorerTask?: (request: {
    projectDir: string;
    loopId: string;
    task: string;
    env?: NodeJS.ProcessEnv;
    signal?: AbortSignal;
  }) => Promise<{ task: string; summary: string; artifactRef?: string }>;
};

export type SdkCapabilityTools = {
  customTools: ToolDefinition[];
  activeToolNames: string[];
};

const webSearchParameters = Type.Object({
  query: Type.String({
    minLength: 1,
    description: "Focused search query for current external facts.",
  }),
});
const webFetchParameters = Type.Object({
  url: Type.String({
    minLength: 1,
    description: "Absolute URL to fetch as bounded markdown/plain text.",
  }),
});
const explorerParameters = Type.Object({
  task: Type.String({
    minLength: 1,
    description: "Focused read-only codebase investigation task for the JRI explorer.",
  }),
});

export function buildSdkCapabilityTools(request: SdkCapabilityToolsRequest): SdkCapabilityTools {
  const customTools: ToolDefinition[] = [];

  if (declaresWebOperation(request, "search")) {
    customTools.push(
      defineTool({
        name: jriWebSearchToolName,
        label: "JRI Web Search",
        description: "Search current external sources through JRI-owned bounded web capability.",
        promptSnippet: "Search current external sources through the JRI-owned web capability",
        promptGuidelines: [`Use ${jriWebSearchToolName} when current external facts are required. Do not guess current facts.`],
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
      }),
    );
  }

  if (declaresWebOperation(request, "fetch")) {
    customTools.push(
      defineTool({
        name: jriWebFetchToolName,
        label: "JRI Web Fetch",
        description: "Fetch one source URL through JRI-owned bounded markdown/plain-text web capability.",
        promptSnippet: "Fetch a specific source URL through the JRI-owned web capability",
        promptGuidelines: [
          `Use ${jriWebFetchToolName} when you need bounded markdown/plain-text content from a known source URL.`,
          "Do not fetch pages through ad hoc shell commands or raw HTML dumps.",
        ],
        parameters: webFetchParameters,
        async execute(_toolCallId, params, signal) {
          const result = await runWebFetch({
            projectDir: request.projectDir,
            owner: request.owner,
            url: params.url,
            ...(request.env ? { env: request.env } : {}),
            ...(signal ? { signal } : {}),
          });
          return {
            content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }],
            details: { capability: "web.fetch", result },
          };
        },
      }),
    );
  }

  if (shouldRegisterLoopExplorer(request)) {
    customTools.push(
      defineTool({
        name: jriExplorerToolName,
        label: "JRI Explorer",
        description: "Run one read-only JRI-owned explorer delegation for focused codebase investigation.",
        promptSnippet: "Delegate focused read-only codebase investigation through the JRI explorer capability",
        promptGuidelines: [
          `Use ${jriExplorerToolName} for focused read-only codebase investigation before risky changes.`,
          "Keep the task narrow and concrete; the explorer returns a bounded summary and optional artifact reference.",
        ],
        parameters: explorerParameters,
        async execute(_toolCallId, params, signal) {
          const result = await request.runExplorerTask!({
            projectDir: request.projectDir,
            loopId: request.owner.loopId,
            task: params.task,
            ...(request.env ? { env: request.env } : {}),
            ...(signal ? { signal } : {}),
          });
          return {
            content: [
              {
                type: "text" as const,
                text: JSON.stringify(
                  {
                    task: result.task,
                    summary: result.summary,
                    ...(result.artifactRef ? { artifactRef: result.artifactRef } : {}),
                  },
                  null,
                  2,
                ),
              },
            ],
            details: {
              capability: "explorer",
              task: result.task,
              summary: result.summary,
              ...(result.artifactRef ? { artifactRef: result.artifactRef } : {}),
            },
          };
        },
      }),
    );
  }

  return {
    customTools,
    activeToolNames: customTools.map((tool) => tool.name),
  };
}

function declaresWebOperation(request: SdkCapabilityToolsRequest, operation: "search" | "fetch"): boolean {
  return request.capabilities.some((capability) => capability.name === "web" && capability.operation === operation);
}

function shouldRegisterLoopExplorer(request: SdkCapabilityToolsRequest): request is SdkCapabilityToolsRequest & {
  owner: Extract<CapabilityOwner, { kind: "loop" }>;
  runExplorerTask: NonNullable<SdkCapabilityToolsRequest["runExplorerTask"]>;
} {
  return (
    request.owner.kind === "loop" &&
    (request.agent === "planner" || request.agent === "builder") &&
    (request.phase === "planning" || request.phase === "building") &&
    typeof request.runExplorerTask === "function" &&
    request.capabilities.some((capability) => capability.name === "explorer")
  );
}
