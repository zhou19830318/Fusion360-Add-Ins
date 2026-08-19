"""DesignSpec JSON Schema 定义与校验（需求 §4 & §14）。

实现说明：**不依赖第三方 jsonschema 库**。
原因：jsonschema → referencing → rpds 的 C 扩展与 Fusion 内嵌解释器(3.14)不兼容，
会导致可靠性层在 Fusion 内 import 失败（ModuleNotFoundError: rpds.rpds）。
本模块提供纯标准库的轻量校验器，覆盖 MVP 用到的 Schema 关键字：
  type / enum / required / properties / additionalProperties /
  pattern / minimum / maximum / items / const
并可嵌套。接口保持: validate_intent / validate_llm_intent 返回
[{"path": str, "message": str, ...}] 错误列表。
"""

from __future__ import annotations

import copy
import re
from typing import Any, Optional

from .design_spec import PARAMETER_SOURCES, PARAMETER_STATUSES, CONSTRAINT_PRIORITIES


# ---------------------------------------------------------------------------
# 轻量 Schema 校验器（纯标准库）
# ---------------------------------------------------------------------------

_TRUE_TYPES = (int, float, bool, str, list, dict, type(None))

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _type_ok(instance: Any, tspec: Any) -> bool:
    """type 字段支持单个字符串或数组（任一类型匹配即通过）。"""
    if isinstance(tspec, list):
        return any(_type_ok(instance, t) for t in tspec)
    check = _TYPE_CHECKS.get(tspec)
    if check is None:
        return True  # 未知类型关键词: 不阻塞
    return check(instance)


class MiniValidator:
    """覆盖 MVP 需求的 JSON Schema 子集校验器。"""

    def __init__(self, schema: dict) -> None:
        self.schema = schema

    def iter_errors(self, instance: Any, schema: Optional[dict] = None,
                    path: str = "") -> list[dict]:
        schema = schema if schema is not None else self.schema
        errors: list[dict] = []

        # type（可能是数组）
        if "type" in schema:
            if not _type_ok(instance, schema["type"]):
                wanted = schema["type"] if isinstance(schema["type"], str) else "/".join(schema["type"])
                errors.append({"path": path or "$",
                               "message": f"{path or '$'} 应为 {wanted}, 实际是 {_typename(instance)}",
                               "schema_path": f"{path or '$'}/type"})

        # enum
        if "enum" in schema and instance not in schema["enum"]:
            errors.append({"path": path or "$",
                           "message": f"{path or '$'} 值 {instance!r} 不在允许枚举内: {schema['enum']}",
                           "schema_path": f"{path or '$'}/enum"})

        # const
        if "const" in schema and instance != schema["const"]:
            errors.append({"path": path or "$",
                           "message": f"{path or '$'} 必须等于 {schema['const']!r}",
                           "schema_path": f"{path or '$'}/const"})

        # number 边界
        if isinstance(instance, (int, float)) and not isinstance(instance, bool):
            if "minimum" in schema and instance < schema["minimum"]:
                errors.append({"path": path or "$",
                               "message": f"{path or '$'} 值 {instance} 低于下限 {schema['minimum']}",
                               "schema_path": f"{path or '$'}/minimum"})
            if "maximum" in schema and instance > schema["maximum"]:
                errors.append({"path": path or "$",
                               "message": f"{path or '$'} 值 {instance} 高于上限 {schema['maximum']}",
                               "schema_path": f"{path or '$'}/maximum"})

        # string pattern
        if isinstance(instance, str):
            tspec = schema.get("type")
            if isinstance(tspec, str) and tspec in ("string",) or (isinstance(tspec, list) and "string" in (tspec or [])):
                pat = schema.get("pattern")
                if pat:
                    try:
                        if re.search(pat, instance) is None:
                            errors.append({"path": path or "$",
                                           "message": f"{path or '$'} 字符串 {instance!r} 不匹配模式 {pat!r}",
                                           "schema_path": f"{path or '$'}/pattern"})
                    except re.error:
                        pass

        # object
        if isinstance(instance, dict):
            tspec = schema.get("type")
            is_obj = (isinstance(tspec, str) and tspec == "object") or \
                     (isinstance(tspec, list) and "object" in (tspec or []))
            if schema.get("type") is None or schema.get("type") == {} or is_obj:
                props = schema.get("properties", {})
                required = schema.get("required", [])
                for r in required:
                    if r not in instance:
                        errors.append({"path": f"{path}.{r}" if path else r,
                                       "message": f"缺少必填字段: {r}",
                                       "schema_path": f"{path or '$'}/required"})
                for key, value in instance.items():
                    child = props.get(key)
                    if child is not None:
                        errors.extend(self.iter_errors(
                            value, child, f"{path}.{key}" if path else key))
                    elif schema.get("additionalProperties") is False:
                        errors.append({"path": f"{path}.{key}" if path else key,
                                       "message": f"未定义的额外字段: {key}",
                                       "schema_path": f"{path or '$'}/additionalProperties"})

        # array items
        if isinstance(instance, list):
            tspec = schema.get("type")
            is_arr = (isinstance(tspec, str) and tspec == "array") or \
                     (isinstance(tspec, list) and "array" in (tspec or []))
            if is_arr:
                items_schema = schema.get("items")
                if isinstance(items_schema, dict):
                    for i, item in enumerate(instance):
                        errors.extend(self.iter_errors(
                            item, items_schema, f"{path}[{i}]" if path else f"[{i}]"))

        return errors


def _typename(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    return type(v).__name__


# ---------------------------------------------------------------------------
# 子 Schema 定义（结构同上轮依赖 jsonschema 的版本）
# ---------------------------------------------------------------------------

def parameter_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "name", "value"],
        "properties": {
            "id": {"type": "string", "pattern": r"^param_|^[a-zA-Z_][a-zA-Z0-9_]*$"},
            "name": {"type": "string", "pattern": r"^[a-zA-Z_][a-zA-Z0-9_]*$"},
            "label": {"type": "string"},
            "value": {},
            "expression": {"type": ["string", "null"]},
            "unit": {"type": "string"},
            "type": {"type": "string", "enum": ["length", "angle", "mass", "count", "scalar", "time", "other"]},
            "source": {"type": "string", "enum": list(PARAMETER_SOURCES)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "editable": {"type": "boolean"},
            "locked": {"type": "boolean"},
            "required": {"type": "boolean"},
            "min": {"type": ["number", "null"]},
            "max": {"type": ["number", "null"]},
            "affects": {"type": "array", "items": {"type": "string"}},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": list(PARAMETER_STATUSES)},
        },
    }


def feature_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "type", "label"],
        "properties": {
            "id": {"type": "string", "pattern": r"^feature_" + "[a-zA-Z0-9_]+"},
            "type": {"type": "string"},
            "label": {"type": "string"},
            "semantic_role": {"type": "string"},
            "depends_on": {"type": "array", "items": {"type": "string"}},
            "parameters": {"type": "object"},
            "strategy": {"type": "string",
                          "enum": ["native_fusion_feature", "sketch_driven", "boolean_geometry", "script"]},
            "optional": {"type": "boolean"},
            "enabled": {"type": "boolean"},
            "risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "requires_confirmation": {"type": "boolean"},
        },
    }


def constraint_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "expression"],
        "properties": {
            "id": {"type": "string"},
            "type": {"type": "string"},
            "expression": {"type": "string"},
            "priority": {"type": "string", "enum": list(CONSTRAINT_PRIORITIES)},
            "source": {"type": "string"},
            "locked": {"type": "boolean"},
            "message": {"type": "string"},
        },
    }


def clarification_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "question", "severity"],
        "properties": {
            "id": {"type": "string"},
            "question": {"type": "string"},
            "reason": {"type": "string"},
            "severity": {"type": "string", "enum": ["blocking", "default", "preference"]},
            "options": {"type": "array", "items": {"type": "object"}},
            "default": {},
            "related_features": {"type": "array", "items": {"type": "string"}},
            "answer": {},
        },
    }


def intent_schema() -> dict:
    """顶层 DesignSpec 的 JSON Schema（需求 §4.1）。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version", "intent_id", "operation", "domain",
            "goal", "environment", "parameters", "features", "constraints",
            "assumptions", "clarifications",
        ],
        "properties": {
            "schema_version": {"type": "string"},
            "intent_id": {"type": "string"},
            "operation": {"type": "string",
                          "enum": ["create", "modify", "analyze", "query", "delete", "unknown"]},
            "domain": {"type": "string"},
            "goal": {
                "type": "object",
                "additionalProperties": True,
                "required": ["summary", "user_text", "language"],
                "properties": {
                    "summary": {"type": "string"},
                    "user_text": {"type": "string"},
                    "language": {"type": "string"},
                },
            },
            "environment": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "document_units": {"type": "string"},
                    "active_component": {},
                    "selection_refs": {"type": "array", "items": {}},
                    "design_history_required": {"type": "boolean"},
                    "manufacturing_method": {
                        "type": "string",
                        "enum": ["cnc", "3d_print", "injection", "sheet_metal", "unknown", ""],
                    },
                },
            },
            "parameters": {"type": "array", "items": parameter_schema()},
            "features": {"type": "array", "items": feature_schema()},
            "constraints": {"type": "array", "items": constraint_schema()},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "clarifications": {"type": "array", "items": clarification_schema()},
            "plan": {},
            "validation": {"type": "object", "additionalProperties": True},
            "metadata": {"type": "object", "additionalProperties": True},
        },
    }


_validators: dict[str, MiniValidator] = {}


def _validator(name: str, schema: dict) -> MiniValidator:
    if name not in _validators:
        _validators[name] = MiniValidator(schema)
    return _validators[name]


def format_errors(errors: list) -> list[dict]:
    """错误归一（兼容旧接口，保证 path/message 字段存在）。"""
    out = []
    for e in errors:
        out.append({
            "path": e.get("path") or "$",
            "message": e.get("message") or "unknown error",
            "schema_path": e.get("schema_path") or "$",
        })
    return out


def validate_intent(data: dict) -> list[dict]:
    """校验完整 DesignSpec；通过返回 []。"""
    errs = _validator("intent", intent_schema()).iter_errors(data)
    errs.sort(key=lambda e: e.get("path", "$"))
    return format_errors(errs)


def validate_parameters(parameters: list) -> list[dict]:
    errs = _validator("params", {"type": "array", "items": parameter_schema()}) \
        .iter_errors(parameters)
    return format_errors(errs)


def _coerce_before_validate(data: dict) -> dict:
    """对 LLM 输出做温和容错归一（不改语义）：
    缺省 schema_version/intent_id 等顶层字段时补默认值。其余仍严格校验。
    """
    d = copy.deepcopy(data) if isinstance(data, dict) else {}
    if not isinstance(d, dict):
        return {}
    d.setdefault("schema_version", "1.0")
    d.setdefault("intent_id", "")
    d.setdefault("operation", "unknown")
    d.setdefault("domain", "mechanical_part")
    d.setdefault("parameters", [])
    d.setdefault("features", [])
    d.setdefault("constraints", [])
    d.setdefault("assumptions", [])
    d.setdefault("clarifications", [])
    if not isinstance(d.get("goal"), dict):
        d["goal"] = {"summary": "", "user_text": "", "language": "zh-CN"}
    return d


def post_validate_intent(data: dict) -> list[dict]:
    """DesignSpec 语义级后检查(超越纯 schema):

    1. confirmed/locked 参数必须带数值 value(执行链路需要真实值);
    2. feature.parameters 中以 param_ 开头的引用必须指向已存在的参数 id;
    3. 参数 id 唯一性;
    4. confidence 越界钳制后再判(由 sanitize 处理, 此处仅报告未定义值)。
    """
    errors: list[dict] = []
    param_ids: set[str] = set()
    for i, p in enumerate(data.get("parameters", [])):
        pid = p.get("id")
        if not pid:
            continue
        if pid in param_ids:
            errors.append({"path": f"parameters[{i}].id",
                           "message": f"参数 id 重复: {pid}"})
        param_ids.add(pid)
        status = p.get("status")
        val = p.get("value")
        if status in ("confirmed", "locked") and val is None:
            errors.append({
                "path": f"parameters[{i}].value",
                "message": f"参数 {p.get('name', pid)} 状态为 {status} 但缺少 value; "
                           f"请给出具体数值, 或将其状态改为 needs_confirmation",
            })
        if status in ("confirmed", "locked") and isinstance(val, str) and not str(val).strip():
            errors.append({
                "path": f"parameters[{i}].value",
                "message": f"参数 {p.get('name', pid)} 的 value 为空字符串",
            })
    # 特征参数引用解析
    for f in data.get("features", []):
        params = f.get("parameters") or {}
        for key, ref in params.items():
            if isinstance(ref, str) and ref.startswith("param_") and ref not in param_ids:
                errors.append({
                    "path": f"features/{f.get('id', '?')}.parameters.{key}",
                    "message": f"特征参数引用 {ref} 指向不存在的参数",
                })
    return errors


def sanitize_intent(data: dict) -> dict:
    """温和清洗(不改变语义):
    - confidence 钳制到 [0,1], 缺失默认 0.5;
    - confirmed/locked 但 value=None 的参数降级为 needs_confirmation
      (执行链路会跳过无值参数, 状态语义必须一致);
    - 参数缺少 status 时按 source 推断默认。
    """
    from .design_spec import PARAMETER_STATUSES
    d = copy.deepcopy(data)
    for p in d.get("parameters", []):
        conf = p.get("confidence")
        if conf is None:
            p["confidence"] = 0.5
        elif isinstance(conf, (int, float)) and not isinstance(conf, bool):
            p["confidence"] = max(0.0, min(1.0, float(conf)))
        if p.get("status") not in PARAMETER_STATUSES:
            p["status"] = "confirmed" if p.get("source") == "user" else "needs_confirmation"
        if p.get("status") in ("confirmed", "locked") and p.get("value") is None:
            p["status"] = "needs_confirmation"
    return d


def validate_llm_intent(data: dict) -> tuple[bool, Optional[dict], list[dict]]:
    """LLM 输出 Intent 的校验入口。

    返回 (ok, coerced_data, errors)：
    - ok=True 时 coerced_data 为补全后的 dict；
    - ok=False 时 errors 为可读错误列表（供重试提示）。
    """
    if not isinstance(data, dict):
        return False, None, [{"path": "$", "message": "LLM 输出不是 JSON 对象"}]
    d = _coerce_before_validate(data)
    errors = validate_intent(d)
    if errors:
        return False, None, errors
    # 语义级后检查: confirmed/locked 必须有值、feature 引用可解析、id 唯一
    semi = post_validate_intent(d)
    if semi:
        return False, None, semi
    d = sanitize_intent(d)
    return True, d, []


__all__ = [
    "intent_schema", "parameter_schema", "feature_schema",
    "constraint_schema", "clarification_schema",
    "validate_intent", "validate_llm_intent", "format_errors",
]