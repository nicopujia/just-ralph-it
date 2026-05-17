from pathlib import Path

import pytest

from jri.core.service import JriService
from tests.conftest import run_cli


def _init(repo: Path) -> JriService:
    assert run_cli(["init"], cwd=repo) == 0
    return JriService(repo)


def test_promote_is_not_a_public_cli_command(git_repo: Path) -> None:
    _init(git_repo)

    with pytest.raises(SystemExit) as exc_info:
        run_cli(["promote", "clarify-scope"], cwd=git_repo)

    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "method_name", ["check" + "_draft" + "_promotion", "approve" + "_draft" + "_promotion", "promote" + "_drafts"]
)
def test_removed_promotion_service_apis_are_not_available(git_repo: Path, method_name: str) -> None:
    service = _init(git_repo)

    assert not hasattr(service, method_name)
