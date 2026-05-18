import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

REQUIRED_AGENT_RESOURCES = {
    "src/jri/core/agents/bundle/manifest.json",
    "src/jri/core/agents/bundle/extension.ts",
    "src/jri/core/agents/bundle/_shared/registry.ts",
    "src/jri/core/agents/bundle/_shared/subagents.ts",
    "src/jri/core/agents/bundle/_shared/commits.ts",
    "src/jri/core/agents/bundle/_shared/assets.ts",
    "src/jri/core/agents/resources.py",
    "src/jri/core/agents/bundle/theme.json",
    "src/jri/core/agents/bundle/ralph/prompt.md",
    "src/jri/core/agents/bundle/interrogator/prompt.md",
    "src/jri/core/agents/bundle/compiler/prompt.md",
    "src/jri/core/agents/bundle/explorer/prompt.md",
    "src/jri/core/agents/bundle/ralph/validator/prompt.md",
    "src/jri/core/agents/bundle/ralph/skills/project-setup/SKILL.md",
    "src/jri/core/agents/bundle/ralph/skills/hosted-projects/SKILL.md",
    "src/jri/core/agents/bundle/interrogator/skills/reverse-ralph/SKILL.md",
    "src/jri/core/agents/bundle/_shared/runner.ts",
    "src/jri/core/agents/tools/__init__.py",
    "src/jri/core/agents/tools/__main__.py",
    "src/jri/core/agents/tools/colors.py",
    "src/jri/core/agents/tools/graph.py",
    "src/jri/core/agents/tools/readme.py",
    "src/jri/core/agents/tools/_registry.py",
    "src/jri/core/agents/tools/tasks.py",
    "src/jri/core/agents/tools/_validation.py",
    "src/jri/core/agents/tools/ralph_result.py",
}

REQUIRED_TEMPLATE_RESOURCES = {
    "src/jri/core/template/graph/.gitkeep",
    "src/jri/core/template/tasks/todo/.gitkeep",
    "src/jri/core/template/tasks/doing/.gitkeep",
    "src/jri/core/template/tasks/done/.gitkeep",
    "src/jri/core/template/attempts/.gitkeep",
    "src/jri/core/template/learnings.md",
}

FORBIDDEN_ARTIFACT_PARTS = {"__pycache__", ".sisyphus", ".jri", "htmlcov", ".pytest_cache"}

FORBIDDEN_ARTIFACT_NAMES = {".coverage", "coverage.xml"}

FORBIDDEN_ARTIFACT_SUFFIXES = {".pyc", ".pyo"}

FORBIDDEN_PACKAGED_JAVASCRIPT_SUFFIXES = {".js", ".mjs"}


def test_built_wheel_and_sdist_include_agent_runtime_resources(tmp_path: Path) -> None:
    subprocess.run(["uv", "build", "--out-dir", str(tmp_path)], check=True, cwd=Path(__file__).resolve().parents[2])

    wheel_paths = sorted(tmp_path.glob("*.whl"))
    sdist_paths = sorted(tmp_path.glob("*.tar.gz"))

    assert len(wheel_paths) == 1
    assert len(sdist_paths) == 1
    _assert_required_resources(
        _wheel_names(wheel_paths[0]),
        required_paths={name.removeprefix("src/") for name in REQUIRED_AGENT_RESOURCES | REQUIRED_TEMPLATE_RESOURCES},
    )
    _assert_required_resources(
        sdist_names := _sdist_names(sdist_paths[0]),
        required_paths={
            f"{_sdist_root(sdist_names)}/{name}" for name in REQUIRED_AGENT_RESOURCES | REQUIRED_TEMPLATE_RESOURCES
        },
    )


def _wheel_names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as wheel:
        return set(wheel.namelist())


def _sdist_names(path: Path) -> set[str]:
    with tarfile.open(path) as sdist:
        return set(sdist.getnames())


def _sdist_root(names: set[str]) -> str:
    roots = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
    assert len(roots) == 1
    return roots.pop()


def _assert_required_resources(names: set[str], *, required_paths: set[str]) -> None:
    assert required_paths <= names
    assert not [name for name in names if _is_forbidden_artifact(name)]


def _is_forbidden_artifact(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        any(part in FORBIDDEN_ARTIFACT_PARTS for part in path.parts)
        or path.name in FORBIDDEN_ARTIFACT_NAMES
        or path.suffix in FORBIDDEN_ARTIFACT_SUFFIXES
        or _is_packaged_stale_javascript(path)
    )


def _is_packaged_stale_javascript(path: PurePosixPath) -> bool:
    if path.suffix not in FORBIDDEN_PACKAGED_JAVASCRIPT_SUFFIXES:
        return False

    parts = path.parts
    return parts[0] == "jri" or any(parts[index : index + 2] == ("src", "jri") for index in range(len(parts) - 1))
