export const PruneToolCalls = async () => {
  return {
    "experimental.chat.messages.transform": async (_input, output) => {
      for (const msg of output.messages) {
        if (Array.isArray(msg.parts)) {
          msg.parts = msg.parts.filter((part) => part.type !== "tool");
        }
      }
      output.messages = output.messages.filter(
        (msg) => Array.isArray(msg.parts) && msg.parts.length > 0,
      );
    },
  };
};
