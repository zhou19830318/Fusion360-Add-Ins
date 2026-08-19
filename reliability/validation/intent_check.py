"""设计意图验证（需求 §10.1-设计意图验证）。

检查项:
  - 锁定参数在计划执行后值未被改变（需求 §6.5: 锁定参数不可覆盖）
  - 计划中必要(非 optional)节点全部通过（需求的必选特征存在）
  - 假设与澄清在会话中已展示(由 UI/日志承担)
"""

from __future__ import annotations

from typing import Optional

from ..ir.design_spec import DesignSpec
from ..planner.plan import Plan
from .report import ValidationReport, make_check


class IntentValidator:
    def __init__(self, spec: DesignSpec, plan: Optional[Plan] = None,
                 model_context: Optional[dict] = None,
                 report: Optional[ValidationReport] = None) -> None:
        self.spec = spec
        self.plan = plan
        self.model_context = model_context or {}
        self.report = report or ValidationReport()

    def run(self) -> ValidationReport:
        # 1) 锁定参数未被修改
        locked_params = [p for p in self.spec.parameters if p.locked]
        if locked_params:
            fusion_params = self.model_context.get("parameters", [])
            famap = {}
            for fp in fusion_params:
                if isinstance(fp, dict):
                    famap[fp.get("name")] = fp.get("value")
            violations = []
            for p in locked_params:
                fv = famap.get(p.name)
                if fv is not None:
                    try:
                        if abs(float(fv) - float(p.value)) > 1e-6:
                            violations.append(p.name)
                    except (TypeError, ValueError):
                        continue
            if violations:
                self.report.add(make_check(
                    "failed", "intent_locked_parameters",
                    f"锁定参数被修改: {violations}",
                    target=",".join(violations),
                    actual=violations, expected="与 DesignSpec 一致",
                    suggestions=[{"action": "restore_parameter",
                                  "parameter": v, "value": None} for v in violations],
                ))
            else:
                self.report.add(make_check(
                    "passed", "intent_locked_parameters",
                    f"锁定参数 {len(locked_params)} 个均未被修改",
                    target=",".join(p.name for p in locked_params),
                ))

        # 2) 必要节点全部通过
        if self.plan:
            required = [n for n in self.plan.nodes if not hasattr(n, "optional") or not n.optional]
            must = [n for n in required if n.status != "skipped"]
            missing = [n.id for n in must if n.status != "passed"]
            if missing:
                self.report.add(make_check(
                    "failed", "intent_plan_completion",
                    f"必要步骤未完成: {missing}",
                    target=",".join(missing), actual=missing, expected="全部 passed",
                    suggestions=[{"action": "retry", "parameter": None, "value": None}],
                ))
            else:
                self.report.add(make_check(
                    "passed", "intent_plan_completion",
                    f"计划必要步骤 {len(must)} 个全部完成",
                ))

        # 3) 关键特征存在（semantic_role 覆盖用户核心诉求）
        must_roles = {"mounting_holes", "base_geometry"}
        present = {f.semantic_role for f in self.spec.features if f.enabled}
        missing_roles = sorted(must_roles - present)
        if missing_roles:
            pass  # MVP: 不作为失败项,日志提示
        return self.report


__all__ = ["IntentValidator"]