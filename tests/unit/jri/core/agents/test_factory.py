"""Tests for interviewer construction."""

from pathlib import Path

import pytest

from jri.core.agents.factory import (
    INTERVIEWER_FACTORY_ENV,
    create_interviewer,
    validate_interviewer_configuration,
)
from jri.core.agents.interviewer import Interviewer
from jri.core.config import ConfigError
from jri.core.logging import JsonlLogger


def test_factory_creates_interviewer_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory uses the live interviewer unless a test factory is supplied."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    interviewer = create_interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        env={
            "OPENROUTER_API_KEY": "fake",
            "JRI_INTERVIEWER_MODEL_ID": "test",
            "JRI_EXPLORER_MODEL_ID": "test",
        },
    )

    assert isinstance(interviewer, Interviewer)


def test_factory_uses_custom_interviewer_factory_without_credentials(
    tmp_path: Path,
) -> None:
    """Subprocess tests can inject a double without source fake classes."""
    env = {
        INTERVIEWER_FACTORY_ENV: (
            "tests.doubles.interviewers:create_scripted_interviewer"
        )
    }

    validate_interviewer_configuration(env)
    interviewer = create_interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        env=env,
    )

    assert not interviewer.should_exit


def test_factory_rejects_invalid_custom_factory(tmp_path: Path) -> None:
    """Invalid factory paths fail as configuration errors."""
    with pytest.raises(ConfigError, match=INTERVIEWER_FACTORY_ENV):
        create_interviewer(
            project_root=tmp_path,
            logger=JsonlLogger(tmp_path / "events.jsonl"),
            env={INTERVIEWER_FACTORY_ENV: "tests.doubles.interviewers"},
        )


def test_factory_rejects_non_callable_custom_factory(tmp_path: Path) -> None:
    """Factory paths must point to callables."""
    with pytest.raises(ConfigError, match="callable"):
        create_interviewer(
            project_root=tmp_path,
            logger=JsonlLogger(tmp_path / "events.jsonl"),
            env={
                INTERVIEWER_FACTORY_ENV: "tests.doubles.interviewers:__doc__"
            },
        )


def test_validate_interviewer_configuration_requires_credentials() -> None:
    """Live provider configuration requires selected-provider credentials."""
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        validate_interviewer_configuration({})
