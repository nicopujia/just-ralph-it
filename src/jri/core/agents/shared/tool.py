import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Self, cast

from openai.types.responses import FunctionToolParam

type JsonSchemaType = Literal["string", "integer", "number", "boolean"]
type RuntimeType = type[str | int | float | bool]

_JSON_TYPE_BY_RUNTIME_TYPE: dict[RuntimeType, JsonSchemaType] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}

# Attribute name used to store `ToolMetadata` on decorated funcs.
_META_ATTR: str = "__jri_tool_metadata__"


def tool(
    description: str,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Decorator that marks a method as an agent tool.

    The tool name is always inferred from the decorated function name.

    Tools are discovered automatically by `Tool.from_owner` on
    `Agent` subclasses via the `TOOL_META_ATTR` attribute.

    Returns:
        A decorator that attaches `ToolMetadata` to the function.
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
class ToolParameter:
    name: str
    schema_type: JsonSchemaType
    runtime_type: RuntimeType
    has_default: bool

    @classmethod
    def from_signature_parameter(
        cls,
        tool_name: str,
        param: inspect.Parameter,
    ) -> Self:
        """Build tool metadata from a callable signature parameter.

        If a parameter has no annotation, it defaults to `str`.

        Returns:
            Parsed tool parameter metadata.

        Raises:
            TypeError: If the parameter kind or annotation is
                unsupported.
        """
        if param.kind not in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            msg = (
                f"Tool `{tool_name}` parameter `{param.name}`"
                " must be callable as a keyword argument."
            )
            raise TypeError(msg)

        annotation = cast("object", param.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = str
        if annotation not in _JSON_TYPE_BY_RUNTIME_TYPE:
            msg = (
                f"Tool `{tool_name}` parameter `{param.name}`"
                " must be annotated as str, int, float, or bool."
            )
            raise TypeError(msg)

        runtime_type = cast("RuntimeType", annotation)
        default = cast("object", param.default)
        return cls(
            name=param.name,
            schema_type=_JSON_TYPE_BY_RUNTIME_TYPE[runtime_type],
            runtime_type=runtime_type,
            has_default=default is not inspect.Parameter.empty,
        )

    def validate(self, value: object) -> str | None:
        if self.runtime_type is str:
            if isinstance(value, str) and value.strip():
                return None
            return f"Missing required string argument `{self.name}`."

        if self.runtime_type is int:
            if isinstance(value, int) and not isinstance(value, bool):
                return None

        elif self.runtime_type is float:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return None

        elif self.runtime_type is bool and isinstance(value, bool):
            return None

        return (
            f"Invalid type for argument `{self.name}`; "
            f"expected {self.runtime_type.__name__}."
        )

    @property
    def definition(self) -> dict[str, object]:
        schema_type: object = self.schema_type
        if self.has_default:
            schema_type = [self.schema_type, "null"]
        return {"type": schema_type}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: tuple[ToolParameter, ...]
    func: Callable[..., str]

    @classmethod
    def get_list_from_owner(cls, owner: object) -> list[Self]:
        """Discover tools from an object's ``@tool``-decorated methods.

        Walks the MRO to collect every method tagged with ``@tool``.

        Returns:
            List of `Tool` instances found on the owner.
        """
        tools: list[Self] = []
        seen_attr_names: set[str] = set()
        for owner_cls in type(owner).__mro__:
            if owner_cls is object:
                continue
            for attr_name in owner_cls.__dict__:
                if attr_name in seen_attr_names:
                    continue
                seen_attr_names.add(attr_name)
                func = getattr(owner, attr_name, None)
                if not callable(func):
                    continue
                tool = cls.get_obj_from_callback(func)
                if tool is not None:
                    tools.append(tool)
        return tools

    @classmethod
    def get_obj_from_callback(
        cls,
        func: Callable[..., object],
    ) -> Self | None:
        """Build a `Tool` from a ``@tool``-decorated callable.

        Returns:
            A `Tool` instance, or `None` if the callable lacks
            `ToolMetadata`.
        """
        wrapped = getattr(func, "__func__", func)
        meta = getattr(wrapped, _META_ATTR, None)
        if not isinstance(meta, ToolMetadata):
            return None
        tool_func = cast("Callable[..., str]", func)
        parameters = tuple(
            ToolParameter.from_signature_parameter(tool_func.__name__, param)
            for param in inspect.signature(tool_func).parameters.values()
        )
        return cls(
            name=tool_func.__name__,
            description=meta.description,
            parameters=parameters,
            func=tool_func,
        )

    @property
    def definition(self) -> FunctionToolParam:
        """OpenAI function-tool definition for the Responses API."""
        properties: dict[str, object] = {
            param.name: param.definition for param in self.parameters
        }
        required = [param.name for param in self.parameters]
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            "strict": True,
        }

    def invoke(self, args: str) -> str:
        """Parse JSON `args` and call the underlying function.

        Returns:
            The function result, or an error string on invalid/missing
            arguments.
        """
        try:
            obj = cast("object", json.loads(args))
        except json.JSONDecodeError:
            obj = None
        if not isinstance(obj, dict):
            return f"Invalid arguments for `{self.name}`."

        payload = cast("dict[str, object]", obj)
        expected_names = {param.name for param in self.parameters}
        unexpected_arg = next(
            (
                arg_name
                for arg_name in payload
                if arg_name not in expected_names
            ),
            None,
        )
        if unexpected_arg is not None:
            return f"Unexpected argument `{unexpected_arg}`."

        kwargs: dict[str, object] = {}
        for param in self.parameters:
            if param.name not in payload:
                return f"Missing required argument `{param.name}`."

            value = payload[param.name]
            if value is None:
                if param.has_default:
                    continue
                return f"Missing required argument `{param.name}`."

            error = param.validate(value)
            if error is not None:
                return error
            kwargs[param.name] = value
        return self.func(**kwargs)
