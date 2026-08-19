"""澄清问题分类与选择（需求 §5.1、§5.2）。

每轮最多提出 3 个问题，优先级：
  几何对象歧义 > 单位和坐标系 > 关键制造参数 > 结构方案 > 外观和命名
"""

from __future__ import annotations

from typing import Optional

from ..ir.design_spec import DesignSpec

# 优先级(数值越小越优先)
_SEVERITY_PRIORITY = {"blocking": 0, "default": 1, "preference": 2}
_MAX_QUESTIONS_PER_ROUND = 3

# 主题优先级 → 判别关键词
_TOPIC_ORDER = [
    ("geometry", ("面", "边", "顶点", "组件", "哪一侧", "方向", "位置", "对称", "中心")),
    ("units_coord", ("单位", "坐标系", "原点", "单位制")),
    ("manufacturing", ("制造", "工艺", "打印", "公差", "壁厚", "螺纹", "配合")),
    ("structure", ("结构", "方案", "一体", "分体", "拆件")),
    ("appearance", ("外观", "颜色", "命名", "材料", "标签")),
]


def rank_questions(spec: DesignSpec, extra: Optional[list[dict]] = None) -> list[dict]:
    """从 spec.clarifications 中选出本轮应提问的问题(最多 3 个)。

    - 只选 severity 为 blocking / default 且未回答的
    - blocking 优先于 default 优先于 preference
    - 同 severity 按主题优先级排序
    """
    qs: list[dict] = []
    for c in spec.clarifications:
        if c.answer is not None:
            continue
        if c.severity not in _SEVERITY_PRIORITY:
            continue
        topic = _topic_of(c.question)
        qs.append({
            "id": c.id,
            "question": c.question,
            "reason": c.reason,
            "severity": c.severity,
            "options": c.options,
            "default": c.default,
            "related_features": c.related_features,
            "topic": topic,
        })
    for extra_q in (extra or []):
        if extra_q.get("answered"):
            continue
        qs.append({
            "id": extra_q.get("id", f"extra_{len(qs)}"),
            "question": extra_q.get("question", ""),
            "reason": extra_q.get("reason", ""),
            "severity": extra_q.get("severity", "default"),
            "options": extra_q.get("options", []),
            "default": extra_q.get("default"),
            "related_features": extra_q.get("related_features", []),
            "topic": _topic_of(extra_q.get("question", "")),
        })

    qs.sort(key=lambda q: (_SEVERITY_PRIORITY.get(q["severity"], 9), q["topic"]))
    return qs[:_MAX_QUESTIONS_PER_ROUND]


def has_blocking_questions(spec: DesignSpec) -> bool:
    return any(
        c.severity == "blocking" and c.answer is None
        for c in spec.clarifications
    )


def _topic_of(question: str) -> int:
    for idx, (_name, keywords) in enumerate(_TOPIC_ORDER):
        for kw in keywords:
            if kw in question:
                return idx
    return 99


def apply_answers(spec: DesignSpec, answers: dict) -> list[str]:
    """把澄清回答应用到 spec，返回被更新的 clarification id 列表。"""
    applied = []
    for c in spec.clarifications:
        if c.id in answers:
            c.answer = answers[c.id]
            applied.append(c.id)
    return applied


__all__ = ["rank_questions", "has_blocking_questions", "apply_answers", "_MAX_QUESTIONS_PER_ROUND"]