"""LLM 结构化输出生成（需求 §14：IntentResponse / PlanResponse 等必须 JSON Schema 校验）。

流程: 请求 JSON 输出 → jsonschema 校验 → 失败返回错误给模型 → 最多自动修正 2 次
      → 仍失败则报错(进入人工处理)。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional, Tuple

from ..ir.schema import intent_schema, validate_llm_intent

# llm_call 形式: callable(messages: list[dict], **kw) -> str (纯文本)
LlmCall = Callable[[list[dict], dict], str]

_MAX_AUTO_FIX = 2  # 需求 §14: 最多自动修正两次

_INTENT_SYSTEM_PROMPT_TPL = (
    "You are a CAD design intent extractor. Convert the user's natural language request into "
    "STRICT JSON matching the schema below. Do NOT wrap in markdown fences — output ONLY the JSON object.\n\n"
    "Rules:\n"
    "- Every inferred/assumed value must be tagged with source ('user'|'inferred'|'default') and a confidence 0..1.\n"
    "- Parameters with ambiguous values must set status='needs_confirmation'.\n"
    "- List every blocking ambiguity as a {\"severity\":\"blocking\"} clarification question.\n"
    "- features: use semantic roles like 'base_geometry', 'mounting_holes', 'shell', 'cosmetic'.\n"
    "- Never invent locked parameters unless the user explicitly fixed them.\n\n"
    "SCHEMA:\n{schema_json}\n"
)

_RE_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    # 反引号包裹
    m = _RE_JSON.search(text)
    if m:
        text = m.group(1)
    # 直接 JSON 对象
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def generate_intent_with_llm(
    llm_call: LlmCall,
    user_text: str,
    language: str = "zh-CN",
    domain_hint: str = "",
) -> Tuple[bool, Optional[dict], Optional[list], int]:
    """生成并校验 Intent。

    返回 (ok, data_dict, errors, attempts):
      ok=True 时 data_dict 为通过 schema 校验的 dict；
      ok=False 时 errors 为错误列表，attempts 为尝试次数(≤3)。
    """
    schema_json = json.dumps(intent_schema(), ensure_ascii=False, indent=1)
    # 注意: 不能用 .format() —— 模板里包含字面 JSON 花括号(如 {"severity":"blocking"}),
    # 会被 format 误认为占位符而抛 KeyError。用 replace 仅替换唯一占位符。
    system = _INTENT_SYSTEM_PROMPT_TPL.replace("{schema_json}", schema_json)
    user = user_text
    if language:
        user += f"\n(language: {language})"
    if domain_hint:
        user += f"\n(hint: domain={domain_hint})"

    messages: list[dict] = [{"role": "system", "content": system}]
    attempts = 0
    current_user = user
    for attempt in range(1 + _MAX_AUTO_FIX):  # 1 + 2 次修正
        attempts += 1
        msg = messages + [{"role": "user", "content": current_user}]
        try:
            text = llm_call(msg, {})
        except Exception as exc:
            return False, None, [{"path": "$", "message": f"LLM 调用失败: {exc}"}], attempts

        data = _extract_json(text)
        if data is None:
            feedback = (f"你的输出不是有效 JSON。原文:\n{text[:500]}\n"
                        "请只输出符合 schema 的 JSON 对象。")
        else:
            ok, coerced, errors = validate_llm_intent(data)
            if ok:
                return True, coerced, None, attempts
            feedback = ("JSON 未通过 schema 校验，错误如下，请修正后重新输出完整 JSON:\n"
                        + json.dumps(errors, ensure_ascii=False, indent=1))
        current_user = feedback  # 下一轮的消息
    return False, None, [{"path": "$", "message": f"连续 {attempts} 次未通过 schema 校验"}], attempts


def generate_plan_with_llm(
    llm_call: LlmCall,
    spec_dict: dict,
) -> Tuple[bool, Optional[dict], Optional[list], int]:
    """LLM 生成 Plan（后续阶段启用; MVP 使用模板生成器）。

    预留: 要求模型基于 spec 输出 plan 节点列表, 由 planner.generator 合并/校验。
    """
    system = (
        "You are a CAD execution planner. Given the design spec JSON, produce a plan JSON of "
        "execution nodes: [{\"id\",\"type\",\"label\",\"depends_on\",\"inputs\",\"risk\"}]. "
        "Prefer native parametric Fusion features. Output ONLY JSON."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(spec_dict, ensure_ascii=False, indent=1)},
    ]
    text = llm_call(messages, {})
    data = _extract_json(text)
    if data is None:
        return False, None, [{"message": "LLM plan 输出不是有效 JSON"}], 1
    nodes = data.get("nodes") or []
    if not isinstance(nodes, list) or not all(isinstance(n, dict) for n in nodes):
        return False, None, [{"message": "plan.nodes 必须是数组"}], 1
    return True, data, None, 1


__all__ = ["generate_intent_with_llm", "generate_plan_with_llm", "_extract_json"]