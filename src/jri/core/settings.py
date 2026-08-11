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
# Use the JRI reasoning-effort values, not the provider library list. The provider decides if a model supports a value.
# This type documents the values. It does not promise support.
type ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
type Temperature = Annotated[float, Field(ge=0, le=2)] | None

APPLICATION_NAME = "jri"
COMMENT_WIDTH = 100
CONFIG_INTRO = (
    "Welcome to JRI! You can use this file now, with no changes. The values below are the defaults JRI uses. "
    "The commented lines are optional settings: remove the # to turn one on."
)


def read_api_key(variable: str) -> str:
    return os.environ[variable]


class AgentProfile(BaseModel):
    model: str = Field(description="ID of the model.")
    reasoning_effort: ReasoningEffort = Field(
        default=None,
        description=(
            "Reasoning effort. The values are: none, minimal, low, medium, high, xhigh, and max. The value none "
            "turns reasoning off. Omit this setting to let the model pick its own default. Not every model supports "
            "every value. A model rejects a value it does not support."
        ),
    )
    temperature: Temperature = Field(
        default=None,
        examples=[0.2],
        description="Sampling temperature. The value 0 gives focused output. The value 2 gives varied output.",
    )

    model_config = ConfigDict(extra="forbid")


class AgentProfiles(BaseModel):
    interviewer: AgentProfile = Field(
        default=AgentProfile(model="gpt-5.6-sol", reasoning_effort="medium"),
        description="Leads the requirements-gathering interview. Recommended model type: smart, and fairly fast.",
    )
    explorer: AgentProfile = Field(
        default=AgentProfile(model="gpt-5.6-terra", reasoning_effort="low"),
        description=(
            "Runs shell commands, reads files, and browses the web for the interviewer. "
            "Recommended model type: low cost, fast, and able to read images."
        ),
    )
    functional_analyst: AgentProfile = Field(
        default=AgentProfile(model="gpt-5.6-sol", reasoning_effort="xhigh"),
        description=(
            "Turns the interview notes into functional specifications. "
            "Recommended model type: the smartest model available."
        ),
    )
    architect: AgentProfile = Field(
        default=AgentProfile(model="gpt-5.6-sol", reasoning_effort="xhigh"),
        description=(
            "Designs the system that meets those specifications. Recommended model type: the smartest model available."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class LLM(BaseModel):
    provider: str = Field(
        default="openai-subscription",
        description=(
            'Set this to "openai-subscription" to reuse a ChatGPT subscription through the Codex CLI. Or set this '
            "to the base URL of an OpenAI-compatible provider, for example https://api.openai.com/v1\n\n"
            "The subscription option needs the Codex CLI (https://learn.chatgpt.com/docs/codex/cli). "
            'Set `cli_auth_credentials_store = "file"` in ~/.codex/config.toml. Then run `codex login`.'
        ),
    )
    api_key: str | None = Field(
        default=None,
        examples=["OPENAI_API_KEY"],
        description=(
            "Name of the environment variable that holds the API key for the provider above. This setting is "
            'required unless the provider is "openai-subscription". Do not put the key itself here. JRI reads the '
            "key from your shell, or from the .env file at the root of your project."
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
        description="Name of the environment variable that holds the Brave Search LLM Context API key.",
    )

    model_config = ConfigDict(extra="forbid")


class Logging(BaseModel):
    level: LoggingLevel = Field(
        default="INFO",
        description=(
            "Minimum logging level. The values are: DEBUG, INFO, WARNING, ERROR, and CRITICAL. "
            f"JRI saves logs in the {paths.LOGS_DIR}/ directory."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class Settings(BaseModel):
    llm: LLM = Field(default_factory=LLM, description="The provider that every agent sends model requests to.")
    brave_search: BraveSearch = Field(
        default_factory=BraveSearch,
        description=(
            "Adds web search for the explorer agent. The explorer agent already has the shell, files, and URLs. "
            "Get an API key at https://brave.com/search/api/."
        ),
    )
    agents: AgentProfiles = Field(
        default_factory=AgentProfiles,
        description=(
            "Each agent uses a model from the provider above. Omit the reasoning effort for models that do not "
            "support reasoning. Omit the temperature to let the model pick its own value. Reasoning models reject "
            "the temperature setting."
        ),
    )
    logging: Logging = Field(
        default_factory=Logging, description="The diagnostic messages that JRI writes while it runs."
    )

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
            # A short abbreviation can fail the similarity match. Suggest the abbreviation only if it matches one
            # setting prefix.
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
    # A cross-setting validation error still belongs to one setting. Report the error at that setting, not at the
    # whole file.
    return ValidationError.from_exception_data(
        Settings.__name__,
        [
            InitErrorDetails(
                type=PydanticCustomError("value_error", cast("LiteralString", message)), loc=path, input=None
            )
        ],
    )
