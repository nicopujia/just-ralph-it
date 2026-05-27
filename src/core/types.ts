export type ReasoningLevel = "low" | "medium" | "high" | "xhigh";
export type AgentName = "interrogator" | "explorer" | "auditor" | "planner" | "builder";

export type AgentConfig = {
  model?: string;
  reasoning?: ReasoningLevel;
};

export type ProjectConfig = {
  $schema?: string;
  schemaVersion: 1;
  provider: "openai";
  modelPreset: "openai";
  agents?: Partial<Record<AgentName, AgentConfig>>;
};

export type ProjectState =
  | "idle"
  | "auditing"
  | "planning"
  | "building"
  | "blocked"
  | "stopped"
  | "halted";

export type BlockerReason = "ambiguousSpecs" | "needsHumanTask";

export type Blocker = {
  reason: BlockerReason;
  description: string;
  resolutionGuide: {
    summary: string;
    steps: string[];
    successCriteria?: string[];
    resumeInstruction: string;
    sensitive?: boolean;
  };
  changedFiles?: string[];
  validationRan?: boolean;
  resolution?: {
    status: "verified";
    verifiedAt: string;
    verificationSummary?: string;
  };
};

export type LockOperation = "audit" | "plan" | "build" | "halt" | "resume";

export type ProjectStatus = {
  schemaVersion: 1;
  projectDir: string;
  state: ProjectState;
  activeLoopId: string | null;
  lastLoopId?: string;
  iteration?: number;
  iterations?: number;
  startedAt?: string;
  finishedAt?: string;
  stopRequested: boolean;
  process?: {
    pid: number;
    command?: string;
    startedAt: string;
  };
  blocker?: Blocker;
  currentIteration?: {
    iteration: number;
    rollbackCommit?: string;
    trackedTreeCleanAtStart: boolean;
    dirtySummary?: string;
  };
  lastResult?: {
    outcome: "completed" | "stopped" | "halted" | "blocked" | "failed";
    summary?: string;
    url?: string;
    validationPassed?: boolean;
    commit?: string;
    tag?: string;
  };
  recoveryNote?: {
    timestamp: string;
    message: string;
    repairedFrom?: string;
  };
  lock?: {
    owner: "daemon";
    pid: number;
    operation: LockOperation;
    acquiredAt: string;
    heartbeatAt: string;
    expiresAt: string;
  };
};

export type AuthState = {
  provider: "openai";
  authenticated: boolean;
};

export type AuthResult =
  | { status: "authenticated"; state: AuthState }
  | { status: "userActionRequired"; instructions: string; url?: string; deviceCode?: string; expiresAt?: string };

export type ChatInput = {
  message: string;
};

export type BaseEvent = {
  id: string;
  sequence: number;
  timestamp: string;
  loopId?: string;
  iteration?: number;
  stdoutOffset?: number;
  message?: string;
};

export type CoreEvent =
  | (BaseEvent & { type: "loopStarted"; loopId: string; data: { projectDir: string; pid?: number } })
  | (BaseEvent & { type: "auditStarted"; loopId: string; data: Record<string, never> })
  | (BaseEvent & { type: "auditPassed"; loopId: string; data: { specFiles: string[] } })
  | (BaseEvent & { type: "auditFailed"; loopId: string; data: { feedback: string; ambiguousSpecFiles?: string[] } })
  | (BaseEvent & { type: "planningStarted"; loopId: string; data: Record<string, never> })
  | (BaseEvent & { type: "planningFinished"; loopId: string; data: { planPath: ".jri/IMPLEMENTATION_PLAN.md" } })
  | (BaseEvent & { type: "planRegenerationRequested"; loopId: string; data: { reason: "needsReplan" | "specsChanged" | "ambiguousSpecsResolved" } })
  | (BaseEvent & { type: "planRegenerationStarted"; loopId: string; data: Record<string, never> })
  | (BaseEvent & { type: "planRegenerationFinished"; loopId: string; data: Record<string, never> })
  | (BaseEvent & {
      type: "iterationStarted";
      loopId: string;
      iteration: number;
      data: { rollbackCommit?: string; trackedTreeCleanAtStart: boolean; dirtySummary?: string };
    })
  | (BaseEvent & {
      type: "iterationFinished";
      loopId: string;
      iteration: number;
      data: { outcome: "committed" | "noChanges" | "validationFailed" | "blocked"; commit?: string; tag?: string; changedFiles?: string[] };
    })
  | (BaseEvent & { type: "subagentStarted"; loopId: string; data: { agent: "explorer"; task: string; mode: "spawn" | "fork" } })
  | (BaseEvent & { type: "subagentFinished"; loopId: string; data: { agent: "explorer"; summary: string; artifactRef?: string } })
  | (BaseEvent & { type: "subagentFailed"; loopId: string; data: { agent: "explorer"; error: string; artifactRef?: string } })
  | (BaseEvent & { type: "validationStarted"; loopId: string; iteration: number; data: { command: string } })
  | (BaseEvent & { type: "validationFinished"; loopId: string; iteration: number; data: { command: string; exitCode: number; passed: boolean } })
  | (BaseEvent & { type: "commitCreated"; loopId: string; iteration: number; data: { sha: string; subject?: string } })
  | (BaseEvent & { type: "tagCreated"; loopId: string; iteration: number; data: { tag: string; sha?: string } })
  | (BaseEvent & {
      type: "blockerReported";
      loopId: string;
      data: Pick<Blocker, "reason" | "description" | "resolutionGuide" | "changedFiles" | "validationRan">;
    })
  | (BaseEvent & { type: "blockerResolved"; loopId: string; data: { reason: BlockerReason } })
  | (BaseEvent & { type: "stopRequested"; loopId: string; data: { requested: boolean } })
  | (BaseEvent & { type: "loopStopped"; loopId: string; data: { reason: "gracefulStopRequested"; iteration?: number } })
  | (BaseEvent & {
      type: "loopHalted";
      loopId: string;
      data: { killedPid?: number; resetOffered: boolean; resetAccepted: boolean; resetSucceeded?: boolean; rollbackCommit?: string };
    })
  | (BaseEvent & { type: "loopFinished"; loopId: string; data: { outcome: "completed" | "failed"; summary?: string; url?: string; commit?: string; tag?: string } })
  | (BaseEvent & { type: "statusRepaired"; loopId?: string; data: { repairedFrom: string; repairedTo: string; reason: string } })
  | (BaseEvent & { type: "chatMessageStarted"; data: { role: "assistant" } })
  | (BaseEvent & { type: "chatMessageDelta"; data: { role: "assistant"; text: string } })
  | (BaseEvent & { type: "chatMessageFinished"; data: { role: "assistant" } })
  | (BaseEvent & { type: "chatTurnRecorded"; data: { role: "user" | "assistant"; logPath: ".jri/logs/interrogation.jsonl" } })
  | (BaseEvent & { type: "specsUpdated"; data: { specFiles: string[]; summary: string } });
