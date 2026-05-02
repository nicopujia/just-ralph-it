import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";
import { registerMappedPythonTool, registerPythonTool, SLUG_RE, text } from "./common.ts";
import {
  CHILD_PI_MAX_BUFFER,
  configuredModel,
  finalAssistantText,
  getPiInvocation,
} from "./python-bridge.ts";

export function registerInterrogatorValidator(pi: ExtensionAPI) {
  const extensionDir = dirname(fileURLToPath(import.meta.url));
  const packageRoot = dirname(extensionDir);
  const jriExtension = join(extensionDir, "jri.ts");
  const validatorExtension = join(extensionDir, "jri-validator.ts");
  const validatorPrompt = join(packageRoot, "prompts", "interrogator-validator.md");

  pi.registerTool({
    name: "interrogator-validator",
    label: "interrogator-validator",
    description: "Run the Interrogator validator in an isolated runtime for selected draft task slugs.",
    parameters: Type.Object({ slugs: Type.Array(Type.String()) }),
    async execute(_toolCallId, params) {
      const slugs = (params as { slugs?: unknown }).slugs;
      if (
        !Array.isArray(slugs) ||
        !slugs.every((slug) => typeof slug === "string" && SLUG_RE.test(slug))
      ) {
        return text("`slugs` must be an array of valid task slugs");
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
        "--extension",
        validatorExtension,
        "--append-system-prompt",
        validatorPrompt,
        "--tools",
        "read-tasks,check-draft-promotion,approve-draft-promotion",
      ];
      const model = configuredModel(packageRoot, "interrogator-validator");
      if (model) args.push("--model", model);
      args.push(slugs.join("\n"));

      const invocation = getPiInvocation(args);
      const childEnv = { ...process.env };
      delete childEnv.JRI_CHAT_RUNTIME;
      childEnv.JRI_INTERROGATOR_VALIDATOR_RUNTIME = "1";
      const result = spawnSync(invocation.command, invocation.args, {
        cwd: process.cwd(),
        env: childEnv,
        encoding: "utf-8",
        maxBuffer: CHILD_PI_MAX_BUFFER,
      });
      const output = finalAssistantText(result.stdout ?? "");
      if (result.error) {
        return text(`${output}\n${result.error.message}`.trim());
      }
      if (result.status !== 0) {
        const stderr = (result.stderr ?? "").trim();
        return text(stderr ? `${output}\n${stderr}`.trim() : output);
      }
      return text(output);
    },
  });
}

export function registerRalphValidator(pi: ExtensionAPI) {
  const extensionDir = dirname(fileURLToPath(import.meta.url));
  const packageRoot = dirname(extensionDir);
  const jriExtension = join(extensionDir, "jri.ts");
  const validatorPrompt = join(packageRoot, "prompts", "ralph-validator.md");

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
      if (model) args.push("--model", model);
      args.push(slug);

      const invocation = getPiInvocation(args);
      const childEnv = { ...process.env };
      delete childEnv.JRI_CHAT_RUNTIME;
      const result = spawnSync(invocation.command, invocation.args, {
        cwd: process.cwd(),
        env: childEnv,
        encoding: "utf-8",
        maxBuffer: CHILD_PI_MAX_BUFFER,
      });
      const output = finalAssistantText(result.stdout ?? "");
      if (result.error) {
        return text(`${output}\n${result.error.message}`.trim());
      }
      if (result.status !== 0) {
        const stderr = (result.stderr ?? "").trim();
        return text(stderr ? `${output}\n${stderr}`.trim() : output);
      }
      return text(output);
    },
  });
}

export function registerInterrogatorValidationTools(pi: ExtensionAPI) {
  registerPythonTool(
    pi,
    "read-tasks",
    "Read one or more tasks by slug and return their structured contents.",
    Type.Object({ slugs: Type.Array(Type.String()) }),
  );
  registerMappedPythonTool(
    pi,
    "check-draft-promotion",
    "Validate draft task promotion readiness without moving tasks.",
    Type.Object({ slugs: Type.Optional(Type.Array(Type.String())) }),
    "promote-tasks",
    (params) => ({ ...params, check_only: true }),
  );
}
