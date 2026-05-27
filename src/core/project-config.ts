import { join } from "node:path";
import { parseJsonObject, validateConfig } from "./schema";

export async function readProjectConfig(projectDir: string): Promise<unknown> {
  const path = join(projectDir, ".jri", "config.json");
  if (!(await Bun.file(path).exists())) return undefined;
  return validateConfig(parseJsonObject(await Bun.file(path).text(), path), path);
}
