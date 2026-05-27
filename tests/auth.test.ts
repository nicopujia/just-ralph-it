import { mkdir, mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { describe, expect, test } from "bun:test";
import { JriError, open } from "../src/core";

async function tempProject(): Promise<string> {
  return await mkdtemp(join(tmpdir(), "jri-auth-test-"));
}

describe("project auth", () => {
  test("reports OpenAI auth from OPENAI_API_KEY", async () => {
    const dir = await tempProject();
    await withAuthEnv({ OPENAI_API_KEY: "test-key", PI_CODING_AGENT_DIR: join(dir, "pi-agent") }, async () => {
      try {
        const project = await open(dir);
        expect(await project.auth.status()).toEqual({ provider: "openai", authenticated: true });
        expect(await project.auth.login()).toEqual({
          status: "authenticated",
          state: { provider: "openai", authenticated: true },
        });
      } finally {
        await rm(dir, { recursive: true, force: true });
      }
    });
  });

  test("reports OpenAI auth from Pi auth storage", async () => {
    const dir = await tempProject();
    const piDir = join(dir, "pi-agent");
    await mkdir(piDir, { recursive: true });
    await writeFile(
      join(piDir, "auth.json"),
      `${JSON.stringify({
        openai: {
          apiKey: "sk-test",
        },
      })}\n`,
      "utf8",
    );

    await withAuthEnv({ OPENAI_API_KEY: undefined, PI_CODING_AGENT_DIR: piDir }, async () => {
      try {
        const project = await open(dir);
        expect(await project.auth.status()).toEqual({ provider: "openai", authenticated: true });
      } finally {
        await rm(dir, { recursive: true, force: true });
      }
    });
  });

  test("does not report authenticated when the configured interrogator model is unavailable", async () => {
    const dir = await tempProject();
    await mkdir(join(dir, ".jri"), { recursive: true });
    await writeFile(
      join(dir, ".jri", "config.json"),
      `${JSON.stringify(
        {
          $schema: "https://justralph.it/schemas/config.schema.json",
          schemaVersion: 1,
          provider: "openai",
          modelPreset: "openai",
          agents: {
            interrogator: {
              model: "not-a-real-model",
              reasoning: "xhigh",
            },
          },
        },
        null,
        2,
      )}\n`,
      "utf8",
    );

    await withAuthEnv({ OPENAI_API_KEY: "test-key", PI_CODING_AGENT_DIR: join(dir, "pi-agent") }, async () => {
      try {
        const project = await open(dir);
        expect(await project.auth.status()).toEqual({
          provider: "openai",
          authenticated: false,
          recovery: {
            code: "model-not-found",
            message: "JRI could not resolve OpenAI model not-a-real-model.",
            instructions: "Check .jri/config.json agent model overrides or update the Pi SDK model registry.",
          },
        });
        expect(await project.auth.login()).toMatchObject({
          status: "userActionRequired",
          instructions: expect.stringContaining("Check .jri/config.json agent model overrides or update the Pi SDK model registry."),
        });
      } finally {
        await rm(dir, { recursive: true, force: true });
      }
    });
  });

  test("login returns actionable guidance when no Pi-backed auth exists", async () => {
    const dir = await tempProject();
    await withAuthEnv({ OPENAI_API_KEY: undefined, PI_CODING_AGENT_DIR: join(dir, "pi-agent") }, async () => {
      try {
        const project = await open(dir);
        expect(await project.auth.status()).toEqual({ provider: "openai", authenticated: false });
        expect(await project.auth.login()).toMatchObject({
          status: "userActionRequired",
          instructions: expect.stringContaining("OPENAI_API_KEY"),
        });
      } finally {
        await rm(dir, { recursive: true, force: true });
      }
    });
  });

  test("treats corrupt Pi auth cache as recoverable unauthenticated state", async () => {
    const dir = await tempProject();
    const piDir = join(dir, "pi-agent");
    await mkdir(piDir, { recursive: true });
    await writeFile(join(piDir, "auth.json"), "{not json", "utf8");

    await withAuthEnv({ OPENAI_API_KEY: undefined, PI_CODING_AGENT_DIR: piDir }, async () => {
      try {
        const project = await open(dir);
        expect(await project.auth.status()).toEqual({
          provider: "openai",
          authenticated: false,
          recovery: {
            code: "invalid-pi-auth",
            message: expect.stringContaining("is not valid JSON"),
            instructions: expect.stringContaining("Fix or remove"),
          },
        });
        expect(await project.auth.login()).toMatchObject({
          status: "userActionRequired",
          instructions: expect.stringContaining("Fix or remove"),
        });
      } finally {
        await rm(dir, { recursive: true, force: true });
      }
    });
  });

  test("login guidance calls out stale Pi auth entries", async () => {
    const dir = await tempProject();
    const piDir = join(dir, "pi-agent");
    await mkdir(piDir, { recursive: true });
    await writeFile(
      join(piDir, "auth.json"),
      `${JSON.stringify({
        openai: {
          type: "oauth",
          access: "expired-access-token",
          refresh: "",
          expires: Date.now() - 60_000,
        },
      })}\n`,
      "utf8",
    );

    await withAuthEnv({ OPENAI_API_KEY: undefined, PI_CODING_AGENT_DIR: piDir }, async () => {
      try {
        const project = await open(dir);
        expect(await project.auth.login()).toMatchObject({
          status: "userActionRequired",
          instructions: expect.stringContaining("expired or empty"),
        });
      } finally {
        await rm(dir, { recursive: true, force: true });
      }
    });
  });

  test("logout removes OpenAI entries from Pi auth storage but refuses environment auth", async () => {
    const dir = await tempProject();
    const piDir = join(dir, "pi-agent");
    await mkdir(piDir, { recursive: true });
    const authPath = join(piDir, "auth.json");
    await writeFile(
      authPath,
      `${JSON.stringify({
        "openai-codex": { access: "access-token" },
        google: { access: "google-token" },
      })}\n`,
      "utf8",
    );

    await withAuthEnv({ OPENAI_API_KEY: undefined, PI_CODING_AGENT_DIR: piDir }, async () => {
      try {
        const project = await open(dir);
        await project.auth.logout();
        expect(JSON.parse(await Bun.file(authPath).text())).toEqual({ google: { access: "google-token" } });
      } finally {
        await rm(dir, { recursive: true, force: true });
      }
    });

    const envDir = await tempProject();
    await withAuthEnv({ OPENAI_API_KEY: "test-key", PI_CODING_AGENT_DIR: join(envDir, "pi-agent") }, async () => {
      try {
        const project = await open(envDir);
        await expect(project.auth.logout()).rejects.toThrow(JriError);
      } finally {
        await rm(envDir, { recursive: true, force: true });
      }
    });
  });

  test("logout moves corrupt Pi auth cache aside so auth can recover", async () => {
    const dir = await tempProject();
    const piDir = join(dir, "pi-agent");
    await mkdir(piDir, { recursive: true });
    const authPath = join(piDir, "auth.json");
    await writeFile(authPath, "{not json", "utf8");

    await withAuthEnv({ OPENAI_API_KEY: undefined, PI_CODING_AGENT_DIR: piDir }, async () => {
      try {
        const project = await open(dir);
        await project.auth.logout();
        expect(await Bun.file(authPath).exists()).toBe(false);
        expect((await readdir(piDir)).some((entry) => entry.startsWith("auth.json.corrupt."))).toBe(true);
        expect(await project.auth.status()).toEqual({ provider: "openai", authenticated: false });
      } finally {
        await rm(dir, { recursive: true, force: true });
      }
    });
  });
});

async function withAuthEnv(values: { OPENAI_API_KEY?: string | undefined; PI_CODING_AGENT_DIR: string }, run: () => Promise<void>): Promise<void> {
  const previousOpenAi = process.env.OPENAI_API_KEY;
  const previousPiDir = process.env.PI_CODING_AGENT_DIR;
  try {
    if (values.OPENAI_API_KEY === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = values.OPENAI_API_KEY;
    process.env.PI_CODING_AGENT_DIR = values.PI_CODING_AGENT_DIR;
    await run();
  } finally {
    if (previousOpenAi === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = previousOpenAi;
    if (previousPiDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = previousPiDir;
  }
}
