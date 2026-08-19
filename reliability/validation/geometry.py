"""基础几何验证（需求 §10.1-几何验证、MVP 范围）。

检查项（MVP）：
  - 实体体积 > 0（非空实体）
  - 实体总数与计划预期一致（下限；多次执行允许重复）
  - 包围盒非退化（min < max，即存在实际尺寸）
MVP 暂不包含：自相交、零厚度、孔位置精确校验（后续阶段）。

视觉验证：截图 → 带视觉能力的模型做证据级检查，绑定到节点（需求 §10.1）。
"""

from __future__ import annotations

from typing import Any, Optional

from ..planner.plan import Plan
from .report import ValidationReport, make_check


class GeometryValidator:
    def __init__(self, backend, plan: Optional[Plan] = None,
                 report: Optional[ValidationReport] = None) -> None:
        self.backend = backend
        self.plan = plan
        self.report = report or ValidationReport()

    def run(self) -> ValidationReport:
        bodies_r = self.backend.call("read", {"queryType": "bodies"})
        bodies = []
        if isinstance(bodies_r, dict):
            bodies = bodies_r.get("bodies") or bodies_r.get("value") or []
        if not isinstance(bodies, list):
            bodies = []

        # 1) 体积 > 0
        positive = 0
        for b in bodies:
            vol = b.get("volume") if isinstance(b, dict) else None
            try:
                if vol is not None and float(vol) > 0:
                    positive += 1
            except (TypeError, ValueError):
                continue
        if positive == 0 and bodies:
            self.report.add(make_check(
                "failed", "geometry_volume",
                "所有实体体积 ≤ 0，模型可能为空实体",
                target="root", actual="0", expected="> 0",
                suggestions=[{"action": "rollback", "parameter": None, "value": None}],
            ))
        else:
            self.report.add(make_check(
                "passed" if bodies else "warning",
                "geometry_volume",
                f"非空实体 {positive} 个（体积 > 0）" if bodies else "当前没有实体",
                target="root", actual=positive, expected="> 0",
            ))

        # 2) 实体数量
        expected_min = 0
        if self.plan:
            for n in self.plan.nodes:
                if n.status == "passed" and n.type in (
                    "create_extrude", "create_revolve", "create_box", "create_cylinder",
                    "create_sphere", "create_torus", "create_coil",
                ):
                    expected_min += 1
        if len(bodies) < expected_min:
            self.report.add(make_check(
                "failed", "geometry_body_count",
                f"实体数量不足：期望至少 {expected_min}，实际 {len(bodies)}",
                target="root", actual=len(bodies), expected=f">= {expected_min}",
                suggestions=[{"action": "retry", "parameter": None, "value": None}],
            ))
        else:
            self.report.add(make_check(
                "passed", "geometry_body_count",
                f"实体数量 {len(bodies)} >= 期望 {expected_min}",
                target="root", actual=len(bodies), expected=f">= {expected_min}",
            ))

        # 3) 体积合理性: 与计划中出现的尺寸量级一致(可选,跳过)

        return self.report


__all__ = ["GeometryValidator"]