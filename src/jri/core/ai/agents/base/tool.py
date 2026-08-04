import inspect
import logging
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, replace
from typing import ClassVar, ParamSpec, Self, TypeVar, cast, get_type_hints

from openai import pydantic_function_tool
from openai.types.responses import FunctionToolParam, ResponseFunctionCallOutputItemListParam
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from jri.core import ai

type Stream = Generator[ai.ToolCallStarted | ai.ToolCallFinished | ToolOutput]

DEFAULT_SYMBOL = "⚙︎"

Params = ParamSpec("Params")
Return = TypeVar("Return")

logger = logging.getLogger(__name__)


def tool(
    description: str,
    *,
    started_label: str,
    finished_label: str,
    symbol: str = DEFAULT_SYMBOL,
    strict: bool = True,
    read_only: bool = False,
) -> Callable[[Callable[Params, Return]], Callable[Params, Return]]:
    def mark_as_tool(func: Callable[Params, Return]) -> Callable[Params, Return]:
        setattr(
            func, Tool.METADATA_ATTR, _Metadata(description, started_label, finished_label, symbol, strict, read_only)
        )
        return func

    return mark_as_tool


@dataclass(frozen=True)
class ToolOutput:
    value: str | ResponseFunctionCallOutputItemListParam


class Invocation:
    MAX_OUTPUT_LENGTH = 100_000

    def __init__(self, output: str | ResponseFunctionCallOutputItemListParam | Stream, *, failed: bool = False) -> None:
        self.stream = output if isinstance(output, Iterator) else iter((ToolOutput(output),))
        self.failed = failed
        self._output: str | ResponseFunctionCallOutputItemListParam | None = None

    def __iter__(self) -> Generator["ai.ChatEvent"]:
        while True:
            try:
                item = next(self.stream)
            except StopIteration:
                self.failed = self.failed or self._output is None
                return
            except (RuntimeError, TypeError, ValueError) as error:
                logger.exception("stream_failed")
                self._output = f"Tool call failed: {error}"
                self.failed = True
                return
            if isinstance(item, ToolOutput):
                self._output = item.value
                logger.debug("stream_output output=%r", item.value)
            else:
                logger.debug("stream_event value=%r", item)
                yield replace(item, depth=item.depth + 1)

    @property
    def output(self) -> str | ResponseFunctionCallOutputItemListParam:
        if isinstance(self._output, str) and len(self._output) > self.MAX_OUTPUT_LENGTH:
            return (
                self._output[: self.MAX_OUTPUT_LENGTH]
                + "\n\n[Output truncated. Try splitting into more targeted calls.]"
            )
        if isinstance(self._output, list):
            output: ResponseFunctionCallOutputItemListParam = []
            remaining = self.MAX_OUTPUT_LENGTH
            for item in self._output:
                field = next((name for name in ("text", "image_url", "file_data") if name in item), None)
                value = item[field] if field else ""
                if not isinstance(value, str) or len(value) <= remaining:
                    output.append(item)
                    remaining -= len(value)
                    continue
                message = "\n\n[Output truncated. Try splitting into more targeted calls.]"
                if item["type"] == "input_text":
                    output.append({**item, "text": value[:remaining] + message})
                else:
                    output.append({"type": "input_text", "text": message.strip()})
                break
            return output
        return self._output if self._output is not None else "Tool call failed: streaming tool returned no output."


@dataclass(frozen=True)
class Tool:
    METADATA_ATTR: ClassVar[str] = "__jri_tool_metadata__"

    name: str
    description: str
    started_label: str
    finished_label: str
    symbol: str
    strict: bool
    read_only: bool
    func: Callable[..., str | ResponseFunctionCallOutputItemListParam | Stream]
    arguments_model: type[BaseModel]

    @classmethod
    def discover(cls, owner: object) -> list[Self]:
        tools: list[Self] = []
        for name in type(owner).__dict__:
            func = getattr(owner, name)
            wrapped = getattr(func, "__func__", func)
            if not (metadata := getattr(wrapped, cls.METADATA_ATTR, None)):
                continue
            annotations = get_type_hints(wrapped, include_extras=True)
            fields = {
                parameter.name: (
                    annotations.get(parameter.name, str),
                    ... if parameter.default is inspect.Parameter.empty else parameter.default,
                )
                for parameter in inspect.signature(func).parameters.values()
            }
            arguments_model = create_model(
                f"{func.__name__.title()}Arguments",
                __config__=ConfigDict(extra="forbid"),
                **fields,  # pyright: ignore[reportCallIssue, reportArgumentType]
            )
            tools.append(
                cls(
                    name=func.__name__,
                    description=metadata.description,
                    started_label=metadata.started_label,
                    finished_label=metadata.finished_label,
                    symbol=metadata.symbol,
                    strict=metadata.strict,
                    read_only=metadata.read_only,
                    func=func,
                    arguments_model=arguments_model,
                )
            )
        return tools

    def format_label(self, label: str, arguments: str) -> str:
        try:
            payload = self.arguments_model.model_validate_json(arguments, strict=True)
        except ValidationError:
            return self.name
        return label.format(**payload.model_dump())

    @property
    def definition(self) -> FunctionToolParam:
        if self.strict:
            function = pydantic_function_tool(self.arguments_model, name=self.name, description=self.description)[
                "function"
            ]
            parameters = cast("dict[str, object]", function.get("parameters"))
        else:
            parameters = self.arguments_model.model_json_schema()
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": parameters,
            "strict": self.strict,
        }

    def invoke(self, arguments: str) -> Invocation:
        logger.info("invocation_started name=%s", self.name)
        logger.debug("arguments name=%s arguments=%r", self.name, arguments)
        failed = False
        try:
            payload = self.arguments_model.model_validate_json(arguments, strict=True)
            output = self.func(**{name: getattr(payload, name) for name in self.arguments_model.model_fields})
        except ValidationError as error:
            logger.exception("validation_failed name=%s", self.name)
            first = error.errors(include_url=False)[0]
            output = f"Tool call failed: {first['msg']}."
            failed = True
        except (RuntimeError, TypeError, ValueError) as error:
            logger.exception("invocation_failed name=%s", self.name)
            output = f"Tool call failed: {error}"
            failed = True
        logger.info("invocation_finished name=%s", self.name)
        logger.debug("output name=%s output=%r", self.name, output)
        return Invocation(output, failed=failed)

    def replay(self, arguments: str) -> None:
        if self.read_only:
            return
        list(self.invoke(arguments))


@dataclass(frozen=True)
class _Metadata:
    description: str
    started_label: str
    finished_label: str
    symbol: str
    strict: bool
    read_only: bool
