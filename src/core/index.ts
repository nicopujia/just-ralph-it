import { resolveProjectRoot } from "./project-root";
import { Project, validateExistingProject } from "./project";

export type * from "./types";
export { JriError, isJriError } from "./errors";
export { handoffPrefix, parseHandoff, extractLatestHandoffFromText, extractLatestBuilderHandoffFromText } from "./handoffs";
export { Project } from "./project";
export { resolveProjectRoot } from "./project-root";

export async function open(projectDir: string): Promise<Project> {
  const resolved = await resolveProjectRoot(projectDir);
  await validateExistingProject(resolved.root);
  return new Project(resolved.root, resolved.needsGitInit);
}
