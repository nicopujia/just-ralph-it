import subprocess
from pathlib import Path

from tests.helpers import write_live_makefile


def test_live_makefile_passes_without_tests_and_runs_pytest(git_repo: Path) -> None:
    write_live_makefile(git_repo)

    empty_check = subprocess.run(
        ["make", "check"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert empty_check.returncode == 0

    src_dir = git_repo / "src"
    src_dir.mkdir()
    (src_dir / "greet.py").write_text(
        'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n',
        encoding="utf-8",
    )
    tests_dir = git_repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_greet.py").write_text(
        "from greet import greet\n\n"
        "\n"
        "def test_greet() -> None:\n"
        '    assert greet("world") == "nope"\n',
        encoding="utf-8",
    )

    failing_check = subprocess.run(
        ["make", "check"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert failing_check.returncode != 0
    assert "FAILED" in failing_check.stdout
