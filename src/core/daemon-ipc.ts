import { mkdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { createConnection, createServer, type Server, type Socket } from "node:net";
import { homedir, tmpdir, userInfo } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { JriError, isJriError } from "./errors";
import { getRecoveredStatus, haltLoop, observeLoop, requestGracefulStop, resumeLoop } from "./daemon-runtime";
import { isActiveState } from "./runtime-state";
import type { CoreEvent, HaltOptions, LoopObserveOptions, ProjectStatus } from "./types";

export const DAEMON_PROTOCOL_VERSION = 1;

export type DaemonRequest = {
  id: string;
  method: string;
  params?: unknown;
};

export type DaemonResponse =
  | { id: string; ok: true; result?: unknown }
  | { id: string; ok: false; error: { code: string; message: string; recovery?: string } };

export type DaemonPaths = {
  runtimeDir: string;
  stateDir: string;
  socketPath: string;
  registryPath: string;
};

export type DaemonRegistry = {
  protocolVersion: 1;
  projects: Array<{
    projectDir: string;
    lastSeenAt: string;
    activeLoopId?: string | null;
  }>;
};

type DaemonStreamMessage =
  | { id: string; ok: true; event: CoreEvent }
  | { id: string; ok: true; done: true }
  | DaemonResponse;

type DaemonServerHandle = {
  socketPath: string;
  close(): Promise<void>;
};

type DaemonServerOptions = {
  paths?: DaemonPaths;
  idleTimeoutMs?: number;
};

type DaemonClientOptions = LoopObserveOptions &
  HaltOptions & {
  paths?: DaemonPaths;
  startIfUnavailable?: boolean;
  startupTimeoutMs?: number;
};

type DaemonHandshake = {
  protocolVersion: number;
};

export function daemonPaths(): DaemonPaths {
  const runtimeDir = process.env.JRI_DAEMON_RUNTIME_DIR ?? process.env.XDG_RUNTIME_DIR ?? join(tmpdir(), `jri-${currentUserId()}`);
  const stateDir =
    process.env.JRI_DAEMON_STATE_DIR ?? (process.env.XDG_STATE_HOME ? join(process.env.XDG_STATE_HOME, "jri") : join(homedir(), ".local", "state", "jri"));
  return {
    runtimeDir,
    stateDir,
    socketPath:
      process.env.JRI_DAEMON_SOCKET_PATH ??
      (process.platform === "win32" ? `\\\\.\\pipe\\jri-${currentUserId()}` : join(runtimeDir, `daemon-${DAEMON_PROTOCOL_VERSION}.sock`)),
    registryPath: process.env.JRI_DAEMON_REGISTRY_PATH ?? join(stateDir, "daemon-registry.json"),
  };
}

export async function daemonStatus(projectDir: string, options: DaemonClientOptions = {}): Promise<ProjectStatus> {
  const result = await daemonRequest("status.get", { projectDir }, { ...options, startIfUnavailable: false });
  return result as ProjectStatus;
}

export async function* daemonObserveLoop(projectDir: string, options: DaemonClientOptions = {}): AsyncIterable<CoreEvent> {
  yield* daemonStream("loop.observe", { projectDir, observe: observeOptionsParam(options) }, { ...options, startIfUnavailable: false });
}

export async function daemonRequestStop(projectDir: string, options: DaemonClientOptions = {}): Promise<void> {
  await daemonRequest("loop.stop", { projectDir }, { ...options, startIfUnavailable: true });
}

export async function* daemonHaltLoop(projectDir: string, options: DaemonClientOptions = {}): AsyncIterable<CoreEvent> {
  yield* daemonStream("loop.halt", { projectDir, halt: haltOptionsParam(options) }, { ...options, startIfUnavailable: true });
}

export async function* daemonResumeLoop(projectDir: string, options: DaemonClientOptions = {}): AsyncIterable<CoreEvent> {
  yield* daemonStream("loop.resume", { projectDir }, { ...options, startIfUnavailable: true });
}

export async function startDaemonServer(options: DaemonServerOptions = {}): Promise<DaemonServerHandle> {
  const paths = options.paths ?? daemonPaths();
  const idleTimeoutMs = options.idleTimeoutMs ?? 30_000;
  await mkdir(paths.runtimeDir, { recursive: true });
  await mkdir(paths.stateDir, { recursive: true });
  if (process.platform !== "win32") await rm(paths.socketPath, { force: true });

  let connections = 0;
  let idleTimer: ReturnType<typeof setTimeout> | undefined;
  let closing = false;

  const server = createServer((socket) => {
    connections += 1;
    clearIdleTimer();
    handleConnection(socket, paths, () => {
      closing = true;
      void closeWithCleanup();
    }).finally(() => {
      connections -= 1;
      if (!closing && connections === 0) scheduleIdleClose();
    });
  });

  await new Promise<void>((resolveListen, rejectListen) => {
    const onError = (error: Error) => {
      server.off("listening", onListening);
      rejectListen(error);
    };
    const onListening = () => {
      server.off("error", onError);
      resolveListen();
    };
    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(paths.socketPath);
  });

  scheduleIdleClose();

  return {
    socketPath: paths.socketPath,
    close: async () => {
      closing = true;
      clearIdleTimer();
      await closeWithCleanup();
    },
  };

  function scheduleIdleClose(): void {
    clearIdleTimer();
    idleTimer = setTimeout(() => {
      void closeIfIdle();
    }, idleTimeoutMs);
    idleTimer.unref?.();
  }

  async function closeIfIdle(): Promise<void> {
    if (connections !== 0 || closing) return;
    try {
      if (await hasActiveRegisteredLoop(paths)) {
        scheduleIdleClose();
        return;
      }
      if (connections === 0 && !closing) {
        closing = true;
        await closeWithCleanup();
      }
    } catch {
      scheduleIdleClose();
    }
  }

  function clearIdleTimer(): void {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = undefined;
  }

  async function closeWithCleanup(): Promise<void> {
    clearIdleTimer();
    await closeServer(server);
    if (process.platform !== "win32") await rm(paths.socketPath, { force: true });
  }
}

async function hasActiveRegisteredLoop(paths: DaemonPaths): Promise<boolean> {
  const registry = await readRegistry(paths.registryPath);
  let changed = false;

  for (const project of registry.projects) {
    try {
      const status = await getRecoveredStatus(project.projectDir);
      if (project.activeLoopId !== status.activeLoopId) {
        project.activeLoopId = status.activeLoopId;
        project.lastSeenAt = new Date().toISOString();
        changed = true;
      }
      if (isActiveState(status.state)) {
        if (changed) await writeRegistry(paths.registryPath, registry);
        return true;
      }
    } catch {
      continue;
    }
  }

  if (changed) await writeRegistry(paths.registryPath, registry);
  return false;
}

export async function runDaemon(options: DaemonServerOptions = {}): Promise<void> {
  const handle = await startDaemonServer(options);
  await new Promise<void>((resolveDone) => {
    const cleanup = async () => {
      await handle.close();
      resolveDone();
    };
    process.once("SIGTERM", cleanup);
    process.once("SIGINT", cleanup);
  });
}

async function daemonRequest(method: string, params: Record<string, unknown>, options: DaemonClientOptions): Promise<unknown> {
  const socket = await connectDaemon(options);
  try {
    const request: DaemonRequest = { id: crypto.randomUUID(), method, params };
    socket.write(`${JSON.stringify(request)}\n`);
    for await (const line of readSocketLines(socket)) {
      const message = JSON.parse(line) as DaemonStreamMessage;
      if (message.id !== request.id) continue;
      if (!("ok" in message) || !message.ok) throw daemonError(message);
      if ("event" in message || "done" in message) {
        throw new JriError("Daemon returned a stream response for a unary request.", "daemon-protocol-error", "Retry the command with a compatible JRI daemon.");
      }
      return message.result;
    }
    throw new JriError("Daemon closed the connection before responding.", "daemon-disconnected", "Retry the command; if it repeats, inspect project status from .jri/status.json.");
  } finally {
    socket.end();
  }
}

async function* daemonStream(method: string, params: Record<string, unknown>, options: DaemonClientOptions): AsyncIterable<CoreEvent> {
  const socket = await connectDaemon(options);
  const request: DaemonRequest = { id: crypto.randomUUID(), method, params };
  socket.write(`${JSON.stringify(request)}\n`);
  try {
    for await (const line of readSocketLines(socket)) {
      const message = JSON.parse(line) as DaemonStreamMessage;
      if (message.id !== request.id) continue;
      if (!("ok" in message) || !message.ok) throw daemonError(message);
      if ("event" in message) {
        yield message.event;
        continue;
      }
      if ("done" in message) return;
      throw new JriError("Daemon returned a unary response for a stream request.", "daemon-protocol-error", "Retry the command with a compatible JRI daemon.");
    }
    throw new JriError("Daemon closed the connection before finishing the stream.", "daemon-disconnected", "Retry the command; if it repeats, inspect project status from .jri/status.json.");
  } finally {
    socket.end();
  }
}

async function connectDaemon(options: DaemonClientOptions): Promise<Socket> {
  const paths = options.paths ?? daemonPaths();
  let socket: Socket;
  try {
    socket = await connectSocket(paths.socketPath);
  } catch (error) {
    if (!options.startIfUnavailable) throw connectionError(error);
    await startDaemonProcess(paths);
    return await waitForCompatibleDaemon(paths, options.startupTimeoutMs ?? 2_000);
  }

  if (await isCompatibleDaemon(socket)) return socket;
  socket.end();

  if (await hasActiveRegisteredLoop(paths)) {
    throw new JriError(
      "A running JRI daemon uses an incompatible protocol while a loop may still be active.",
      "daemon-protocol-incompatible",
      "Use the matching JRI version to attach or stop the active loop. Do not kill the daemon while work is active.",
    );
  }

  await requestDaemonShutdown(paths);
  if (!options.startIfUnavailable) {
    throw new JriError(
      "The existing JRI daemon uses an incompatible protocol and was stopped because it was idle.",
      "daemon-restarted-required",
      "Retry the command so JRI can start a compatible daemon.",
    );
  }
  await startDaemonProcess(paths);
  return await waitForCompatibleDaemon(paths, options.startupTimeoutMs ?? 2_000);
}

async function startDaemonProcess(paths: DaemonPaths): Promise<void> {
  await mkdir(paths.runtimeDir, { recursive: true });
  await mkdir(paths.stateDir, { recursive: true });
  const cliPath = resolve(dirname(fileURLToPath(import.meta.url)), "..", "cli", "index.ts");
  Bun.spawn([process.execPath, cliPath, "--daemon"], {
    env: {
      ...process.env,
      JRI_DAEMON_RUNTIME_DIR: paths.runtimeDir,
      JRI_DAEMON_STATE_DIR: paths.stateDir,
      JRI_DAEMON_SOCKET_PATH: paths.socketPath,
      JRI_DAEMON_REGISTRY_PATH: paths.registryPath,
    },
    stdout: "ignore",
    stderr: "ignore",
    stdin: "ignore",
  }).unref();
}

async function waitForCompatibleDaemon(paths: DaemonPaths, timeoutMs: number): Promise<Socket> {
  const startedAt = Date.now();
  let lastError: unknown;
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const socket = await connectSocket(paths.socketPath);
      if (await isCompatibleDaemon(socket)) return socket;
      socket.end();
      lastError = new JriError(
        "The JRI daemon uses an incompatible protocol.",
        "daemon-protocol-incompatible",
        "Retry after the old daemon exits, or use the matching JRI version if a loop is active.",
      );
    } catch (error) {
      lastError = error;
      await Bun.sleep(25);
    }
  }
  throw connectionError(lastError);
}

async function isCompatibleDaemon(socket: Socket): Promise<boolean> {
  try {
    const result = await sendUnaryRequest(socket, "handshake");
    return isHandshake(result) && result.protocolVersion === DAEMON_PROTOCOL_VERSION;
  } catch {
    return false;
  }
}

async function requestDaemonShutdown(paths: DaemonPaths): Promise<void> {
  try {
    const socket = await connectSocket(paths.socketPath);
    try {
      await sendUnaryRequest(socket, "daemon.shutdown");
    } finally {
      socket.end();
    }
  } catch {
    // An incompatible idle daemon may exit or drop the socket before acknowledging.
  }
}

async function sendUnaryRequest(socket: Socket, method: string, params?: Record<string, unknown>): Promise<unknown> {
  const request: DaemonRequest = { id: crypto.randomUUID(), method, ...(params === undefined ? {} : { params }) };
  socket.write(`${JSON.stringify(request)}\n`);
  for (;;) {
    const line = await readOneSocketLine(socket);
    const message = JSON.parse(line) as DaemonStreamMessage;
    if (message.id !== request.id) continue;
    if (!("ok" in message) || !message.ok) throw daemonError(message);
    if ("event" in message || "done" in message) {
      throw new JriError("Daemon returned a stream response for a unary request.", "daemon-protocol-error", "Retry the command with a compatible JRI daemon.");
    }
    return message.result;
  }
}

function isHandshake(value: unknown): value is DaemonHandshake {
  return Boolean(value && typeof value === "object" && "protocolVersion" in value && typeof value.protocolVersion === "number");
}

async function connectSocket(socketPath: string): Promise<Socket> {
  return await new Promise<Socket>((resolveConnect, rejectConnect) => {
    const socket = createConnection(socketPath);
    const onError = (error: Error) => {
      socket.destroy();
      rejectConnect(error);
    };
    socket.once("error", onError);
    socket.once("connect", () => {
      socket.off("error", onError);
      resolveConnect(socket);
    });
  });
}

async function handleConnection(socket: Socket, paths: DaemonPaths, requestShutdown: () => void): Promise<void> {
  let buffer = "";
  socket.setEncoding("utf8");
  for await (const chunk of socket) {
    buffer += chunk;
    for (;;) {
      const newline = buffer.indexOf("\n");
      if (newline === -1) break;
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (!line) continue;
      await handleRequestLine(socket, paths, line, requestShutdown);
    }
  }
}

async function handleRequestLine(socket: Socket, paths: DaemonPaths, line: string, requestShutdown: () => void): Promise<void> {
  let request: DaemonRequest;
  try {
    request = parseRequest(line);
  } catch (error) {
    writeResponse(socket, {
      id: "unknown",
      ok: false,
      error: serializeError(error),
    });
    return;
  }

  try {
    if (request.method === "handshake") {
      writeResponse(socket, {
        id: request.id,
        ok: true,
        result: { protocolVersion: DAEMON_PROTOCOL_VERSION },
      });
      return;
    }
    if (request.method === "daemon.shutdown") {
      writeResponse(socket, { id: request.id, ok: true, result: { exiting: true } });
      requestShutdown();
      return;
    }

    const projectDir = projectDirParam(request.params);
    await updateRegistry(paths, projectDir);

    if (request.method === "status.get") {
      const status = await getRecoveredStatus(projectDir);
      await updateRegistry(paths, projectDir, status);
      writeResponse(socket, { id: request.id, ok: true, result: status });
      return;
    }
    if (request.method === "loop.stop") {
      const event = await requestGracefulStop(projectDir);
      await updateRegistry(paths, projectDir);
      writeResponse(socket, { id: request.id, ok: true, result: event.type === "stopRequested" ? { requested: event.data.requested } : undefined });
      return;
    }
    if (request.method === "loop.observe") {
      for await (const event of observeLoop(projectDir, observeOptionsFromParams(request.params))) {
        writeStreamEvent(socket, request.id, event);
      }
      writeStreamDone(socket, request.id);
      return;
    }
    if (request.method === "loop.halt") {
      for await (const event of haltLoop(projectDir, haltOptionsFromParams(request.params))) {
        writeStreamEvent(socket, request.id, event);
      }
      await updateRegistry(paths, projectDir);
      writeStreamDone(socket, request.id);
      return;
    }
    if (request.method === "loop.resume") {
      for await (const event of resumeLoop(projectDir)) {
        writeStreamEvent(socket, request.id, event);
      }
      await updateRegistry(paths, projectDir);
      writeStreamDone(socket, request.id);
      return;
    }

    throw new JriError(`Unsupported daemon method: ${request.method}`, "unsupported-daemon-method", "Upgrade JRI or retry with a supported command.");
  } catch (error) {
    writeResponse(socket, {
      id: request.id,
      ok: false,
      error: serializeError(error),
    });
  }
}

async function updateRegistry(paths: DaemonPaths, projectDir: string, status?: ProjectStatus): Promise<void> {
  const registry = await readRegistry(paths.registryPath);
  const currentStatus = status ?? (await getRecoveredStatus(projectDir));
  const lastSeenAt = new Date().toISOString();
  const existing = registry.projects.find((project) => project.projectDir === projectDir);
  const entry = {
    projectDir,
    lastSeenAt,
    activeLoopId: currentStatus.activeLoopId,
  };
  if (existing) Object.assign(existing, entry);
  else registry.projects.push(entry);
  await writeRegistry(paths.registryPath, registry);
}

async function readRegistry(path: string): Promise<DaemonRegistry> {
  if (!(await pathExists(path))) return { protocolVersion: DAEMON_PROTOCOL_VERSION, projects: [] };
  const parsed = JSON.parse(await readFile(path, "utf8")) as DaemonRegistry;
  if (parsed.protocolVersion !== DAEMON_PROTOCOL_VERSION || !Array.isArray(parsed.projects)) {
    return { protocolVersion: DAEMON_PROTOCOL_VERSION, projects: [] };
  }
  return parsed;
}

async function writeRegistry(path: string, registry: DaemonRegistry): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const tmpPath = `${path}.${process.pid}.${Date.now()}.tmp`;
  await writeFile(tmpPath, `${JSON.stringify(registry, null, 2)}\n`, "utf8");
  await rename(tmpPath, path);
}

async function* readSocketLines(socket: Socket): AsyncIterable<string> {
  let buffer = "";
  socket.setEncoding("utf8");
  for await (const chunk of socket) {
    buffer += chunk;
    for (;;) {
      const newline = buffer.indexOf("\n");
      if (newline === -1) break;
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (!line) continue;
      yield line;
    }
  }
}

function readOneSocketLine(socket: Socket): Promise<string> {
  return new Promise((resolveLine, rejectLine) => {
    let buffer = "";
    const cleanup = () => {
      socket.off("data", onData);
      socket.off("error", onError);
      socket.off("close", onClose);
    };
    const onData = (chunk: string | Buffer) => {
      buffer += chunk.toString();
      const newline = buffer.indexOf("\n");
      if (newline === -1) return;
      const line = buffer.slice(0, newline).trim();
      cleanup();
      if (line) resolveLine(line);
      else rejectLine(new JriError("Daemon returned an empty response.", "daemon-protocol-error", "Retry the command with a compatible JRI daemon."));
    };
    const onError = (error: Error) => {
      cleanup();
      rejectLine(error);
    };
    const onClose = () => {
      cleanup();
      rejectLine(
        new JriError(
          "Daemon closed the connection before responding.",
          "daemon-disconnected",
          "Retry the command; if it repeats, inspect project status from .jri/status.json.",
        ),
      );
    };
    socket.on("data", onData);
    socket.once("error", onError);
    socket.once("close", onClose);
  });
}

function parseRequest(line: string): DaemonRequest {
  const parsed = JSON.parse(line) as Partial<DaemonRequest>;
  if (!parsed || typeof parsed !== "object" || typeof parsed.id !== "string" || typeof parsed.method !== "string") {
    throw new JriError("Daemon request is malformed.", "invalid-daemon-request", "Retry with a compatible JRI client.");
  }
  return { id: parsed.id, method: parsed.method, params: parsed.params };
}

function projectDirParam(params: unknown): string {
  if (!params || typeof params !== "object" || !("projectDir" in params) || typeof params.projectDir !== "string") {
    throw new JriError("Daemon request is missing projectDir.", "invalid-daemon-request", "Retry with a compatible JRI client.");
  }
  return resolve(params.projectDir);
}

function observeOptionsParam(options: LoopObserveOptions): LoopObserveOptions {
  return {
    ...(options.includeStdout === undefined ? {} : { includeStdout: options.includeStdout }),
    ...(options.recentStdoutLines === undefined ? {} : { recentStdoutLines: options.recentStdoutLines }),
    ...(options.follow === undefined ? {} : { follow: options.follow }),
  };
}

function observeOptionsFromParams(params: unknown): LoopObserveOptions {
  if (!params || typeof params !== "object" || !("observe" in params) || !params.observe || typeof params.observe !== "object") return {};
  const observe = params.observe as Record<string, unknown>;
  return {
    ...(typeof observe.includeStdout === "boolean" ? { includeStdout: observe.includeStdout } : {}),
    ...(typeof observe.recentStdoutLines === "number" && Number.isInteger(observe.recentStdoutLines) && observe.recentStdoutLines > 0
      ? { recentStdoutLines: observe.recentStdoutLines }
      : {}),
    ...(typeof observe.follow === "boolean" ? { follow: observe.follow } : {}),
  };
}

function haltOptionsParam(options: HaltOptions): HaltOptions {
  return {
    ...(options.resetGit === undefined ? {} : { resetGit: options.resetGit }),
  };
}

function haltOptionsFromParams(params: unknown): HaltOptions {
  if (!params || typeof params !== "object" || !("halt" in params) || !params.halt || typeof params.halt !== "object") return {};
  const halt = params.halt as Record<string, unknown>;
  return {
    ...(typeof halt.resetGit === "boolean" ? { resetGit: halt.resetGit } : {}),
  };
}

function writeResponse(socket: Socket, response: DaemonResponse): void {
  socket.write(`${JSON.stringify(response)}\n`);
}

function writeStreamEvent(socket: Socket, id: string, event: CoreEvent): void {
  socket.write(`${JSON.stringify({ id, ok: true, event })}\n`);
}

function writeStreamDone(socket: Socket, id: string): void {
  socket.write(`${JSON.stringify({ id, ok: true, done: true })}\n`);
}

function serializeError(error: unknown): { code: string; message: string; recovery?: string } {
  if (isJriError(error)) return { code: error.code, message: error.message, recovery: error.recovery };
  return { code: "daemon-error", message: error instanceof Error ? error.message : String(error) };
}

function daemonError(message: DaemonStreamMessage): JriError {
  if ("ok" in message && !message.ok) {
    return new JriError(message.error.message, message.error.code, message.error.recovery ?? "Retry the command or inspect the durable .jri files.");
  }
  return new JriError("Daemon returned an invalid response.", "daemon-protocol-error", "Retry the command with a compatible JRI daemon.");
}

function connectionError(error: unknown): JriError {
  return new JriError(
    "The JRI daemon is unavailable.",
    "daemon-unavailable",
    error instanceof Error ? error.message : "Retry the command; if it repeats, inspect the durable .jri files.",
  );
}

function closeServer(server: Server): Promise<void> {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolveClose, rejectClose) => {
    server.close((error) => (error ? rejectClose(error) : resolveClose()));
  });
}

function currentUserId(): string {
  try {
    return String(userInfo().uid);
  } catch {
    return "user";
  }
}

async function pathExists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") return false;
    throw error;
  }
}
