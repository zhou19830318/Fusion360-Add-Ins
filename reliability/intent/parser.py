"""规则启发式意图解析器（离线可用，LLM 不可用时的兜底 + 基线）。

从自然语言抽取:
  - 参数(宽/长/高/厚/直径等 + 单位) → user/inferred source + confidence
  - 目标物/特征关键词 → feature 结构
  - 默认假设 → assumptions
  - 歧义 → clarifications(severity 分级)

LLM 结构化生成器(带 JSON Schema 校验与重试)在 reliability/intent/llm.py。
"""

from __future__ import annotations

import re
from typing import Any

from ..ir.design_spec import (
    Clarification, Constraint, DesignSpec, Feature, Parameter,
)

# ---------------------------------------------------------------------------
# 尺寸关键词 → 语义参数名
# ---------------------------------------------------------------------------
_LENGTH_KEYS = {
    "宽": ("width", "宽度"), "宽度": ("width", "宽度"),
    "长": ("length", "长度"), "长度": ("length", "长度"),
    "高": ("height", "高度"), "高度": ("height", "高度"),
    "深": ("depth", "深度"), "深度": ("depth", "深度"),
    "厚": ("thickness", "厚度"), "厚度": ("thickness", "厚度"),
    "壁厚": ("wall_thickness", "壁厚"),
    "直径": ("diameter", "直径"), "内径": ("inner_diameter", "内径"),
    "半径": ("radius", "半径"),
    "圆角": ("fillet_radius", "圆角半径"),
    "倒角": ("chamfer", "倒角"),
    "螺距": ("pitch", "螺距"),
    "齿数": ("tooth_count", "齿数"),
    "数量": ("count", "数量"),
    "角度": ("angle", "角度"),
}

# 带空格的键(如 "圆角半径")优先匹配 —— 排序保证最长匹配优先
_SORTED_KEYS = sorted(_LENGTH_KEYS.keys(), key=len, reverse=True)

_UNIT_FACTORS = {"mm": 1.0, "毫米": 1.0, "厘米": 10.0, "cm": 10.0, "米": 1000.0, "m": 1000.0}
_DEFAULT_UNIT = "mm"


def _find_length_params(text: str) -> list[dict]:
    """抽取 '宽50' / '高 30' / '厚5mm' / '直径 6 毫米' 形式的参数。"""
    found: dict[str, dict] = {}
    # 形如: 关键词[:：=]? 数字 [单位]
    for key in _SORTED_KEYS:
        if key not in _LENGTH_KEYS:
            continue
        name, label = _LENGTH_KEYS[key]
        # "孔直径" 语境 → 汇入 hole_diameter(而非通用 diameter)
        if name == "diameter" and "孔" in text:
            name, label = "hole_diameter", "孔直径"
        # 匹配 "宽50"、"宽 50"、"宽度=50mm"、"直径6毫米"、"高度改为40mm"、"宽度设为 50 毫米"
        pat = re.compile(
            rf"{re.escape(key)}\s*[:：=]?\s*"
            r"(?:改为到|调整为|调整到|设置为|设定为|改成|调成|改为|设为|设)?"
            r"\s*(\d+(?:\.\d+)?)\s*(毫米|厘米|mm|cm|米|m|°|度)?"
        )
        m = pat.search(text)
        if not m:
            continue
        value_raw = float(m.group(1))
        unit = m.group(2) or _DEFAULT_UNIT
        factor = _UNIT_FACTORS.get(unit, 1.0)
        value = round(value_raw * factor, 4)
        if name in found:
            # 保留更精确的一次
            continue
        found[name] = {
            "id": f"param_{name}",
            "name": name,
            "label": label,
            "value": value,
            "unit": "mm",
            "type": "angle" if name == "angle" else "length",
            "source": "user",
            "confidence": 1.0,
            "status": "confirmed",
        }
    return list(found.values())


# ---------------------------------------------------------------------------
# 特征关键词检测
# ---------------------------------------------------------------------------
def _detect_features(text: str, params: list[dict]) -> list[Feature]:
    feats: list[Feature] = []
    n = len(feats)

    def aid(prefix: str) -> str:
        return f"{prefix}_{len(feats)}" if False else f"feature_{prefix}_{n:02d}"

    text_l = text.lower()

    # 孔
    holes = any(k in text_l for k in ("孔", " hole", "holes", "钻孔"))
    if holes:
        has_6mm = re.search(r"(\d+(?:\.\d+)?)\s*(毫米|mm)?\s*(安装孔|孔)", text)
        feats.append(Feature(
            id=aid("hole"),
            type="hole",
            label="打孔" + ("(规格已指定)" if has_6mm else "(规格未指定，需澄清)"),
            semantic_role="mounting_holes",
            parameters={"diameter": "param_hole_diameter"} if params else {},
            strategy="native_fusion_feature",
        ))

    # 阵列 (四角/阵列/分布)
    if any(k in text_l for k in ("阵列", "四角", "分布", "四 个", "4 个", "四个")):
        feats.append(Feature(
            id=aid("hole_pattern"),
            type="rectangular_pattern",
            label="四角安装孔阵列",
            semantic_role="mounting_holes",
            parameters={"quantity_x": "param_hole_count_x", "quantity_y": "param_hole_count_y"},
            strategy="native_fusion_feature",
            risk="medium",
        ))

    # 拉伸 / 主体
    if any(k in text_l for k in ("拉伸", "支架", "板", "壳体", "外壳", "盒", "箱", "座")):
        feats.insert(0, Feature(
            id=aid("base"),
            type="extrude",
            label="基础主体(拉伸)",
            semantic_role="base_geometry",
            strategy="native_fusion_feature",
            risk="low",
        ))

    # 圆角
    if any(k in text_l for k in ("圆角", "倒圆")):
        feats.append(Feature(
            id=aid("fillet"),
            type="fillet",
            label="圆角",
            semantic_role="cosmetic",
            risk="medium",
        ))

    # 倒角
    if "倒角" in text_l:
        feats.append(Feature(
            id=aid("chamfer"),
            type="chamfer",
            label="倒角",
            semantic_role="cosmetic",
            risk="medium",
        ))

    # 抽壳
    if any(k in text_l for k in ("抽壳", "壳", "镂空")):
        feats.append(Feature(
            id=aid("shell"),
            type="shell",
            label="抽壳",
            semantic_role="shell",
            risk="medium",
        ))

    for i, f in enumerate(feats):
        f.id = f"feature_{i:02d}"
    return feats


def _detect_manufacturing(text: str) -> str:
    tl = text.lower()
    if any(k in tl for k in ("3d打印", "打印", "增材", "pla", "fdm", "sla")):
        return "3d_print"
    if any(k in tl for k in ("cnc", "铣", "车", "加工")):
        return "cnc"
    if any(k in tl for k in ("注塑", "模具")):
        return "injection"
    if "钣金" in tl:
        return "sheet_metal"
    return "unknown"


def _detect_domain(text: str) -> str:
    tl = text.lower()
    if any(k in tl for k in ("外壳", "壳体", "盒子", "盒子", "箱", "case", "enclosure", "housing")):
        return "enclosure"
    if any(k in tl for k in ("支架", "bracket", "holder", "mount")):
        return "mechanical_bracket"
    if any(k in tl for k in ("机械", "零件", "part")):
        return "mechanical_part"
    return "mechanical_part"


class RuleIntentParser:
    """纯规则解析器：零网络、确定性、可单测。"""

    def parse(self, user_text: str, language: str = "zh-CN") -> DesignSpec:
        text = (user_text or "").strip()
        params_raw = _find_length_params(text)

        params: list[Parameter] = []
        for d in params_raw:
            p = Parameter(**{k: v for k, v in d.items() if k in Parameter.__dataclass_fields__})
            p.affects = []
            params.append(p)

        # 孔规格参数: “6mm 安装孔” → hole_diameter
        hole_m = re.search(r"(\d+(?:\.\d+)?)\s*(毫米|mm)?\s*(安装孔|孔)", text)
        if hole_m:
            val = float(hole_m.group(1)) * _UNIT_FACTORS.get(hole_m.group(2) or "mm", 1.0)
            params.append(Parameter(
                id="param_hole_diameter", name="hole_diameter", value=val,
                label="安装孔直径", unit="mm", type="length", source="user",
                confidence=0.95, status="confirmed",
            ))

        # ── 紧固件语义(螺栓/螺丝/螺母 + M 规格) ──
        has_bolt_like = any(k in text for k in ("螺栓", "螺丝", "螺钉", "螺杆", "沉头螺丝"))
        has_nut = "螺母" in text
        spec_m = re.search(r"M(\d+(?:\.\d+)?)", text, re.IGNORECASE)
        if has_bolt_like or has_nut:
            nominal = float(spec_m.group(1)) if spec_m else None
            if nominal is not None and not any(p.name == "nominal_diameter" for p in params):
                params.append(Parameter(
                    id="param_nominal_diameter", name="nominal_diameter", value=nominal,
                    unit="mm", type="length", source="user", confidence=1.0,
                    status="confirmed", label="螺纹公称直径",
                ))
            n = nominal or 5.0
            # 粗牙螺距按公称直径查表(MVP 常用表)
            pitch_map = {2: 0.4, 2.5: 0.45, 3: 0.5, 3.5: 0.6, 4: 0.7, 5: 0.8,
                         6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75}
            pitch = pitch_map.get(round(n), 0.8)
            if not any(p.name == "thread_pitch" for p in params):
                params.append(Parameter(
                    id="param_thread_pitch", name="thread_pitch", value=pitch,
                    unit="mm", type="length", source="default", confidence=0.8,
                    status="needs_confirmation", label="螺距(粗牙)",
                    description=f"M{round(n)} 粗牙默认螺距 {pitch}mm",
                ))
            if has_bolt_like:
                if not any(p.name == "bolt_length" for p in params):
                    params.append(Parameter(
                        id="param_bolt_length", name="bolt_length", value=15.0,
                        unit="mm", type="length", source="default", confidence=0.6,
                        status="needs_confirmation", label="公称长度(含头)",
                        description="公称长度未指定, 默认 15mm",
                    ))
                # 头部类型: 沉头/盘头/杯头
                head_type = "countersunk" if "沉头" in text else "hex"
                if not any(p.name == "head_type" for p in params):
                    params.append(Parameter(
                        id="param_head_type", name="head_type", value=head_type,
                        unit="", type="other", source="user", confidence=1.0,
                        status="confirmed", label="头部类型",
                    ))
                if head_type == "countersunk":
                    head_d = round(max(n * 2.0, 10.4), 1)  # 沉头大端 ≈2D(≥10.4)
                    if not any(p.name == "head_diameter" for p in params):
                        params.append(Parameter(
                            id="param_head_diameter", name="head_diameter", value=head_d,
                            unit="mm", type="length", source="inferred", confidence=0.6,
                            status="needs_confirmation", label="沉头大端直径",
                            description="沉头大端直径(约 2×公称直径)",
                        ))
                    if not any(p.name == "head_radius" for p in params):
                        params.append(Parameter(
                            id="param_head_radius", name="head_radius", value=round(head_d / 2, 2),
                            unit="mm", type="length", source="derived", confidence=1.0,
                            status="confirmed", label="沉头大端半径(D/2)",
                        ))
                if not any(p.name == "head_height" for p in params):
                    # 90° 沉头锥台高 = (大端直径-小端直径)/2 (M5: (10.4-5)/2=2.7)
                    params.append(Parameter(
                        id="param_head_height", name="head_height", value=round((head_d - n) / 2, 2),
                        unit="mm", type="length", source="inferred", confidence=0.6,
                        status="needs_confirmation", label="头部高度(锥台高)",
                        description="沉头锥台高度 = (大端-小端)/2",
                    ))
                if not any(p.name == "head_small_radius" for p in params):
                    params.append(Parameter(
                        id="param_head_small_radius", name="head_small_radius", value=round(n / 2, 2),
                        unit="mm", type="length", source="derived", confidence=1.0,
                        status="confirmed", label="沉头小端半径(=D/2=螺杆半径)",
                    ))
                if not any(p.name == "bolt_rod_radius" for p in params):
                    params.append(Parameter(
                        id="param_bolt_rod_radius", name="bolt_rod_radius", value=round(n / 2, 2),
                        unit="mm", type="length", source="derived", confidence=1.0,
                        status="confirmed", label="螺杆半径(=D/2)",
                    ))
            if has_nut:
                if not any(p.name == "nut_across_flats" for p in params):
                    params.append(Parameter(
                        id="param_nut_across_flats", name="nut_across_flats",
                        value=round(n * 1.6, 1), unit="mm", type="length",
                        source="inferred", confidence=0.75, status="needs_confirmation",
                        label="螺母对边宽度",
                        description="六角螺母对边宽度(约 1.6×公称直径)",
                    ))
                if not any(p.name == "nut_height" for p in params):
                    params.append(Parameter(
                        id="param_nut_height", name="nut_height",
                        value=round(n * 0.9, 1), unit="mm", type="length",
                        source="inferred", confidence=0.7, status="needs_confirmation",
                        label="螺母高度",
                        description=f"M{round(n)} 标准螺母高度(约 0.9×公称直径)",
                    ))
                if not any(p.name == "nut_od_radius" for p in params):
                    params.append(Parameter(
                        id="param_nut_od_radius", name="nut_od_radius",
                        value=round(n * 0.92, 2), unit="mm", type="length",
                        source="derived", confidence=0.9, status="confirmed",
                        label="螺母外接圆半径(≈0.92D)",
                    ))

        # 未指定圆角半径默认
        if "圆角" in text and not any(p.name == "fillet_radius" for p in params):
            params.append(Parameter(
                id="param_fillet_radius", name="fillet_radius", value=1.0,
                label="圆角半径", unit="mm", type="length", source="default",
                confidence=0.7, status="needs_confirmation",
                description="默认圆角 1mm（可默认问题）",
            ))

        # 壁厚默认(外壳类)
        if any(p.name == "wall_thickness" for p in params) is False and "壳" in text:
            params.append(Parameter(
                id="param_wall_thickness", name="wall_thickness", value=2.0,
                label="壁厚", unit="mm", type="length", source="default",
                confidence=0.7, status="needs_confirmation",
                description="默认壁厚 2mm（可默认问题）",
            ))

        # L 形支架深度默认
        if "l" in text.lower() and "支架" in text and not any(p.name == "depth" for p in params):
            params.append(Parameter(
                id="param_depth", name="depth",
                value=10,
                label="支架深度", unit="mm", type="length", source="default",
                confidence=0.5, status="needs_confirmation",
                description="L 形支架深度未指定，默认 10mm，可修改",
            ))

        features = _detect_features(text, params)
        # 紧固件特征标记(供 planner 专用规划)
        if has_bolt_like:
            features.append(Feature(
                id="feature_bolt", type="bolt", label="螺栓/螺钉主体",
                semantic_role="bolt_subject", strategy="native_fusion_feature",
            ))
        if has_nut:
            features.append(Feature(
                id="feature_nut", type="nut", label="配套防松螺母",
                semantic_role="nut", strategy="native_fusion_feature",
            ))

        # 依赖关系: 特征按创建顺序
        base_ids = [f.id for f in features if f.type == "extrude" or f.id.endswith("_base")]
        for f in features:
            if f.type in ("hole", "hole_pattern", "fillet", "chamfer", "shell"):
                if base_ids:
                    f.depends_on = list(base_ids)

        # 澄清: 单位不明确 → blocking
        clarifications: list[Clarification] = []
        # 螺纹规格(M5 等)隐含公制 mm, 不再误判为存在单位
        has_spec = bool(re.search(r"M\d(?:\.\d+)?", text, re.IGNORECASE))
        if not has_spec and not re.search(r"(毫米|厘米|mm|cm|英寸|inch|米|m)", text, re.IGNORECASE):
            clarifications.append(Clarification(
                id="clarify_units",
                question="所有尺寸的单位是什么？（默认按毫米处理）",
                reason="单位不明确会影响全部参数与几何结果",
                severity="blocking",
                options=[{"id": "mm", "label": "毫米 mm"}, {"id": "cm", "label": "厘米 cm"},
                          {"id": "m", "label": "米 m"}],
                default="mm",
            ))

        # 澄清: 孔规格不明确
        if "孔" in text and not hole_m:
            clarifications.append(Clarification(
                id="clarify_hole_spec",
                question="孔的直径是多少？",
                reason="孔规格不明确无法安全建模",
                severity="blocking",
                default="6 mm",
                related_features=[f.id for f in features if f.type in ("hole", "hole_pattern")],
            ))

        assumptions = [
            f"单位按 {_DEFAULT_UNIT} 处理（未显式声明时）",
            "坐标原点位于主体中心，XY 为底面，+Z 向上",
        ]
        if not any(p.name == "wall_thickness" for p in params) and "壳" in text:
            assumptions.append("默认壁厚 2mm")
        if "圆角" in text and not any(p.name == "fillet_radius" for p in params):
            assumptions.append("默认圆角 1mm")

        manufacturing = _detect_manufacturing(text)
        env = {
            "document_units": "mm",
            "active_component": None,
            "selection_refs": [],
            "design_history_required": True,
            "manufacturing_method": manufacturing,
        }

        goal = {
            "summary": _summarize(text),
            "user_text": text,
            "language": language,
        }

        spec = DesignSpec(
            operation="create" if not any(k in text for k in ("修改", "删除", "加圆角", "更改", "加上")) else "modify",
            domain=_detect_domain(text),
            goal=goal,
            environment=env,
            parameters=params,
            features=features,
            assumptions=assumptions,
            clarifications=clarifications,
        )
        spec.metadata["parser"] = "rule/v1"
        if has_bolt_like or has_nut:
            spec.metadata["fastener"] = True
        return spec


def _summarize(text: str) -> str:
    t = text.strip()
    return t[:80] + ("…" if len(t) > 80 else "")


__all__ = ["RuleIntentParser", "_find_length_params", "_detect_features"]