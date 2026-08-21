#!/usr/bin/env -S uv run --script

import ast
import shutil
import subprocess
import tomllib
from argparse import ArgumentParser
from collections.abc import Callable, Iterator
from pathlib import Path

BUILD_DIR = ".dist"
# A contract test connects to its reference endpoint. Do not run it after every change. A flight, hotel
# Wi-Fi, or a DNS failure must not show as broken code. The release gate runs the test, because a release
# makes an unchecked wire format into a format that every user runs.
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
        # A test waits for a subprocess, a Git command, or a file much longer than it calculates. One worker
        # for each core uses that wait time for other tests. More workers than cores only add load, which
        # makes a test that waits for a deadline miss the deadline.
        "-n",
        "auto",
        "--cov=src/jri/core",
        "--cov=src/jri/lib",
        "--cov-report=term-missing",
        # This floor finds tests that went away. It does not say what the tests that stay are worth. This suite
        # covered 99% of the code, but it did not find one injected bug in three. Set the number from the loss
        # it must find, and keep it under the platform that covers least. Windows covers 97.8%, because it skips
        # 49 tests that need a shell or a signal it does not have. Without `tests/test_specs.py`, its largest
        # module, the suite covers 95.1%. This floor finds a loss larger than that one module.
        "--cov-fail-under=95",
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
# Each package can import these packages and itself.
LAYERS = {"lib": frozenset[str](), "core": frozenset({"lib"}), "tui": frozenset({"core", "lib"})}
# This depth names a module in a package, such as `jri.core.ai.tool`, which the roles beside it import. It does
# not name a module below that one, such as `jri.core.ai.roles.interviewer`, which stays inside its package.
MAX_IMPORT_DEPTH = 4
# Another project can reuse this package. Its modules declare their exports, and they do not make every name
# reachable.
PUBLIC_API_PACKAGE = "lib"
TEST_SUPPORT_MODULES = frozenset({"__init__.py", "conftest.py"})
# `uv version` reads pyproject.toml and the lockfile. This list names every other file that contains the
# version and the line that contains it. A release has one list to update, and this check has one list to check.
VERSION_COPIES = {Path("src/jri/__init__.py"): '__version__ = "{version}"'}


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--contracts", action="store_true")
    arguments = parser.parse_args()
    check_project(Path(__file__).parent.parent, contracts=arguments.contracts)


def check_project(root: Path, *, contracts: bool) -> None:
    source = root / "src"
    package = source / "jri"
    tests = root / "tests"
    scripts = root / "scripts"
    check_version(root)
    check_member_order(source, tests)
    check_constant_publicity(source, tests)
    check_layering(package, tests)
    check_public_api(package)
    check_import_depth(source, tests)
    check_test_layout(package, tests)
    check_error_wording(tests)
    check_expected_values(tests)
    check_black_box(tests)
    check_deferred_annotations(source, tests)
    check_docstrings(source, tests, scripts)
    check_prompt_style(package / "core" / "ai" / "prompts")
    run_uv_commands(root, contracts=contracts)


def read_version(root: Path) -> str:
    # This repository uses UTF-8. A machine with another default encoding reads a source file with an emoji as
    # different text.
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def check_version(root: Path) -> None:
    version = read_version(root)
    stale = [
        str(root / path)
        for path, spelling in VERSION_COPIES.items()
        if spelling.format(version=version) not in (root / path).read_text(encoding="utf-8")
    ]
    if stale:
        raise RuntimeError(f"Every copy of the version must be {version}, as pyproject.toml says:\n" + "\n".join(stale))


def check_member_order(*roots: Path) -> None:
    disorder = [
        line
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for line in _find_disorder(
            ast.parse(path.read_text(encoding="utf-8")).body, _rank_module, MODULE_GROUPS, path, "module"
        )
    ]
    if disorder:
        raise RuntimeError(
            f"A module orders its members {', '.join(MODULE_GROUPS)}. A class orders its members "
            f"{', '.join(CLASS_GROUPS)}.\n" + "\n".join(disorder)
        )


def check_constant_publicity(*roots: Path) -> None:
    private = [
        line
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for line in _find_private_constants(ast.parse(path.read_text(encoding="utf-8")).body, path)
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


# This names the test module of every module that a test must cover. `scripts/mutate.py` reads the same answer,
# so a mutant runs the tests that really import its module. Two separate rules would become different with
# time. The second rule would then report a hole in a test file that never imports the module of that hole.
def assign_test_modules(package: Path) -> dict[Path, str | None]:
    modules = [
        path
        for path in package.rglob("*.py")
        if path.name != "__init__.py"
        and path.relative_to(package).parts[0] != "tui"
        and any(isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) for node in _walk(path))
    ]
    # Give a module the nearest name that no other module took. The shallowest module keeps the plain name.
    # Two modules that share a stem ask for two files, and one test cannot report both as covered.
    claimed: set[str] = set()
    assigned: dict[Path, str | None] = {}
    for path in sorted(modules, key=lambda module: (len(module.relative_to(package).parts), module)):
        name = next(
            (candidate for candidate in _name_test_modules(path.relative_to(package)) if candidate not in claimed), None
        )
        if name is not None:
            claimed.add(name)
        assigned[path] = name
    return assigned


def check_test_layout(package: Path, tests: Path) -> None:
    misplaced = [
        f"{path}: helpers belong under {tests.name}/doubles/"
        for path in sorted(tests.glob("*.py"))
        if not path.name.startswith("test_") and path.name not in TEST_SUPPORT_MODULES
    ]
    untested = [
        f"{path}: another module took every name this one can take"
        if name is None
        else f"{path}: no {tests.name}/{name}"
        for path, name in assign_test_modules(package).items()
        if name is None or not (tests / name).exists()
    ]
    if misplaced or untested:
        raise RuntimeError("Tests laid out wrongly:\n" + "\n".join(misplaced + untested))


# A bare `pytest.raises(SomeError)` accepts every message. Two tests here used one, and each hid a hole. The
# first let the most common model failure ship with wording that no test held. The second passed although the
# code spent a one-time credential before it raised. The wording of an error is a result, and a test names it.
def check_error_wording(*roots: Path) -> None:
    loose = [
        f"{path}:{node.lineno}: pytest.raises names no wording"
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for node in _walk(path)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "raises"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and not any(keyword.arg == "match" for keyword in node.keywords)
    ]
    if loose:
        raise RuntimeError("Every expected error must name the wording it expects, with `match=`:\n" + "\n".join(loose))


# A test that imports the value it expects compares the module with itself. Such a test cannot fail. One of them
# passed after the shipped notice became a single character, and 1008 other tests passed with it.
def check_expected_values(*roots: Path) -> None:
    borrowed = [
        line for root in roots for path in sorted(root.rglob("*.py")) for line in _find_borrowed_expectations(path)
    ]
    if borrowed:
        raise RuntimeError(
            "A test writes out the value it expects. It does not import it from the code it checks:\n"
            + "\n".join(borrowed)
        )


# A test asserts the result, and not the way the code reached the result. The prompt in `inputs` is a result,
# so a test reads it. The other options of the request are not results, and the order of the calls is not one
# either. A test that reads them fails after a rewrite that keeps the result. It also misses a change of the
# result that keeps the request.
def check_black_box(*roots: Path) -> None:
    peeking = [line for root in roots for path in sorted(root.rglob("*.py")) for line in _find_request_reads(path)]
    if peeking:
        raise RuntimeError("A test asserts the result, and not the request that carried it:\n" + "\n".join(peeking))


def check_deferred_annotations(*roots: Path) -> None:
    deferred = [
        f"{path}:{node.lineno}: annotations deferred"
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for node in _walk(path)
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


# A model reads an indented line as a code block and a wide gap as a column. Both show a meaning that the
# prompt does not have. This is authoring style. A check reports it where a formatter would, and not a
# test.
def check_prompt_style(prompts: Path) -> None:
    stray = [
        f"{path}:{number}: {line!r}"
        for path in sorted(prompts.glob("*.md"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if line.startswith(" ") or "  " in line or line != line.rstrip()
    ]
    if stray:
        raise RuntimeError(
            "A prompt line starts at the margin and separates its words with one space:\n" + "\n".join(stray)
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


def _find_borrowed_expectations(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Only a name that the module under test gave this file can make the expectation follow the module.
    # A constant that the test file writes itself is the written-out value that this rule asks for.
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("jri.")
        for alias in node.names
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for comparison in ast.walk(node.test):
            if not isinstance(comparison, ast.Compare) or [type(op) for op in comparison.ops] != [ast.Eq]:
                continue
            for side in (comparison.left, *comparison.comparators):
                if _name_root(side) in imported:
                    yield f"{path}:{side.lineno}: {ast.unparse(side)} comes from the code under test"


def _find_request_reads(path: Path) -> Iterator[str]:
    for node in _walk(path):
        match node:
            case ast.Attribute(attr="options", value=ast.Attribute(attr="responses")):
                yield f"{path}:{node.lineno}: {ast.unparse(node)} holds the request that was posted"
            case ast.Subscript(value=ast.Attribute(attr="calls")):
                yield f"{path}:{node.lineno}: {ast.unparse(node)} holds the order of the calls"
            case _:
                continue


def _find_docstrings(path: Path) -> Iterator[int]:
    for node in _walk(path):
        if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            yield first.lineno


def _name_test_modules(relative: Path) -> Iterator[str]:
    # When a stem conflicts, prefix it with the enclosing subpackages. Use the nearest prefix first. Continue
    # until no other module claims the name.
    packages = relative.parts[:-1]
    # A leading underscore keeps a module inside its package. A test covers the same behavior from outside,
    # so it takes the name without the mark.
    stem = relative.stem.lstrip("_")
    for start in range(len(packages), -1, -1):
        yield f"test_{'_'.join([*packages[start:], stem])}.py"


# A bare name and a chain of attributes on one, such as `notice` or `logs.NOTICE`, name the value at their end.
# A subscript or a call at the root makes a new value. The expectation is then not the imported value.
def _name_root(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


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
    for node in _walk(path):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module, node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno


def _walk(path: Path) -> Iterator[ast.AST]:
    yield from ast.walk(ast.parse(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
