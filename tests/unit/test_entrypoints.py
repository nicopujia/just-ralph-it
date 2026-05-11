import importlib
import runpy
import sys

import pytest


def test_jri_package_entrypoint_exits_with_cli_main_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jri.cli as cli_package

    monkeypatch.setattr(cli_package, "main", lambda: 7)

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("jri", run_name="__main__")

    assert exc_info.value.code == 7


def test_tools_package_entrypoint_exits_with_shared_main_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jri.core.agents.bundle._shared import tools as tools_package

    monkeypatch.setattr(tools_package, "main", lambda: 9)

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("jri.core.agents.bundle._shared.tools", run_name="__main__")

    assert exc_info.value.code == 9


def test_tools_package_main_module_imports_without_running_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(
        sys.modules, "jri.core.agents.bundle._shared.tools.__main__", raising=False
    )

    module = importlib.import_module("jri.core.agents.bundle._shared.tools.__main__")

    assert hasattr(module, "main")
