import difflib
import os
import textwrap
from pathlib import Path
from typing import Annotated, Any, Literal, LiteralString, Self, cast

import yaml
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError

from jri.lib.providers import codex, gateway

from . import paths
from .exceptions import PersistenceError
from .workspace import Workspace

type LoggingLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
# Use the JRI reasoning-effort values, and not the list of the provider library.
# The provider decides whether a model supports a value.
# This type documents the values. It does not promise support.
type ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
type Temperature = Annotated[float, Field(ge=0, le=2)] | None

APPLICATION_NAME = "jri"
API_KEY_DESCRIPTION = "Name of the environment variable (NOT the key itself!)"
COMMENT_WIDTH = 100
# JRI writes a section that has no value as a comment. This line tells the user how to use that section.
SECTION_INSTRUCTION = "Remove the first # and the space after it from each line of this section to use it."
# JRI indents a setting this many spaces inside the comment of its section. The indent is deeper than the
# one a section gives its settings. YAML then refuses a file where the user removes the # of only some lines.
SECTION_INDENT = "    "
INTRO = (
    "Welcome to Just Ralph It!\n\n"
    "When asked for an API key, you have to specify the name of the corresponding environment variable, "
    "and then JRI reads the variables defined at your shell and at the .env file at the root of this project, "
    "if any.\n"
    "That is because this settings file is meant to be committed.\n\n"
    "To start all your future projects with the settings that you like, write them in "
    f"{paths.GLOBAL_SETTINGS_FILE}, a file with the same shape as this one."
)


def read_api_key(variable: str) -> str:
    return os.environ[variable]


class AgentProfile(BaseModel):
    model: str = Field(description="The ID of the model, as the provider writes it.")
    reasoning_effort: ReasoningEffort = Field(
        default=None,
        description="One of none, minimal, low, medium, high, xhigh, max. Not all models accept all the values.",
    )
    temperature: Temperature = Field(
        default=None,
        examples=[0.2],
        description="0 gives predictable output and 2 gives random output. Reasoning models refuse this setting.",
    )

    model_config = ConfigDict(extra="forbid")


class AgentProfiles(BaseModel):
    interviewer: AgentProfile = Field(
        default=AgentProfile(model="xai/grok-4.6", reasoning_effort="medium"),
        examples=[{"temperature": 0.75}],
        description=("Interacts with you and takes notes.\nUse a smart model with a relatively fast reasoning_effort."),
    )
    explorer: AgentProfile = Field(
        default=AgentProfile(model="openai/gpt-5.6-luna", reasoning_effort="low"),
        examples=[{"temperature": 0}],
        description=(
            "Runs commands, reads files, and browses the web for the interviewer.\n"
            "Use a low-cost and fast model with vision capabilities."
        ),
    )
    functional_analyst: AgentProfile = Field(
        default=AgentProfile(model="openai/gpt-5.6-sol", reasoning_effort="xhigh"),
        description="Writes the functional specifications.\nUse the smartest model that you have.",
    )
    architect: AgentProfile = Field(
        default=AgentProfile(model="anthropic/claude-opus-5", reasoning_effort="xhigh"),
        description=(
            "Designs the system.\n"
            "Use the smartest model that you have and, if possible, from a different lab than the "
            "functional_analyst."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class LLM(BaseModel):
    provider: str = Field(
        default=gateway.BASE_URL,
        description=(
            "Here you can set the base URL of any OpenAI-compatible provider.\n"
            "Or, for a ChatGPT subscription, write `openai-subscription`. "
            "For that, you need the Codex CLI (https://learn.chatgpt.com/docs/codex/cli) installed and logged in."
        ),
    )
    api_key: str | None = Field(default="AI_GATEWAY_API_KEY", description=API_KEY_DESCRIPTION)

    model_config = ConfigDict(extra="forbid")

    @property
    def client(self) -> OpenAI:
        if self.provider == "openai-subscription":
            return codex.Client(APPLICATION_NAME)
        api_key = read_api_key(cast("str", self.api_key))
        # Only the gateway reads the fields its client adds. Give every other address a client that sends the
        # request as the provider library writes it.
        if self.provider == gateway.BASE_URL:
            return gateway.Client(base_url=self.provider, api_key=api_key)
        return OpenAI(base_url=self.provider, api_key=api_key)

    def validate_authentication(self) -> None:
        if self.provider == "openai-subscription":
            codex.Auth(APPLICATION_NAME).validate()


class BraveSearch(BaseModel):
    api_key: str | None = Field(default=None, examples=["BRAVE_SEARCH_API_KEY"], description=API_KEY_DESCRIPTION)

    model_config = ConfigDict(extra="forbid")


class Logging(BaseModel):
    level: LoggingLevel = Field(default="INFO", description="One of DEBUG, INFO, WARNING, ERROR, CRITICAL.")

    model_config = ConfigDict(extra="forbid")


class Settings(BaseModel):
    llm: LLM = Field(
        default_factory=LLM,
        description=(
            "To start using JRI, you need an LLM inference provider.\n\n"
            "Vercel AI Gateway is set as the default one for simplicity—with one key, it gives you access to "
            "practically all models from all providers. You can get an API key at "
            "https://vercel.com/d?to=/[team]/~/ai-gateway/api-keys. "
            "It is also useful because if you already have API keys from other providers, it lets you unify "
            "them under a single provider, as it supports to Bring Your Own Key (BYOK).\n\n"
            "Nevertheless, you can also use any OpenAI-compatible provider of your choice, or even a ChatGPT "
            "subscription."
        ),
    )
    brave_search: BraveSearch = Field(
        default_factory=BraveSearch,
        description="[Optional] Get a key at https://brave.com/search/api/ to add web search support.",
    )
    agents: AgentProfiles = Field(
        default_factory=AgentProfiles, description="Each agent can use a different model from the provider above."
    )
    logging: Logging = Field(default_factory=Logging, description=f"JRI writes the logs in {paths.LOGS_DIR}/.")

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def load(cls) -> Self:
        values = yaml.safe_load(_read(Workspace.find().settings_file))
        settings = cls.model_validate({} if values is None else values)
        settings.validate_api_key_variables()
        return settings

    @classmethod
    def load_global(cls) -> Self | None:
        settings_file = Path(paths.GLOBAL_SETTINGS_FILE).expanduser()
        if not settings_file.exists():
            return None
        values = yaml.safe_load(_read(settings_file))
        if isinstance(values, dict):
            values = _merge(cls.model_validate({}).model_dump(), values)
        # A blank file names no setting. The model rejects a file that is not a mapping.
        return cls.model_validate({} if values is None else values)

    @classmethod
    def render(cls, values: "Settings | None" = None, *, comments: bool = True) -> str:
        body = _render_settings(cls() if values is None else values, 0, set(), {}, comments=comments)
        if not comments:
            return "\n".join([*body, ""])
        return "\n".join([*_wrap_comment(INTRO, ""), "", *body, ""])

    @classmethod
    def suggest(cls, path: tuple[int | str, ...]) -> str | None:
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
            # The similarity match can fail on a short abbreviation.
            # Suggest the abbreviation only when it matches one setting prefix.
            prefixed = [name for name in candidates if name.startswith(unknown)]
            matches = prefixed if len(prefixed) == 1 else []
        return ".".join([*map(str, path[:-1]), *matches]) if matches else None

    @model_validator(mode="after")
    def validate_api_keys(self) -> "Settings":
        subscription = self.llm.provider == "openai-subscription"
        if not subscription and not self.llm.api_key:
            raise _reject_setting(
                ("llm", "api_key"),
                "must name the environment variable holding the API key, unless llm.provider is openai-subscription",
            )
        return self

    # Only a command that loads the settings of a project reads these variables. `jri init` writes a settings file
    # before the project has an .env file, so it validates the settings and stops there.
    def validate_api_key_variables(self) -> None:
        # The subscription has its own login. JRI does not read a key variable for it.
        subscription = self.llm.provider == "openai-subscription"
        for section, variable in (
            ("llm", None if subscription else self.llm.api_key),
            ("brave_search", self.brave_search.api_key),
        ):
            if variable and variable not in os.environ:
                raise _reject_setting(
                    (section, "api_key"), f"names {variable}, but that environment variable is not set"
                )


def _wrap_comment(description: str, indent: str) -> list[str]:
    lines: list[str] = []
    for paragraph in description.split("\n\n"):
        if lines:
            lines.append(f"{indent}#")
        for text in paragraph.split("\n"):
            # The # and the space after it are part of the width.
            lines.extend(f"{indent}# {line}" for line in textwrap.wrap(text, COMMENT_WIDTH - len(indent) - 2))
    return lines


def _render_settings(
    values: BaseModel,
    level: int,
    documented: set[tuple[type[BaseModel], str]],
    examples: dict[str, Any],
    *,
    comments: bool,
    inside_a_comment: bool = False,
) -> list[str]:
    indent = "  " * level
    model = type(values)
    entries: list[tuple[list[str], list[str]]] = []
    for name, field in model.model_fields.items():
        comment: list[str] = []
        # Document a setting one time. The agents repeat the settings of the same profile.
        if comments and (model, name) not in documented:
            documented.add((model, name))
            comment = _wrap_comment(field.description or "", indent)
        value = getattr(values, name)
        if isinstance(value, BaseModel):
            # A section holds no value when every setting in it holds none.
            unset = all(setting is None for setting in value.model_dump().values())
            # A file with no comments holds only the settings that have a value. An unset section has none.
            if unset and not comments:
                continue
            body = _render_settings(
                value,
                level + 1,
                documented,
                field.examples[0] if field.examples else {},
                comments=comments,
                inside_a_comment=unset,
            )
            if unset:
                comment = [*comment, *_wrap_comment(SECTION_INSTRUCTION, indent)]
                # Start the # of every line of the body at the indent of the section. A # that starts
                # deeper leaves a setting at the indent of the section above. YAML then reads that setting
                # as one of the section above. The user gives a value to a setting that they did not choose.
                body = [f"{indent}#{SECTION_INDENT}{line.strip()}" if line.strip() else line for line in body]
            entries.append((comment, [f"{indent}# {name}:" if unset else f"{indent}{name}:", *body]))
            continue
        unset = value is None
        if unset and not comments:
            continue
        if unset:
            # A section can suggest its own value. Each agent suggests a different temperature.
            value = examples.get(name, field.examples[0] if field.examples else None)
        setting = yaml.safe_dump({name: value}, sort_keys=False, allow_unicode=True, width=10**9).strip()
        # The comment of a section already marks the settings inside it. JRI does not mark such a setting twice.
        marked = unset and not inside_a_comment
        entries.append((comment, [f"{indent}# {setting}" if marked else f"{indent}{setting}"]))

    if not comments:
        return [line for _, entry in entries for line in entry]
    # A blank line separates a comment from the setting above it. Settings with no comment stay together.
    lines = [line for comment, entry in entries for line in (["", *comment, *entry] if comment else entry)]
    return lines[1:] if lines and not lines[0] else lines


# JRI reads two settings files the same way. A file that JRI cannot read holds no setting to fix, so report the
# file and the reason instead.
def _read(settings_file: Path) -> str:
    try:
        return settings_file.read_text(encoding="utf-8")
    except OSError as error:
        raise PersistenceError(f"Could not read the settings file `{settings_file}`: {error.strerror}") from error


# The global settings can name only some of the settings of a section. Each setting that they do not name keeps
# its default value.
def _merge(defaults: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    merged = {**defaults}
    for key, value in values.items():
        default = merged.get(key)
        merged[key] = _merge(default, value) if isinstance(default, dict) and isinstance(value, dict) else value
    return merged


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
