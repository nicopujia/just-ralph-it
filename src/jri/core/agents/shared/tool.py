import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import ParamSpec, Self, TypeVar, cast, get_type_hints

from openai import pydantic_function_tool
from openai.types.responses import FunctionToolParam, ResponseFunctionCallOutputItemListParam
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

Params = ParamSpec("Params")
Return = TypeVar("Return")
Output = str | ResponseFunctionCallOutputItemListParam

_DESCRIPTION_ATTR = "__jri_tool_description__"


def tool(description: str) -> Callable[[Callable[Params, Return]], Callable[Params, Return]]:
    """Mark a method as an agent tool.

    The tool name is inferred from the decorated function name.
    `Tool.discover` discovers these methods on `Agent`
    subclasses.

    Returns:
        A decorator that attaches tool metadata to the function.
    """

    def mark_as_tool(func: Callable[Params, Return]) -> Callable[Params, Return]:
        setattr(func, _DESCRIPTION_ATTR, description)
        return func

    return mark_as_tool


@dataclass(frozen=True)
class Tool:
    """Runtime wrapper for an `@tool`-decorated callable."""

    name: str
    description: str
    func: Callable[..., Output]
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
            if not (description := getattr(wrapped, _DESCRIPTION_ATTR, None)):
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
            tools.append(cls(name=func.__name__, description=description, func=func, args_model=args_model))
        return tools

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

    def invoke(self, args: str) -> Output:
        """Validate JSON args and call the tool.

        Returns:
            The tool result, or a user-facing error string.
        """

        try:
            payload = self.args_model.model_validate_json(args, strict=True)
            return self.func(**payload.model_dump())
        except ValidationError as error:
            first = error.errors(include_url=False)[0]
            return f"Tool call failed: {first['msg']}."
        except (RuntimeError, TypeError, ValueError) as error:
            return f"Tool call failed: {error}"
