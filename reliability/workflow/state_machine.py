"""WorkflowState 工作流状态机（需求 §3）。

状态：
  IDLE → UNDERSTAND → CLARIFY → SPEC_READY → PLAN_READY → REVIEW
      → PREVIEW → EXECUTING → VALIDATING
  VALIDATING: PASSED → COMPLETED | WARNING → USER_DECISION | FAILED → REPAIR_PLANNING
  另支持: PAUSED / CANCELLED / ROLLED_BACK / REPLANNING / USER_DECISION / REPAIR_PLANNING

核心约束（需求 §3.2）：禁止从 UNDERSTAND 直接进入 EXECUTING。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# 状态集合
# ---------------------------------------------------------------------------
IDLE = "IDLE"
UNDERSTAND = "UNDERSTAND"
CLARIFY = "CLARIFY"
SPEC_READY = "SPEC_READY"
PLAN_READY = "PLAN_READY"
REVIEW = "REVIEW"
PREVIEW = "PREVIEW"
EXECUTING = "EXECUTING"
VALIDATING = "VALIDATING"
COMPLETED = "COMPLETED"
USER_DECISION = "USER_DECISION"
REPAIR_PLANNING = "REPAIR_PLANNING"
PAUSED = "PAUSED"
CANCELLED = "CANCELLED"
ROLLED_BACK = "ROLLED_BACK"
REPLANNING = "REPLANNING"

ALL_STATES = (
    IDLE, UNDERSTAND, CLARIFY, SPEC_READY, PLAN_READY, REVIEW, PREVIEW,
    EXECUTING, VALIDATING, COMPLETED, USER_DECISION, REPAIR_PLANNING,
    PAUSED, CANCELLED, ROLLED_BACK, REPLANNING,
)

# ---------------------------------------------------------------------------
# 状态权限（需求 §3.2 表格）
# ---------------------------------------------------------------------------
STATE_PERMISSIONS: dict[str, dict] = {
    UNDERSTAND:      {"meaning": "AI 分析需求", "user_actions": ["cancel"]},
    CLARIFY:         {"meaning": "存在关键歧义", "user_actions": ["answer_questions", "modify_assumptions"]},
    SPEC_READY:      {"meaning": "结构化设计规格已生成", "user_actions": ["modify_parameters", "lock_parameters"]},
    PLAN_READY:      {"meaning": "执行计划已生成", "user_actions": ["view_steps"]},
    REVIEW:          {"meaning": "用户审查计划", "user_actions": ["modify", "delete", "reorder", "insert_pause"]},
    PREVIEW:         {"meaning": "生成轻量预览", "user_actions": ["accept", "return_to_review"]},
    EXECUTING:       {"meaning": "调用 Fusion API", "user_actions": ["pause", "stop"]},
    VALIDATING:      {"meaning": "执行验证", "user_actions": ["wait", "cancel"]},
    USER_DECISION:   {"meaning": "存在警告或冲突", "user_actions": ["accept", "repair", "rollback"]},
    REPAIR_PLANNING: {"meaning": "制定修复计划", "user_actions": ["view_confirm"]},
    COMPLETED:       {"meaning": "任务完成", "user_actions": ["continue_chat", "export_report"]},
}


def _invalid_transitions() -> set[tuple[str, str]]:
    """非法转换列表（返回 (from, to) 集合）。"""
    forbidden = {
        # 需求 §3.2: 禁止 UNDERSTAND 直接进入 EXECUTING
        (UNDERSTAND, EXECUTING),
        (IDLE, EXECUTING),
        (IDLE, VALIDATING),
        (IDLE, COMPLETED),
        (CLARIFY, SPEC_READY),       # 必须先回到 UNDERSTAND 重新理解
        (CLARIFY, PLAN_READY),
        (CLARIFY, EXECUTING),
        (SPEC_READY, EXECUTING),     # 必须先出计划
        (SPEC_READY, VALIDATING),
        (PLAN_READY, EXECUTING),     # 计划未审查不得直接执行
        (PAUSED, COMPLETED),
        (COMPLETED, EXECUTING),
        (ROLLED_BACK, EXECUTING),
        (REPAIR_PLANNING, COMPLETED),
    }
    return forbidden


_FORBIDDEN = _invalid_transitions()


# ---------------------------------------------------------------------------
# 合法转换表（显示用）
# ---------------------------------------------------------------------------
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    IDLE:          {UNDERSTAND, CANCELLED},
    UNDERSTAND:    {CLARIFY, SPEC_READY, CANCELLED},
    CLARIFY:       {UNDERSTAND, CANCELLED},
    SPEC_READY:    {PLAN_READY, UNDERSTAND, CANCELLED},
    PLAN_READY:    {REVIEW, REPLANNING, CANCELLED},
    REVIEW:        {PREVIEW, EXECUTING, SPEC_READY, REPLANNING, CANCELLED},
    PREVIEW:       {EXECUTING, REVIEW, CANCELLED},
    EXECUTING:     {PAUSED, VALIDATING, CANCELLED, REPLANNING, EXECUTING, ROLLED_BACK},
    PAUSED:        {EXECUTING, CANCELLED, ROLLED_BACK},
    VALIDATING:    {COMPLETED, USER_DECISION, REPAIR_PLANNING, CANCELLED},
    USER_DECISION: {EXECUTING, REPAIR_PLANNING, ROLLED_BACK, CANCELLED},
    REPAIR_PLANNING: {REVIEW, PLAN_READY, ROLLED_BACK, CANCELLED},
    REPLANNING:    {PLAN_READY, SPEC_READY, CANCELLED},
    ROLLED_BACK:   {UNDERSTAND, IDLE, CANCELLED},
    COMPLETED:     {IDLE, UNDERSTAND, CANCELLED},
    CANCELLED:     {IDLE, UNDERSTAND},
}


@dataclass
class TransitionRecord:
    from_state: str
    to_state: str
    reason: str = ""


class WorkflowStateMachine:
    """带合法转换校验、权限查询和转换历史的状态机。"""

    def __init__(self, initial: str = IDLE) -> None:
        if initial not in ALL_STATES:
            raise ValueError(f"invalid state: {initial}")
        self._state: str = initial
        self.history: list[TransitionRecord] = []
        self._session_id: Optional[str] = None

    @property
    def state(self) -> str:
        return self._state

    def session_id(self) -> Optional[str]:
        return self._session_id

    def start_session(self, sid: str) -> None:
        self._session_id = sid

    def is_valid(self, to_state: str) -> bool:
        return to_state in ALLOWED_TRANSITIONS.get(self._state, set())

    def can_transition(self, to_state: str) -> tuple[bool, str]:
        if to_state not in ALL_STATES:
            return False, f"未知状态: {to_state}"
        if not self.is_valid(to_state):
            return False, f"非法转换: {self._state} → {to_state}"
        if (self._state, to_state) in _FORBIDDEN:
            return False, f"禁止的转换: {self._state} → {to_state}"
        return True, "ok"

    def transition(self, to_state: str, reason: str = "") -> None:
        ok, msg = self.can_transition(to_state)
        if not ok:
            raise InvalidTransitionError(msg, self._state, to_state)
        self.history.append(TransitionRecord(self._state, to_state, reason))
        self._state = to_state

    def transition_or_none(self, to_state: str, reason: str = "") -> bool:
        """尽力转换:非法时返回 False 并记录,不抛异常。"""
        ok, _ = self.can_transition(to_state)
        if not ok:
            return False
        try:
            self.transition(to_state, reason)
            return True
        except InvalidTransitionError:
            return False

    def permissions(self) -> dict:
        return STATE_PERMISSIONS.get(self._state, {})

    def to_dict(self) -> dict:
        return {
            "state": self._state,
            "permissions": self.permissions(),
            "session_id": self._session_id,
            "history": [
                {"from": r.from_state, "to": r.to_state, "reason": r.reason}
                for r in self.history
            ],
        }


class InvalidTransitionError(Exception):
    def __init__(self, message: str, from_state: str, to_state: str) -> None:
        super().__init__(message)
        self.from_state = from_state
        self.to_state = to_state


__all__ = [
    "IDLE", "UNDERSTAND", "CLARIFY", "SPEC_READY", "PLAN_READY", "REVIEW",
    "PREVIEW", "EXECUTING", "VALIDATING", "COMPLETED", "USER_DECISION",
    "REPAIR_PLANNING", "PAUSED", "CANCELLED", "ROLLED_BACK", "REPLANNING",
    "ALL_STATES", "ALLOWED_TRANSITIONS", "STATE_PERMISSIONS",
    "WorkflowStateMachine", "InvalidTransitionError",
]