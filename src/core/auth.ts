import { mkdir, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { homedir } from "node:os";
import { JriError } from "./errors";
import type { AuthResult, AuthState } from "./types";

type PiAuthEntry = {
  type?: unknown;
  access?: unknown;
  refresh?: unknown;
  expires?: unknown;
  apiKey?: unknown;
};

const provider = "openai" as const;

export async function getAuthStatus(env: NodeJS.ProcessEnv = process.env): Promise<AuthState> {
  if (hasOpenAiApiKey(env)) return { provider, authenticated: true };
  const diagnostics = await inspectPiOpenAiAuth(env);
  return {
    provider,
    authenticated: diagnostics.hasUsableOpenAiAuth,
    ...(diagnostics.recovery
      ? {
          recovery: diagnostics.recovery,
        }
      : {}),
  };
}

export async function login(env: NodeJS.ProcessEnv = process.env): Promise<AuthResult> {
  const state = await getAuthStatus(env);
  if (state.authenticated) return { status: "authenticated", state };
  const diagnostics = await inspectPiOpenAiAuth(env);
  const invalidAuthNote = diagnostics.recovery ? ` ${diagnostics.recovery.instructions}` : "";
  const staleAuthNote = diagnostics.hasStaleOpenAiAuth
    ? ` Existing Pi OpenAI credentials in ${diagnostics.authPath} are expired or empty; refresh them with Pi auth or remove that entry.`
    : "";

  return {
    status: "userActionRequired",
    instructions: [
      "OpenAI authentication is required before JRI can start controlled Pi sessions.",
      `Set OPENAI_API_KEY in this shell, or run jri auth login to launch the Pi-backed OpenAI auth flow and store credentials in ${diagnostics.authPath}, then rerun jri auth status.${invalidAuthNote}${staleAuthNote}`,
    ].join(" "),
  };
}

export async function logout(env: NodeJS.ProcessEnv = process.env): Promise<void> {
  if (hasOpenAiApiKey(env)) {
    throw new JriError(
      "OpenAI authentication is provided by OPENAI_API_KEY in the current environment.",
      "auth-env-logout-unsupported",
      "Unset OPENAI_API_KEY in your shell, then rerun jri auth status.",
    );
  }

  const authPath = piAuthPath(env);
  if (!(await Bun.file(authPath).exists())) return;

  const parsed = parsePiAuthFileForLogout(await Bun.file(authPath).text(), authPath);
  if (!parsed) {
    await moveCorruptAuthAside(authPath);
    return;
  }
  let removed = false;
  for (const key of Object.keys(parsed)) {
    if (isOpenAiAuthKey(key)) {
      delete parsed[key];
      removed = true;
    }
  }
  if (!removed) return;
  await atomicWrite(authPath, `${JSON.stringify(parsed, null, 2)}\n`);
}

function hasOpenAiApiKey(env: NodeJS.ProcessEnv): boolean {
  return typeof env.OPENAI_API_KEY === "string" && env.OPENAI_API_KEY.trim().length > 0;
}

async function inspectPiOpenAiAuth(env: NodeJS.ProcessEnv): Promise<{
  authPath: string;
  hasUsableOpenAiAuth: boolean;
  hasStaleOpenAiAuth: boolean;
  recovery?: NonNullable<AuthState["recovery"]>;
}> {
  const authPath = piAuthPath(env);
  if (!(await Bun.file(authPath).exists())) return { authPath, hasUsableOpenAiAuth: false, hasStaleOpenAiAuth: false };

  const parsed = parsePiAuthFileForStatus(await Bun.file(authPath).text(), authPath);
  if (parsed.recovery) {
    return { authPath, hasUsableOpenAiAuth: false, hasStaleOpenAiAuth: false, recovery: parsed.recovery };
  }
  let hasStaleOpenAiAuth = false;
  for (const [key, value] of Object.entries(parsed.auth)) {
    if (!isOpenAiAuthKey(key)) continue;
    if (isUsableAuthEntry(value)) return { authPath, hasUsableOpenAiAuth: true, hasStaleOpenAiAuth: false };
    hasStaleOpenAiAuth = true;
  }
  return { authPath, hasUsableOpenAiAuth: false, hasStaleOpenAiAuth };
}

function piAuthPath(env: NodeJS.ProcessEnv): string {
  return join(env.PI_CODING_AGENT_DIR ?? join(homedir(), ".pi", "agent"), "auth.json");
}

function parsePiAuthFile(raw: string, path: string): Record<string, PiAuthEntry> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new JriError(
      `${path} is not valid JSON.`,
      "invalid-pi-auth",
      `Fix or remove ${path}, or run jri auth logout to move the corrupt cache aside, then rerun jri auth status. ${error instanceof Error ? error.message : ""}`.trim(),
    );
  }

  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new JriError(
      `${path} must contain a JSON object.`,
      "invalid-pi-auth",
      `Fix or remove ${path}, or run jri auth logout to move the corrupt cache aside, then rerun jri auth status.`,
    );
  }

  return parsed as Record<string, PiAuthEntry>;
}

async function moveCorruptAuthAside(path: string): Promise<void> {
  const suffix = new Date().toISOString().replace(/[:.]/g, "-");
  await rename(path, `${path}.corrupt.${suffix}`);
}

function parsePiAuthFileForLogout(raw: string, path: string): Record<string, PiAuthEntry> | null {
  try {
    return parsePiAuthFile(raw, path);
  } catch (error) {
    if (error instanceof JriError && error.code === "invalid-pi-auth") return null;
    throw error;
  }
}

function parsePiAuthFileForStatus(
  raw: string,
  path: string,
): { auth: Record<string, PiAuthEntry>; recovery?: NonNullable<AuthState["recovery"]> } {
  try {
    return { auth: parsePiAuthFile(raw, path) };
  } catch (error) {
    if (error instanceof JriError && error.code === "invalid-pi-auth") {
      return {
        auth: {},
        recovery: {
          code: error.code,
          message: error.message,
          instructions: error.recovery,
        },
      };
    }
    throw error;
  }
}

function isOpenAiAuthKey(key: string): boolean {
  return key === "openai" || key.startsWith("openai-");
}

function isUsableAuthEntry(entry: PiAuthEntry): boolean {
  const hasCredential =
    (typeof entry.access === "string" && entry.access.length > 0) ||
    (typeof entry.refresh === "string" && entry.refresh.length > 0) ||
    (typeof entry.apiKey === "string" && entry.apiKey.length > 0);
  if (!hasCredential) return false;
  if (typeof entry.expires !== "number") return true;
  return entry.expires > Date.now();
}

async function atomicWrite(path: string, contents: string): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const tmpPath = `${path}.${process.pid}.${Date.now()}.tmp`;
  await writeFile(tmpPath, contents, { encoding: "utf8", mode: 0o600 });
  await rename(tmpPath, path);
}
