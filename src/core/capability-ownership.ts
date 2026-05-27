import { JriError } from "./errors";
import { isActiveState, readStatus } from "./runtime-state";

export type LoopCapabilityName = "web" | "explorer";

export async function assertLoopCapabilityOwnership(
  projectDir: string,
  loopId: string,
  capability: LoopCapabilityName,
): Promise<void> {
  const status = await readStatus(projectDir);
  if (status.projectDir !== projectDir) {
    throw new JriError(
      `JRI ${capability} capability was invoked for the wrong project.`,
      "capability-project-mismatch",
      "Retry through the JRI-managed capability command emitted for this project.",
    );
  }
  if (status.activeLoopId !== loopId) {
    throw new JriError(
      `JRI ${capability} capability was invoked for a stale or mismatched loop.`,
      "capability-loop-mismatch",
      "Retry from the currently running Ralph loop so artifacts and events attach to the right lifecycle.",
    );
  }
  if (!isActiveState(status.state)) {
    throw new JriError(
      `JRI ${capability} capability cannot run while the loop is ${status.state}.`,
      "capability-loop-inactive",
      "Capabilities are available only to an active auditing, planning, or building loop.",
    );
  }
}
