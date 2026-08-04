#!/usr/bin/env -S uv run --script

"""Build, compile, format, lint, type-check, and test the project."""

import ast
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path

MODULE_GROUPS = (
    "dunder",
    "type",
    "constant",
    "variable",
    "private variable",
    "function",
    "class",
    "private function",
    "private class",
)
CLASS_GROUPS = ("constant", "nested type", "magic method", "method", "private method")


def main() -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv must be installed")
    root = Path(__file__).parent.parent
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if f'__version__ = "{version}"' not in (root / "src" / "jri" / "__init__.py").read_text():
        raise RuntimeError(f"jri.__version__ must be {version}, as pyproject.toml says")
    check_member_order(root / "src", root / "tests")
    check_constant_publicity(root / "src", root / "tests")
    build_path = root / ".dist"
    if build_path.exists():
        shutil.rmtree(build_path)
    subprocess.run([uv, "build", "--no-sources", "--out-dir", build_path], check=True)
    for command in (
        ["ruff", "format", "-q"],
        ["ruff", "check", "--fix", "-q"],
        ["python", "-m", "compileall", "-q", "--invalidation-mode", "checked-hash", "src"],
        ["basedpyright"],
        ["pytest", "-q", "--cov=src/jri/core", "--cov=src/jri/lib", "--cov-report=term-missing", "--cov-fail-under=80"],
    ):
        subprocess.run([uv, "run", "--locked", *command], check=True)


def check_member_order(*roots: Path) -> None:
    """Check every module and class against the AGENTS.md order.

    Members no group covers, such as a conditional definition, are
    left alone.

    Raises:
        RuntimeError: If a member comes before an earlier group.
    """

    disorder = [
        line
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for line in _find_disorder(ast.parse(path.read_text()).body, _rank_module, MODULE_GROUPS, path, "module")
    ]
    if disorder:
        raise RuntimeError("Members out of the order AGENTS.md describes:\n" + "\n".join(disorder))


def check_constant_publicity(*roots: Path) -> None:
    """Check every module constant against the AGENTS.md publicity.

    A leading underscore reads as a constant to the order check, since
    `"_FOO".isupper()` holds, so it would otherwise pass unnoticed.

    Raises:
        RuntimeError: If a module keeps a constant to itself.
    """

    private = [
        line
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for line in _find_private_constants(ast.parse(path.read_text()).body, path)
    ]
    if private:
        raise RuntimeError("Constants private though AGENTS.md requires them public:\n" + "\n".join(private))


def _find_disorder(
    body: list[ast.stmt], rank: Callable[[ast.stmt], int | None], groups: tuple[str, ...], path: Path, scope: str
) -> Iterator[str]:
    """Report every member of a body that its predecessors outrank.

    Yields:
        One `file:line:` complaint per member out of order.
    """

    highest = 0
    for node in body:
        group = rank(node)
        if group is not None:
            if group < highest:
                yield f"{path}:{node.lineno}: {groups[group]} after {groups[highest]} in {scope}"
            highest = max(highest, group)
        if isinstance(node, ast.ClassDef):
            yield from _find_disorder(node.body, _rank_class, CLASS_GROUPS, path, f"class {node.name}")


def _find_private_constants(body: list[ast.stmt], path: Path) -> Iterator[str]:
    """Report every constant a module hides behind an underscore.

    Yields:
        One `file:line:` complaint per private constant.
    """

    for node in body:
        match node:
            case ast.Assign(targets=[ast.Name(id=name)]) | ast.AnnAssign(target=ast.Name(id=name)):
                if name.startswith("_") and not name.endswith("__") and name.isupper():
                    yield f"{path}:{node.lineno}: private constant {name} in module"
            case _:
                continue


def _rank_module(node: ast.stmt) -> int | None:
    """Place a module member in `MODULE_GROUPS`.

    Returns:
        The group index, or nothing for members without a group.
    """

    match node:
        case ast.Assign(targets=[ast.Name(id=name)]) | ast.AnnAssign(target=ast.Name(id=name)):
            if name.startswith("__") and name.endswith("__"):
                return 0
            if name.isupper():
                return 2
            return 4 if name.startswith("_") else 3
        case ast.TypeAlias():
            return 1
        case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
            return 7 if name.startswith("_") else 5
        case ast.ClassDef(name=name):
            return 8 if name.startswith("_") else 6
        case _:
            return None


def _rank_class(node: ast.stmt) -> int | None:
    """Place a class member in `CLASS_GROUPS`.

    Returns:
        The group index, or nothing for members without a group.
    """

    match node:
        case ast.Assign() | ast.AnnAssign():
            return 0
        case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
            if name.startswith("__") and name.endswith("__"):
                return 2
            return 4 if name.startswith("_") else 3
        case ast.ClassDef() | ast.TypeAlias():
            return 1
        case _:
            return None


if __name__ == "__main__":
    main()
