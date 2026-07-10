import inspect
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, replace
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic
from typing import Any, ParamSpec, Self, TypeVar, cast, get_type_hints

from openai import pydantic_function_tool
from openai.types.responses import FunctionToolParam, ResponseFunctionCallOutputItemListParam
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from .events import ChatEvent, ToolCallFinished, ToolCallStarted

_METADATA_ATTR = "__jri_tool_metadata__"


@dataclass(frozen=True)
class _Metadata:
    description: str
    started_label: str
    finished_label: str
    symbol: str
    timeout: float | None


@dataclass(frozen=True)
class Output:
    """Represent the final output emitted by a streaming tool."""

    value: str | ResponseFunctionCallOutputItemListParam


Stream = Generator[ToolCallStarted | ToolCallFinished | Output]
Params = ParamSpec("Params")
Return = TypeVar("Return")


def tool(
    description: str, *, started_label: str, finished_label: str, symbol: str = "⚙︎", timeout: float | None = None
) -> Callable[[Callable[Params, Return]], Callable[Params, Return]]:
    """Mark a method as an agent tool.

    The tool name is inferred from the decorated function name. Labels
    may interpolate tool arguments. A timeout limits the callable's
    execution time in seconds.
    `Tool.discover` discovers these methods on `Agent`
    subclasses.

    Returns:
        A decorator that attaches tool metadata to the function.
    """

    def mark_as_tool(func: Callable[Params, Return]) -> Callable[Params, Return]:
        setattr(func, _METADATA_ATTR, _Metadata(description, started_label, finished_label, symbol, timeout))
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

        try:
            for item in self.stream:
                if isinstance(item, Output):
                    self._output = item.value
                else:
                    yield replace(item, depth=item.depth + 1)
        except (RuntimeError, TypeError, ValueError) as error:
            self._output = f"Tool call failed: {error}"

    @property
    def output(self) -> str | ResponseFunctionCallOutputItemListParam:
        """Return the resolved tool output."""

        return self._output if self._output is not None else "Tool call failed: streaming tool returned no output."


@dataclass(frozen=True)
class Tool:
    """Runtime wrapper for an `@tool`-decorated callable."""

    name: str
    description: str
    started_label: str
    finished_label: str
    symbol: str
    timeout: float | None
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
                param.name: (annotations.get(param.name, str), ...)
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
                    timeout=metadata.timeout,
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

        function = pydantic_function_tool(self.args_model, name=self.name, description=self.description)["function"]
        parameters = cast("dict[str, object]", function.get("parameters"))
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": parameters,
            "strict": True,
        }

    def invoke(self, args: str, cancellation_event: Event | None = None) -> Invocation:
        """Validate JSON args and call the tool.

        Returns:
            An invocation that streams events and retains the result.
        """

        try:
            payload = self.args_model.model_validate_json(args, strict=True)
            output = (
                self.func(**payload.model_dump())
                if self.timeout is None
                else self._invoke_with_timeout(payload.model_dump(), self.timeout, cancellation_event)
            )
        except ValidationError as error:
            first = error.errors(include_url=False)[0]
            output = f"Tool call failed: {first['msg']}."
        except (RuntimeError, TypeError, ValueError) as error:
            output = f"Tool call failed: {error}"
        return Invocation(output)

    def _invoke_with_timeout(
        self, kwargs: dict[str, Any], timeout: float, cancellation_event: Event | None
    ) -> str | ResponseFunctionCallOutputItemListParam | Stream:
        results: Queue[str | ResponseFunctionCallOutputItemListParam | Stream | Exception] = Queue(maxsize=1)

        def invoke() -> None:
            try:
                results.put(self.func(**kwargs))
            except Exception as error:  # noqa: BLE001
                results.put(error)

        Thread(target=invoke, name=f"tool {self.name}", daemon=True).start()
        deadline = monotonic() + timeout
        while True:
            if cancellation_event and cancellation_event.is_set():
                return "Tool call cancelled."
            remaining = deadline - monotonic()
            if remaining <= 0:
                return f"Tool call failed: timed out after {timeout:g} seconds."
            try:
                result = results.get(timeout=min(remaining, 0.1))
            except Empty:
                continue
            if isinstance(result, Exception):
                raise result
            return result
