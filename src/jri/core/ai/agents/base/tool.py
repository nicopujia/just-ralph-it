import inspect
import logging
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, replace
from typing import ClassVar, ParamSpec, Self, TypeVar, cast, get_type_hints

from openai import pydantic_function_tool
from openai.types.responses import FunctionToolParam, ResponseFunctionCallOutputItemListParam
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from jri.core import ai
from jri.lib import prompt

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
    outcome: "ai.Outcome" = "done"


class Invocation:
    # Enough of a reason to recognise on a row, cut before it wraps.
    MAX_DETAIL_LENGTH = 120
    MAX_OUTPUT_LENGTH = 100_000

    def __init__(
        self, output: str | ResponseFunctionCallOutputItemListParam | Stream, *, failed: bool = False, detail: str = ""
    ) -> None:
        self.stream = output if isinstance(output, Iterator) else iter((ToolOutput(output),))
        self._failed = failed
        self._detail = detail
        self._outcome: ai.Outcome = "done"
        self._output: str | ResponseFunctionCallOutputItemListParam | None = None

    def __iter__(self) -> Generator["ai.AgentEvent"]:
        while True:
            try:
                item = next(self.stream)
            except StopIteration:
                return
            except (RuntimeError, TypeError, ValueError) as error:
                logger.exception("stream_failed")
                failure = prompt.render(tool_call_failed=str(error))
                # Whatever the stream already reported is real work, so
                # the failure joins that output instead of replacing it.
                if self._output is None:
                    self._output = failure
                elif isinstance(self._output, str):
                    self._output = f"{self._output}\n\n{failure}"
                else:
                    self._output = [*self._output, {"type": "input_text", "text": failure}]
                self._failed = True
                self._detail = str(error)
                return
            if isinstance(item, ToolOutput):
                self._output = item.value
                self._outcome = item.outcome
                logger.debug("stream_output output=%r", item.value)
            else:
                logger.debug("stream_event value=%r", item)
                yield replace(item, depth=item.depth + 1)

    @property
    def detail(self) -> str:
        # The reason comes from the exception, never from reading the
        # rendered output back: that output is quoted inside a fence.
        return self._detail.partition("\n")[0][: self.MAX_DETAIL_LENGTH]

    @property
    def outcome(self) -> "ai.Outcome":
        # A stream left without an output has none to report, however
        # far it got, so the call it stands for did not succeed.
        return "failed" if self._failed or self._output is None else self._outcome

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
                # Images and files are already bounded by the
                # model's own input limits, so only text spends
                # this budget.
                if item["type"] != "input_text":
                    output.append(item)
                    continue
                if len(item["text"]) <= remaining:
                    output.append(item)
                    remaining -= len(item["text"])
                    continue
                message = "\n\n[Output truncated. Try splitting into more targeted calls.]"
                output.append({**item, "text": item["text"][:remaining] + message})
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
        # An owner inherits the tools of the classes it extends, base
        # class first, so the whole line of ancestors is walked.
        for name in dict.fromkeys(name for klass in reversed(type(owner).__mro__) for name in vars(klass)):
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
        detail = ""
        try:
            payload = self.arguments_model.model_validate_json(arguments, strict=True)
            output = self.func(**{name: getattr(payload, name) for name in self.arguments_model.model_fields})
        except ValidationError as error:
            logger.exception("validation_failed name=%s", self.name)
            first = error.errors(include_url=False)[0]
            detail = f"{first['msg']}."
            output = prompt.render(tool_call_failed=detail)
            failed = True
        except (RuntimeError, TypeError, ValueError) as error:
            logger.exception("invocation_failed name=%s", self.name)
            detail = str(error)
            output = prompt.render(tool_call_failed=detail)
            failed = True
        logger.info("invocation_finished name=%s", self.name)
        logger.debug("output name=%s output=%r", self.name, output)
        return Invocation(output, failed=failed, detail=detail)

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
