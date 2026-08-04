#!/usr/bin/env -S uv run --script

"""Build, compile, format, lint, type-check, and test the project."""

import ast
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path

BUILD_DIR = ".dist"
UV_COMMANDS = (
    ("build", "--no-sources", "--out-dir", BUILD_DIR),
    ("run", "--locked", "ruff", "format", "-q"),
    ("run", "--locked", "ruff", "check", "--fix", "-q"),
    ("run", "--locked", "python", "-m", "compileall", "-q", "--invalidation-mode", "checked-hash", "src"),
    ("run", "--locked", "basedpyright"),
    (
        "run",
        "--locked",
        "pytest",
        "-q",
        "--cov=src/jri/core",
        "--cov=src/jri/lib",
        "--cov-report=term-missing",
        "--cov-fail-under=80",
    ),
)
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
# What each package may reach for, on top of itself.
LAYERS = {"lib": frozenset[str](), "core": frozenset({"lib"}), "tui": frozenset({"core", "lib"})}
MAX_IMPORT_DEPTH = 3
TEST_SUPPORT_MODULES = frozenset({"__init__.py", "conftest.py"})


def main() -> None:
    root = Path(__file__).parent.parent
    source = root / "src"
    package = source / "jri"
    tests = root / "tests"
    check_version(root)
    check_member_order(source, tests)
    check_constant_publicity(source, tests)
    check_layering(package, tests)
    check_import_depth(source, tests)
    check_test_layout(package, tests)
    check_deferred_annotations(source, tests)
    run_uv_commands(root)


def check_version(root: Path) -> None:
    """Check `jri.__version__` against the pyproject.toml version.

    Raises:
        RuntimeError: If the two versions disagree.
    """

    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if f'__version__ = "{version}"' not in (root / "src" / "jri" / "__init__.py").read_text():
        raise RuntimeError(f"jri.__version__ must be {version}, as pyproject.toml says")


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


def check_layering(package: Path, tests: Path) -> None:
    """Check that every package reaches only for the ones below it.

    Nothing fails at import time when `lib` reaches into `core`, so a
    package quietly stops being reusable long before anyone notices.

    Raises:
        RuntimeError: If a package imports one it must not know.
    """

    crossings = [
        f"{path}:{line}: {path.relative_to(package).parts[0]} imports {module}"
        for path in sorted(package.rglob("*.py"))
        for module, line in _find_imports(path)
        if (layer := path.relative_to(package).parts[0]) in LAYERS
        and module.startswith("jri.")
        and module.split(".")[1] not in LAYERS[layer] | {layer}
    ]
    crossings += [
        f"{path}:{line}: tests import {module}"
        for path in sorted(tests.rglob("*.py"))
        for module, line in _find_imports(path)
        if module.startswith("jri.tui")
    ]
    if crossings:
        raise RuntimeError("Packages reaching past the boundaries AGENTS.md draws:\n" + "\n".join(crossings))


def check_import_depth(*roots: Path) -> None:
    """Check every project import against the AGENTS.md depth.

    Raises:
        RuntimeError: If an import path runs deeper than the limit.
    """

    deep = [
        f"{path}:{line}: {module} is {module.count('.') + 1} levels deep"
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for module, line in _find_imports(path)
        if module.startswith(("jri.", "tests.")) and module.count(".") + 1 > MAX_IMPORT_DEPTH
    ]
    if deep:
        raise RuntimeError(f"Imports deeper than the {MAX_IMPORT_DEPTH} levels AGENTS.md allows:\n" + "\n".join(deep))


def check_test_layout(package: Path, tests: Path) -> None:
    """Check the test tree against tests/AGENTS.md.

    A module earns a test module once it defines a function, so the
    ones that only declare types, constants or exceptions are exempt.

    Raises:
        RuntimeError: If a helper sits outside `doubles/`, or a module
            with behavior has no test module.
    """

    misplaced = [
        f"{path}: helpers belong under {tests.name}/doubles/"
        for path in sorted(tests.glob("*.py"))
        if not path.name.startswith("test_") and path.name not in TEST_SUPPORT_MODULES
    ]
    untested = [
        f"{path}: no {tests.name}/test_{path.stem}.py"
        for path in sorted(package.rglob("*.py"))
        if path.name != "__init__.py"
        and path.relative_to(package).parts[0] != "tui"
        and any(isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) for node in _parse(path))
        and not (tests / f"test_{path.stem}.py").exists()
    ]
    if misplaced or untested:
        raise RuntimeError("Tests laid out against tests/AGENTS.md:\n" + "\n".join(misplaced + untested))


def check_deferred_annotations(*roots: Path) -> None:
    """Check that no module defers every annotation it writes.

    `from __future__ import annotations` turns all of them into strings
    at once, including the ones pydantic and the tool schemas read back
    at runtime. Quote the few that need it instead.

    Raises:
        RuntimeError: If a module imports the future annotations.
    """

    deferred = [
        f"{path}:{node.lineno}: annotations deferred"
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for node in _parse(path)
        if isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
    ]
    if deferred:
        raise RuntimeError("Annotations deferred where a quoted one would do:\n" + "\n".join(deferred))


def run_uv_commands(root: Path) -> None:
    """Run every command of `UV_COMMANDS` from the project root.

    They run on a build directory the last run left behind no files
    in. A failing one stops the rest.

    Raises:
        RuntimeError: If uv is missing.
    """

    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv must be installed")
    build_path = root / BUILD_DIR
    if build_path.exists():
        shutil.rmtree(build_path)
    for command in UV_COMMANDS:
        subprocess.run([uv, *command], cwd=root, check=True)


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


def _find_imports(path: Path) -> Iterator[tuple[str, int]]:
    """Report every absolute module a file imports.

    Relative imports stay inside their own package, so they cannot
    cross a boundary and are left out.

    Yields:
        One module name and line number per import.
    """

    for node in _parse(path):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module, node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno


def _parse(path: Path) -> Iterator[ast.AST]:
    """Walk every node of a Python file.

    Yields:
        Each node of the parsed file.
    """

    yield from ast.walk(ast.parse(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
