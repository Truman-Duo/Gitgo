"""Bounded workbench arithmetic; shared by the current and legacy tool adapters."""
from __future__ import annotations

import ast
import math
import operator
from typing import Any

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def safe_calculate(expression: str) -> int | float | bool:
    """Evaluate a bounded arithmetic/comparison expression without ``eval``."""
    expression = str(expression).strip()
    if not expression or len(expression) > 500:
        raise ValueError("expression must contain 1..500 characters")
    tree = ast.parse(expression, mode="eval")

    def visit(node: ast.AST, depth: int = 0) -> int | float | bool:
        if depth > 32:
            raise ValueError("expression is too deeply nested")
        if isinstance(node, ast.Expression):
            return visit(node.body, depth + 1)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return node.value
            if isinstance(node.value, (int, float)) and math.isfinite(float(node.value)):
                if abs(float(node.value)) > 1e100:
                    raise ValueError("numeric literal is too large")
                return node.value
            raise ValueError("only finite numeric constants are allowed")
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _bounded(_UNARY[type(node.op)](visit(node.operand, depth + 1)))
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left = visit(node.left, depth + 1)
            right = visit(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and abs(float(right)) > 100:
                raise ValueError("power exponent exceeds the safety bound")
            return _bounded(_BINARY[type(node.op)](left, right))
        if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators):
            left = visit(node.left, depth + 1)
            for operation, comparator in zip(node.ops, node.comparators):
                function = _COMPARE.get(type(operation))
                if function is None:
                    raise ValueError("unsupported comparison")
                right = visit(comparator, depth + 1)
                if not function(left, right):
                    return False
                left = right
            return True
        raise ValueError(f"unsupported expression element: {type(node).__name__}")

    return visit(tree)


def _bounded(value: Any) -> int | float | bool:
    if isinstance(value, bool):
        return value
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("calculation produced a non-finite result")
    if abs(float(value)) > 1e100:
        raise ValueError("calculation result exceeds the safety bound")
    return value


