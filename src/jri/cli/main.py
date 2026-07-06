from jri.core.exceptions import ConfigurationError
from jri.core.service import Service
from jri.core.settings import Settings, get_settings
from jri.tui.app import App

CONFIG_ERROR_COPY = """Invalid configuration.
Set or fix these environment variables:

%s

You can define them in your shell or in a .env file on this directory.
"""


def main() -> None:
    try:
        settings = get_settings()
    except ConfigurationError as error:
        print(_get_config_error_message(error))
        raise SystemExit(1) from error

    service = Service(settings)
    app = App(service)
    app.run()


def _get_config_error_message(error: ConfigurationError) -> str:
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


if __name__ == "__main__":
    main()
