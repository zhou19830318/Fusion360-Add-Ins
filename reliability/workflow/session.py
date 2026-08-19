"""WorkflowSession — 一次可靠设计会话的聚合根。

持有: 状态机 / DesignSpec / Plan / 执行日志 / 检查点 / 验证结果 / 授权开关。
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from ..ir.design_spec import DesignSpec
from .log import ExecutionLog
from .state_machine import (
    COMPLETED, EXECUTING, IDLE, PREVIEW, REVIEW, WorkflowStateMachine,
)


class WorkflowSession:
    def __init__(self, session_id: str = "", execute_enabled: bool = False) -> None:
        self.session_id = session_id or f"session_{uuid.uuid4().hex[:12]}"
        self.state = WorkflowStateMachine(IDLE)
        self.state.start_session(self.session_id)
        self.spec: Optional[DesignSpec] = None
        self.plan: Any = None  # planner.plan.Plan
        self.log = ExecutionLog(session_id=self.session_id)
        self.checkpoints: list[dict] = []
        self.validation_reports: dict[str, dict] = {}
        self.user_decisions: dict[str, dict] = {}
        # 授权开关（需求问答 Q3）: execute 任意脚本类工具默认禁用
        self.execute_enabled: bool = execute_enabled
        self.mode: str = "reliability"  # reliability | legacy-direct

    # ------------------------------------------------------------------
    # 状态转换封装
    # ------------------------------------------------------------------
    def begin(self, user_text: str) -> None:
        self.state.transition("UNDERSTAND", "user input received")
        self.log.info("会话开始，进入 UNDERSTAND", actor="user", category="intent",
                      data={"user_text": user_text[:200]})

    def to_clarify(self, reason: str = "") -> None:
        self.state.transition("CLARIFY", reason)
        self.log.info("进入需求澄清", category="clarify", data={"reason": reason})

    def spec_ready(self) -> None:
        self.state.transition("SPEC_READY", "design spec generated")
        self.log.info("DesignSpec 已生成并校验", category="intent", data={
            "parameters": len(self.spec.parameters) if self.spec else 0,
            "features": len(self.spec.features) if self.spec else 0,
        })

    def plan_ready(self) -> None:
        self.state.transition("PLAN_READY", "execution plan generated")
        self.log.info("执行计划已生成", category="plan",
                      data={"nodes": len(self.plan.nodes) if self.plan else 0})

    def review_approved(self) -> None:
        """用户审查批准计划: PLAN_READY → REVIEW → PREVIEW。"""
        if self.state.state == "PLAN_READY":
            self.state.transition("REVIEW", "user reviewing")
        if self.state.state == "REVIEW":
            self.state.transition("PREVIEW", "user approved plan")
        self.log.info("用户已审查并批准计划", actor="user", category="plan")

    def preview_accepted(self) -> None:
        """预览接受（MVP 不切换状态; PREVIEW → EXECUTING 由 start_execution 接管）。"""
        self.log.info("预览已接受，准备执行", actor="user", category="plan")

    def start_execution(self) -> None:
        if self.state.state == "PREVIEW":
            self.state.transition("EXECUTING", "execution started")
        elif self.state.state == "REVIEW":
            # 需求 §7.6: 关键路径 —— 审查通过后进入预览再执行；
            # 若未提供轻量预览(可选), 允许直接进入执行
            self.state.transition("EXECUTING", "execution started (no preview)")
        else:
            ok, msg = self.state.can_transition(EXECUTING)
            if not ok:
                raise ValueError(f"当前状态 {self.state.state} 不能开始执行: {msg}")
            self.state.transition(EXECUTING, "execution started")
        self.log.info("开始执行", category="execute")

    def pause(self) -> None:
        if self.state.transition_or_none("PAUSED", "user paused"):
            self.log.info("执行已暂停", actor="user", category="execute")

    def resume(self) -> None:
        if self.state.transition_or_none("EXECUTING", "user resumed"):
            self.log.info("执行恢复", actor="user", category="execute")

    def cancel(self) -> None:
        if self.state.transition_or_none("CANCELLED", "user cancelled"):
            self.log.warning("会话被用户取消", actor="user", category="execute")

    def complete(self) -> None:
        self.state.transition(COMPLETED, "validation passed")
        self.log.info("任务完成", category="validate")

    def to_validation(self) -> None:
        self.state.transition("VALIDATING", "execution finished")
        self.log.info("进入验证", category="validate")

    # ------------------------------------------------------------------
    # 检查点
    # ------------------------------------------------------------------
    def add_checkpoint(self, label: str, data: Optional[dict] = None) -> dict:
        cp = {
            "id": f"ckpt_{len(self.checkpoints) + 1}",
            "seq": len(self.checkpoints) + 1,
            "label": label,
            "data": data or {},
        }
        self.checkpoints.append(cp)
        self.log.info(f"创建检查点 {cp['id']}: {label}", category="checkpoint", data=cp)
        return cp

    def get_checkpoint(self, cid: str) -> Optional[dict]:
        for cp in self.checkpoints:
            if cp["id"] == cid:
                return cp
        return None

    # ------------------------------------------------------------------
    # 工具授权
    # ------------------------------------------------------------------
    def set_execute_enabled(self, enabled: bool) -> None:
        self.execute_enabled = bool(enabled)
        self.log.warning(
            f"execute 任意脚本工具授权已{'开启' if self.execute_enabled else '关闭'}",
            actor="user", category="gate",
        )

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "state": self.state.state,
            "mode": self.mode,
            "execute_enabled": self.execute_enabled,
            "intent_id": self.spec.intent_id if self.spec else None,
            "plan_id": self.plan.plan_id if self.plan else None,
            "checkpoints": self.checkpoints,
            "validation_reports": list(self.validation_reports.keys()),
            "log_count": len(self.log.entries),
            "state_machine": self.state.to_dict(),
        }


__all__ = ["WorkflowSession"]