import difflib
import os
import textwrap
from typing import Annotated, Literal, LiteralString, Self, cast

import yaml
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError

from jri.lib.providers import codex

from . import paths
from .workspace import Workspace

type LoggingLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
# Use JRI reasoning-effort values, not the provider-library list. The provider decides whether a model supports a value.
# This type documents values. It does not promise support.
type ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
type Temperature = Annotated[float, Field(ge=0, le=2)] | None

APPLICATION_NAME = "jri"
COMMENT_WIDTH = 100
CONFIG_INTRO = "The values below are the ones JRI already uses, and the commented ones are optional."


def read_api_key(variable: str) -> str:
    return os.environ[variable]


class AgentProfile(BaseModel):
    model: str = Field(description="Model ID.")
    reasoning_effort: ReasoningEffort = Field(
        default=None,
        description=(
            "Reasoning effort: none, minimal, low, medium, high, xhigh, or max. Setting none turns reasoning off, "
            "where omitting this leaves the model its own default. Not every model offers every one of these, and a "
            "model rejects an effort it does not offer."
        ),
    )
    temperature: Temperature = Field(
        default=None, examples=[0.2], description="Sampling temperature: 0 is focused and 2 is varied."
    )

    model_config = ConfigDict(extra="forbid")


class AgentProfiles(BaseModel):
    interviewer: AgentProfile = Field(
        default=AgentProfile(model="gpt-5.6-sol", reasoning_effort="medium"),
        description="Leads the requirements gathering interview. Recommended model type: smart yet relatively fast.",
    )
    explorer: AgentProfile = Field(
        default=AgentProfile(model="gpt-5.6-terra", reasoning_effort="low"),
        description=(
            "Runs shell commands, reads files, and browses the web on the interviewer's behalf. "
            "Recommended model type: low cost, fast and with vision capabilities."
        ),
    )
    functional_analyst: AgentProfile = Field(
        default=AgentProfile(model="gpt-5.6-sol", reasoning_effort="xhigh"),
        description=(
            "Turns the interview notes into functional specifications. Recommended model type: as smart as possible."
        ),
    )
    architect: AgentProfile = Field(
        default=AgentProfile(model="gpt-5.6-sol", reasoning_effort="xhigh"),
        description=(
            "Designs the system that satisfies those specifications. Recommended model type: as smart as possible."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class LLM(BaseModel):
    provider: str = Field(
        default="openai-subscription",
        description=(
            'Either "openai-subscription", to reuse a ChatGPT subscription through the Codex CLI, or the base URL '
            "of any OpenAI-compatible provider, such as https://api.openai.com/v1\n\n"
            "The subscription needs the Codex CLI (https://learn.chatgpt.com/docs/codex/cli) to store its "
            'credentials in a file, so set `cli_auth_credentials_store = "file"` in ~/.codex/config.toml and run '
            "`codex login`."
        ),
    )
    api_key: str | None = Field(
        default=None,
        examples=["OPENAI_API_KEY"],
        description=(
            "Name of the environment variable holding the API key of the provider above. Required unless the "
            'provider is "openai-subscription". NEVER put the key itself here: JRI reads it from your shell and '
            "from the .env file at the root of your project."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @property
    def client(self) -> OpenAI:
        if self.provider == "openai-subscription":
            return codex.Client(APPLICATION_NAME)
        return OpenAI(base_url=self.provider, api_key=read_api_key(cast("str", self.api_key)))

    def validate_authentication(self) -> None:
        if self.provider == "openai-subscription":
            codex.Auth(APPLICATION_NAME).validate()


class BraveSearch(BaseModel):
    api_key: str | None = Field(
        default=None,
        examples=["BRAVE_SEARCH_API_KEY"],
        description="Name of the environment variable holding the Brave Search LLM Context API key.",
    )

    model_config = ConfigDict(extra="forbid")


class Logging(BaseModel):
    level: LoggingLevel = Field(
        default="INFO",
        description=(
            f"Minimum logging level: DEBUG, INFO, WARNING, ERROR, or CRITICAL. Logs are saved under {paths.LOGS_DIR}/."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class Settings(BaseModel):
    llm: LLM = Field(default_factory=LLM, description="Provider every agent sends its model requests to.")
    brave_search: BraveSearch = Field(
        default_factory=BraveSearch,
        description=(
            "Web search for the explorer agent, on top of the shell, files, and URLs it always has. "
            "Get a key at https://brave.com/search/api/."
        ),
    )
    agents: AgentProfiles = Field(
        default_factory=AgentProfiles,
        description=(
            "Each agent picks a model available on the provider above. Omit the reasoning effort on models without "
            "reasoning, and the temperature to let the model pick it; reasoning models reject the temperature "
            "outright."
        ),
    )
    logging: Logging = Field(default_factory=Logging, description="Diagnostics JRI writes down while it runs.")

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def load(cls) -> Self:
        config = yaml.safe_load(Workspace.find().config_file.read_text(encoding="utf-8"))
        return cls.model_validate({} if config is None else config)

    @classmethod
    def render_config(cls) -> str:
        intro = [f"# {line}" for line in textwrap.wrap(CONFIG_INTRO, COMMENT_WIDTH)]
        return "\n".join([*intro, "", *_render_settings(cls, None, 0), ""])

    @classmethod
    def suggest_setting(cls, path: tuple[int | str, ...]) -> str | None:
        model: type[BaseModel] = cls
        for key in map(str, path[:-1]):
            annotation = model.model_fields[key].annotation if key in model.model_fields else None
            if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
                return None
            model = annotation
        candidates = list(model.model_fields)
        unknown = str(path[-1])
        matches = difflib.get_close_matches(unknown, candidates, n=1)
        if not matches:
            # A short abbreviation can fail similarity matching. Suggest it only when it matches one setting prefix.
            prefixed = [name for name in candidates if name.startswith(unknown)]
            matches = prefixed if len(prefixed) == 1 else []
        return ".".join([*map(str, path[:-1]), *matches]) if matches else None

    @model_validator(mode="after")
    def validate_api_keys(self) -> "Settings":
        if self.llm.provider != "openai-subscription" and not self.llm.api_key:
            raise _reject_setting(
                ("llm", "api_key"),
                "must name the environment variable holding the API key, unless llm.provider is openai-subscription",
            )
        for section, variable in (("llm", self.llm.api_key), ("brave_search", self.brave_search.api_key)):
            if variable and variable not in os.environ:
                raise _reject_setting(
                    (section, "api_key"), f"names {variable}, but that environment variable is not set"
                )
        return self


def _render_settings(model: type[BaseModel], values: BaseModel | None, level: int) -> list[str]:
    indent = "  " * level
    entries: list[list[str]] = []
    for name, field in model.model_fields.items():
        comment: list[str] = []
        for paragraph in (field.description or "").split("\n\n"):
            if comment:
                comment.append(f"{indent}#")
            comment.extend(f"{indent}# {line}" for line in textwrap.wrap(paragraph, COMMENT_WIDTH - len(indent)))
        value = getattr(values, name) if values is not None else field.default
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            body = _render_settings(annotation, value if isinstance(value, BaseModel) else None, level + 1)
            unset = all(line.lstrip().startswith("#") for line in body if line)
            entries.append([*comment, f"{indent}# {name}:" if unset else f"{indent}{name}:", *body])
            continue
        unset = value is None
        if unset:
            value = field.examples[0] if field.examples else None
        setting = yaml.safe_dump({name: value}, sort_keys=False, allow_unicode=True, width=10**9).strip()
        entries.append([*comment, f"{indent}# {setting}" if unset else f"{indent}{setting}"])

    lines = [line for entry in entries for line in ("", *entry)]
    return lines[1:]


def _reject_setting(path: tuple[str, ...], message: str) -> ValidationError:
    # A cross-setting validation error still belongs to one setting. Report it at that setting, not the whole file.
    return ValidationError.from_exception_data(
        Settings.__name__,
        [
            InitErrorDetails(
                type=PydanticCustomError("value_error", cast("LiteralString", message)), loc=path, input=None
            )
        ],
    )
