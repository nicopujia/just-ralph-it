import { tool } from "@opencode-ai/plugin";
import { existsSync, lstatSync, mkdirSync, realpathSync, writeFileSync } from "fs";
import path from "path";

const SLUG_RE = /^[a-zA-Z0-9][-a-zA-Z0-9_.]*$/;

function slugify(title) {
  const slug = title
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^[^a-z0-9]+|[^a-z0-9]+$/g, "");
  if (!slug) {
    throw new Error("could not derive a valid slug from title; pass `slug`");
  }
  return slug;
}

function assertStringList(name, value) {
  if (value === undefined) {
    return;
  }
  if (
    !Array.isArray(value) ||
    value.some((item) => typeof item !== "string" || !item.trim())
  ) {
    throw new Error(`\`${name}\` must be a list of non-empty strings`);
  }
  if (new Set(value).size !== value.length) {
    throw new Error(`\`${name}\` must not contain duplicates`);
  }
}

function serializeTask({
  title,
  priority,
  assignee,
  depends_on,
  acceptance_criteria,
  body,
}) {
  const metadata = {
    title,
    priority,
    assignee,
    depends_on,
  };
  if (acceptance_criteria !== undefined) {
    metadata.acceptance_criteria = acceptance_criteria;
  }
  return `---\n${JSON.stringify(metadata, null, 2)}\n---\n\n${body}`;
}

function ensureExpectedRealPath(parentDir, childName) {
  const childPath = path.join(parentDir, childName);
  mkdirSync(childPath, { recursive: true });
  const realChildPath = realpathSync(childPath);
  if (realChildPath !== childPath) {
    throw new Error("refusing to write outside `.jri/tasks/draft/`");
  }
  return childPath;
}

export default tool({
  name: "create-task",
  description:
    "Create or replace one JRI draft task at .jri/tasks/draft/<slug>.md from structured fields. Draft tasks only; this does not promote, move, or rename tasks.",
  args: {
    title: tool.schema.string().describe("Brief task title, max 50 chars"),
    body: tool.schema.string().describe("Markdown task body"),
    assignee: tool.schema
      .enum(["Ralph", "Human"])
      .describe("Who should own the draft task"),
    priority: tool.schema
      .number()
      .int()
      .min(0)
      .max(4)
      .describe("Task priority from 0 to 4"),
    slug: tool.schema
      .string()
      .optional()
      .describe("Optional existing slug to update; otherwise derived from title"),
    depends_on: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe("Optional list of blocking task slugs"),
    acceptance_criteria: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe("Optional acceptance criteria; drafts may omit this"),
  },
  async execute({ title, body, assignee, priority, slug, depends_on, acceptance_criteria }) {
    if (!title.trim()) {
      throw new Error("`title` must be a non-empty string");
    }
    if (title.length > 50) {
      throw new Error("`title` must be 50 characters or fewer");
    }
    if (!body.trim()) {
      throw new Error("`body` must be a non-empty string");
    }

    const taskSlug = slug ? slug.trim() : slugify(title);
    if (!SLUG_RE.test(taskSlug)) {
      throw new Error(
        "`slug` contains characters not allowed in task filenames; use only letters, digits, hyphens, dots, and underscores"
      );
    }

    assertStringList("depends_on", depends_on);
    assertStringList("acceptance_criteria", acceptance_criteria);

    const repoRoot = realpathSync(process.cwd());
    const jriDir = ensureExpectedRealPath(repoRoot, ".jri");
    const tasksDir = ensureExpectedRealPath(jriDir, "tasks");
    const draftDir = ensureExpectedRealPath(tasksDir, "draft");

    const taskPath = path.resolve(draftDir, `${taskSlug}.md`);
    const relative = path.relative(draftDir, taskPath);
    if (relative.startsWith("..") || path.isAbsolute(relative)) {
      throw new Error("refusing to write outside `.jri/tasks/draft/`");
    }

    if (existsSync(taskPath) && lstatSync(taskPath).isSymbolicLink()) {
      throw new Error("refusing to overwrite symlinked draft task");
    }

    const contents = serializeTask({
      title: title.trim(),
      priority,
      assignee,
      depends_on: depends_on ?? [],
      acceptance_criteria,
      body,
    });
    const action = existsSync(taskPath) ? "updated" : "created";
    writeFileSync(taskPath, contents, "utf-8");
    return `${action} draft task: .jri/tasks/draft/${taskSlug}.md`;
  },
});
