from jri.core.exceptions import ConfigurationError
from jri.core.service import Service
from jri.core.settings import Settings, get_settings
from jri.tui.app import App

CONFIG_ERROR_COPY = """Invalid configuration.
Set or fix these settings:

{errors}

You can define them in your shell, in a .env file in this directory,
or pass them as CLI flags.
"""


def main() -> None:
    settings = _get_settings_or_print_error()
    service = Service(settings)
    app = App(service)
    app.run()


def _get_settings_or_print_error() -> Settings:
    try:
        return get_settings()
    except ConfigurationError as error:
        print(_get_config_error_message(error))
        raise SystemExit(1) from error


def _get_config_error_message(error: ConfigurationError) -> str:
    use_cli_kebab_case = Settings.model_config.get("cli_kebab_case")
    env_prefix = Settings.model_config.get("env_prefix", "")
    error_lines: list[str] = []
    for issue in error.validation_error.errors():
        field_name = str(issue["loc"][0])
        if not (field := Settings.model_fields.get(field_name)):
            continue
        cli_name = field_name.replace("_", "-") if use_cli_kebab_case else field_name
        error_lines.append(
            f"- {env_prefix}{field_name.upper()} or --{cli_name}: {field.description or '<no description available>'}"
        )
    return CONFIG_ERROR_COPY.format(errors="\n".join(error_lines))


if __name__ == "__main__":
    main()
