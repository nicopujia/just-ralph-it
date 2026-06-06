"""Interviewer backend selection."""

import importlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from jri.core.agents.interviewer import Interviewer
from jri.core.config import (
    AgentRuntimeConfig,
    ConfigError,
    load_agent_runtime_config,
    validate_agent_runtime_credentials,
)
from jri.core.interview import InterviewSession
from jri.core.logging import JsonlLogger

INTERVIEWER_FACTORY_ENV = "JRI_INTERVIEWER_FACTORY"
type InterviewerFactory = Callable[
    [Path, JsonlLogger],
    InterviewSession,
]


def create_interviewer(
    *,
    project_root: Path,
    logger: JsonlLogger,
    env: Mapping[str, str],
    runtime_config: AgentRuntimeConfig | None = None,
) -> InterviewSession:
    """Create the configured interviewer session."""
    if factory_path := env.get(INTERVIEWER_FACTORY_ENV):
        # Subprocess tests need a deterministic interview outside src.
        # Keep that boundary explicit and narrow.
        factory = _load_interviewer_factory(factory_path)
        return factory(project_root, logger)

    config = runtime_config or load_agent_runtime_config(env)
    validate_agent_runtime_credentials(config, env)
    return Interviewer(
        project_root=project_root,
        logger=logger,
        model_config=config.models,
    )


def validate_interviewer_configuration(env: Mapping[str, str]) -> None:
    """Validate the configured interviewer before project mutation."""
    if env.get(INTERVIEWER_FACTORY_ENV):
        return
    config = load_agent_runtime_config(env)
    validate_agent_runtime_credentials(config, env)


def _load_interviewer_factory(path: str) -> InterviewerFactory:
    module_name, separator, function_name = path.partition(":")
    if not module_name or separator != ":" or not function_name:
        msg = (
            f"{INTERVIEWER_FACTORY_ENV} must be formatted as module:function."
        )
        raise ConfigError(msg)

    module = importlib.import_module(module_name)
    candidate = getattr(module, function_name, None)
    if not callable(candidate):
        msg = f"{INTERVIEWER_FACTORY_ENV} does not point to a callable."
        raise ConfigError(msg)
    return cast("InterviewerFactory", candidate)
