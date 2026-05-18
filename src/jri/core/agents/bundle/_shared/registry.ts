import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { runPythonTool } from "./runner.ts";

export const SLUG_RE = /^[a-zA-Z0-9][-a-zA-Z0-9_.]*$/;

export const ExactEdit = Type.Object({
  oldText: Type.String(),
  newText: Type.String(),
});

export function text(content: string) {
  return { content: [{ type: "text" as const, text: content }], details: {} };
}

export function registerPythonTool(
  pi: ExtensionAPI,
  name: string,
  description: string,
  parameters: object,
  toolName = name,
) {
  pi.registerTool({
    name,
    label: name,
    description,
    parameters,
    async execute(_toolCallId, params) {
      return text(runPythonTool(toolName, params));
    },
  });
}

export function registerMappedPythonTool(
  pi: ExtensionAPI,
  name: string,
  description: string,
  parameters: object,
  toolName: string,
  mapParams: (params: Record<string, unknown>) => Record<string, unknown>,
) {
  pi.registerTool({
    name,
    label: name,
    description,
    parameters,
    async execute(_toolCallId, params) {
      return text(runPythonTool(toolName, mapParams(params as Record<string, unknown>)));
    },
  });
}
