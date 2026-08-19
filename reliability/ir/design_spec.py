"""DesignSpec — 结构化设计意图数据模型（需求文档 §4）。

DesignSpec 是可审查、可序列化、可追溯的设计意图载体：
  goal / environment / parameters / features / constraints /
  assumptions / clarifications / plan / validation / metadata
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 参数来源 / 状态（需求 §4.2）
# ---------------------------------------------------------------------------
PARAMETER_SOURCES = ("user", "inferred", "default", "derived", "constraint", "model", "system")
PARAMETER_STATUSES = ("draft", "needs_confirmation", "confirmed", "locked", "conflict", "invalid")

# 约束优先级（需求 §4.4）
CONSTRAINT_PRIORITIES = ("hard", "soft", "preference", "suggestion")


@dataclass
class Parameter:
    """参数结构（需求 §4.2）。id/name/value 为必填。"""

    id: str
    name: str
    value: Any
    label: str = ""
    expression: Optional[str] = None
    unit: str = "mm"
    type: str = "length"
    source: str = "user"
    confidence: float = 1.0
    editable: bool = True
    locked: bool = False
    required: bool = False
    min: Optional[float] = None
    max: Optional[float] = None
    affects: list = field(default_factory=list)
    description: str = ""
    status: str = "draft"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            "value": self.value,
            "expression": self.expression,
            "unit": self.unit,
            "type": self.type,
            "source": self.source,
            "confidence": self.confidence,
            "editable": self.editable,
            "locked": self.locked,
            "required": self.required,
            "min": self.min,
            "max": self.max,
            "affects": list(self.affects),
            "description": self.description,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Parameter":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Feature:
    """特征结构（需求 §4.3）。"""

    id: str
    type: str
    label: str = ""
    semantic_role: str = ""
    depends_on: list = field(default_factory=list)
    parameters: dict = field(default_factory=dict)
    strategy: str = "native_fusion_feature"
    optional: bool = False
    enabled: bool = True
    risk: str = "medium"
    requires_confirmation: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "semantic_role": self.semantic_role,
            "depends_on": list(self.depends_on),
            "parameters": dict(self.parameters),
            "strategy": self.strategy,
            "optional": self.optional,
            "enabled": self.enabled,
            "risk": self.risk,
            "requires_confirmation": self.requires_confirmation,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Feature":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Constraint:
    """约束结构（需求 §4.4）。发生冲突时必须输出冲突集合，不得静默取舍。"""

    id: str
    expression: str
    type: str = "geometric"
    priority: str = "hard"
    source: str = "user"
    locked: bool = True
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "expression": self.expression,
            "priority": self.priority,
            "source": self.source,
            "locked": self.locked,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Constraint":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Clarification:
    """澄清问题（需求 §5.2）。"""

    id: str
    question: str
    reason: str = ""
    severity: str = "blocking"  # blocking | default | preference
    options: list = field(default_factory=list)
    default: Any = None
    related_features: list = field(default_factory=list)
    answer: Any = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "question": self.question,
            "reason": self.reason,
            "severity": self.severity,
            "options": list(self.options),
            "default": self.default,
            "related_features": list(self.related_features),
            "answer": self.answer,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Clarification":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class DesignSpec:
    """顶层设计规格（需求 §4.1）。"""

    intent_id: str = ""
    operation: str = "create"
    domain: str = "mechanical_part"
    schema_version: str = "1.0"
    goal: dict = field(default_factory=lambda: {
        "summary": "",
        "user_text": "",
        "language": "zh-CN",
    })
    environment: dict = field(default_factory=lambda: {
        "document_units": "mm",
        "active_component": None,
        "selection_refs": [],
        "design_history_required": True,
        "manufacturing_method": "cnc",
    })
    parameters: list = field(default_factory=list)
    features: list = field(default_factory=list)
    constraints: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    clarifications: list = field(default_factory=list)
    plan: Optional[dict] = None
    validation: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.intent_id:
            self.intent_id = f"intent_{uuid.uuid4().hex[:12]}"
        self._params: dict[str, Parameter] = {}
        self._features: dict[str, Feature] = {}
        self._constraints: dict[str, Constraint] = {}
        self._clarifications: dict[str, Clarification] = {}
        self._reindex()

    # ---- 索引维护 ----
    def _reindex(self) -> None:
        self._params = {p.id: p for p in self.parameters}
        self._features = {f.id: f for f in self.features}
        self._constraints = {c.id: c for c in self.constraints}
        self._clarifications = {c.id: c for c in self.clarifications}

    # ---- 查询 ----
    def get_parameter(self, pid: str) -> Optional[Parameter]:
        return self._params.get(pid)

    def get_parameter_by_name(self, name: str) -> Optional[Parameter]:
        for p in self.parameters:
            if p.name == name:
                return p
        return None

    def get_feature(self, fid: str) -> Optional[Feature]:
        return self._features.get(fid)

    def get_constraint(self, cid: str) -> Optional[Constraint]:
        return self._constraints.get(cid)

    def get_clarification(self, cid: str) -> Optional[Clarification]:
        return self._clarifications.get(cid)

    # ---- 修改 ----
    def add_parameter(self, p: Parameter) -> None:
        self.parameters.append(p)
        self._params[p.id] = p

    def add_feature(self, f: Feature) -> None:
        self.features.append(f)
        self._features[f.id] = f

    def add_constraint(self, c: Constraint) -> None:
        self.constraints.append(c)
        self._constraints[c.id] = c

    def add_clarification(self, c: Clarification) -> None:
        self.clarifications.append(c)
        self._clarifications[c.id] = c

    def answer_clarification(self, cid: str, answer: Any) -> None:
        c = self.get_clarification(cid)
        if c:
            c.answer = answer

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "intent_id": self.intent_id,
            "operation": self.operation,
            "domain": self.domain,
            "goal": dict(self.goal),
            "environment": dict(self.environment),
            "parameters": [p.to_dict() for p in self.parameters],
            "features": [f.to_dict() for f in self.features],
            "constraints": [c.to_dict() for c in self.constraints],
            "assumptions": list(self.assumptions),
            "clarifications": [c.to_dict() for c in self.clarifications],
            "plan": self.plan,
            "validation": dict(self.validation),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DesignSpec":
        params = [Parameter.from_dict(p) for p in d.get("parameters", [])]
        features = [Feature.from_dict(f) for f in d.get("features", [])]
        constraints = [Constraint.from_dict(c) for c in d.get("constraints", [])]
        clarifications = [Clarification.from_dict(c) for c in d.get("clarifications", [])]
        spec = cls(
            intent_id=d.get("intent_id", ""),
            operation=d.get("operation", "create"),
            domain=d.get("domain", "mechanical_part"),
            schema_version=d.get("schema_version", "1.0"),
            goal=d.get("goal", {"summary": "", "user_text": "", "language": "zh-CN"}),
            environment=d.get("environment", {}),
            parameters=params,
            features=features,
            constraints=constraints,
            assumptions=list(d.get("assumptions", [])),
            clarifications=clarifications,
            plan=d.get("plan"),
            validation=d.get("validation", {}),
            metadata=d.get("metadata", {}),
        )
        spec._reindex()
        return spec


# ===========================================================================
# 参数三向同步规则（需求 §6.3）+ 参数状态流转（需求 §4.2）
# ===========================================================================

def apply_parameter_change(
    spec: DesignSpec,
    pid: str,
    new_value: Any,
    actor: str,  # "user" | "ai" | "model" | "system"
    reason: str = "",
) -> dict:
    """修改参数并遵守同步规则。

    规则：
    - locked 参数不允许任何 actor 覆盖（除非显式 unlock）。
    - AI 提出的修改：写入 pending_change（不落值），不能直接覆盖锁定值。
    - user 修改：参数记为 confirmed；来源变 user。
    - model 修改（Fusion 手工改）：标记 model_override。
    - new_value 超出 [min, max] → status=invalid，拒绝应用。

    返回 {"ok": bool, "parameter": {...}, "pending": bool, "message": str, "conflicts": []}
    """
    p = spec.get_parameter(pid)
    if p is None:
        return {"ok": False, "error": f"parameter not found: {pid}"}

    constraints = _check_constraint_violations(spec, pid, new_value)
    if constraints:
        return {
            "ok": False,
            "status": "conflict",
            "parameter": p.to_dict(),
            "message": f"参数修改违反约束: {constraints}",
            "conflicts": constraints,
        }

    if p.min is not None and isinstance(new_value, (int, float)) and new_value < p.min:
        p.status = "invalid"
        return {"ok": False, "status": "invalid", "parameter": p.to_dict(),
                "message": f"值 {new_value} 低于下限 {p.min}"}
    if p.max is not None and isinstance(new_value, (int, float)) and new_value > p.max:
        p.status = "invalid"
        return {"ok": False, "status": "invalid", "parameter": p.to_dict(),
                "message": f"值 {new_value} 高于上限 {p.max}"}

    if p.locked:
        # 需求 §6.3: 锁定值不可被静默覆盖;AI 修改进入 pending_change
        if actor == "user":
            return {"ok": False, "status": "locked", "parameter": p.to_dict(),
                    "message": f"参数 {p.name} 已锁定,请先解锁"}
        p.status = "locked"
        return {
            "ok": False,
            "status": "locked",
            "parameter": p.to_dict(),
            "message": f"参数 {p.name} 已锁定({actor} 的修改被拒绝)", "pending": True,
        }

    p.value = new_value
    if actor == "user":
        p.source = "user"
        p.status = "confirmed"
    elif actor == "model":
        p.source = "model"
        p.status = "confirmed"
    elif actor == "ai":
        p.status = "needs_confirmation"
    elif actor == "system":
        p.status = "confirmed"
    p._last_change = {"actor": actor, "reason": reason}  # type: ignore[attr-defined]
    return {"ok": True, "status": p.status, "parameter": p.to_dict(), "message": "ok"}


def lock_parameter(spec: DesignSpec, pid: str, locked: bool = True) -> dict:
    p = spec.get_parameter(pid)
    if p is None:
        return {"ok": False, "error": f"parameter not found: {pid}"}
    p.locked = bool(locked)
    if locked and p.status not in ("confirmed", "locked"):
        p.status = "confirmed"
    return {"ok": True, "status": "locked" if locked else "unlocked", "parameter": p.to_dict()}


def _check_constraint_violations(spec: DesignSpec, pid: str, new_value: Any) -> list:
    """约束冲突检测(需求 §4.4: 发生冲突必须输出冲突集合)。

    MVP 支持形式: "paramA == paramB", "paramA >= 2 * paramB" 等简单比较。
    解析失败时返回空(不阻塞执行,由 schema/几何验证兜底)。
    """
    from .formulas import evaluate_expression
    violations = []

    # 构造“参数名 → 值”映射；被修改的参数用新值参与求值
    valmap = {p.name: p.value for p in spec.parameters}
    target = spec.get_parameter(pid)
    if target is not None and target.name in valmap:
        valmap[target.name] = new_value

    for c in spec.constraints:
        try:
            lhs, op, rhs = _split_expression(c.expression)
        except Exception:
            continue
        try:
            lv = evaluate_expression(lhs, valmap)
            rv = evaluate_expression(rhs, valmap)
        except Exception:
            continue
        flag = _compare(lv, op, rv)
        if flag is False:
            violations.append({
                "constraint_id": c.id,
                "expression": c.expression,
                "priority": c.priority,
                "message": c.message or f"约束 {c.expression} 被违反",
            })
    return violations


def evaluate_constraint_expression(expr: str, spec: DesignSpec) -> Optional[bool]:
    """评估一个约束表达式（如 hole_pattern.center == base_sketch.center）。"""
    # MVP: 仅支持数值比较; 几何相等比较由几何验证承担
    try:
        lhs, op, rhs = _split_expression(expr)
    except ValueError:
        return None
    try:
        lv = evaluate_expression(lhs, {p.name: p.value for p in spec.parameters})
        rv = evaluate_expression(rhs, {p.name: p.value for p in spec.parameters})
    except Exception:
        return None
    return _compare(lv, op, rv)


_OPERATORS = ("==", "!=", ">=", "<=", ">", "<")


def _split_expression(expr: str):
    for op in _OPERATORS:
        if op in expr:
            lhs, rhs = expr.split(op, 1)
            return lhs.strip(), op, rhs.strip()
    raise ValueError(f"unsupported expression: {expr}")


def _compare(lv, op: str, rv) -> bool:
    if op == "==":
        return bool(lv == rv)
    if op == "!=":
        return bool(lv != rv)
    if op == ">=":
        return bool(lv >= rv)
    if op == "<=":
        return bool(lv <= rv)
    if op == ">":
        return bool(lv > rv)
    if op == "<":
        return bool(lv < rv)
    return False


def impact_analysis(spec: DesignSpec, pid: str, new_value: Any) -> dict:
    """参数影响分析（需求 §6.4）。返回受影响特征、依赖它的参数、可能风险。"""
    p = spec.get_parameter(pid)
    if p is None:
        return {"ok": False, "error": f"parameter not found: {pid}"}

    affected_features = []
    for f in spec.features:
        if pid in (p.affects or []) or pid in (f.parameters or {}).values() or pid in (f.parameters or {}).keys():
            affected_features.append(f.id)

    # 被公式引用的参数（按 formula 的 AST 引用提取）
    from .formulas import extract_parameter_refs
    dependent_params = []
    for other in spec.parameters:
        if other.expression and other.id != pid:
            refs = extract_parameter_refs(other.expression)
            if pid in refs or p.name in refs:
                dependent_params.append(other.name)

    risks = []
    if p.min is not None and isinstance(new_value, (int, float)) and new_value < p.min:
        risks.append({"severity": "high", "message": f"新值 {new_value} 低于设计下限 {p.min}"})
    if p.max is not None and isinstance(new_value, (int, float)) and new_value > p.max:
        risks.append({"severity": "high", "message": f"新值 {new_value} 高于设计上限 {p.max}"})

    return {
        "ok": True,
        "parameter": p.to_dict(),
        "new_value": new_value,
        "affected_features": affected_features,
        "dependent_parameters": dependent_params,
        "risks": risks,
    }


def detect_model_override(spec: DesignSpec, fusion_params: dict) -> list:
    """检测 Fusion User Parameters 被手工修改(不同于 spec)并标记 model_override。

    返回被覆盖的参数 id 列表。需求 §6.3: 读取回 Design Spec, 并标记为 model_override。
    """
    overridden = []
    for p in spec.parameters:
        fv = fusion_params.get(p.name)
        if fv is not None and p.value is not None:
            try:
                if abs(float(fv) - float(p.value)) > 1e-9:
                    p.source = "model"
                    status = p.status
                    p.status = "confirmed" if status in ("confirmed", "locked") else status
                    overridden.append(p.id)
            except (TypeError, ValueError):
                continue
    return overridden


def find_feature_conflicts(spec: DesignSpec) -> list:
    """返回 features 中 depends_on 引用了不存在特征的集合（供 Schema 验证之外使用）。"""
    bad = []
    for f in spec.features:
        for dep in f.depends_on:
            if dep not in spec._features and not any(p.id == dep for p in spec.parameters):
                bad.append({"feature": f.id, "missing_dependency": dep})
    return bad


def clone_spec(spec: DesignSpec) -> DesignSpec:
    return DesignSpec.from_dict(copy.deepcopy(spec.to_dict()))