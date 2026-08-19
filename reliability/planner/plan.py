"""Plan DAG 数据模型（需求 §7.1-§7.3）。

每个节点必须能显示：步骤名称/设计意图/使用的参数/依赖步骤/预计生成的
Fusion 对象/风险等级/是否修改现有对象/是否需要确认/失败替代策略/验证方法。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

NODE_STATUSES = ("pending", "running", "passed", "failed", "skipped", "paused", "blocked")
RISK_LEVELS = ("low", "medium", "high")


@dataclass
class PlanNode:
    id: str
    type: str
    label: str
    order: int = 0
    depends_on: list = field(default_factory=list)
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    status: str = "pending"
    risk: str = "medium"
    checkpoint: bool = False
    requires_confirmation: bool = False
    strategy: str = "native_fusion_feature"
    notes: str = ""
    verification: str = ""          # 验证方法
    alternative: str = ""           # 失败后的替代策略
    modifies_existing: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id, "order": self.order, "type": self.type,
            "label": self.label, "depends_on": list(self.depends_on),
            "inputs": list(self.inputs), "outputs": list(self.outputs),
            "status": self.status, "risk": self.risk,
            "checkpoint": self.checkpoint,
            "requires_confirmation": self.requires_confirmation,
            "strategy": self.strategy, "notes": self.notes,
            "verification": self.verification, "alternative": self.alternative,
            "modifies_existing": self.modifies_existing,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlanNode":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class Plan:
    def __init__(self, plan_id: str = "", version: int = 1, nodes: Optional[list[PlanNode]] = None) -> None:
        self.plan_id = plan_id or f"plan_{uuid.uuid4().hex[:12]}"
        self.version = version
        self.nodes: list[PlanNode] = nodes or []
        self._index = {n.id: n for n in self.nodes}

    # ------------------------------------------------------------------
    def node(self, nid: str) -> Optional[PlanNode]:
        return self._index.get(nid)

    def add_node(self, node: PlanNode) -> None:
        node.order = len(self.nodes) + 1
        self.nodes.append(node)
        self._index[node.id] = node

    def remove_node(self, nid: str) -> bool:
        node = self._index.pop(nid, None)
        if node is None:
            return False
        self.nodes = [n for n in self.nodes if n.id != nid]
        for n in self.nodes:
            n.depends_on = [d for d in n.depends_on if d != nid]
        self._renumber()
        return True

    def _renumber(self) -> None:
        for i, n in enumerate(self.nodes, start=1):
            n.order = i

    def set_enabled(self, nid: str, enabled: bool) -> bool:
        """禁用步骤(需求 §7.3): 被禁用的节点标记 skipped 并阻断其下游。"""
        node = self.node(nid)
        if node is None:
            return False
        node.status = "pending" if enabled else "skipped"
        self._index = {n.id: n for n in self.nodes}
        return True

    # ------------------------------------------------------------------
    # 依赖与拓扑
    # ------------------------------------------------------------------
    def validate(self) -> list[str]:
        """依赖引用存在性、order 唯一性检查。"""
        errs = []
        ids = set(self._index.keys())
        for n in self.nodes:
            for dep in n.depends_on:
                if dep not in ids:
                    errs.append(f"node {n.id} depends on unknown node {dep}")
            if n.status not in NODE_STATUSES:
                errs.append(f"node {n.id} invalid status {n.status}")
            if n.risk not in RISK_LEVELS:
                errs.append(f"node {n.id} invalid risk {n.risk}")
        orders = [n.order for n in self.nodes]
        if len(set(orders)) != len(orders):
            errs.append("node order values are not unique")
        return errs

    def topological_order(self) -> list[str]:
        """Kahn 拓扑排序；存在环时抛 ValueError 并给出环中节点。"""
        in_degree: dict[str, int] = {nid: 0 for nid in self._index}
        adj: dict[str, list[str]] = {nid: [] for nid in self._index}
        for n in self.nodes:
            for dep in n.depends_on:
                if dep in adj:
                    adj[dep].append(n.id)
                    in_degree[n.id] += 1
        queue = [nid for nid, d in in_degree.items() if d == 0]
        result = []
        while queue:
            nid = queue.pop(0)
            result.append(nid)
            for nxt in adj[nid]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)
        if len(result) != len(self._index):
            remaining = [nid for nid, d in in_degree.items() if d > 0]
            raise ValueError(f"计划中存在循环依赖: {remaining}")
        return result

    def has_cycle(self) -> bool:
        try:
            self.topological_order()
            return False
        except ValueError:
            return True

    def ready_nodes(self) -> list[PlanNode]:
        """当前可执行的节点: pending/running 且其依赖全部 passed 或 skipped。"""
        out = []
        for n in self.nodes:
            if n.status not in ("pending", "running"):
                continue
            deps_ok = all(
                (self.node(d).status in ("passed", "skipped") if self.node(d) else False)
                for d in n.depends_on
            )
            if deps_ok:
                out.append(n)
        return sorted(out, key=lambda n: n.order)

    def pending_count(self) -> int:
        return sum(1 for n in self.nodes if n.status in ("pending", "running"))

    def all_passed(self) -> bool:
        return all(n.status == "passed" for n in self.nodes)

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "nodes": [n.to_dict() for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        plan = cls(plan_id=d.get("plan_id", ""), version=d.get("version", 1))
        plan.nodes = [PlanNode.from_dict(n) for n in d.get("nodes", [])]
        plan._index = {n.id: n for n in plan.nodes}
        return plan

    def bumped(self) -> "Plan":
        """返回 version+1 的副本。"""
        return Plan.from_dict({**self.to_dict(), "version": self.version + 1})


def summarize_plan(plan: Plan) -> list[dict]:
    """面向用户的计划摘要（需求 §7.2 每个节点必须显示的信息）。"""
    out = []
    for n in sorted(plan.nodes, key=lambda x: x.order):
        out.append({
            "id": n.id,
            "step": n.order,
            "label": n.label,
            "type": n.type,
            "depends_on": n.depends_on,
            "inputs": n.inputs,
            "outputs": n.outputs,
            "risk": n.risk,
            "status": n.status,
            "checkpoint": n.checkpoint,
            "requires_confirmation": n.requires_confirmation,
            "verification": n.verification or "执行后核实体数与关键尺寸",
            "alternative": n.alternative or "参数化特征失败时降级为草图驱动/布尔几何，并在失败报告中说明",
        })
    return out


__all__ = ["Plan", "PlanNode", "NODE_STATUSES", "RISK_LEVELS", "summarize_plan"]