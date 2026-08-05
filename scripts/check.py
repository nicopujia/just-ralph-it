#!/usr/bin/env -S uv run --script

import ast
import shutil
import subprocess
import tomllib
from argparse import ArgumentParser
from collections.abc import Callable, Iterator
from pathlib import Path

BUILD_DIR = ".dist"
# A contract test reaches the endpoint it is the oracle for, so a check
# a developer runs after every change leaves it out -- an aeroplane, a
# hotel wifi or a DNS blip must not read as broken code -- and the
# release gate asks for it, since a release is the moment a wire shape
# nothing checked becomes a shape every user runs.
CONTRACT_MARKER = "contract"
CONTRACT_COMMAND = ("run", "--locked", "pytest", "-q", "-m", CONTRACT_MARKER)
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
        "-m",
        f"not {CONTRACT_MARKER}",
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
# The package meant to be reusable outside JRI, so its modules declare
# what they export rather than leaving every name reachable.
PUBLIC_API_PACKAGE = "lib"
TEST_SUPPORT_MODULES = frozenset({"__init__.py", "conftest.py"})


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--contracts", action="store_true")
    arguments = parser.parse_args()
    root = Path(__file__).parent.parent
    source = root / "src"
    package = source / "jri"
    tests = root / "tests"
    scripts = Path(__file__).parent
    check_version(root)
    check_member_order(source, tests)
    check_constant_publicity(source, tests)
    check_layering(package, tests)
    check_public_api(package)
    check_import_depth(source, tests)
    check_test_layout(package, tests)
    check_deferred_annotations(source, tests)
    check_docstrings(source, tests, scripts)
    run_uv_commands(root, contracts=arguments.contracts)


def check_version(root: Path) -> None:
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if f'__version__ = "{version}"' not in (root / "src" / "jri" / "__init__.py").read_text():
        raise RuntimeError(f"jri.__version__ must be {version}, as pyproject.toml says")


def check_member_order(*roots: Path) -> None:
    disorder = [
        line
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for line in _find_disorder(ast.parse(path.read_text()).body, _rank_module, MODULE_GROUPS, path, "module")
    ]
    if disorder:
        raise RuntimeError("Members must follow the documented order:\n" + "\n".join(disorder))


def check_constant_publicity(*roots: Path) -> None:
    private = [
        line
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for line in _find_private_constants(ast.parse(path.read_text()).body, path)
    ]
    if private:
        raise RuntimeError("Module constants must be public:\n" + "\n".join(private))


def check_layering(package: Path, tests: Path) -> None:
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
        raise RuntimeError("Packages must reach only for the ones below them:\n" + "\n".join(crossings))


def check_public_api(package: Path) -> None:
    silent = [
        f"{path}: no __all__"
        for path in sorted((package / PUBLIC_API_PACKAGE).rglob("*.py"))
        if path.name != "__init__.py"
        and not any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
            for node in ast.parse(path.read_text(encoding="utf-8")).body
        )
    ]
    if silent:
        raise RuntimeError(
            f"`{PUBLIC_API_PACKAGE}` is reusable code, so every module states what it exports:\n" + "\n".join(silent)
        )


def check_import_depth(*roots: Path) -> None:
    deep = [
        f"{path}:{line}: {module} is {module.count('.') + 1} levels deep"
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for module, line in _find_imports(path)
        if module.startswith(("jri.", "tests.")) and module.count(".") + 1 > MAX_IMPORT_DEPTH
    ]
    if deep:
        raise RuntimeError(f"Imports must stay within {MAX_IMPORT_DEPTH} levels:\n" + "\n".join(deep))


def check_test_layout(package: Path, tests: Path) -> None:
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
        and not any((tests / name).exists() for name in _name_test_modules(path.relative_to(package)))
    ]
    if misplaced or untested:
        raise RuntimeError("Tests laid out wrongly:\n" + "\n".join(misplaced + untested))


def check_deferred_annotations(*roots: Path) -> None:
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


def check_docstrings(*roots: Path) -> None:
    written = [
        f"{path}:{line}: docstring"
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for line in _find_docstrings(path)
    ]
    if written:
        raise RuntimeError(
            "Docstrings are not written here; carry the what in a name, a type or a test, and the why in a `#` "
            "comment:\n" + "\n".join(written)
        )


def run_uv_commands(root: Path, *, contracts: bool) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv must be installed")
    build_path = root / BUILD_DIR
    if build_path.exists():
        shutil.rmtree(build_path)
    for command in (*UV_COMMANDS, CONTRACT_COMMAND) if contracts else UV_COMMANDS:
        subprocess.run([uv, *command], cwd=root, check=True)


def _find_disorder(
    body: list[ast.stmt], rank: Callable[[ast.stmt], int | None], groups: tuple[str, ...], path: Path, scope: str
) -> Iterator[str]:
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
    for node in body:
        match node:
            case ast.Assign(targets=[ast.Name(id=name)]) | ast.AnnAssign(target=ast.Name(id=name)):
                if name.startswith("_") and not name.endswith("__") and name.isupper():
                    yield f"{path}:{node.lineno}: private constant {name} in module"
            case _:
                continue


def _find_docstrings(path: Path) -> Iterator[int]:
    for node in _parse(path):
        if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            yield first.lineno


def _name_test_modules(relative: Path) -> Iterator[str]:
    # A conflicting stem earns the sub-packages enclosing it as a
    # prefix, closest first, until nothing else claims the name.
    packages = relative.parts[:-1]
    for start in range(len(packages), -1, -1):
        yield f"test_{'_'.join([*packages[start:], relative.stem])}.py"


def _rank_module(node: ast.stmt) -> int | None:
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
    for node in _parse(path):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module, node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno


def _parse(path: Path) -> Iterator[ast.AST]:
    yield from ast.walk(ast.parse(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
