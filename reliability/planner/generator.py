"""Plan 生成器（MVP: 模板规则 + 可选的 LLM 结构化输出合并）。

生成原则（需求 §7）：
- 每个关键节点带 checkpoint 标记
- 高风险节点 requires_confirmation=True
- 策略遵循: 原生参数化特征 > 草图驱动 > 布尔几何 > 脚本
"""

from __future__ import annotations

from typing import Optional

from ..ir.design_spec import DesignSpec, Feature
from .plan import Plan, PlanNode
from .strategies import NATIVE_TOOL_MAP, risk_for_tool, fallback_strategies

_LAYOUT_FEATURES = ("shell", "fillet", "chamfer")  # 靠后执行的修饰特征

# 尚未完成/不稳定的几何工具: 对应节点必须经用户确认
# 注: create_hole 的 cut(fusion_api create_cut_holes) 在 Fusion 2704 有内核崩溃风险
# (ObjectCollection profiles + setAllExtent 组合曾致进程退出), 因此视为实验性需确认;
# 其余 sketch/extrude 等待 fusion_api 几何桥。
UNBRIDGED_TOOLS = {
    "create_extrude", "create_revolve", "create_hole", "create_shell",
    "create_fillet", "create_chamfer",
    "create_circular_pattern", "create_rectangular_pattern",
    "create_sketch", "create_sketch_entity", "create_construction_plane",
    "split_body",
}

# LLM/规则输出的自由语义特征类型 → 规范特征类型（对应原生工具映射）
FEATURE_TYPE_ALIASES: dict[str, str] = {
    "solid_body": "extrude",
    "base": "extrude",
    "base_geometry": "extrude",
    "extrude": "extrude",
    "box": "box",
    "cylinder": "cylinder",
    "sphere": "sphere",
    "torus": "torus",
    "coil": "coil",
    "holes": "hole",
    "hole": "hole",
    "mounting_hole": "hole",
    "mounting_holes": "hole",
    "rectangular_pattern": "rectangular_pattern",
    "pattern": "rectangular_pattern",
    "circular_pattern": "circular_pattern",
    "fillet": "fillet",
    "chamfer": "chamfer",
    "shell": "shell",
    "revolve": "revolve",
    "sketch": "sketch",
    "sketch_entity": "sketch_entity",
    "construction_plane": "construction_plane",
    "split": "split",
    "projection": "projection",
    "material": "material",
    "appearance": "appearance",
}


def normalize_feature_type(raw: str) -> str:
    """把 LLM/规则输出的任意语义特征类型归一为规范类型；无法识别时原样返回。"""
    if not raw:
        return ""
    key = raw.lower().strip()
    key = key.replace("-", "_")
    return FEATURE_TYPE_ALIASES.get(key, raw)


def _is_l_bracket(spec: DesignSpec) -> bool:
    goal = f"{spec.goal.get('summary', '')} {spec.goal.get('user_text', '')}"
    return "l" in goal.lower() and ("支架" in goal or "bracket" in goal.lower())


def _param_id(spec: DesignSpec, name: str) -> Optional[str]:
    p = spec.get_parameter_by_name(name)
    return p.id if p else None


def _param_value(spec: DesignSpec, name: str, default: float) -> float:
    """取参数数值(mm 基准); 缺失/无值回退默认。用于坐标计算。"""
    p = spec.get_parameter_by_name(name)
    if p is not None and isinstance(p.value, (int, float)):
        return float(p.value)
    return float(default)


def _build_l_bracket_features(spec: DesignSpec) -> bool:
    """把 L 形 base_geometry 特征分解为 腹板+立板 两个 box 特征(原语组合)。

    布局: 腹板(宽=width,高=thickness,深=depth), 立板(宽=thickness,高=height,深=depth)。
    中心坐标用表达式字符串, Fusion 端对 user parameter 求值(探针已验证)。
    返回是否发生分解。"""
    base = [f for f in spec.features
            if f.enabled and (f.type in ("extrude", "solid_body", "base_geometry")
                              or f.semantic_role == "base_geometry")]
    if not base:
        return False
    base[0].enabled = False  # 原特征停用, 由两个 box 替代

    w = _param_id(spec, "width") or "50"
    h = _param_id(spec, "height") or "30"
    t = _param_id(spec, "thickness") or "5"
    d = _param_id(spec, "depth") or "10"

    # 坐标直接用数值(mm)——不依赖 Fusion 表达式对参数名的求值(实测 evaluateExpression
    # 对 '-thickness / 2' 类表达式在新文档中不稳定), 由 runner 转 "x mm" 表达式。
    wv = _param_value(spec, "width", 50.0)
    hv = _param_value(spec, "height", 30.0)
    tv = _param_value(spec, "thickness", 5.0)
    dv = _param_value(spec, "depth", 10.0)

    spec.add_feature(Feature(
        id="feature_l_web", type="box", label="L形支架腹板",
        semantic_role="base_geometry",
        parameters={"width": w, "height": t, "length": d,
                    "center": [0.0, -tv / 2.0, 0.0]},
        strategy="native_fusion_feature",
    ))
    spec.add_feature(Feature(
        id="feature_l_leg", type="box", label="L形支架立板",
        semantic_role="base_geometry", depends_on=["feature_l_web"],
        parameters={"width": t, "height": h, "length": d,
                    "center": [0.0, tv / 2.0 + hv / 2.0, 0.0]},
        strategy="native_fusion_feature",
    ))
    return True


def _feature_layer(f) -> int:
    """特征层级(决定执行顺序): 0=基础几何, 1=孔/阵列等实体特征, 2=修饰(倒角/圆角/抽壳)。"""
    if f.type in _LAYOUT_FEATURES:
        return 2
    if f.semantic_role in ("mounting_holes", "holes") or f.type in (
            "hole", "rectangular_pattern", "circular_pattern"):
        return 1
    return 0


def _build_fastener_features(spec: DesignSpec) -> bool:
    """紧固件规划(螺栓/螺丝 + 螺母): 圆柱(+锥台)原语组合。

    沉头头部实现: cone_mode=True 用 create_cone(两点式锥台, 需 Fusion 2704 实测);
    默认(稳定)用 3 段递减直径圆柱近似 90° 锥面(不依赖新 API)。
    """
    cone_mode = bool(spec.metadata.get("cone_mode"))
    rod_r = _param_id(spec, "bolt_rod_radius") or "2.5"
    length = _param_id(spec, "bolt_length") or "15.0"
    head_r = _param_id(spec, "head_radius") or "5.2"
    head_sr = _param_id(spec, "head_small_radius") or "2.5"
    head_h = _param_id(spec, "head_height") or "2.7"
    nut_h = _param_id(spec, "nut_height") or "4.5"
    nut_r = _param_id(spec, "nut_od_radius") or "4.6"

    def hval(name, default):
        p = spec.get_parameter_by_name(name)
        if p and isinstance(p.value, (int, float)):
            return float(p.value)
        return float(default)

    L = hval("bolt_length", 15.0)
    hh = hval("head_height", 2.7)
    nh = hval("nut_height", 4.5)
    rod_len = max(L - hh, 1.0)   # 总长含头: 杆 = L - 头高
    rod_half = rod_len / 2.0
    head_center_z = rod_half + hh / 2.0          # 锥台中心(总高 L)
    nut_center_z = L + 2.0 + nh / 2.0            # 螺母伸出 ~2mm

    def add_feature(fid, ftype, label, pars, deps, role):
        if spec.get_feature(fid) is None:
            spec.add_feature(Feature(
                id=fid, type=ftype, label=label, semantic_role=role,
                parameters=pars, depends_on=deps, strategy="native_fusion_feature",
            ))

    add_feature("feature_bolt_rod", "cylinder", "螺杆(Φ5×杆长)",
                {"radius": rod_r, "height": f"{rod_len} mm",
                 "center": [0.0, 0.0, 0.0]}, [], "bolt_rod")
    if cone_mode:
        add_feature("feature_bolt_head", "cone", "沉头头部(90°锥台 Φ10.4→Φ5)",
                    {"radius_big": f"{head_r}", "radius_small": f"{head_sr}",
                     "height": f"{head_h}", "center": [0.0, 0.0, head_center_z]},
                    ["feature_bolt_rod"], "bolt_head")
    else:
        # 多段递减直径圆柱近似 90° 锥面(稳定, 不经 createCylinderOrCone)
        rb = hval("head_radius", 5.2)
        rs = hval("head_small_radius", 2.5)
        n = 3
        head_bottom = rod_half
        for i in range(n):
            r_bot = rb - (rb - rs) * i / n
            seg_h = hh / n
            add_feature(f"feature_bolt_head_seg{i}", "cylinder",
                        f"沉头锥段{i + 1}/{n}",
                        {"radius": f"{round((r_bot + r_bot - (rb - rs) / n) / 2, 3)} mm",
                         "height": f"{round(seg_h, 3)} mm",
                         "center": [0.0, 0.0, round(head_bottom + seg_h / 2, 3)]},
                        ["feature_bolt_rod"] + [f"feature_bolt_head_seg{j}"
                                                 for j in range(i)], "bolt_head")
            head_bottom += seg_h
    add_feature("feature_nut_body", "cylinder", "配套螺母(Φ9.2 近似)",
                {"radius": f"{nut_r}", "height": f"{nut_h}",
                 "center": [0.0, 0.0, nut_center_z]}, [], "nut")
    # 收尾节点: 零件合并 + 真实螺纹(由执行器 fastener_finish 分支实现)
    if spec.get_feature("feature_fastener_finish") is None:
        spec.add_feature(Feature(
            id="feature_fastener_finish", type="fastener_finish",
            label="紧固件收尾(零件合并+真实螺纹)",
            semantic_role="finish",
            depends_on=(["feature_bolt_rod"]
                        + (["feature_bolt_head"] if cone_mode
                            else [f"feature_bolt_head_seg{i}" for i in range(3)])
                        + ["feature_nut_body"]),
            strategy="native_fusion_feature",
        ))
    return True


def generate_plan(spec: DesignSpec, options: Optional[dict] = None) -> Plan:
    """从 DesignSpec 生成 Plan。

    节点编排：
      step 1   create_user_parameters（检查点1：参数创建完成）
      step n   feature → 执行节点（映射原生工具；依赖按 feature.depends_on）
      末尾     （可选）save / verify 节点由执行器负责，不入 DAG
    检查点规则（需求 §9）：
      Checkpoint 1: 参数创建完成
      Checkpoint 2: 基础实体（首个非修饰特征）完成
      高风险节点执行前: 自动补检查点
    """
    options = options or {}
    # 紧固件强制规划: 只要设计意图含紧固件语义(无论解析来源/特征形态),
    # 统一走 杆+沉头锥台+螺母 圆柱/锥台原语组合(避免 LLM 特征名不一致导致未映射)
    fastener_hint = (
        spec.metadata.get("fastener")
        or any(k in f"{spec.goal.get('summary', '')} {spec.goal.get('user_text', '')}"
               for k in ("螺栓", "螺丝", "螺钉", "螺杆"))
        or any(f.type in ("bolt", "nut") or f.semantic_role in ("bolt_subject", "nut")
               for f in spec.features)
    )
    if fastener_hint:
        _build_fastener_features(spec)  # 先补齐组合特征
        gen = {"feature_bolt_rod", "feature_bolt_head", "feature_nut_body",
                "feature_fastener_finish"}
        for f in list(spec.features):
            if f.id not in gen and not f.id.startswith("feature_bolt_head_seg"):
                f.enabled = False  # 仅保留紧固件组合, 禁用解析来源的占位特征
        spec._reindex()
    # L形支架: 分解为腹板+立板 box 原语组合(探针实证 Fusion box 契约可用)
    if _is_l_bracket(spec):
        _build_l_bracket_features(spec)
        spec._reindex()
    plan = Plan()

    # ── 1. 参数创建节点 ──
    param_ids = [p.id for p in spec.parameters if p.source != "derived"]
    if param_ids:
        plan.add_node(PlanNode(
            id="step_create_params",
            type="create_user_parameters",
            label="创建设计参数 (Fusion User Parameters)",
            depends_on=[],
            inputs=param_ids,
            outputs=param_ids,
            status="pending",
            risk="low",
            checkpoint=True,
            strategy="native_fusion_feature",
            verification="核对用户参数数量与名称/表达式",
            alternative="参数重复创建时跳过(幂等)",
        ))

    features = [f for f in spec.features if f.enabled]
    # 执行顺序: 基础几何(0) → 孔/阵列(1) → 修饰特征(2, 圆角/倒角/抽壳靠后避免影响后续特征)
    ordered = sorted(features, key=lambda f: (_feature_layer(f), 0))

    feat_node_ids: dict[str, str] = {}
    for idx, f in enumerate(ordered, start=1):
        ftype = normalize_feature_type(f.type)
        tool = NATIVE_TOOL_MAP.get(ftype)
        if not tool:
            # 无原生映射: 生成一个占位脚本节点(默认禁用,需显式授权)
            plan.add_node(PlanNode(
                id=f"step_{f.id}",
                type=f"feature_{f.type}",
                label=f"{f.label or f.type}（无原生工具映射）",
                depends_on=[feat_node_ids[d] for d in f.depends_on if d in feat_node_ids],
                inputs=list(f.parameters.values()) or [],
                outputs=[f.id],
                risk="high",
                checkpoint=True,
                requires_confirmation=True,
                strategy="script",
                notes="该特征类型尚无受限工具映射；若需执行必须由用户在 UI 显式授权任意脚本。",
                alternative="建议改用结构化工具(extrude/revolve/hole)实现等效特征",
                verification="目视 + 实体数量核对",
                modifies_existing=False,
            ))
            feat_node_ids[f.id] = f"step_{f.id}"
            continue

        deps = [feat_node_ids[d] for d in f.depends_on if d in feat_node_ids]
        if param_ids:
            deps = ["step_create_params"] + deps
        risk = f.risk if f.risk in ("low", "medium", "high") else risk_for_tool(tool)
        alt = fallback_strategies(ftype)
        nid = f"step_{f.id}"
        need_confirm = (risk == "high") or f.requires_confirmation or tool in UNBRIDGED_TOOLS
        plan.add_node(PlanNode(
            id=nid,
            type=tool,
            label=f.label or f"{f.type} ({f.semantic_role or 'feature'})",
            depends_on=deps,
            inputs=list(f.parameters.values()) or [],
            outputs=[f.id],
            risk=risk,
            checkpoint=(risk == "high") or (idx == 1 and ftype not in _LAYOUT_FEATURES),
            requires_confirmation=need_confirm,
            strategy=f.strategy if f.strategy in ("native_fusion_feature", "sketch_driven",
                                                  "boolean_geometry", "script") else "native_fusion_feature",
            notes=f.semantic_role or "",
            verification=f"执行后核对 {f.outputs_hint if hasattr(f,'outputs_hint') else '实体数量与关键尺寸'}",
            alternative=alt[1]["description"] if len(alt) > 1 else alt[0]["description"],
            modifies_existing=(spec.operation == "modify"),
        ))
        feat_node_ids[f.id] = nid

    # 校验
    errs = plan.validate()
    if errs:
        raise ValueError(f"生成的计划自检失败: {errs}")
    plan._index = {n.id: n for n in plan.nodes}
    return plan


def apply_user_edits(plan: Plan, edits: list[dict]) -> Plan:
    """应用用户对计划的编辑（需求 §7.3），返回 version+1 的计划副本。

    支持操作:
      {"action": "disable", "node": "step_x"}
      {"action": "enable",  "node": "step_x"}
      {"action": "delete",  "node": "step_x"}
      {"action": "insert_pause", "node": "step_x"}   → 在此节点后插入暂停点
      {"action": "append_confirm", "node": "step_x"} → 对该节点设置确认点
    """
    new_plan = plan.bumped()
    for edit in edits:
        action = edit.get("action")
        nid = edit.get("node")
        node = new_plan.node(nid)
        if action == "disable" and node:
            node.status = "skipped"
        elif action == "enable" and node:
            if node.status == "skipped":
                node.status = "pending"
        elif action == "delete" and node:
            new_plan.remove_node(nid)
        elif action == "insert_pause" and node:
            idx = new_plan.nodes.index(node)
            new_plan.nodes.insert(idx + 1, PlanNode(
                id=f"pause_{new_plan.version}_{nid}",
                order=0, type="pause", label="⏸ 暂停点",
                depends_on=[nid], risk="low",
            ))
            new_plan._renumber()
            new_plan._index = {n.id: n for n in new_plan.nodes}
        elif action == "append_confirm" and node:
            node.requires_confirmation = True
        elif action == "move":
            # {"action":"move","node":"a","before":"b"}
            before_id = edit.get("before")
            if node:
                new_plan.nodes.remove(node)
                node.depends_on = []
                if before_id and new_plan.node(before_id):
                    bi = new_plan.nodes.index(new_plan.node(before_id))
                    new_plan.nodes.insert(bi, node)
                else:
                    new_plan.nodes.append(node)
                new_plan._renumber()
                new_plan._index = {n.id: n for n in new_plan.nodes}
    errs = new_plan.validate()
    if errs:
        raise ValueError(f"计划编辑后自检失败: {errs}")
    return new_plan


__all__ = ["generate_plan", "apply_user_edits"]