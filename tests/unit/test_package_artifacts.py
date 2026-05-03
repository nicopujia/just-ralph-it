import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

REQUIRED_AGENT_RESOURCES = {
    "src/jri/core/agents/resource-manifest.json",
    "src/jri/core/agents/extension.ts",
    "src/jri/core/agents/common.ts",
    "src/jri/core/agents/python-bridge.ts",
    "src/jri/core/agents/commit-guard.ts",
    "src/jri/core/agents/resources.ts",
    "src/jri/core/agents/resources.py",
    "src/jri/core/agents/ralph/prompt.md",
    "src/jri/core/agents/interrogator/prompt.md",
    "src/jri/core/agents/explorer/prompt.md",
    "src/jri/core/agents/ralph/validator/prompt.md",
    "src/jri/core/agents/interrogator/validator/prompt.md",
    "src/jri/core/agents/interrogator/validator/extension.ts",
    "src/jri/core/agents/ralph/skills/hosted-projects/SKILL.md",
    "src/jri/core/agents/ralph/skills/reverse-ralph/SKILL.md",
    "src/jri/core/agents/tools/run-python-tool.ts",
    "src/jri/core/agents/tools/__init__.py",
    "src/jri/core/agents/tools/__main__.py",
    "src/jri/core/agents/tools/colors.py",
    "src/jri/core/agents/tools/promotion.py",
    "src/jri/core/agents/tools/readme.py",
    "src/jri/core/agents/tools/_registry.py",
    "src/jri/core/agents/tools/tasks.py",
    "src/jri/core/agents/tools/_validation.py",
    "src/jri/core/agents/tools/ralph_result.py",
}

FORBIDDEN_ARTIFACT_PARTS = {
    "__pycache__",
    ".sisyphus",
    "htmlcov",
    ".pytest_cache",
}

FORBIDDEN_ARTIFACT_NAMES = {
    ".coverage",
    "coverage.xml",
}

FORBIDDEN_ARTIFACT_SUFFIXES = {
    ".pyc",
    ".pyo",
}


def test_built_wheel_and_sdist_include_agent_runtime_resources(
    tmp_path: Path,
) -> None:
    subprocess.run(
        ["uv", "build", "--out-dir", str(tmp_path)],
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )

    wheel_paths = sorted(tmp_path.glob("*.whl"))
    sdist_paths = sorted(tmp_path.glob("*.tar.gz"))

    assert len(wheel_paths) == 1
    assert len(sdist_paths) == 1
    _assert_required_resources(
        _wheel_names(wheel_paths[0]),
        required_paths={name.removeprefix("src/") for name in REQUIRED_AGENT_RESOURCES},
    )
    _assert_required_resources(
        sdist_names := _sdist_names(sdist_paths[0]),
        required_paths={
            f"{_sdist_root(sdist_names)}/{name}" for name in REQUIRED_AGENT_RESOURCES
        },
    )


def _wheel_names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as wheel:
        return set(wheel.namelist())


def _sdist_names(path: Path) -> set[str]:
    with tarfile.open(path) as sdist:
        return set(sdist.getnames())


def _sdist_root(names: set[str]) -> str:
    roots = {
        PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts
    }
    assert len(roots) == 1
    return roots.pop()


def _assert_required_resources(names: set[str], *, required_paths: set[str]) -> None:
    assert required_paths <= names
    assert not [name for name in names if _is_forbidden_artifact(name)]


def _is_forbidden_artifact(name: str) -> bool:
    path = Path(name)
    return (
        any(part in FORBIDDEN_ARTIFACT_PARTS for part in path.parts)
        or path.name in FORBIDDEN_ARTIFACT_NAMES
        or path.suffix in FORBIDDEN_ARTIFACT_SUFFIXES
    )
