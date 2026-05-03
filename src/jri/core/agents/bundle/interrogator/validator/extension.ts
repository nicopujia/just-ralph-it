import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { runPythonTool } from "../../_shared/tools/runner.ts";

const SLUG_RE = /^[a-zA-Z0-9][-a-zA-Z0-9_.]*$/;

export default function (pi: ExtensionAPI) {
  let requestedSlugs: string[] | null = null;
  const matchingCheckCalls = new Set<string>();
  let promotionCheckPassed = false;
  let approvalRecorded = false;

  pi.on("before_agent_start", (event) => {
    requestedSlugs = slugsFromPrompt(event.prompt);
    matchingCheckCalls.clear();
    promotionCheckPassed = false;
    approvalRecorded = false;
  });

  pi.on("tool_call", (event) => {
    if (event.toolName === "check-draft-promotion") {
      if (sameSlugs(requestedSlugs, slugsFromInput(event.input))) {
        matchingCheckCalls.add(event.toolCallId);
      }
      return;
    }
    if (event.toolName !== "approve-draft-promotion") return;
    if (!sameSlugs(requestedSlugs, slugsFromInput(event.input))) {
      return {
        block: true,
        reason: "validator approval requires the exact input slug set",
      };
    }
    return {
      block: true,
      reason: "validator approval is recorded automatically after APPROVED",
    };
  });

  pi.on("tool_result", (event) => {
    if (event.toolName === "check-draft-promotion" && matchingCheckCalls.has(event.toolCallId)) {
      promotionCheckPassed = !event.isError;
    }
  });

  pi.on("agent_end", (event) => {
    if (
      approvalRecorded ||
      !promotionCheckPassed ||
      requestedSlugs === null ||
      normalizedFinalAssistantText(event.messages) !== "APPROVED"
    ) {
      return;
    }
    runPythonTool("approve-draft-promotion", { slugs: requestedSlugs });
    approvalRecorded = true;
  });

  registerPythonTool(
    pi,
    "approve-draft-promotion",
    "Record validator approval for an exact draft task promotion set.",
    Type.Object({ slugs: Type.Optional(Type.Array(Type.String())) }),
  );
}

function slugsFromPrompt(prompt: string): string[] | null {
  if (!prompt.trim()) return null;
  const lines = prompt.split("\n");
  if (lines.some((line) => line.trim() !== line || !line)) return null;
  if (!lines.every((line) => SLUG_RE.test(line))) return null;
  return lines;
}

function slugsFromInput(input: Record<string, unknown>): string[] {
  const slugs = input.slugs;
  if (!Array.isArray(slugs)) return [];
  return slugs.filter((slug): slug is string => typeof slug === "string");
}

function sameSlugs(left: string[] | null, right: string[]): boolean {
  if (left === null || left.length !== right.length) return false;
  return left.every((slug, index) => slug === right[index]);
}

function finalAssistantText(messages: unknown): string {
  if (!Array.isArray(messages)) return "";
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index] as { role?: unknown; content?: unknown };
    if (message.role !== "assistant" || !Array.isArray(message.content)) continue;
    return message.content
      .map((part) => part as { type?: unknown; text?: unknown })
      .filter((part) => part.type === "text" && typeof part.text === "string")
      .map((part) => part.text as string)
      .join("")
      .trim();
  }
  return "";
}

function normalizedFinalAssistantText(messages: unknown): string {
  const text = finalAssistantText(messages);
  const match = text.match(/^```(?:md|markdown)?\s*\n([\s\S]*?)\n```$/i);
  return (match ? match[1] : text).trim();
}

function text(content: string) {
  return { content: [{ type: "text" as const, text: content }], details: {} };
}

function registerPythonTool(
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
