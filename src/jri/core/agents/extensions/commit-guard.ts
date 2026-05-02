import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

const RESERVED_PREFIX = "jri:";

function extractCommitMessage(command: string): string | null {
  const patterns = [
    /(?:^|[;&|]\s*)git\s+commit\b[\s\S]*?--message\s*=\s*(["'])(.*?)\1/i,
    /(?:^|[;&|]\s*)git\s+commit\b[\s\S]*?--message\s+(["'])(.*?)\1/i,
    /(?:^|[;&|]\s*)git\s+commit\b[\s\S]*?-m\s+(["'])(.*?)\1/i,
  ];
  for (const pattern of patterns) {
    const match = command.match(pattern);
    if (match) return match[2].trimStart();
  }
  return null;
}

export function registerCommitPrefixGuard(pi: ExtensionAPI) {
  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return;
    const input = event.input as { command?: unknown } | undefined;
    if (typeof input?.command !== "string") return;
    const message = extractCommitMessage(input.command);
    if (message === null || !message.toLowerCase().startsWith(RESERVED_PREFIX)) {
      return;
    }
    return {
      block: true,
      reason: `Prefix "${RESERVED_PREFIX}" is reserved for JRI-managed commits. Update your commit message.`,
    };
  });
}
