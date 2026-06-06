"""Tests for black-box CLI integration-test boundaries."""

import ast
from pathlib import Path

BLACK_BOX_TEST_FILES = (
    Path("tests/conftest.py"),
    Path("tests/integration/test_cli.py"),
    Path("tests/support/cli.py"),
)

CLI_CONTRACT_TEST_PATH = Path("tests/integration/test_cli.py")
CONFTEXT_PATH = Path("tests/conftest.py")


def test_cli_integration_tests_do_not_import_jri_modules() -> None:
    """CLI integration tests exercise the console script as a black box."""
    for path in BLACK_BOX_TEST_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [
            imported
            for node in ast.walk(tree)
            for imported in _jri_imports(node)
        ]

        assert imports == []


def test_cli_contract_tests_do_not_know_runtime_mode() -> None:
    """Shared CLI scenarios should not branch on provider mode."""
    tree = ast.parse(CLI_CONTRACT_TEST_PATH.read_text(encoding="utf-8"))
    mode_fixtures = [
        arg.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        for arg in node.args.args
        if arg.arg == "live"
    ]
    pytest_imports = [
        imported
        for node in ast.walk(tree)
        for imported in _module_imports(node)
        if imported == "pytest"
    ]

    assert mode_fixtures == []
    assert pytest_imports == []


def test_cli_contract_tests_do_not_configure_doubles_directly() -> None:
    """Factory hooks belong in fixtures, not behavior assertions."""
    text = CLI_CONTRACT_TEST_PATH.read_text(encoding="utf-8")

    assert "INTERVIEWER_FACTORY_ENV" not in text
    assert "tests.doubles" not in text


def test_cli_harness_uses_repo_local_console_script() -> None:
    """Contract tests must not accidentally run a stale global jri."""
    text = CONFTEXT_PATH.read_text(encoding="utf-8")

    assert 'shutil.which("jri")' not in text
    assert ".venv" in text
    assert "bin" in text
    assert "jri" in text


def test_cli_integration_tests_do_not_assert_scripted_phrases() -> None:
    """Exact double wording belongs in unit tests for the double."""
    text = CLI_CONTRACT_TEST_PATH.read_text(encoding="utf-8")
    forbidden = [
        "Missing: target user and success criteria.",
        "High-level question:",
        "Ralph handoff",
    ]

    assert [phrase for phrase in forbidden if phrase in text] == []


def _jri_imports(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [
            alias.name
            for alias in node.names
            if alias.name == "jri" or alias.name.startswith("jri.")
        ]
    if (
        isinstance(node, ast.ImportFrom)
        and node.module
        and (node.module == "jri" or node.module.startswith("jri."))
    ):
        return [node.module]
    return []


def _module_imports(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module]
    return []
