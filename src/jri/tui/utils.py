import platform
import subprocess

from jri.core.exceptions import ConfigurationError
from jri.core.settings import Settings

from .constants import (
    CONFIG_ERROR_COPY,
    THEME_DARK,
    THEME_DEFAULT,
    THEME_LIGHT,
)


def get_config_error_help_message(error: ConfigurationError) -> str:
    error_lines: list[str] = []
    for issue in error.validation_error.errors():
        field_name = str(issue["loc"][0])
        env_prefix = str(Settings.model_config.get("env_prefix", ""))
        field = Settings.model_fields.get(field_name)
        if not field:
            continue
        description = field.description or "<no description available>"
        line = f"- {env_prefix}{field_name.upper()}: {description}"
        error_lines.append(line)
    return CONFIG_ERROR_COPY % "\n".join(error_lines)


def detect_system_theme() -> str:
    if platform.system() != "Darwin":
        return THEME_DEFAULT

    result = subprocess.run(
        ["/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle"],
        capture_output=True,
        text=True,
        check=False,
    )

    return THEME_DARK if result.stdout.strip() == "Dark" else THEME_LIGHT
