import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { registerChatTools } from "./chat-tools.ts";
import { registerCommitPrefixGuard } from "./commit-guard.ts";
import { registerExplorerTools } from "./explorer.ts";
import { registerRalphTools } from "./ralph-tools.ts";
import { registerInterrogatorValidationTools } from "./validators.ts";

export default function (pi: ExtensionAPI) {
  registerCommitPrefixGuard(pi);
  if (process.env.JRI_EXPLORER_RUNTIME === "1") {
    registerExplorerTools(pi);
  } else if (process.env.JRI_CHAT_RUNTIME === "1") {
    registerChatTools(pi);
  } else if (process.env.JRI_INTERROGATOR_VALIDATOR_RUNTIME === "1") {
    registerInterrogatorValidationTools(pi);
  } else {
    registerRalphTools(pi);
  }
}
