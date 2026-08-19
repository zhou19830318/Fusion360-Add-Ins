"""执行策略优先级（需求 §7.4）与风险映射。

策略优先级：原生参数化特征 > 草图驱动特征 > 布尔几何 > 任意脚本
"""

from __future__ import annotations

from typing import Optional

# 策略优先级（数值越小越优先）
STRATEGY_PRIORITY = ["native_fusion_feature", "sketch_driven", "boolean_geometry", "script"]

# 特征类型 → 首选 Fusion 原生工具（对应 handlers/create.pyc 的能力）
NATIVE_TOOL_MAP: dict[str, str] = {
    "user_parameter": "create_user_parameter",
    "sketch": "create_sketch",
    "sketch_entity": "create_sketch_entity",
    "extrude": "create_extrude",
    "revolve": "create_revolve",
    "hole": "create_hole",
    "fillet": "create_fillet",
    "chamfer": "create_chamfer",
    "shell": "create_shell",
    "circular_pattern": "create_circular_pattern",
    "rectangular_pattern": "create_rectangular_pattern",
    "construction_plane": "create_construction_plane",
    "split": "split_body",
    "projection": "project_to_sketch",
    "box": "create_box",
    "cylinder": "create_cylinder",
    "sphere": "create_sphere",
    "torus": "create_torus",
    "coil": "create_coil",
    "cone": "create_cone",
    "fastener_finish": "fastener_finish",
    "material": "set_material",
    "appearance": "set_appearance",
    "selection_set": "create_selection_set",
}

# 风险分级（需求 §8.1）
TOOL_RISK: dict[str, str] = {
    "create_user_parameter": "low",
    "create_sketch": "low",
    "create_sketch_entity": "low",
    "create_box": "low",
    "create_cylinder": "low",
    "create_sphere": "low",
    "create_torus": "low",
    "create_coil": "medium",
    "create_extrude": "medium",
    "create_revolve": "medium",
    "create_hole": "medium",
    "create_fillet": "medium",
    "create_chamfer": "medium",
    "create_shell": "medium",
    "create_circular_pattern": "medium",
    "create_rectangular_pattern": "medium",
    "create_construction_plane": "medium",
    "split_body": "high",
    "project_to_sketch": "low",
    "set_material": "low",
    "set_appearance": "low",
    "create_selection_set": "low",
    "delete_entity": "high",
    "update_parameter": "medium",
    "undo": "medium",
    "redo": "medium",
    "timeline_roll": "high",
    "execute_script": "high",
    "read": "low",
    "screenshot": "low",
    "document_save": "medium",
    "document_open": "low",
    "document_close": "medium",
}

RISK_LABEL = {"low": "低", "medium": "中", "high": "高"}


def tool_for_feature(feature_type: str) -> Optional[str]:
    return NATIVE_TOOL_MAP.get(feature_type)


def risk_for_tool(tool: str) -> str:
    return TOOL_RISK.get(tool, "medium")


def fallback_strategies(feature_type: str) -> list[dict]:
    """按策略优先级列出该特征的可用策略(含替代项)。用于节点 alternative 字段。"""
    native = tool_for_feature(feature_type)
    out = []
    if native:
        out.append({"strategy": "native_fusion_feature", "tool": native,
                    "description": "使用 Fusion 原生参数化特征"})
    if feature_type in ("hole", "extrude", "revolve", "rectangular_pattern", "circular_pattern"):
        out.append({"strategy": "sketch_driven", "tool": f"create_sketch + {native or 'sketch_driven'}",
                    "description": "草图轮廓 + 拉伸/旋转切除"})
    out.append({"strategy": "boolean_geometry", "tool": "boolean_operation",
                "description": "实体布尔(临时实体 + 减除)"})
    out.append({"strategy": "script", "tool": "execute_script",
                "description": "任意脚本(默认禁用，需显式授权)"})
    return out


__all__ = [
    "STRATEGY_PRIORITY", "NATIVE_TOOL_MAP", "TOOL_RISK", "RISK_LABEL",
    "tool_for_feature", "risk_for_tool", "fallback_strategies",
]