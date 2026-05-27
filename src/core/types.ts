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
  resumePhase?: "planning" | "building";
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
  authorizedSpecsFingerprint?: string;
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
    explorer?: {
      used: boolean;
      summary?: string;
      artifactRef?: string;
    };
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
  recovery?: {
    code: string;
    message: string;
    instructions: string;
  };
};

export type AuthResult =
  | { status: "authenticated"; state: AuthState }
  | { status: "userActionRequired"; instructions: string; url?: string; deviceCode?: string; expiresAt?: string };

export type ChatInput = {
  message: string;
};

export type LoopObserveOptions = {
  includeStdout?: boolean;
  recentStdoutLines?: number;
  follow?: boolean;
};

export type HaltOptions = {
  resetGit?: boolean;
};

export type ArtifactRef = {
  path: `.jri/logs/${string}/artifacts/${string}` | `.jri/logs/interrogation-artifacts/${string}`;
  summary?: string;
};

export type InterrogatorHandoff =
  | { agent: "interrogator"; action: "messageOnly"; summary?: string }
  | { agent: "interrogator"; action: "specsUpdated"; specFiles: string[]; summary: string; sealedSpecFiles?: string[] }
  | { agent: "interrogator"; action: "scratchpadUpdated"; summary: string }
  | { agent: "interrogator"; action: "humanTaskVerified"; verificationSummary?: string }
  | { agent: "interrogator"; action: "humanTaskStillBlocked"; blocker: Blocker }
  | { agent: "interrogator"; action: "startRequested"; trigger: "just ralph it" | "ralfealo" };

export type AuditorHandoff =
  | { agent: "auditor"; action: "passed"; specFiles: string[]; specsFingerprint: string; summary?: string }
  | { agent: "auditor"; action: "failed"; feedback: string; ambiguousSpecFiles?: string[]; affectedTopics?: string[]; findings?: string[]; questions: string[] };

export type PlannerHandoff =
  | { agent: "planner"; action: "planned"; planPath: ".jri/IMPLEMENTATION_PLAN.md"; summary: string }
  | { agent: "planner"; action: "blocked"; blocker: Blocker };

export type ValidationHandoff = {
  command: string;
  exitCode: number;
  passed: boolean;
  summary: string;
  artifacts?: ArtifactRef[];
};

export type BuilderHandoff =
  | { agent: "builder"; action: "continue"; summary: string; url?: string; artifacts?: ArtifactRef[]; validation?: ValidationHandoff[] }
  | { agent: "builder"; action: "complete"; summary: string; url?: string; artifacts?: ArtifactRef[]; validation?: ValidationHandoff[] }
  | { agent: "builder"; action: "blocked"; blocker: Blocker; validation?: ValidationHandoff[] }
  | { agent: "builder"; action: "needsReplan"; reason: string; summary?: string; validation?: ValidationHandoff[] }
  | { agent: "builder"; action: "failedValidation"; validation: ValidationHandoff; summary?: string };

export type HumanTaskVerificationHandoff =
  | { agent: "verifier"; action: "verified"; verificationSummary?: string }
  | { agent: "verifier"; action: "stillBlocked"; blocker: Blocker };

export type AgentHandoff =
  | InterrogatorHandoff
  | AuditorHandoff
  | PlannerHandoff
  | BuilderHandoff
  | HumanTaskVerificationHandoff;

export type BaseEvent = {
  id: string;
  sequence: number;
  timestamp: string;
  loopId?: string;
  iteration?: number;
  stdoutOffset?: number;
  message?: string;
};

export type RuntimeStateEvent =
  | (BaseEvent & { type: "loopStarted"; loopId: string; data: { projectDir: string; pid?: number } })
  | (BaseEvent & { type: "auditStarted"; loopId: string; data: Record<string, never> })
  | (BaseEvent & { type: "auditPassed"; loopId: string; data: { specFiles: string[]; specsFingerprint: string } })
  | (BaseEvent & { type: "auditFailed"; loopId: string; data: { feedback: string; ambiguousSpecFiles?: string[]; affectedTopics?: string[]; findings?: string[]; questions: string[] } })
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
  | (BaseEvent & { type: "validationFinished"; loopId: string; iteration: number; data: { command: string; exitCode: number; passed: boolean; artifacts?: ArtifactRef[] } })
  | (BaseEvent & { type: "commitCreated"; loopId: string; iteration: number; data: { sha: string; subject?: string } })
  | (BaseEvent & { type: "tagCreated"; loopId: string; iteration: number; data: { tag: string; sha?: string } })
  | (BaseEvent & {
      type: "blockerReported";
      loopId: string;
      data: Pick<Blocker, "reason" | "description" | "resolutionGuide" | "changedFiles" | "validationRan">;
    })
  | (BaseEvent & { type: "blockerResolved"; loopId: string; data: { reason: BlockerReason } })
  | (BaseEvent & { type: "stopRequested"; loopId: string; data: { requested: boolean } })
  | (BaseEvent & {
      type: "loopStopped";
      loopId: string;
      data: { reason: "gracefulStopRequested"; nextPhase: "planning" | "building"; iteration?: number; specsFingerprint?: string };
    })
  | (BaseEvent & {
      type: "loopHalted";
      loopId: string;
      data: {
        killedPid?: number;
        killedChildPids?: number[];
        resetOffered: boolean;
        resetAccepted: boolean;
        resetSucceeded?: boolean;
        resetError?: string;
        rollbackCommit?: string;
      };
    })
  | (BaseEvent & {
      type: "loopFinished";
      loopId: string;
      data: {
        outcome: "completed" | "failed";
        summary?: string;
        url?: string;
        commit?: string;
        tag?: string;
        explorer?: {
          used: boolean;
          summary?: string;
          artifactRef?: string;
        };
      };
    })
  | (BaseEvent & { type: "statusRepaired"; loopId?: string; data: { repairedFrom: string; repairedTo: string; reason: string } })
  | (BaseEvent & { type: "loopOutput"; loopId: string; stdoutOffset: number; data: { text: string; replayed: boolean } })
  | (BaseEvent & { type: "chatMessageStarted"; data: { role: "assistant" } })
  | (BaseEvent & { type: "chatMessageDelta"; data: { role: "assistant"; text: string } })
  | (BaseEvent & { type: "chatMessageFinished"; data: { role: "assistant" } })
  | (BaseEvent & { type: "chatTurnRecorded"; data: { role: "user" | "assistant"; logPath: ".jri/logs/interrogation.jsonl"; content?: string } })
  | (BaseEvent & { type: "specsUpdated"; data: { specFiles: string[]; summary: string; sealedSpecFiles?: string[] } })
  | (BaseEvent & { type: "scratchpadUpdated"; data: { scratchpadPath: ".jri/scratchpad.md"; summary: string } });

export type CoreEvent = RuntimeStateEvent;
