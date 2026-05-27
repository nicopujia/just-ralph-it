import { stat } from "node:fs/promises";
import { dirname, resolve } from "node:path";

export async function resolveProjectRoot(startDir: string): Promise<{ root: string; needsGitInit: boolean }> {
  const start = resolve(startDir);
  const jriRoot = await findAncestor(start, ".jri");
  if (jriRoot) {
    return { root: jriRoot, needsGitInit: false };
  }

  const gitRoot = await findAncestor(start, ".git");
  if (gitRoot) {
    return { root: gitRoot, needsGitInit: false };
  }

  return { root: start, needsGitInit: true };
}

async function findAncestor(startDir: string, marker: string): Promise<string | null> {
  let current = startDir;

  while (true) {
    if (await pathExists(resolve(current, marker))) {
      return current;
    }

    const parent = dirname(current);
    if (parent === current) {
      return null;
    }
    current = parent;
  }
}

async function pathExists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
      return false;
    }
    throw error;
  }
}
