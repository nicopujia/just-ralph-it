from rich.markdown import Markdown

from jri.core.exceptions import ConfigurationError
from jri.core.settings import Settings

from .constants import CONFIG_ERROR_HELP_MESSAGE_TEMPLATE


def get_config_error_help_message(error: ConfigurationError) -> Markdown:
    error_lines: list[str] = []
    for issue in error.validation_error.errors():
        field_name = str(issue["loc"][0])
        env_prefix = Settings.model_config.get("env_prefix")
        field = Settings.model_fields.get(field_name)
        if not field:
            continue
        description = field.description or "<no description available>"
        line = f"- **{env_prefix}{field_name.upper()}**: {description}"
        error_lines.append(line)
    markup = CONFIG_ERROR_HELP_MESSAGE_TEMPLATE % "\n".join(error_lines)
    return Markdown(markup)
