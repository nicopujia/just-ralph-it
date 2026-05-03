import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { registerCommitPrefixGuard } from "./_shared/commits.ts";
import { registerExplorerTools } from "./explorer/tools.ts";
import { registerChatTools, registerInterrogatorValidationTools } from "./interrogator/tools.ts";
import { registerRalphTools } from "./ralph/tools.ts";

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
