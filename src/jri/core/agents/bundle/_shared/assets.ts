import { readFileSync } from "node:fs";
import { dirname, join, posix, win32 } from "node:path";
import { fileURLToPath } from "node:url";

const manifestPath = new URL("../manifest.json", import.meta.url);
const moduleDir = dirname(fileURLToPath(import.meta.url));

let cachedManifest: Readonly<Record<string, string>> | undefined;

export function resourceManifest(): Readonly<Record<string, string>> {
  if (cachedManifest !== undefined) {
    return cachedManifest;
  }
  const parsed: unknown = JSON.parse(readFileSync(manifestPath, "utf-8"));
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("agent resource manifest must be an object");
  }
  const resources: Record<string, string> = {};
  for (const [resourceId, resourcePath] of Object.entries(parsed)) {
    if (resourceId.length === 0) {
      throw new Error("agent resource manifest IDs must be non-empty strings");
    }
    if (typeof resourcePath !== "string") {
      throw new Error(`agent resource '${resourceId}' path must be a string`);
    }
    resources[resourceId] = validateManifestPath(resourceId, resourcePath);
  }
  cachedManifest = Object.freeze(resources);
  return cachedManifest;
}

export function resourceRelativePath(resourceId: string): string {
  const relativePath = resourceManifest()[resourceId];
  if (relativePath === undefined) {
    throw new Error(`unknown agent resource ID: ${resourceId}`);
  }
  return relativePath;
}

export function resourcePath(resourceId: string, root = moduleDir): string {
  return join(root, ...resourceRelativePath(resourceId).split("/"));
}

function validateManifestPath(resourceId: string, rawPath: string): string {
  if (rawPath.length === 0 || rawPath.includes("\0") || rawPath.includes("\\")) {
    throw new Error(`agent resource '${resourceId}' path must be a POSIX path`);
  }
  if (posix.isAbsolute(rawPath) || win32.isAbsolute(rawPath)) {
    throw new Error(`agent resource '${resourceId}' path must be relative`);
  }
  const parts = rawPath.split("/");
  if (parts.some((part) => part === "" || part === "." || part === "..")) {
    throw new Error(`agent resource '${resourceId}' path must not traverse parents`);
  }
  return rawPath;
}
