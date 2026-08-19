"""受限公式解析与求值（需求 §6.5）。

第一阶段支持:
  - 加减乘除 (+, -, *, /)
  - 括号
  - 参数引用 (identifier → 从 params 字典取值)
  - 单位后缀 (mm / cm / m / deg / g / kg / s)
  - 条件表达式 (a if cond else b)

禁止使用 Python eval/exec 解析用户表达式 —— 本模块基于 AST 白名单。
"""

from __future__ import annotations

import ast
import re
from typing import Any, Optional

# 单位 → 基准（mm, 度, 克）
_UNIT_FACTORS = {
    "mm": 1.0, "cm": 10.0, "m": 1000.0,
    "um": 0.001, "micron": 0.001,
    "deg": 1.0, "degree": 1.0, "°": 1.0, "rad": 57.2958,
    "g": 1.0, "kg": 1000.0,
    "s": 1.0, "sec": 1.0, "min": 60.0,
}

_NUMBER_RE = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?([a-zA-Zµ°]+)?$")

_ALLOWED_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}
_ALLOWED_UNARY = {ast.UAdd: lambda a: a, ast.USub: lambda a: -a}
_ALLOWED_COMPARE = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}

# 预处理: "3 mm" / "3mm" 这类带单位字面量在 Python AST 中非法,
# 需在 parse 前归一为基准单位数值。来自用户文本的公式单位后缀统一换算。
_UNIT_IN_EXPR_RE = re.compile(
    r"(?<![\.\w])(\d+(?:\.\d+)?)\s*([a-zA-Zµ°]{1,8})(?![\.\w])"
)


def _normalize_unit_literals(expression: str) -> str:
    def _repl(m):
        num = float(m.group(1))
        unit = m.group(2)
        factor = _UNIT_FACTORS.get(unit)
        if factor is None:
            return m.group(0)  # 非已知单位, 交给 AST 判定(多半是参数名)
        return repr(num * factor)
    return _UNIT_IN_EXPR_RE.sub(_repl, expression)


def _parse_literal(token: str) -> float:
    """解析带单位的字面量，如 '3 mm' → 3.0（基准单位）。"""
    m = _NUMBER_RE.match(token.strip())
    if not m:
        raise ValueError(f"invalid number literal: {token!r}")
    num = float(m.group(1))
    unit = (m.group(3) or "").strip()
    factor = _UNIT_FACTORS.get(unit)
    if unit and factor is None:
        raise ValueError(f"unknown unit: {unit!r}")
    return num * (factor or 1.0)


def _eval_node(node: ast.AST, params: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, params)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node.value, str):
            return _parse_literal(node.value)
        raise ValueError(f"unsupported constant: {node.value!r}")
    if isinstance(node, ast.Name):
        name = node.id
        if name not in params:
            raise ValueError(f"unknown parameter: {name!r}")
        val = params[name]
        if isinstance(val, str):
            return _parse_literal(val)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(f"parameter {name!r} is not numeric: {val!r}")
        return float(val)
    if isinstance(node, ast.BinOp):
        fn = _ALLOWED_BINOPS.get(type(node.op))
        if fn is None:
            raise ValueError(f"operator not allowed: {type(node.op).__name__}")
        left = _eval_node(node.left, params)
        right = _eval_node(node.right, params)
        if isinstance(left, bool) or not isinstance(left, (int, float)):
            raise ValueError("left operand not numeric")
        if isinstance(right, bool) or not isinstance(right, (int, float)):
            raise ValueError("right operand not numeric")
        return fn(left, right)
    if isinstance(node, ast.UnaryOp):
        fn = _ALLOWED_UNARY.get(type(node.op))
        if fn is None:
            raise ValueError(f"unary operator not allowed: {type(node.op).__name__}")
        return fn(_eval_node(node.operand, params))
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise ValueError("chained comparisons not supported")
        fn = _ALLOWED_COMPARE.get(type(node.ops[0]))
        if fn is None:
            raise ValueError(f"comparison not allowed: {type(node.ops[0]).__name__}")
        left = _eval_node(node.left, params)
        right = _eval_node(node.comparators[0], params)
        if isinstance(left, bool) or not isinstance(left, (int, float)):
            raise ValueError("left operand not numeric")
        if isinstance(right, bool) or not isinstance(right, (int, float)):
            raise ValueError("right operand not numeric")
        return fn(left, right)
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise ValueError("bool op not allowed")
        results = [_eval_node(v, params) for v in node.values]
        if all(r is True for r in results) if isinstance(node.op, ast.And) else any(r is True for r in results):
            return True
        return False
    if isinstance(node, ast.IfExp):
        cond = _eval_node(node.test, params)
        if cond is True:
            return _eval_node(node.body, params)
        if cond is False:
            return _eval_node(node.orelse, params)
        # 数值条件按非零处理 → 简化：仅接受 Bool
        raise ValueError("if-expression condition must be boolean")
    raise ValueError(f"unsupported AST node: {type(node).__name__}")


def evaluate_expression(expression: str, params: dict[str, Any]) -> Any:
    """求值公式。表达式必须来自 AST 白名单；任何异常向上抛出。"""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("empty expression")
    normalized = _normalize_unit_literals(expression)
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"syntax error: {exc}") from exc
    return _eval_node(tree, params)


def extract_parameter_refs(expression: str) -> list[str]:
    """从公式中提取引用的参数名（供依赖分析/影响分析用）。"""
    refs: list[str] = []
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return refs
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id not in refs:
                refs.append(node.id)
    return refs


def is_safe_expression(expression: str) -> bool:
    """是否可通过 AST 白名单校验（语法层面）。"""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return False
    try:
        _eval_node(tree, {})  # 空参数表,仅用于节点类型校验
        return True
    except (ValueError, KeyError):
        # 引用未知参数等情况不应影响“语法安全”，但为白名单保守起见:
        # 只有参数名缺失导致的错误才视为语法安全
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Expression, ast.Constant, ast.Name, ast.BinOp,
                                     ast.UnaryOp, ast.Compare, ast.BoolOp, ast.IfExp,
                                     ast.Add, ast.Sub, ast.Mult, ast.Div,
                                     ast.UAdd, ast.USub, ast.Eq, ast.NotEq, ast.Lt,
                                     ast.LtE, ast.Gt, ast.GtE, ast.And, ast.Or)):
                return False
        return True