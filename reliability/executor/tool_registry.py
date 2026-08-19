"""工具安全台账与分级（需求 §8.1）。

工具分类:
  只读(不需要确认) / 低风险写 / 中风险写 / 高风险(必须确认 + 检查点 + 日志)

execute(任意 Python 脚本) 默认禁用（需求 §8.2 + MVP 范围），
仅当用户在 UI 显式授权（session.execute_enabled）后才可通过门控。
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# 工具 → 风险等级（需求 §8.1 列举）
# ---------------------------------------------------------------------------
READ_TOOLS = {
    "read_document", "read_selection", "read_components", "read_bodies",
    "read_features", "read_parameters", "read_attributes", "capture_view",
}
LOW_RISK_WRITE = {
    "create_user_parameter", "set_attribute", "rename_entity",
    "create_component", "create_sketch",
}
MEDIUM_RISK_WRITE = {
    "create_extrude", "create_fillet", "create_shell", "create_hole",
    "create_pattern", "move_component", "update_feature",
    "create_revolve", "create_chamfer", "create_sketch_entity",
    "create_circular_pattern", "create_rectangular_pattern",
    "create_construction_plane", "update_parameter", "set_material",
    "set_appearance", "undo", "redo", "document_save", "document_close",
    "create_box", "create_cylinder", "create_sphere", "create_torus",
    "create_coil", "project_to_sketch", "create_selection_set",
}
HIGH_RISK_WRITE = {
    "delete_entity", "delete_feature", "replace_body", "run_python",
    "modify_existing_history", "export_file", "overwrite_document",
    "split_body", "timeline_roll", "execute_script", "execute",
}

RISK_LEVEL = {"read": "read", "low": "low", "medium": "medium", "high": "high"}


def classify(tool: str) -> str:
    """返回工具风险等级: read / low / medium / high。"""
    if tool in READ_TOOLS:
        return "read"
    if tool in LOW_RISK_WRITE:
        return "low"
    if tool in MEDIUM_RISK_WRITE:
        return "medium"
    if tool in HIGH_RISK_WRITE or tool.startswith("execute_") or tool == "execute":
        return "high"
    # handlers 兼容名: read → read; create_* / update_* / delete_* 按前缀
    if tool.startswith("read"):
        return "read"
    if tool.startswith("delete") or tool in ("run_python",):
        return "high"
    if tool.startswith("create_"):
        return "medium"
    if tool.startswith("update_"):
        return "medium"
    return "medium"


# execute 类任意脚本（需求 §8.2 明确禁止直通）
EXECUTE_SCRIPT_TOOLS = {"execute", "execute_script", "run_python"}


class ToolGate:
    """执行门控: 检查风险等级(只读直接放行/写操作需授权与会话状态)。"""

    def __init__(self, session=None) -> None:
        self._session = session

    @property
    def execute_enabled(self) -> bool:
        return bool(getattr(self._session, "execute_enabled", False))

    def evaluate(self, tool: str, args: Optional[dict] = None,
                 state: str = "", plan: object = None, node_id: str = "") -> dict:
        """对一次工具调用做门控评估。

        返回 {"allowed": bool, "risk": str, "requires_confirmation": bool,
              "reason": str, "checkpoints": bool, "logged": bool}
        """
        args = args or {}
        risk = classify(tool)

        # 任意脚本工具: 默认禁用
        if tool in EXECUTE_SCRIPT_TOOLS:
            if not self.execute_enabled:
                return {
                    "allowed": False, "risk": risk,
                    "requires_confirmation": True,
                    "reason": "execute 任意脚本工具默认禁用; 请在设置中显式授权后才能使用(需求 §8.2)",
                    "checkpoints": True, "logged": True,
                }
            return {
                "allowed": True, "risk": risk, "requires_confirmation": True,
                "reason": "任意脚本已授权, 但属高风险操作; 强制确认点 + 检查点",
                "checkpoints": True, "logged": True,
            }

        # 只读
        if risk == "read":
            return {
                "allowed": True, "risk": risk, "requires_confirmation": False,
                "reason": "只读查询", "checkpoints": False, "logged": False,
            }

        # 高风险写
        if risk == "high":
            return {
                "allowed": True, "risk": risk, "requires_confirmation": True,
                "reason": "高风险写操作: 必须用户确认 + 执行前检查点 + 写操作日志",
                "checkpoints": True, "logged": True,
            }

        # 低/中风险: 允许(但记录日志), 除非处于 REVIEW/未进入执行状态
        if state and state not in ("EXECUTING", "PAUSED"):
            return {
                "allowed": False, "risk": risk, "requires_confirmation": False,
                "reason": f"会话当前状态 {state} 不允许写操作",
                "checkpoints": False, "logged": True,
            }

        return {
            "allowed": True, "risk": risk, "requires_confirmation": False,
            "reason": "低/中风险写操作", "checkpoints": risk == "medium", "logged": True,
        }


__all__ = ["classify", "ToolGate", "EXECUTE_SCRIPT_TOOLS",
           "READ_TOOLS", "LOW_RISK_WRITE", "MEDIUM_RISK_WRITE", "HIGH_RISK_WRITE"]