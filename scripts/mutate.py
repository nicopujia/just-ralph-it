#!/usr/bin/env -S uv run --script

import ast
import json
import os
import re
import shutil
import subprocess
import time
from argparse import ArgumentParser
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from functools import partial
from pathlib import Path
from queue import SimpleQueue
from tempfile import TemporaryDirectory
from typing import NamedTuple

import check

# A mutant is one small wrong version of a line the change touched. The tests of that file must fail on it.
# A mutant that lives says the tests run the line but read nothing it produced. Coverage cannot find this:
# it counts the lines a test runs, not the values a test looks at. This gate is a tripwire on the lines a
# change touched, and not a proof. It writes a few kinds of wrong line, and a suite that kills them all can
# still miss another kind.
# A mutant gets one of three answers, and they mean different things. `killed` says a test read the value the
# line makes. `SURVIVED` says a test ran the line and read nothing it makes, which is a hole in an assertion.
# `UNREACHED` says no test ran the line at all, which is a hole in the coverage. Only a survivor fails the
# gate: a line that no test reaches has no assertion to be missing, and to call it a fault would make a
# branch that only one operating system takes into a permanent failure.
DEFAULT_BASE_REVISION = "main"
# One mutant runs one test module. The workers run a core's worth of them at a time, so this budget is a few
# rounds and keeps a pull request under a few minutes. The report names the mutants it left out, because
# silence would read as a clean run.
DEFAULT_BUDGET = 30
SOURCE_DIR = "src"
PACKAGE = "jri"
TESTS_DIR = "tests"
DIFF_COMMAND = ("diff", "--unified=0")
# `-x` stops at the first failure, because one failure already kills the mutant. `no:cacheprovider` keeps the
# run from writing a cache into the repository.
PYTEST_COMMAND = ("run", "--locked", "pytest", "-x", "-q", "-p", "no:cacheprovider")
PYTEST_PASSED = 0
PYTEST_FAILED = 1
# A mutant that breaks an import stops the collection, and pytest answers 2. The tests did not pass, so the
# mutant is dead. Every other answer, such as no test at all or a wrong option, says the gate is broken. A
# dead mutant there would read as a clean run.
PYTEST_INTERRUPTED = 2
# A mutant can spin instead of answering: a negated `while` runs a loop the code left. Wait this multiple of
# the slowest unmutated run, and no longer. `-x` stops the tests at the first failure, so only a survivor runs
# its module to the end, and this leaves room for one under the load the other workers add. A run that reaches
# the limit hangs, which is a fault the tests found, so the mutant dies.
TIMEOUT_FACTOR = 5
# The slowest module of a small change takes a few seconds. Hold this floor under the limit, so that the noise
# of one quick run does not end the next one early and report a mutant that no test killed as dead.
TIMEOUT_FLOOR = 60
COVERAGE_DATA_FILE = ".coverage"
COVERAGE_REPORT_FILE = "coverage.json"
FILE_HEADER = "+++ b/"
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
COMPARISON_FLIPS = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}
# A narrower clause lets through every failure the wider one caught. A test that reaches the handler fails.
EXCEPTION_NARROWINGS = {"BaseException": "Exception", "Exception": "RuntimeError", "OSError": "FileNotFoundError"}
# The first argument of each of these repeats the name that takes the answer. It reaches no behavior.
TYPE_DECLARERS = frozenset({"NewType", "ParamSpec", "TypeVar", "TypeVarTuple", "cast"})
CLOCK_CALLS = frozenset({"monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "time"})
SLEEP_CALLS = frozenset({"sleep"})
MAX_QUOTED_CHARACTERS = 90


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("base", nargs="?", default=DEFAULT_BASE_REVISION)
    parser.add_argument("--max", type=int, default=DEFAULT_BUDGET)
    arguments = parser.parse_args()
    check_change(Path(__file__).parent.parent, arguments.base, budget=arguments.max)


def check_change(root: Path, base: str, *, budget: int) -> None:
    git = shutil.which("git")
    uv = shutil.which("uv")
    if not git or not uv:
        raise RuntimeError("git and uv must be installed")
    changed = _read_changed_lines(git, root, base)
    targets = _assign_targets(root, changed)
    unmeasured = sorted(str(path.relative_to(root)) for path in changed if path not in targets)
    mutants = sorted(
        (mutant for path, lines in changed.items() if path in targets for mutant in _find_mutants(path, lines)),
        key=lambda mutant: (str(mutant.path), mutant.line),
    )
    print(f"{base}...HEAD changes {len(changed)} file(s) under {SOURCE_DIR}/, which take {len(mutants)} mutant(s).")
    with TemporaryDirectory() as directory:
        count = max(min(os.cpu_count() or 1, len(mutants)), 1)
        with ThreadPoolExecutor(max_workers=count) as pool:
            workers = Workers(pool, _open_workspaces(root, Path(directory), count))
            measurements = _measure_targets(uv, root, workers, sorted(set(targets.values())))
            unreached, runnable = _sort_by_reach(root, targets, mutants, _find_unrun_lines(targets, measurements))
            # A mutant on a line no test runs needs no test run to answer it, so it costs nothing and spends
            # none of the budget. The budget counts the mutants that the tests must answer.
            over = len(runnable) - budget
            runnable = runnable[:budget]
            survivors = _run_mutants(uv, root, workers, targets, measurements, runnable)
    if over > 0:
        print(f"{over} mutant(s) over the budget of {budget} did not run. `--max` raises it.")
    if unmeasured:
        print(f"No mutant ran on these files, because no test module covers them: {', '.join(unmeasured)}.")
    if unreached:
        print(
            "No test reached these lines, so the gate cannot say whether an assertion reads them. This is a "
            "hole in the coverage, and it does not fail the gate:\n" + "\n".join(unreached)
        )
    if survivors:
        raise SystemExit(
            "Your tests do not guard the lines you changed. Each mutant below is a wrong version of a line, "
            "the tests of that file ran that line, and they passed on it:\n"
            + "\n".join(survivors)
            + "\nAssert the value each line produces, or say why a mutant of it makes no difference."
        )
    if not mutants:
        print("No line this change added holds a value the gate knows how to write wrong.")
        return
    # A run that measured nothing is not a run that found nothing. Say which one it was, or a reader takes an
    # unmeasured change for a guarded one.
    if not runnable:
        print("No mutant ran, so this change is unmeasured. The tests reach none of the lines it wrote.")
        return
    print(f"Every mutant died: the tests answer each of the {len(runnable)} wrong lines the gate ran.")


class Measurement(NamedTuple):
    # Coverage names each source file it read, and gives each one the lines the tests ran, missed and excluded.
    files: dict[Path, dict[str, list[int]]]
    # How long the module took unmutated. A mutant of it gets no longer than a multiple of this.
    seconds: float


class Mutant(NamedTuple):
    path: Path
    line: int
    before: str
    after: str
    text: str


class Workers(NamedTuple):
    pool: ThreadPoolExecutor
    # One copy of the source for each worker. A worker takes a copy while it runs a module and gives it back
    # after, so the mutant one worker writes is never the file another worker reads.
    free: SimpleQueue[Path]


# `check.py` gives one test module to one source module, and refuses a change that leaves one without a test
# module. Read that answer, and do not name the file here. A rule of its own would take the first name that
# exists, which gives `tests/test_repository.py` to `core/ai/prompts/_repository.py`. That file never imports
# the module, so every mutant of it would live, and the report would name tests that never saw the line.
def _assign_targets(root: Path, changed: dict[Path, frozenset[int]]) -> dict[Path, Path]:
    assigned = check.assign_test_modules(root / SOURCE_DIR / PACKAGE)
    tests = root / TESTS_DIR
    return {
        path: tests / name for path in changed if (name := assigned.get(path)) is not None and (tests / name).exists()
    }


# The tests import a copy, because PYTHONPATH comes before the path the virtual environment holds. The gate thus
# never opens the file the developer has. A crash, a failure or a Ctrl-C leaves the working tree as it was,
# because nothing in the run can write to it.
# Each worker holds a copy of its own, so that the mutant one worker writes is not the file another worker reads.
# A test waits for a subprocess, a Git command, or a file much longer than it calculates, so a worker for each
# core reads that wait as free time and fills it, as the suite already does under `-n auto`. Keep to that number:
# more workers only add load, which makes a test that waits for a deadline miss it and report a mutant that no
# test killed as dead.
def _open_workspaces(root: Path, directory: Path, workers: int) -> SimpleQueue[Path]:
    free = SimpleQueue[Path]()
    for number in range(workers):
        # Coverage names each file it read by the path with no link left in it. macOS gives out a temporary
        # directory under `/var`, which is a link to `/private/var`, so take the resolved path here. The names
        # the report gives back then match the names this run asks it for.
        workspace = directory.resolve() / str(number) / SOURCE_DIR
        shutil.copytree(root / SOURCE_DIR, workspace, ignore=shutil.ignore_patterns("__pycache__"))
        free.put(workspace)
    return free


def _sort_by_reach(
    root: Path, targets: dict[Path, Path], mutants: list[Mutant], unrun: dict[Path, frozenset[int]]
) -> tuple[list[str], list[Mutant]]:
    unreached: list[str] = []
    runnable: list[Mutant] = []
    for mutant in mutants:
        if mutant.line not in unrun[mutant.path]:
            runnable.append(mutant)
            continue
        report = _describe(root, mutant)
        print(f"{'UNREACHED':<9} {report}")
        unreached.append(f"{report}\n    {targets[mutant.path].relative_to(root)} never ran the line")
    return unreached, runnable


def _run_mutants(
    uv: str,
    root: Path,
    workers: Workers,
    targets: dict[Path, Path],
    measurements: dict[Path, Measurement],
    runnable: list[Mutant],
) -> list[str]:
    survivors: list[str] = []
    limit = max(TIMEOUT_FLOOR, TIMEOUT_FACTOR * max((run.seconds for run in measurements.values()), default=0))
    # `map` gives the answers back in the order it took the mutants, so the report reads in that order while
    # every worker runs.
    answers = workers.pool.map(partial(_run_mutant, uv, root, workers, targets, limit), runnable)
    for mutant, answer in zip(runnable, answers, strict=True):
        report = _describe(root, mutant)
        if answer == PYTEST_PASSED:
            print(f"{'SURVIVED':<9} {report}")
            survivors.append(f"{report}\n    {targets[mutant.path].relative_to(root)} ran the line and passed")
        else:
            print(f"{'killed':<9} {report}")
    return survivors


# Run each test module once against a copy as it stands, before any mutant. A module that fails for its own
# reason fails again under every mutant, and the gate would read each failure as a kill and report a clean run.
# The same run says which lines the tests reach and how long the module takes.
def _measure_targets(uv: str, root: Path, workers: Workers, modules: list[Path]) -> dict[Path, Measurement]:
    answers = workers.pool.map(partial(_measure_target, uv, root, workers), modules)
    return dict(zip(modules, answers, strict=True))


def _measure_target(uv: str, root: Path, workers: Workers, target: Path) -> Measurement:
    with _borrow(workers) as workspace:
        report = workspace.parent / COVERAGE_REPORT_FILE
        started = time.monotonic()
        result = subprocess.run(
            [uv, *PYTEST_COMMAND, str(target), f"--cov={workspace / PACKAGE}", f"--cov-report=json:{report}"],
            cwd=root,
            env=_environment(workspace),
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        seconds = time.monotonic() - started
        if result.returncode != PYTEST_PASSED:
            raise RuntimeError(
                f"{target.relative_to(root)} does not pass unmutated, so no mutant of it can be measured:\n"
                f"{result.stdout}{result.stderr}"
            )
        # Name each file by the path the developer has, and not by the path of the copy that measured it, so
        # that a reader of the answer does not need to know which worker took the module.
        files = json.loads(report.read_text(encoding="utf-8"))["files"]
        return Measurement(
            {root / SOURCE_DIR / Path(name).relative_to(workspace): lines for name, lines in files.items()}, seconds
        )


# A mutant on a line no test runs cannot be a hole in an assertion, because there is no assertion to be missing.
def _find_unrun_lines(targets: dict[Path, Path], measurements: dict[Path, Measurement]) -> dict[Path, frozenset[int]]:
    unrun: dict[Path, frozenset[int]] = {}
    for path, target in targets.items():
        lines = measurements[target].files.get(path, {})
        ran = frozenset(lines.get("executed_lines", ()))
        # Coverage counts a statement at the line it starts on. Give each statement the lines up to the next
        # one, so that a mutant inside a statement that spans lines belongs to that statement. A file the
        # report does not name at all starts at its first line, which no test ran, so every line is unrun.
        starts = sorted(ran | set(lines.get("missing_lines", ())) | set(lines.get("excluded_lines", ()))) or [1]
        ends = [*starts[1:], len(path.read_text(encoding="utf-8").splitlines()) + 1]
        unrun[path] = frozenset(
            number for start, end in zip(starts, ends, strict=True) if start not in ran for number in range(start, end)
        )
    return unrun


def _run_mutant(uv: str, root: Path, workers: Workers, targets: dict[Path, Path], limit: float, mutant: Mutant) -> int:
    target = targets[mutant.path]
    with _borrow(workers) as workspace:
        mutated = workspace / mutant.path.relative_to(root / SOURCE_DIR)
        original = mutated.read_text(encoding="utf-8")
        mutated.write_text(mutant.text, encoding="utf-8")
        try:
            result = subprocess.run(
                [uv, *PYTEST_COMMAND, str(target)],
                cwd=root,
                env=_environment(workspace),
                capture_output=True,
                encoding="utf-8",
                check=False,
                timeout=limit,
            )
        # A mutant that never returns is one the tests hang on, which is a failure they found.
        except subprocess.TimeoutExpired:
            return PYTEST_FAILED
        # Return the copy whatever the answer was, because the next mutant of this worker reads the same file.
        finally:
            mutated.write_text(original, encoding="utf-8")
    if result.returncode not in {PYTEST_PASSED, PYTEST_FAILED, PYTEST_INTERRUPTED}:
        raise RuntimeError(f"{target.relative_to(root)} did not run:\n{result.stdout}{result.stderr}")
    return result.returncode


# A copy in the queue is a copy that holds the source as the developer wrote it.
@contextmanager
def _borrow(workers: Workers) -> Iterator[Path]:
    workspace = workers.free.get()
    try:
        yield workspace
    finally:
        workers.free.put(workspace)


# Python names a cached module after the second its source changed. Two mutants of one file, of one size, in one
# second would run the first mutant twice. Let the run keep no cache. Keep the coverage data beside the copy
# too, so the run writes nothing at all into the repository and no two workers write one file.
def _environment(workspace: Path) -> dict[str, str]:
    return os.environ | {
        "PYTHONPATH": str(workspace),
        "PYTHONDONTWRITEBYTECODE": "1",
        "COVERAGE_FILE": str(workspace.parent / COVERAGE_DATA_FILE),
    }


def _describe(root: Path, mutant: Mutant) -> str:
    return f"{mutant.path.relative_to(root)}:{mutant.line}: {_quote(mutant.before)} -> {_quote(mutant.after)}"


def _read_changed_lines(git: str, root: Path, base: str) -> dict[Path, frozenset[int]]:
    # A hunk header of a diff with no context names the lines the change added. A line the change only
    # removed leaves a hunk of length zero, which holds no line to mutate.
    # Let Git say what it cannot read, such as a base revision this repository does not hold.
    diff = subprocess.run(
        [git, *DIFF_COMMAND, f"{base}...HEAD", "--", SOURCE_DIR],
        cwd=root,
        stdout=subprocess.PIPE,
        encoding="utf-8",
        check=True,
    ).stdout
    changed: dict[Path, set[int]] = {}
    path: Path | None = None
    for line in diff.splitlines():
        if line.startswith(FILE_HEADER):
            name = line.removeprefix(FILE_HEADER)
            path = root / name if name.endswith(".py") else None
        elif path and (header := HUNK_HEADER.match(line)):
            start, count = int(header[1]), int(header[2] or 1)
            changed.setdefault(path, set()).update(range(start, start + count))
    return {path: frozenset(lines) for path, lines in changed.items() if path.exists() and lines}


def _find_mutants(path: Path, changed: frozenset[int]) -> Iterator[Mutant]:
    text = path.read_text(encoding="utf-8")
    # A column of the tree counts bytes, so cut the bytes and read the text back afterwards.
    data = text.encode("utf-8")
    starts = [0]
    for line in data.splitlines(keepends=True)[:-1]:
        starts.append(starts[-1] + len(line))
    tree = ast.parse(text)
    # No test reads these nodes for what they hold. An annotation and the name a `cast` or a `TypeVar` takes
    # carry no value at run time. A dunder name, such as `__all__`, states what the module is and not what it
    # does: a test that imports the module already names every export it uses. A piece of an f-string is text
    # inside text, which reads as neither in a report: the gate writes the whole f-string wrong instead.
    # The wording of an error stays in, because a test names it and `check.py` makes it name it.
    unread: list[ast.AST] = _find_durations(tree)
    for node in ast.walk(tree):
        match node:
            case (
                ast.Assign(targets=[ast.Name(id=name)], value=ast.expr() as unreadable)
                | ast.AnnAssign(target=ast.Name(id=name), value=ast.expr() as unreadable)
            ) if name.startswith("__") and name.endswith("__"):
                unread.append(unreadable)
            case (
                ast.AnnAssign(annotation=ast.expr() as unreadable)
                | ast.arg(annotation=ast.expr() as unreadable)
                | ast.TypeAlias(value=ast.expr() as unreadable)
                | ast.FunctionDef(returns=ast.expr() as unreadable)
                | ast.AsyncFunctionDef(returns=ast.expr() as unreadable)
            ):
                unread.append(unreadable)
            case ast.Call(args=[ast.expr() as unreadable, *_]) if _name(node.func) in TYPE_DECLARERS:
                unread.append(unreadable)
            case ast.JoinedStr(values=values):
                unread.extend(values)
            case _:
                continue
    hidden = {id(inner) for unreadable in unread for inner in ast.walk(unreadable)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt | ast.expr | ast.ExceptHandler) or node.lineno not in changed:
            continue
        if id(node) in hidden:
            continue
        for target, after in _propose_changes(node):
            if id(target) in hidden or target.end_lineno is None or target.end_col_offset is None:
                continue
            start = starts[target.lineno - 1] + target.col_offset
            end = starts[target.end_lineno - 1] + target.end_col_offset
            mutated = (data[:start] + after.encode("utf-8") + data[end:]).decode("utf-8")
            try:
                ast.parse(mutated)
            except SyntaxError:
                continue
            yield Mutant(path, node.lineno, data[start:end].decode("utf-8"), after, mutated)


# A poll interval and a deadline are lengths of time. No test this repository accepts can read one. To wait for
# it makes the test slow and unsteady, and to read the call that received it asserts the way and not the
# result, which `tests/AGENTS.md` refuses and `check_black_box` finds. `POLL = 0.05 -> 0` and `>= -> >` on a
# clock therefore live whatever the tests say, and a gate that reports an alarm nobody can answer gets turned
# off. Leave them out. A length of time that is wrong appears as a wait, and not as a wrong answer.
def _find_durations(tree: ast.Module) -> list[ast.AST]:
    timed: list[ast.AST] = []
    for node in ast.walk(tree):
        match node:
            # A comparison against a clock only says when the wait ends. Leave the other sides of the chain
            # that holds it: `signalled is None` beside it says whom to signal, which a test can read.
            case ast.Compare() if _reads_clock(node):
                timed.append(node)
            # A wait that goes away turns a poll into a busy loop, which answers the same.
            case ast.Expr(value=ast.Call() as call) if _name(call.func) in SLEEP_CALLS:
                timed.append(node)
            case ast.Call(args=arguments) if _name(node.func) in SLEEP_CALLS:
                timed.extend(arguments)
            case _:
                continue
    # A constant reaches a clock through the names that carry it. Follow each name back through the
    # assignments that fill it, until no more names come in, and leave the numbers those names hold.
    carried = {name for expression in timed for inner in ast.walk(expression) if (name := _name(inner))}
    assignments = [
        (_name(node.targets[0] if isinstance(node, ast.Assign) else node.target), node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None
    ]
    while True:
        widened = carried | {
            name
            for target, value in assignments
            if target in carried and value is not None
            for inner in ast.walk(value)
            if (name := _name(inner))
        }
        if widened == carried:
            break
        carried = widened
    return timed + [
        number
        for target, value in assignments
        if target in carried and value is not None
        for number in ast.walk(value)
        if isinstance(number, ast.Constant) and isinstance(number.value, int | float)
    ]


def _propose_changes(node: ast.stmt | ast.expr | ast.ExceptHandler) -> Iterator[tuple[ast.stmt | ast.expr, str]]:
    match node:
        case ast.Compare(ops=[operator]) if type(operator) in COMPARISON_FLIPS:
            flipped = deepcopy(node)
            flipped.ops = [COMPARISON_FLIPS[type(operator)]()]
            yield node, ast.unparse(flipped)
        case ast.BoolOp(op=operator, values=values):
            swapped = deepcopy(node)
            swapped.op = ast.Or() if isinstance(operator, ast.And) else ast.And()
            yield node, ast.unparse(swapped)
            # A chain that answers the same without one of its sides has a side no test reads. A side that
            # reads a clock is not one of them: it only holds the chain back until the wait ends.
            for index, dropped in enumerate(values):
                if _reads_clock(dropped):
                    continue
                kept = [value for position, value in enumerate(values) if position != index]
                yield node, ast.unparse(kept[0] if len(kept) == 1 else ast.BoolOp(op=operator, values=kept))
        case ast.Constant(value=bool() as value):
            yield node, str(not value)
        case ast.Constant(value=int() | float() as value):
            yield node, "1" if value == 0 else "0"
        case ast.Constant(value=str() as value) if value:
            yield node, '""'
        case ast.JoinedStr():
            yield node, '""'
        case ast.Expr(value=ast.Call() | ast.Await(value=ast.Call())):
            # A call that keeps no answer is there for what it does. Take the whole statement away.
            yield node, "pass"
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            yield node, ast.unparse(operand)
        case ast.If(test=test) | ast.While(test=test) | ast.IfExp(test=test) if not isinstance(
            test, ast.UnaryOp
        ) and not _reads_clock(test):
            yield test, f"not ({ast.unparse(test)})"
        case ast.ExceptHandler(type=ast.Name(id=name) as caught) if name in EXCEPTION_NARROWINGS:
            yield caught, EXCEPTION_NARROWINGS[name]
        case _:
            return


def _reads_clock(node: ast.AST) -> bool:
    return any(isinstance(inner, ast.Call) and _name(inner.func) in CLOCK_CALLS for inner in ast.walk(node))


# A bare name and the last name of a chain of attributes, such as `POLL` or `self.POLL`, both name one value.
def _name(node: ast.AST) -> str | None:
    match node:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
        case _:
            return None


def _quote(source: str) -> str:
    # A statement holds newlines and holds more text than a report line. Give each mutant one line.
    flat = " ".join(source.split())
    return flat if len(flat) <= MAX_QUOTED_CHARACTERS else f"{flat[:MAX_QUOTED_CHARACTERS]}..."


if __name__ == "__main__":
    main()
