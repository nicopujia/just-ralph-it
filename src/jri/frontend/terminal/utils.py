import platform
import subprocess

from jri.core.exceptions import ConfigurationError
from jri.core.settings import Settings, get_settings

from . import constants as c
from .constants import CONFIG_ERROR_COPY


def get_settings_or_print_error() -> Settings:
    """Get validated settings or exit on configuration failure.

    If validation fails, print a useful help message and exit.

    Returns:
        The validated application settings.

    Raises:
        SystemExit: Raised after printing the configuration error.
    """

    try:
        return get_settings()
    except ConfigurationError as error:
        use_cli_kebab_case = Settings.model_config.get("cli_kebab_case")
        env_prefix = Settings.model_config.get("env_prefix", "")
        error_lines: list[str] = []
        for issue in error.validation_error.errors():
            field_name = str(issue["loc"][0])
            field = Settings.model_fields[field_name]
            cli_name = field_name.replace("_", "-") if use_cli_kebab_case else field_name
            error_lines.append(
                f"- {env_prefix}{field_name.upper()} or --{cli_name}: "
                f"{field.description or '<no description available>'}"
            )
        print(CONFIG_ERROR_COPY.format(errors="\n".join(error_lines)))
        raise SystemExit(1) from error


def detect_system_theme() -> str:
    """Detect the preferred theme for the current system.

    Returns:
        The theme name to use in the terminal app.
    """

    if platform.system() != "Darwin":
        return c.THEME_DEFAULT
    cmd = ["/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return c.THEME_DARK if result.stdout.strip() == "Dark" else c.THEME_LIGHT
