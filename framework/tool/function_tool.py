"""``function_tool`` decorator and ``function_schema``: turn a typed Python async
function into a callable tool, deriving its JSON Schema from the signature's type
hints and docstring, with optional failure-to-message error handling.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel, create_model
from pydantic import Field as PydanticField


@dataclass
class FuncSchema:
    """Derived schema for a Python function to be used as a tool."""

    name: str
    description: str | None = None
    params_json_schema: dict[str, Any] = field(default_factory=dict)
    takes_context: bool = False


_ToolFunc = Callable[..., Awaitable[Any]]


class ToolError(Exception):
    """Non-fatal tool error returned to the caller instead of crashing the run."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


ToolFailureHandler = Callable[[Exception, dict[str, Any]], Awaitable[str]]
"""A function that receives (exception, input_data) and returns an error message string."""


async def _call_maybe_awaitable(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _default_failure_handler(error: Exception, input_data: dict[str, Any]) -> str:
    return f"Tool error: {type(error).__name__}: {error}"


def _build_pydantic_model(func: Callable[..., Any], skip_context: bool = False) -> type[BaseModel]:
    hints = get_type_hints(func, include_extras=True)
    fields: dict[str, tuple[type[Any], Any]] = {}
    sig = inspect.signature(func)
    for idx, (param_name, param) in enumerate(sig.parameters.items()):
        if skip_context and idx == 0:
            continue
        if param_name == "self":
            continue
        annotation = hints.get(param_name, Any)
        default = ... if param.default is inspect.Parameter.empty else param.default
        description = None
        if get_origin(annotation) is Annotated:
            args = get_args(annotation)
            annotation = args[0]
            for meta in args[1:]:
                if isinstance(meta, str):
                    description = meta
        field_info = PydanticField(default=default, description=description)
        fields[param_name] = (annotation, field_info)
    model_name = f"{func.__name__.title().replace('_', '')}Params"
    return create_model(model_name, **fields)  # type: ignore[call-overload]


def function_schema(
    func: Callable[..., Any], *, name_override: str | None = None, description_override: str | None = None
) -> FuncSchema:
    """Extract FunctionSchema from a Python function's signature and docstring."""
    name = name_override or func.__name__
    description = description_override or inspect.getdoc(func)
    sig = inspect.signature(func)
    params = list(sig.parameters.items())
    takes_context = bool(params and params[0][0] in ("context", "ctx"))
    pydantic_model = _build_pydantic_model(func, skip_context=takes_context)
    schema = pydantic_model.model_json_schema()
    schema.pop("title", None)
    props = schema.get("properties", {})
    if not props:
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
    else:
        schema["additionalProperties"] = False
    return FuncSchema(
        name=name,
        description=description,
        params_json_schema=schema,
        takes_context=takes_context,
    )


def function_tool(
    *,
    name_override: str | None = None,
    description_override: str | None = None,
    failure_error_function: ToolFailureHandler | None = None,
) -> Callable[[_ToolFunc], _ToolFunc]:
    """Decorator that registers a Python async function as a callable tool.

    Automatically generates a JSON Schema from the function's type hints and docstring.
    Optionally provides a failure handler that converts exceptions into error strings
    instead of crashing the run.

    Usage::

        @function_tool(name_override="fetch_order", failure_error_function=my_handler)
        async def fetch_order(order_id: str, include_history: bool = False) -> dict:
            \"""Fetch order details by ID.\"""
            ...

    The decorated function keeps a ``__tool_schema__`` attribute with the derived schema.
    """

    def decorator(func: _ToolFunc) -> _ToolFunc:
        schema = function_schema(
            func,
            name_override=name_override,
            description_override=description_override,
        )
        func.__tool_name__ = schema.name  # type: ignore[attr-defined]
        func.__tool_description__ = schema.description  # type: ignore[attr-defined]
        func.__tool_schema__ = schema  # type: ignore[attr-defined]
        func.__tool_failure_handler__ = failure_error_function  # type: ignore[attr-defined]

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except ToolError:
                raise
            except Exception as exc:
                handler = getattr(func, "__tool_failure_handler__", None) or _default_failure_handler
                input_data: dict[str, Any] = {}
                if args:
                    input_data["args"] = [str(a) for a in args]
                input_data.update(kwargs)
                error_msg = await _call_maybe_awaitable(handler, exc, input_data)
                raise ToolError(error_msg) from exc

        wrapper.__tool_name__ = schema.name  # type: ignore[attr-defined]
        wrapper.__tool_description__ = schema.description  # type: ignore[attr-defined]
        wrapper.__tool_schema__ = schema  # type: ignore[attr-defined]
        wrapper.__tool_failure_handler__ = failure_error_function  # type: ignore[attr-defined]
        wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator
