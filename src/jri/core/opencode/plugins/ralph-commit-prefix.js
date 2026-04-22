const RESERVERD_PREFIX = "jri:";

function extractCommitMessage(command) {
  const patterns = [
    /(?:^|[;&|]\s*)git\s+commit\b[\s\S]*?--message\s*=\s*(["'])(.*?)\1/i,
    /(?:^|[;&|]\s*)git\s+commit\b[\s\S]*?--message\s+(["'])(.*?)\1/i,
    /(?:^|[;&|]\s*)git\s+commit\b[\s\S]*?-m\s+(["'])(.*?)\1/i,
  ];

  for (const pattern of patterns) {
    const match = command.match(pattern);
    if (match) {
      return match[2].trimStart();
    }
  }

  return null;
}

export const RalphCommitPrefixPlugin = async () => {
  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "bash" || typeof output.args?.command !== "string") {
        return;
      }

      const message = extractCommitMessage(output.args.command);
      if (
        message === null ||
        !message.toLowerCase().startsWith(RESERVERD_PREFIX)
      ) {
        return;
      }

      throw new Error(
        `Prefix "${RESERVERD_PREFIX}" is reserved for JRI-managed commits. Update your commit message.`,
      );
    },
  };
};
