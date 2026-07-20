import inspect
import logging
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, replace
from typing import ParamSpec, Self, TypeVar, cast, get_type_hints

from openai import pydantic_function_tool
from openai.types.responses import FunctionToolParam, ResponseFunctionCallOutputItemListParam
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from .events import ChatEvent, ToolCallFinished, ToolCallStarted

_METADATA_ATTR = "__jri_tool_metadata__"
MAX_OUTPUT_LENGTH = 100_000
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Metadata:
    description: str
    started_label: str
    finished_label: str
    symbol: str
    strict: bool


@dataclass(frozen=True)
class Output:
    """Represent the final output emitted by a streaming tool."""

    value: str | ResponseFunctionCallOutputItemListParam


Stream = Generator[ToolCallStarted | ToolCallFinished | Output]
Params = ParamSpec("Params")
Return = TypeVar("Return")


def tool(
    description: str, *, started_label: str, finished_label: str, symbol: str = "⚙︎", strict: bool = True
) -> Callable[[Callable[Params, Return]], Callable[Params, Return]]:
    """Mark a method as an agent tool.

    The tool name is inferred from the decorated function name. Labels
    may interpolate tool arguments.
    `Tool.discover` discovers these methods on `Agent`
    subclasses.

    Returns:
        A decorator that attaches tool metadata to the function.
    """

    def mark_as_tool(func: Callable[Params, Return]) -> Callable[Params, Return]:
        setattr(func, _METADATA_ATTR, _Metadata(description, started_label, finished_label, symbol, strict))
        return func

    return mark_as_tool


class Invocation:
    """Stream nested tool events and retain the tool's final output."""

    def __init__(self, output: str | ResponseFunctionCallOutputItemListParam | Stream) -> None:
        self.stream = output if isinstance(output, Iterator) else iter((Output(output),))
        self._output: str | ResponseFunctionCallOutputItemListParam | None = None

    def __iter__(self) -> Generator[ChatEvent]:
        """Resolve the final tool output.

        Yields:
            Nested tool events.
        """

        while True:
            try:
                item = next(self.stream)
            except StopIteration:
                return
            except (RuntimeError, TypeError, ValueError) as error:
                logger.exception("stream_failed")
                self._output = f"Tool call failed: {error}"
                return
            if isinstance(item, Output):
                self._output = item.value
                logger.debug("stream_output output=%r", item.value)
            else:
                logger.debug("stream_event value=%r", item)
                yield replace(item, depth=item.depth + 1)

    @property
    def output(self) -> str | ResponseFunctionCallOutputItemListParam:
        """Return the resolved tool output."""

        if isinstance(self._output, str) and len(self._output) > MAX_OUTPUT_LENGTH:
            return self._output[:MAX_OUTPUT_LENGTH] + "\n\n[Output truncated. Try splitting into more targeted calls.]"
        if isinstance(self._output, list):
            output: ResponseFunctionCallOutputItemListParam = []
            remaining = MAX_OUTPUT_LENGTH
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
    """Runtime wrapper for an `@tool`-decorated callable."""

    name: str
    description: str
    started_label: str
    finished_label: str
    symbol: str
    strict: bool
    func: Callable[..., str | ResponseFunctionCallOutputItemListParam | Stream]
    args_model: type[BaseModel]

    @classmethod
    def discover(cls, owner: object) -> list[Self]:
        """Discover every `@tool` method available on `owner`.

        Returns:
            Tools found on the owner.
        """

        tools: list[Self] = []
        for name in type(owner).__dict__:
            func = getattr(owner, name)
            wrapped = getattr(func, "__func__", func)
            if not (metadata := getattr(wrapped, _METADATA_ATTR, None)):
                continue
            annotations = get_type_hints(wrapped, include_extras=True)
            fields = {
                param.name: (
                    annotations.get(param.name, str),
                    ... if param.default is inspect.Parameter.empty else param.default,
                )
                for param in inspect.signature(func).parameters.values()
            }
            args_model = create_model(
                f"{func.__name__.title()}Args",
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
                    func=func,
                    args_model=args_model,
                )
            )
        return tools

    def format_label(self, label: str, args: str) -> str:
        """Format a user-facing label with the tool arguments.

        Returns:
            The label formatted with the tool arguments.
        """

        try:
            payload = self.args_model.model_validate_json(args, strict=True)
        except ValidationError:
            return self.name
        return label.format(**payload.model_dump())

    @property
    def definition(self) -> FunctionToolParam:
        """OpenAI Responses API function-tool definition."""

        if self.strict:
            function = pydantic_function_tool(self.args_model, name=self.name, description=self.description)["function"]
            parameters = cast("dict[str, object]", function.get("parameters"))
        else:
            parameters = self.args_model.model_json_schema()
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": parameters,
            "strict": self.strict,
        }

    def invoke(self, args: str) -> Invocation:
        """Validate JSON args and call the tool.

        Returns:
            An invocation that streams events and retains the result.
        """

        logger.info("invocation_started name=%s", self.name)
        logger.debug("arguments name=%s arguments=%r", self.name, args)
        try:
            payload = self.args_model.model_validate_json(args, strict=True)
            output = self.func(**{name: getattr(payload, name) for name in self.args_model.model_fields})
        except ValidationError as error:
            logger.exception("validation_failed name=%s", self.name)
            first = error.errors(include_url=False)[0]
            output = f"Tool call failed: {first['msg']}."
        except (RuntimeError, TypeError, ValueError) as error:
            logger.exception("invocation_failed name=%s", self.name)
            output = f"Tool call failed: {error}"
        logger.info("invocation_finished name=%s", self.name)
        logger.debug("output name=%s output=%r", self.name, output)
        return Invocation(output)
