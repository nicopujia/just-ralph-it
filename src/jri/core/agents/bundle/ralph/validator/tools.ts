import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";
import { SLUG_RE, text } from "../../(shared)/registry.ts";
import {
  CHILD_PI_MAX_BUFFER,
  VALIDATOR_TIMEOUT_MS,
  configuredModel,
  finalAssistantText,
  getPiInvocation,
  runUntilTerminalOutput,
} from "../../(shared)/subagents.ts";
import { agentSkillPaths, resourcePath } from "../../(shared)/assets.ts";

export function registerRalphValidator(pi: ExtensionAPI) {
  const extensionDir = dirname(fileURLToPath(import.meta.url));
  const packageRoot = dirname(dirname(extensionDir));
  const jriExtension = resourcePath("extensions.default", packageRoot);
  const validatorPrompt = resourcePath("prompts.ralphValidator", packageRoot);

  pi.registerTool({
    name: "ralph-validator",
    label: "ralph-validator",
    description: "Run the Ralph validator in an isolated read-only runtime for one task slug.",
    parameters: Type.Object({ slug: Type.String() }),
    async execute(_toolCallId, params) {
      const slug = (params as { slug?: unknown }).slug;
      if (typeof slug !== "string" || !SLUG_RE.test(slug)) {
        return text("`slug` must be a valid task slug");
      }
      const args = [
        "--mode",
        "json",
        "-p",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--extension",
        jriExtension,
        "--append-system-prompt",
        validatorPrompt,
        "--tools",
        "read,bash,grep,find,ls,list-tasks,read-tasks,check-contrast",
      ];
      const model = configuredModel(packageRoot, "ralph-validator");
      for (const skillPath of agentSkillPaths(packageRoot, "ralph/validator")) {
        args.push("--skill", skillPath);
      }
      if (model) args.push("--model", model);
      args.push(slug);

      const invocation = getPiInvocation(args);
      const childEnv = { ...process.env };
      delete childEnv.JRI_CHAT_RUNTIME;
      const result = await runUntilTerminalOutput(invocation.command, invocation.args, {
        cwd: process.cwd(),
        env: childEnv,
        timeoutMs: VALIDATOR_TIMEOUT_MS,
        maxBuffer: CHILD_PI_MAX_BUFFER,
      });
      const output = finalAssistantText(result.stdout);
      if (result.error) {
        return text(`${output}\n${result.error}`.trim());
      }
      if (result.status !== 0) {
        const stderr = result.stderr.trim();
        return text(stderr ? `${output}\n${stderr}`.trim() : output);
      }
      return text(output);
    },
  });
}
