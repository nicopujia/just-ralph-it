import { JriError } from "./errors";
import { isActiveState, readStatus } from "./runtime-state";

export type LoopCapabilityName = "web" | "explorer";
export type WebCapabilityOperation = "search" | "fetch";
export type CapabilityOwner = { kind: "loop"; loopId: string } | { kind: "chat"; turnId: string };
export type CapabilityInvocationMetadata = {
  owner: CapabilityOwner;
  projectDir: string;
  capability: LoopCapabilityName;
  operation?: WebCapabilityOperation;
};

export async function assertLoopCapabilityOwnership(
  projectDir: string,
  loopId: string,
  capability: LoopCapabilityName,
): Promise<void> {
  await assertCapabilityOwnership({ owner: { kind: "loop", loopId }, projectDir, capability }, capability);
}

export async function assertCapabilityOwnership(
  metadata: CapabilityInvocationMetadata,
  capability: LoopCapabilityName,
  operation?: WebCapabilityOperation,
): Promise<void> {
  if (metadata.capability !== capability) {
    throw new JriError(
      `JRI ${capability} capability received mismatched owner metadata.`,
      "capability-owner-mismatch",
      "Retry through the JRI-managed capability command emitted for this capability.",
    );
  }
  if (capability === "web" && operation && metadata.operation !== operation) {
    throw new JriError(
      `JRI web capability was invoked for ${operation} with mismatched operation metadata.`,
      "capability-operation-mismatch",
      "Retry through the JRI-managed web command emitted for this specific operation.",
    );
  }
  if (metadata.owner.kind === "chat") {
    if (capability !== "web") {
      throw new JriError(
        `JRI ${capability} capability cannot run with chat ownership.`,
        "capability-owner-unsupported",
        "Retry from the currently running Ralph loop so artifacts and events attach to the right lifecycle.",
      );
    }
    if (!metadata.owner.turnId.trim()) {
      throw new JriError(
        "JRI web capability received empty chat ownership metadata.",
        "capability-owner-invalid",
        "Retry through the JRI-managed web command emitted for this chat turn.",
      );
    }
    const status = await readStatus(metadata.projectDir);
    if (status.projectDir !== metadata.projectDir) {
      throw new JriError(
        "JRI web capability was invoked for the wrong project.",
        "capability-project-mismatch",
        "Retry through the JRI-managed web command emitted for this project.",
      );
    }
    return;
  }

  await assertLoopOwner(metadata.projectDir, metadata.owner.loopId, capability);
}

export function encodeCapabilityMetadata(metadata: CapabilityInvocationMetadata): string {
  return JSON.stringify(metadata);
}

export function parseCapabilityMetadata(value: string): CapabilityInvocationMetadata {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch (error) {
    throw new JriError(
      "JRI capability owner metadata must be valid JSON.",
      "capability-owner-invalid-json",
      error instanceof Error ? error.message : "Retry through the JRI-managed capability command.",
    );
  }
  if (!parsed || typeof parsed !== "object") return invalidMetadata();
  const record = parsed as Partial<CapabilityInvocationMetadata>;
  if (typeof record.projectDir !== "string" || !record.projectDir.trim()) return invalidMetadata();
  if (record.capability !== "web" && record.capability !== "explorer") return invalidMetadata();
  if (record.operation !== undefined && record.operation !== "search" && record.operation !== "fetch") return invalidMetadata();
  if (!record.owner || typeof record.owner !== "object") return invalidMetadata();
  const owner = record.owner as Partial<CapabilityOwner>;
  if (owner.kind === "loop" && typeof owner.loopId === "string" && owner.loopId.trim()) {
    return {
      projectDir: record.projectDir,
      capability: record.capability,
      ...(record.operation ? { operation: record.operation } : {}),
      owner: { kind: "loop", loopId: owner.loopId },
    };
  }
  if (owner.kind === "chat" && typeof owner.turnId === "string" && owner.turnId.trim()) {
    return {
      projectDir: record.projectDir,
      capability: record.capability,
      ...(record.operation ? { operation: record.operation } : {}),
      owner: { kind: "chat", turnId: owner.turnId },
    };
  }
  return invalidMetadata();
}

function invalidMetadata(): never {
  throw new JriError(
    "JRI capability owner metadata is malformed.",
    "capability-owner-invalid",
    "Retry through the JRI-managed capability command emitted for this project.",
  );
}

async function assertLoopOwner(projectDir: string, loopId: string, capability: LoopCapabilityName): Promise<void> {
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
