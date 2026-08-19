"""Fusion 后端适配器（黑盒契约 + 上层可靠层）。

FusionBackend：Fusion 进程内直接调用 handlers(.*.pyc) 的 dispatch 契约。
FakeBackend：本地测试/演示用的内存模拟后端，不依赖 Fusion。

契约（已从 bridge/palette.pyc + handlers/__init__.pyc 核实）：
    handlers.dispatch(tool_name: str, args: dict) -> JSON 字符串或 dict
    read/create/update/delete/execute 各模块实现 handle(args) -> ok/err
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

# 3.14 pyc 由 Fusion 内嵌解释器加载；本地 3.12 环境将其标记为不可用
DEFERRED = object()


def _import_handlers():
    """在 Fusion 进程内导入 handlers 包（在该环境字节码版本匹配）。"""
    try:
        import handlers  # type: ignore
        return handlers
    except Exception:
        return None


class FusionBackend:
    """Fusion 端后端。在 Fusion 内嵌 Python 中运行。"""

    def __init__(self, handlers_module=None, log=None) -> None:
        self._handlers = handlers_module or _import_handlers()
        self._log = log
        self._snapshot: Optional[dict] = None

    @property
    def available(self) -> bool:
        return self._handlers is not None

    # ------------------------------------------------------------------
    def call(self, tool: str, args: Optional[dict] = None) -> dict:
        """调用底层工具，返回统一 dict。调用载荷记入日志便于排障。"""
        args = args or {}
        if self._log is not None:
            try:
                self._log.info(f"backend.call({tool})", actor="executor",
                               category="execute", data={"args": args})
            except Exception:
                pass
        if not self.available:
            return {"error": "Fusion 后端不可用(handlers 未导入)"}
        handlers = self._handlers
        dispatch = getattr(handlers, "dispatch", None)
        if dispatch is None:
            return {"error": "handlers.dispatch 不可用"}
        raw = dispatch(tool, args)
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                return parsed
            return {"output": parsed, "raw": raw[:800] if isinstance(raw, str) else raw}
        except Exception:
            return {"output": str(raw)[:2000], "raw": str(raw)[:2000]}

    def read(self, args: Optional[dict] = None, *, query_type: str = "") -> dict:
        a = dict(args or {})
        a.setdefault("queryType", query_type)
        return self.call("read", a)

    # ------------------------------------------------------------------
    # 检查点快照 (需求 §9: 文档级备份)
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """执行前快照: 用户参数 + timeline + 实体名清单 + 文档名。

        entities 记录压缩名称供对象级补偿(删除快照后新增实体)使用。
        """
        snap = {"parameters": {}, "timeline_index": None, "entities": [], "body_names": []}
        try:
            r = self.call("read", {"queryType": "userParameters"})
            if isinstance(r, dict) and r.get("ok"):
                params = r.get("value") or r.get("parameters") or []
                if isinstance(params, list):
                    snap["parameters"] = {
                        (p.get("name") if isinstance(p, dict) else str(p)): p
                        for p in params
                    }
        except Exception:
            pass
        try:
            r = self.call("read", {"queryType": "timeline"})
            if isinstance(r, dict) and r.get("ok"):
                snap["timeline_index"] = r
        except Exception:
            pass
        try:
            r = self.call("read", {"queryType": "bodies"})
            if isinstance(r, dict) and r.get("ok"):
                bodies = r.get("bodies") or []
                snap["body_names"] = [b.get("name") for b in bodies if isinstance(b, dict)]
        except Exception:
            pass
        self._snapshot = snap
        return snap

    def rollback(self, checkpoint: dict) -> dict:
        """回滚到检查点（需求 §9）:
        1. 尝试 Fusion 原生回滚 timeline(rollTo marker) —— 由 update 工具提供
        2. 对象级补偿: 删除快照之后新建的实体
        3. 恢复被修改的参数为检查点时的值
        """
        data = checkpoint.get("data", {})
        results = []
        # 1) timeline rollback
        marker = data.get("timeline_marker_index")
        if isinstance(marker, (int, float)):
            r = self.call("update", {"featureType": "timeline",
                                     "action": "rollTo", "index": int(marker)})
            results.append({"step": "timeline_roll", "result": r})
        # 2) 对象级补偿: 删除快照后新增的实体(按名称)
        try:
            before = set(data.get("body_names") or [])
            r = self.call("read", {"queryType": "bodies"})
            current = []
            if isinstance(r, dict) and r.get("ok"):
                current = [b.get("name") for b in (r.get("bodies") or []) if isinstance(b, dict)]
            to_delete = [n for n in current if n not in before]
            if to_delete:
                dr = self.call("delete", {"names": to_delete})
                results.append({"step": "compensate_delete", "deleted": to_delete,
                                "result": dr})
        except Exception as e:
            results.append({"step": "compensate_delete", "error": str(e)})
        # 3) 参数恢复
        params_before = data.get("parameters", {})
        for name, before in params_before.items():
            if isinstance(before, dict) and "value" in before:
                r = self.call("update", {"featureType": "userParameter",
                                         "name": name, "value": before["value"]})
                results.append({"step": f"restore_param:{name}", "result": r})
        return {"ok": True, "steps": results}

    def model_context(self) -> dict:
        """结构化模型上下文（需求 MVP: 结构化模型上下文）。"""
        ctx: dict[str, Any] = {"document": {}, "components": [], "parameters": []}
        try:
            r = self.call("read", {"queryType": "document"})
            if isinstance(r, dict):
                ctx["document"] = r
        except Exception:
            pass
        try:
            r = self.call("read", {"queryType": "userParameters"})
            if isinstance(r, dict):
                ctx["parameters"] = r.get("value") or r.get("parameters") or []
        except Exception:
            pass
        try:
            r = self.call("read", {"queryType": "bodies"})
            if isinstance(r, dict):
                ctx["bodies"] = r
        except Exception:
            pass
        return ctx

    # ------------------------------------------------------------------
    # 真实 adsk 桥(BOM/属性追溯, 不依赖 handlers .pyc)
    # ------------------------------------------------------------------
    def _fusion_api(self):
        try:
            from . import fusion_api
            return fusion_api
        except Exception:
            return None

    def ensure_design(self) -> dict:
        """确保活动设计文档存在(冷启动自动新建), 返回状态。"""
        fa = self._fusion_api()
        if fa is None:
            return {"ok": False, "error": "fusion_api unavailable"}
        try:
            des = fa.ensure_design()
            return {"ok": True, "design": getattr(des, "name", "?") if des else None}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @property
    def fusion_api_available(self) -> bool:
        return self._fusion_api() is not None and self.available

    def set_ai_attributes(self, body_name: str, attrs: dict) -> dict:
        """给执行器创建的实体写 AI 属性标签(需求 §12.1)。"""
        fa = self._fusion_api()
        if fa is None:
            return {"error": "fusion_api unavailable (must run inside Fusion)"}
        try:
            return fa.set_attributes_by_name(body_name, attrs, group=fa.ATTR_GROUP)
        except Exception as e:
            return {"error": str(e), "set_ok": False}

    def collect_bom(self) -> list:
        """采集设计内 BOM 行(需求 §12)。"""
        fa = self._fusion_api()
        if fa is None:
            return []
        try:
            return fa.collect_bom(group=fa.ATTR_GROUP)
        except Exception:
            return []

    def create_cut_holes(self, points_xy_mm: list, diameter_mm: float) -> dict:
        """贯通圆孔(真实 adsk 桥, 需求: 安装孔)。"""
        fa = self._fusion_api()
        if fa is None:
            return {"ok": False, "error": "fusion_api unavailable"}
        try:
            return fa.create_cut_holes(points_xy_mm, diameter_mm)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def create_cone(self, radius_big_mm: float, radius_small_mm: float,
                    height_mm: float, center_xyz=(0.0, 0.0, 0.0)) -> dict:
        """锥台原语(沉头 90° 锥头部, 真实 adsk 桥)。"""
        fa = self._fusion_api()
        if fa is None:
            return {"ok": False, "error": "fusion_api unavailable"}
        try:
            return fa.create_cone(radius_big_mm, radius_small_mm, height_mm, center_xyz)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def merge_fastener_groups(self) -> dict:
        """紧固件零件合并: 杆+锥段→1 螺栓实体, 螺母→1 实体(combineFeatures Join)。"""
        fa = self._fusion_api()
        if fa is None:
            return {"ok": False, "error": "fusion_api unavailable"}
        try:
            return fa.merge_fastener_groups()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def apply_thread(self, body_name: str, is_internal: bool = False,
                     designation: str = "M5x0.8", thread_class: str = "6g") -> dict:
        """真实螺纹(threadFeatures, isModeled 实体牙型)。"""
        fa = self._fusion_api()
        if fa is None:
            return {"ok": False, "error": "fusion_api unavailable"}
        try:
            return fa.apply_thread(body_name, is_internal=is_internal,
                                   designation=designation, thread_class=thread_class)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class FakeBackend:
    """内存模拟后端：本地单测/演示/冒烟测试使用，行为与 FusionBackend 对齐。"""

    def __init__(self, log=None) -> None:
        self.calls: list[dict] = []
        self._log = log
        self._entities: dict[str, dict] = {}
        self._n = 0
        self.available = True
        self._fail_on: list[str] = []  # 模拟失败的 featureType/queryType

    @staticmethod
    def _parse_mm(val) -> Optional[float]:
        """解析长度字段到 mm: 数字原样; '50 mm'→50; '3 cm'→30; 其它→None。"""
        if isinstance(val, bool):
            return None
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            s = val.strip().lower()
            m = __import__("re").search(r"([\d.]+)\s*(mm|cm)?$", s)
            if m:
                num = float(m.group(1))
                unit = m.group(2)
                return num * 10.0 if unit == "cm" else num
            s2 = s.replace(" ", "")
            m2 = __import__("re").search(r"^([\d.]+)(mm|cm)$", s2)
            if m2:
                num = float(m2.group(1))
                return num * 10.0 if m2.group(2) == "cm" else num
        return None

    def _log_call(self, tool: str, args: dict) -> None:
        self.calls.append({"tool": tool, "args": dict(args)})
        if self._log:
            self._log.info(f"backend.call({tool})", actor="executor", category="execute",
                           data={"args": dict(args)})

    def fail_next(self, tool: str) -> None:
        self._fail_on.append(tool)

    def call(self, tool: str, args: Optional[dict] = None) -> dict:
        args = args or {}
        self._log_call(tool, args)
        ft = args.get("featureType") or args.get("queryType") or tool
        matched = [f for f in self._fail_on if f in ft]
        if matched:
            # 一次性失败: 触发后移除该模拟故障
            self._fail_on = [f for f in self._fail_on if f not in matched]
            return {"ok": False, "error": f"simulated failure for {ft}"}
        if tool == "read":
            return self._read(args)
        if tool == "create":
            return self._create(args)
        if tool == "update":
            return {"ok": True, "message": f"update {args.get('featureType')} done"}
        if tool == "delete":
            return {"ok": True, "deleted": [args.get("name")]}
        if tool == "execute":
            ft = args.get("featureType")
            if ft == "document" and args.get("action") == "save":
                return {"ok": True, "success": True, "message": "fake saved"}
            return {"ok": True, "success": True, "output": "[fake execute ok]"}
        return {"ok": True, "value": "fake"}

    # ---- 模拟 read ----
    def _read(self, args: dict) -> dict:
        qt = args.get("queryType")
        if qt == "userParameters":
            return {"ok": True, "parameters": [
                {"name": k, "value": v.get("value"), "unit": v.get("unit", "mm")}
                for k, v in self._entities.items() if v.get("kind") == "parameter"]
            }
        if qt == "bodies":
            bodies = [{
                "entityToken": e.get("entityToken"), "name": e.get("name"),
                "volume": e.get("volume_mm3"),
                "bbox": e.get("bbox"),
            } for e in self._entities.values() if e.get("kind") == "body"]
            return {"ok": True, "count": len(bodies), "bodies": bodies}
        if qt == "timeline":
            return {"ok": True, "entries": list(range(self._n))}
        if qt == "document":
            return {"ok": True, "name": "<fake-design>"}
        if qt == "screenshot":
            return {"ok": True, "mimeType": "image/png", "data": "base64fake"}
        return {"ok": True, "value": "fake-read"}

    # ---- 模拟 create ----
    def _create(self, args: dict) -> dict:
        ft = args.get("featureType")
        self._n += 1
        token = f"tok_{self._n}"
        if ft == "userParameter":
            # 契约: object.parameters 数组(与真实 handlers 对齐)
            params = (args.get("object") or {}).get("parameters") or []
            created = []
            for p in params:
                name = p.get("name", f"p_{self._n}")
                expr = p.get("expression") or f"{p.get('value', 0)} {p.get('unit', 'mm')}"
                # 数值从表达式解析(mm), 供快照/回滚使用
                parsed = self._parse_mm(expr)
                self._entities[name] = {
                    "entityToken": f"tok_{self._n}", "kind": "parameter",
                    "expression": expr,
                    "value": parsed if parsed is not None else p.get("value", 0.0),
                    "unit": p.get("unit", "mm"),
                }
                created.append({"name": name, "entityToken": f"tok_{self._n}"})
                self._n += 1
            return {"ok": True, "success": True, "createdParameters": created}
        if ft in ("box", "cylinder", "sphere", "torus"):
            # 原语几何模拟: 解析尺寸字段 → 体积(mm³) 与包围盒
            obj = args.get("object") or {}
            dims = {}
            if ft == "box":
                dims["x"] = self._parse_mm(obj.get("width") or obj.get("length"))
                dims["y"] = self._parse_mm(obj.get("length") or obj.get("depth"))
                dims["z"] = self._parse_mm(obj.get("height"))
            elif ft == "cylinder":
                r = self._parse_mm(obj.get("radius")) or 1.0
                dims = {"x": r * 2, "y": r * 2,
                        "z": self._parse_mm(obj.get("height")) or 1.0}
            elif ft == "sphere":
                r = self._parse_mm(obj.get("radius")) or 1.0
                dims = {"x": r * 2, "y": r * 2, "z": r * 2}
            else:  # torus
                major = self._parse_mm(obj.get("majorRadius")) or 1.0
                minor = self._parse_mm(obj.get("minorRadius")) or 0.5
                dims = {"x": 2 * (major + minor), "y": 2 * (major + minor),
                        "z": 2 * minor}
            x, y, z = dims.get("x") or 0, dims.get("y") or 0, dims.get("z") or 0
            name = f"{ft}_{self._n}"
            self._entities[name] = {
                "entityToken": f"tok_{self._n}", "kind": "body",
                "name": name, "volume_mm3": round(x * y * z, 3),
                "bbox": {"min": [0, 0, 0], "max": [round(x, 3), round(y, 3), round(z, 3)]},
                "x_mm": x, "y_mm": y, "z_mm": z,
            }
            return {"ok": True, "success": True, "bodyName": name,
                    "entityToken": f"tok_{self._n}"}
        # 其他特征: 注册一个 body（或 sketch）
        kind = "body"
        entity = {"entityToken": token, "kind": kind, "name": f"{ft}_{self._n}",
                  "volume": 1000.0}
        self._entities[entity["name"]] = entity
        return {"ok": True, "entityToken": token, "name": entity["name"],
                "featureType": ft}

    def snapshot(self) -> dict:
        return {"parameters": {
            k: {"value": v.get("value"), "unit": v.get("unit", "mm")}
            for k, v in self._entities.items() if v.get("kind") == "parameter"
        }, "timeline_index": self._n,
            "entities": list(self._entities.keys()),
            "body_names": [n for n, v in self._entities.items() if v.get("kind") == "body"]}

    def rollback(self, checkpoint: dict) -> dict:
        data = checkpoint.get("data", {})
        steps = []
        # 1) 对象级补偿: 删除快照后新建的实体(模拟证据回滚)
        before = set(data.get("body_names") or [])
        deleted = []
        for name in list(self._entities):
            e = self._entities.get(name)
            if e and e.get("kind") == "body" and name not in before:
                del self._entities[name]
                deleted.append(name)
        steps.append({"step": "compensate_delete", "deleted": deleted})
        # 2) 参数恢复
        params_before = data.get("parameters", {})
        for name, before in params_before.items():
            if isinstance(before, dict) and "value" in before:
                if name in self._entities:
                    self._entities[name]["value"] = before["value"]
        return {"ok": True, "steps": steps}

    def model_context(self) -> dict:
        return {"document": {"name": "<fake-design>"},
                "parameters": [
                    {"name": k, "value": v.get("value")} for k, v in self._entities.items()
                    if v.get("kind") == "parameter"
                ]}

    # ---- 模拟 BOM/属性(与 FusionBackend 对齐) ----
    def ensure_design(self) -> dict:
        return {"ok": True, "design": "<fake-design>"}

    def set_ai_attributes(self, body_name: str, attrs: dict) -> dict:
        ent = self._entities.get(body_name)
        if ent is None:
            return {"error": f"body {body_name} not found"}
        ent["attributes"] = dict(attrs)
        return {"ok": True, "body": body_name}

    def create_cut_holes(self, points_xy_mm: list, diameter_mm: float) -> dict:
        """模拟贯通切孔: 记录到实体(打孔改变体积)。"""
        self._n += 1
        name = f"hole_{self._n}"
        self._entities[name] = {"entityToken": f"tok_{self._n}", "kind": "feature",
                                "name": name, "points": list(points_xy_mm),
                                "diameter_mm": diameter_mm}
        return {"ok": True, "holes": len(points_xy_mm), "note": "fake cut holes"}

    def create_cone(self, radius_big_mm, radius_small_mm, height_mm,
                    center_xyz=(0.0, 0.0, 0.0)) -> dict:
        """模拟锥台: 以最大半径建圆柱近似体积。"""
        self._n += 1
        name = f"cone_{self._n}"
        r = float(radius_big_mm)
        h = float(height_mm)
        self._entities[name] = {"entityToken": f"tok_{self._n}", "kind": "body",
                                "name": name,
                                "volume_mm3": round(3.14159 * r * r * h, 3),
                                "bbox": {"min": [0, 0, 0],
                                         "max": [round(2 * r, 3), round(2 * r, 3), round(h, 3)]},
                                "x_mm": 2 * r, "y_mm": 2 * r, "z_mm": h}
        return {"ok": True, "success": True, "bodyName": name}

    def merge_fastener_groups(self) -> dict:
        """模拟零件合并: 螺栓组(bolt_rod/bolt_head) 与 螺母(nut) 各并为一个实体。"""
        by_role: dict[str, list] = {}
        for name, v in self._entities.items():
            if v.get("kind") != "body":
                continue
            role = (v.get("attributes") or {}).get("semantic_role", "")
            if role in ("bolt_rod", "bolt_head"):
                by_role.setdefault("bolt", []).append(name)
            elif role == "nut":
                by_role.setdefault("nut", []).append(name)
        out = {"ok": True, "bolt_body": None, "nut_body": None,
               "bodies": len([v for v in self._entities.values() if v.get("kind") == "body"])}
        for key, group in by_role.items():
            if not group:
                continue
            keep = group[0]
            for name in group[1:]:
                self._entities.pop(name, None)
            out["bolt_body" if key == "bolt" else "nut_body"] = keep
        out["bodies"] = len([v for v in self._entities.values() if v.get("kind") == "body"])
        return out

    def apply_thread(self, body_name: str, is_internal: bool = False,
                     designation: str = "M5x0.8", thread_class: str = "6g") -> dict:
        """模拟真实螺纹: 记录到实体。"""
        ent = self._entities.get(body_name)
        if ent is None:
            return {"ok": False, "error": f"body {body_name} not found"}
        ent["thread"] = {"internal": is_internal, "designation": designation,
                         "class": thread_class, "modeled": True}
        return {"ok": True, "body": body_name, "thread_class": thread_class}

    def collect_bom(self) -> list:
        out = []
        for name, v in self._entities.items():
            if v.get("kind") != "body":
                continue
            out.append({
                "body_name": name,
                "volume_mm3": v.get("volume_mm3"),
                "mass_g": None,
                "bbox": v.get("bbox"),
                "attributes": v.get("attributes") or {},
                "component_type": "ai_generated" if (v.get("attributes") or {}).get("created_by") == "ai" else "user_existing",
                "manufacturing_method": (v.get("attributes") or {}).get("manufacturing_method", ""),
                "material": "",
            })
        return out


__all__ = ["FusionBackend", "FakeBackend", "_import_handlers"]