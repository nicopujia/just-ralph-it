from importlib.resources import files
from pathlib import Path

AGENT_FILENAMES = (
    "interrogator.md",
    "interrogator-validator.md",
    "ralph.md",
    "ralph-validator.md",
)

TOOL_FILENAMES = (
    "_run-python-tool.mjs",
    "check-draft-promotion.js",
    "delete-task.js",
    "list-tasks.js",
    "promote-tasks.js",
    "ralph-result.js",
    "read-tasks.js",
    "rename-task.js",
    "upsert-task.js",
)


def load_config_text() -> str:
    return (
        files("jri.core.opencode").joinpath("config.json").read_text(encoding="utf-8")
    )


def load_agent_text(name: str) -> str:
    return (
        files("jri.core.opencode").joinpath("agents", name).read_text(encoding="utf-8")
    )


def load_tool_text(name: str) -> str:
    return (
        files("jri.core.opencode").joinpath("tools", name).read_text(encoding="utf-8")
    )


def load_asset_text(name: str) -> str:
    return (
        files("jri.core.opencode")
        .joinpath(*Path(name).parts)
        .read_text(encoding="utf-8")
    )
