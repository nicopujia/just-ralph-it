import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self, get_type_hints

from openai import pydantic_function_tool
from openai.types.responses import FunctionToolParam
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

_DESCRIPTION_ATTR = "__jri_tool_description__"
_RUNNING_LABEL_ATTR = "__jri_tool_running_label__"
_FINISHED_LABEL_ATTR = "__jri_tool_finished_label__"


def tool(
    description: str, *, running_label: str, finished_label: str
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Mark a method as an agent tool.

    The tool name is inferred from the decorated function name.
    `Tool.get_list_from_owner` discovers these methods on `Agent`
    subclasses.

    Returns:
        A decorator that attaches tool metadata to the function.
    """

    def mark_as_tool(func: Callable[..., str]) -> Callable[..., str]:
        setattr(func, _DESCRIPTION_ATTR, description)
        setattr(func, _RUNNING_LABEL_ATTR, running_label)
        setattr(func, _FINISHED_LABEL_ATTR, finished_label)
        return func

    return mark_as_tool


@dataclass(frozen=True)
class Tool:
    """Runtime wrapper for an `@tool`-decorated callable."""

    name: str
    description: str
    running_label: str
    finished_label: str
    func: Callable[..., object]
    args_model: type[BaseModel]

    @classmethod
    def get_list_from_owner(cls, owner: object) -> list[Self]:
        """Discover every `@tool` method available on `owner`.

        Returns:
            Tools found on the owner and its base classes.
        """

        tools: list[Self] = []
        seen: set[str] = set()
        for owner_cls in type(owner).__mro__[:-1]:
            for attr in owner_cls.__dict__:
                if attr in seen:
                    continue
                seen.add(attr)
                if tool_obj := cls.get_obj_from_callback(getattr(owner, attr, None)):
                    tools.append(tool_obj)
        return tools

    @classmethod
    def get_obj_from_callback(cls, func: Callable[..., object] | object) -> Self | None:
        """Build a `Tool` from a ``@tool``-decorated callable.

        Returns:
            A tool instance, or `None` for undecorated callbacks.

        Raises:
            TypeError: If tool parameter annotations are unsupported.
        """

        if not callable(func):
            return None

        wrapped = getattr(func, "__func__", func)
        description = getattr(wrapped, _DESCRIPTION_ATTR, None)
        if not isinstance(description, str):
            return None
        running_label = getattr(wrapped, _RUNNING_LABEL_ATTR, None)
        finished_label = getattr(wrapped, _FINISHED_LABEL_ATTR, None)
        if not isinstance(running_label, str) or not isinstance(finished_label, str):
            raise TypeError(f"Tool `{func.__name__}` is missing display labels.")

        try:
            annotations = get_type_hints(wrapped, include_extras=True)
        except Exception as error:
            raise TypeError(f"Tool `{func.__name__}` has unsupported parameter annotations: {error}") from error

        fields: dict[str, tuple[object, object]] = {}
        for param in inspect.signature(func).parameters.values():
            if param.kind not in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}:
                raise TypeError(
                    f"Tool `{func.__name__}` parameter `{param.name}` must be callable as a keyword argument."
                )
            if param.default is not inspect.Parameter.empty:
                raise TypeError(
                    f"Tool `{func.__name__}` parameter `{param.name}` "
                    "must not define a Python default. Use `T | None` "
                    "for nullable tool input."
                )
            annotation = annotations.get(param.name, param.annotation)
            fields[param.name] = (str if annotation is inspect.Parameter.empty else annotation, ...)

        try:
            args_model = create_model(
                f"{func.__name__.title()}Args",
                __config__=ConfigDict(extra="forbid"),
                # `create_model` accepts this dynamic field map at
                # runtime; pyright cannot type the unpacked shape.
                **fields,  # pyright: ignore[reportCallIssue, reportArgumentType]
            )
        except Exception as error:
            raise TypeError(f"Tool `{func.__name__}` has unsupported parameter annotations: {error}") from error

        return cls(
            name=func.__name__,
            description=description,
            running_label=running_label,
            finished_label=finished_label,
            func=func,
            args_model=args_model,
        )

    @property
    def definition(self) -> FunctionToolParam:
        """OpenAI Responses API function-tool definition.

        Raises:
            TypeError: If the SDK omits the generated parameters schema.
        """

        function = pydantic_function_tool(self.args_model, name=self.name, description=self.description)["function"]
        if (parameters := function.get("parameters")) is None:
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
            return str(self.func(**payload.model_dump()))
        except ValidationError as error:
            first = error.errors(include_url=False)[0]
            parts: list[str] = []
            for part in first["loc"]:
                if isinstance(part, int) and parts:
                    parts[-1] += f"[{part}]"
                elif isinstance(part, int):
                    parts.append(f"[{part}]")
                else:
                    parts.append(str(part))

            if location := ".".join(parts):
                reason = f"Invalid argument `{location}`: {first['msg']}."
            else:
                reason = f"Invalid arguments for `{self.name}`: " + f"{first['msg']}."
            return f"Tool call failed: {reason}"
        except (RuntimeError, TypeError, ValueError) as error:
            return f"Tool call failed: {error}"
