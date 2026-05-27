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
  return { provider, authenticated: hasOpenAiApiKey(env) || (await hasUsablePiOpenAiAuth(env)) };
}

export async function login(env: NodeJS.ProcessEnv = process.env): Promise<AuthResult> {
  const state = await getAuthStatus(env);
  if (state.authenticated) return { status: "authenticated", state };
  const diagnostics = await inspectPiOpenAiAuth(env);
  const staleAuthNote = diagnostics.hasStaleOpenAiAuth
    ? ` Existing Pi OpenAI credentials in ${diagnostics.authPath} are expired or empty; refresh them with Pi auth or remove that entry.`
    : "";

  return {
    status: "userActionRequired",
    instructions: [
      "OpenAI authentication is required before JRI can start controlled Pi sessions.",
      `Set OPENAI_API_KEY in this shell, or run jri auth login after completing OpenAI auth in Pi so credentials are available in ${diagnostics.authPath}, then rerun jri auth status.${staleAuthNote}`,
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

  const parsed = parsePiAuthFile(await Bun.file(authPath).text(), authPath);
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

async function hasUsablePiOpenAiAuth(env: NodeJS.ProcessEnv): Promise<boolean> {
  return (await inspectPiOpenAiAuth(env)).hasUsableOpenAiAuth;
}

async function inspectPiOpenAiAuth(env: NodeJS.ProcessEnv): Promise<{ authPath: string; hasUsableOpenAiAuth: boolean; hasStaleOpenAiAuth: boolean }> {
  const authPath = piAuthPath(env);
  if (!(await Bun.file(authPath).exists())) return { authPath, hasUsableOpenAiAuth: false, hasStaleOpenAiAuth: false };

  const parsed = parsePiAuthFile(await Bun.file(authPath).text(), authPath);
  let hasStaleOpenAiAuth = false;
  for (const [key, value] of Object.entries(parsed)) {
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
      `Fix or remove ${path}, then rerun jri auth status. ${error instanceof Error ? error.message : ""}`.trim(),
    );
  }

  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new JriError(`${path} must contain a JSON object.`, "invalid-pi-auth", `Fix or remove ${path}, then rerun jri auth status.`);
  }

  return parsed as Record<string, PiAuthEntry>;
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
