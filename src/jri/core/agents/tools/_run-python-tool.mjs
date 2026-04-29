import { spawnSync } from "child_process";
import path from "node:path";

export function runPythonTool(toolName, payload) {
  const candidates = [process.env.JRI_PYTHON, "python3", "python"].filter(
    Boolean,
  );
  const env = { ...process.env };
  const pythonPath = [process.env.JRI_PYTHONPATH, process.env.PYTHONPATH]
    .filter(Boolean)
    .join(path.delimiter);
  if (pythonPath) {
    env.PYTHONPATH = pythonPath;
  }

  for (const command of candidates) {
    const result = spawnSync(command, ["-m", "jri.core.agents.tools", toolName], {
      input: JSON.stringify(payload),
      encoding: "utf-8",
      env,
    });
    if (result.error && result.error.code === "ENOENT") {
      continue;
    }
    if (result.error) {
      throw result.error;
    }
    if (result.status !== 0) {
      throw new Error(
        result.stderr.trim() ||
          `python tool failed with exit code ${result.status}`,
      );
    }
    return result.stdout.trimEnd();
  }

  throw new Error("could not find a Python interpreter for JRI tools");
}
