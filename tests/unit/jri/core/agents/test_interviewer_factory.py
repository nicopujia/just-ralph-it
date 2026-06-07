"""Tests for interviewer construction."""

import json
from pathlib import Path
from typing import cast

import pytest

from jri.core.agents.interviewer import (
    INTERVIEWER_FACTORY_ENV,
    Interviewer,
    create_interviewer,
    validate_interviewer_configuration,
)
from jri.core.config import ConfigError
from jri.core.logging import JsonlLogger


def test_create_interviewer_uses_live_interviewer_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live sessions use the interviewer unless a test factory is supplied."""
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


def test_create_interviewer_logs_live_model_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live sessions record the resolved model backend."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    log_path = tmp_path / "events.jsonl"

    _ = create_interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(log_path),
        env={
            "OPENROUTER_API_KEY": "fake",
            "JRI_INTERVIEWER_MODEL_ID": "interviewer-test",
            "JRI_EXPLORER_MODEL_ID": "explorer-test",
        },
    )

    events = [
        cast("dict[str, object]", json.loads(line))
        for line in log_path.read_text().splitlines()
    ]
    config = cast("dict[str, str]", events[-1]["data"])
    assert events[-1]["type"] == "session_config"
    assert config["model_provider"] == "openrouter"
    assert config["interviewer_model"] == "openrouter:interviewer-test"
    assert config["explorer_model"] == "openrouter:explorer-test"


def test_create_interviewer_uses_custom_factory_without_credentials(
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


def test_create_interviewer_rejects_invalid_custom_factory(
    tmp_path: Path,
) -> None:
    """Invalid factory paths fail as configuration errors."""
    with pytest.raises(ConfigError, match=INTERVIEWER_FACTORY_ENV):
        create_interviewer(
            project_root=tmp_path,
            logger=JsonlLogger(tmp_path / "events.jsonl"),
            env={INTERVIEWER_FACTORY_ENV: "tests.doubles.interviewers"},
        )


def test_create_interviewer_rejects_missing_custom_factory_module(
    tmp_path: Path,
) -> None:
    """Factory paths must point to importable modules."""
    with pytest.raises(ConfigError, match="could not import module"):
        create_interviewer(
            project_root=tmp_path,
            logger=JsonlLogger(tmp_path / "events.jsonl"),
            env={
                INTERVIEWER_FACTORY_ENV: (
                    "tests.doubles.missing_interviewers:create_interviewer"
                )
            },
        )


def test_create_interviewer_rejects_non_callable_custom_factory(
    tmp_path: Path,
) -> None:
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
