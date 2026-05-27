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
    operation: "audit" | "plan" | "build" | "halt" | "resume";
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

export type CoreEvent =
  | {
      id: string;
      sequence?: number;
      type: "chatTurnRecorded";
      timestamp: string;
      message?: string;
      data: { role: "user" | "assistant"; logPath: string };
    }
  | {
      id: string;
      sequence?: number;
      type: "statusRepaired";
      timestamp: string;
      message?: string;
      data: { repairedFrom: string; repairedTo: string; reason: string };
    };
