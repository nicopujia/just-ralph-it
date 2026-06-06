"""Tests for the validation helper script."""

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent


def test_validate_help_describes_mode_selection() -> None:
    """The validation script exposes help without running validation."""
    repo_root = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        ["./scripts/validate.py", "--help"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert not result.stderr
    assert "Run the project validation workflow." in result.stdout
    assert "--mode" in result.stdout
    assert "fast" in result.stdout
    assert "smoke" in result.stdout
    assert "full" in result.stdout


def test_validate_clears_pytest_logs_immediately_before_tests(
    tmp_path: Path,
) -> None:
    """Validation clears stale pytest logs only when tests are about to run."""
    (tmp_path / ".pytest_logs" / "stale.log").parent.mkdir()
    (tmp_path / ".pytest_logs" / "stale.log").write_text(
        "old run",
        encoding="utf-8",
    )

    result, observations = run_validation_with_fake_uv(tmp_path)

    assert result.returncode == 0
    coverage_erase = "exists|run --locked coverage erase"
    test_suite = "missing|run --locked coverage run -m pytest --quiet"
    assert coverage_erase in observations
    assert observations[observations.index(coverage_erase) + 1] == test_suite
    assert not (tmp_path / ".pytest_logs").exists()


def test_validate_fast_mode_keeps_pytest_logs_when_tests_do_not_run(
    tmp_path: Path,
) -> None:
    """Fast validation does not clear pytest logs because it skips tests."""
    (tmp_path / ".pytest_logs" / "stale.log").parent.mkdir()
    (tmp_path / ".pytest_logs" / "stale.log").write_text(
        "old run",
        encoding="utf-8",
    )

    result, observations = run_validation_with_fake_uv(
        tmp_path, "--mode", "fast"
    )

    assert result.returncode == 0
    assert (tmp_path / ".pytest_logs" / "stale.log").read_text(
        encoding="utf-8",
    ) == "old run"
    assert all("pytest" not in observation for observation in observations)


def run_validation_with_fake_uv(
    tmp_path: Path,
    *args: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run validation with a fake uv that records log-dir state."""
    repo_root = Path(__file__).resolve().parents[2]
    command_log = tmp_path / "uv-commands.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        dedent(
            """\
            #!/usr/bin/env python3
            import os
            import sys
            from pathlib import Path

            logs_dir = Path.cwd() / ".pytest_logs"
            state = "exists" if logs_dir.exists() else "missing"
            command_log = Path(os.environ["VALIDATE_COMMAND_LOG"])
            with command_log.open("a", encoding="utf-8") as log:
                command = " ".join(sys.argv[1:])
                log.write(f"{state}|{command}\\n")
            """
        ),
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', os.defpath)}"
    env["VALIDATE_COMMAND_LOG"] = str(command_log)

    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "validate.py"), *args],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )
    return result, command_log.read_text(encoding="utf-8").splitlines()
