"""Example calculator tool."""

from __future__ import annotations

import ast
import operator
from typing import Any

from framework.tool.function_tool import ToolError, function_tool

_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


@function_tool(
    name_override="example-calculator",
    description_override="Evaluate a simple arithmetic expression.",
)
async def calculate(expression: str) -> dict[str, Any]:
    """Evaluate a simple arithmetic expression."""
    return {"expression": expression, "result": _eval_expression(expression)}


def _eval_expression(expression: str) -> int | float:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"Invalid arithmetic expression: {expression!r}") from exc
    return _eval_node(tree.body)


def _eval_node(node: ast.AST) -> int | float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_eval_node(node.operand))
    raise ToolError("Only numeric arithmetic expressions are supported")
