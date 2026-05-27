#!/usr/bin/env bun
import { open, isJriError } from "../core";

async function main(argv: string[]): Promise<number> {
  const [command, subcommand] = argv;
  const project = await open(process.cwd());

  if (!command) {
    await project.lifecycle.ensureInitialized();
    console.log(`Initialized JRI in ${project.projectDir}`);
    console.log("The interrogator chat is not implemented yet.");
    return 0;
  }

  if (command === "auth") {
    if (subcommand === "status") {
      const status = await project.auth.status();
      console.log(`${status.provider}: ${status.authenticated ? "authenticated" : "not authenticated"}`);
      return 0;
    }
    if (subcommand === "login") {
      const result = await project.auth.login();
      console.log(result.status === "authenticated" ? "Authenticated." : result.instructions);
      return result.status === "authenticated" ? 0 : 1;
    }
    if (subcommand === "logout") {
      await project.auth.logout();
      console.log("Logged out.");
      return 0;
    }
    return usage(`Unsupported auth command: ${subcommand ?? ""}`.trim());
  }

  if (command === "loop") {
    if (subcommand === "attach") {
      for await (const event of project.loop.observe()) {
        console.log(formatLoopEvent(event));
      }
      return 0;
    }
    if (subcommand === "stop") {
      await project.loop.requestStop();
      const status = await project.status.get();
      console.log(status.stopRequested ? "Graceful stop requested." : "Graceful stop request cleared.");
      return 0;
    }
    if (subcommand === "halt") {
      if (!(await confirm("Force halt the active JRI loop?"))) {
        console.log("Halt canceled.");
        return 0;
      }
      for await (const event of project.loop.halt()) {
        console.log(formatLoopEvent(event));
      }
      return 0;
    }
    if (subcommand === "resume") {
      for await (const event of project.loop.resume()) {
        console.log(formatLoopEvent(event));
      }
      return 0;
    }
    return usage(`Unsupported loop command: ${subcommand ?? ""}`.trim());
  }

  return usage(`Unsupported command: ${command}`);
}

function usage(error?: string): number {
  if (error) console.error(error);
  console.error("Usage: jri | jri auth {status|login|logout} | jri loop {attach|stop|halt|resume}");
  return 1;
}

function formatLoopEvent(event: { type: string; sequence: number; timestamp: string; message?: string; data?: unknown }): string {
  return event.message ?? `[${event.sequence}] ${event.timestamp} ${event.type} ${JSON.stringify(event.data ?? {})}`;
}

async function confirm(question: string): Promise<boolean> {
  process.stderr.write(`${question} y/N `);
  const answer = await new Response(Bun.stdin.stream()).text();
  return answer.trim().toLowerCase() === "y" || answer.trim().toLowerCase() === "yes";
}

main(Bun.argv.slice(2))
  .then((code) => process.exit(code))
  .catch((error) => {
    if (isJriError(error)) {
      console.error(error.message);
      console.error(error.recovery);
    } else {
      console.error(error instanceof Error ? error.message : String(error));
    }
    process.exit(1);
  });
