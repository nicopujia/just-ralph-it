import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self, cast, get_type_hints

from openai import pydantic_function_tool
from openai.types.responses import FunctionToolParam
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

type ToolArgsModel = type[BaseModel]

_META_ATTR = "__jri_tool_metadata__"
"""Attribute name used to store `ToolMetadata` on decorated funcs."""


def tool(
    description: str,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Mark a method as an agent tool.

    The tool name is inferred from the decorated function name.
    `Tool.get_list_from_owner` discovers these methods on `Agent`
    subclasses.

    Returns:
        A decorator that attaches tool metadata to the function.
    """

    def mark_as_tool(func: Callable[..., str]) -> Callable[..., str]:
        meta = ToolMetadata(description=description)
        setattr(func, _META_ATTR, meta)
        return func

    return mark_as_tool


@dataclass(frozen=True)
class ToolMetadata:
    description: str


@dataclass(frozen=True)
class Tool:
    """Runtime wrapper for an `@tool`-decorated callable."""

    name: str
    description: str
    func: Callable[..., str]
    args_model: ToolArgsModel

    @classmethod
    def get_list_from_owner(cls, owner: object) -> list[Self]:
        """Discover every `@tool` method available on `owner`.

        Returns:
            Tools found on the owner and its base classes.
        """

        tools: list[Self] = []
        seen: set[str] = set()
        for owner_cls in type(owner).__mro__:
            if owner_cls is object:
                continue
            for attr in owner_cls.__dict__:
                if attr in seen:
                    continue
                seen.add(attr)
                tool_obj = cls.get_obj_from_callback(
                    getattr(owner, attr, None),
                )
                if tool_obj:
                    tools.append(tool_obj)
        return tools

    @classmethod
    def get_obj_from_callback(
        cls,
        func: Callable[..., object] | object,
    ) -> Self | None:
        """Build a `Tool` from a ``@tool``-decorated callable.

        Returns:
            A tool instance, or `None` for undecorated callbacks.

        Raises:
            TypeError: If tool parameter annotations are unsupported.
        """

        if not callable(func):
            return None

        wrapped = getattr(func, "__func__", func)
        meta = getattr(wrapped, _META_ATTR, None)
        if not isinstance(meta, ToolMetadata):
            return None

        tool_func = cast("Callable[..., str]", func)
        try:
            annotations = cast(
                "dict[str, object]",
                get_type_hints(wrapped, include_extras=True),
            )
        except Exception as error:
            raise TypeError(
                "Tool "
                + f"`{tool_func.__name__}` has unsupported parameter "
                + f"annotations: {error}",
            ) from error

        fields: dict[str, tuple[object, object]] = {}
        for param in inspect.signature(tool_func).parameters.values():
            if param.kind not in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }:
                raise TypeError(
                    f"Tool `{tool_func.__name__}` parameter `{param.name}` "
                    + "must be callable as a keyword argument.",
                )
            default = cast("object", param.default)
            if default is not inspect.Parameter.empty:
                raise TypeError(
                    f"Tool `{tool_func.__name__}` parameter `{param.name}` "
                    + "must not define a Python default. Use `T | None` "
                    + "for nullable tool input.",
                )
            annotation = annotations.get(param.name)
            if annotation is None:
                annotation = cast("object", param.annotation)
            fields[param.name] = (
                str if annotation is inspect.Parameter.empty else annotation,
                ...,
            )

        try:
            args_model = cast(
                "ToolArgsModel",
                create_model(
                    f"{tool_func.__name__.title()}Args",
                    __config__=ConfigDict(extra="forbid"),
                    # `create_model` accepts this dynamic field map at
                    # runtime; pyright cannot type the unpacked shape.
                    **fields,  # pyright: ignore[reportCallIssue, reportArgumentType]
                ),
            )
        except Exception as error:
            raise TypeError(
                "Tool "
                + f"`{tool_func.__name__}` has unsupported parameter "
                + f"annotations: {error}",
            ) from error

        return cls(
            name=tool_func.__name__,
            description=meta.description,
            func=tool_func,
            args_model=args_model,
        )

    @property
    def definition(self) -> FunctionToolParam:
        """OpenAI Responses API function-tool definition.

        Raises:
            TypeError: If the SDK omits the generated parameters schema.
        """

        function = pydantic_function_tool(
            self.args_model,
            name=self.name,
            description=self.description,
        )["function"]
        parameters = function.get("parameters")
        if parameters is None:
            raise TypeError(f"Tool `{self.name}` has no parameters schema.")
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": parameters,
            "strict": True,
        }

    def invoke(self, args: str) -> str:
        """Validate JSON args and call the tool.

        Returns:
            The tool result, or a user-facing error string.
        """

        try:
            payload = self.args_model.model_validate_json(args, strict=True)
            return self.func(**payload.model_dump())
        except ValidationError as error:
            first = error.errors(include_url=False)[0]
            parts: list[str] = []
            for part in cast("tuple[object, ...]", first["loc"]):
                if isinstance(part, int) and parts:
                    parts[-1] += f"[{part}]"
                elif isinstance(part, int):
                    parts.append(f"[{part}]")
                else:
                    parts.append(str(part))

            location = ".".join(parts)
            if location:
                reason = f"Invalid argument `{location}`: {first['msg']}."
            else:
                reason = (
                    f"Invalid arguments for `{self.name}`: "
                    + f"{first['msg']}."
                )
            return f"Tool call failed: {reason}"
        except (RuntimeError, TypeError, ValueError) as error:
            return f"Tool call failed: {error}"
