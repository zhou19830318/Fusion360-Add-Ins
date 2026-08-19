"""分步执行器（需求 §7.3 / §15 MVP：单步执行、暂停、重试、检查点、回滚）。

流程: gate(门控) → checkpoint(检查点) → backend.call → 记录 → 更新节点状态
"""

from __future__ import annotations

import time
from typing import Any, Optional

from ..planner.plan import Plan, PlanNode
from ..workflow.session import WorkflowSession
from .backend import FakeBackend, FusionBackend
from .fusion_api import mounting_hole_corners as _mounting_corners
from .tool_registry import ToolGate

# Fusion API 映射: plan node type(tool) → (handlers.create/update 的 featureType, 备注)
# 注意: 底层 handlers(.pyc) 的 featureType 采用 camelCase(实测 create.pyc 枚举),
#       例如 'userParameter' / 'sketchEntity' / 'circularPattern' —— 不是 snake_case。
TOOL_TO_FEATURE_TYPE: dict[str, tuple[str, dict]] = {
    "create_user_parameters": ("userParameter", {"multi": True}),
    "create_user_parameter": ("userParameter", {}),
    "create_sketch": ("sketch", {}),
    "create_sketch_entity": ("sketchEntity", {}),
    "create_extrude": ("extrude", {}),
    "create_revolve": ("revolve", {}),
    "create_hole": ("hole", {}),
    "create_fillet": ("fillet", {}),
    "create_chamfer": ("chamfer", {}),
    "create_shell": ("shell", {}),
    "create_circular_pattern": ("circularPattern", {}),
    "create_rectangular_pattern": ("rectangularPattern", {}),
    "create_construction_plane": ("constructionPlane", {}),
    "split_body": ("split", {}),
    "project_to_sketch": ("projectToSketch", {}),
    "set_material": ("material", {}),
    "set_appearance": ("appearance", {}),
    "create_selection_set": ("selectionSet", {}),
    "create_box": ("box", {}),
    "create_cylinder": ("cylinder", {}),
    "create_sphere": ("sphere", {}),
    "create_torus": ("torus", {}),
    "create_coil": ("coil", {}),
    "create_cone": ("cone", {}),  # 由 fusion_api.create_cone 真实现(handlers 无此原语)
    "fastener_finish": ("finish", {}),  # 执行器收尾: 零件合并+真实螺纹
}


# 原语特征: feature.parameters 语义键 → handlers create 契约(object 包裹)
# 实测(探针): box 契约键 = length/width/height + baseCenter(非 depth/center!)
#           轴向 width→X, length→Y, height→Z。数值传 mm 表达式, center 传数组。
PRIMITIVE_GEOMETRY: dict[str, dict] = {
    "create_box": {
        "handlers": "box",
        "keys": {"width": "width", "height": "height", "length": "length",
                 "depth": "length",          # 语义 depth → 契约 length(Y)
                 "center": "baseCenter"},
        "required": ["width", "height", "length"],
    },
    "create_cylinder": {
        "handlers": "cylinder",
        "keys": {"radius": "radius", "height": "height", "center": "baseCenter",
                 "axis": "axis", "direction": "axis"},
        "required": ["radius", "height"],
    },
    "create_sphere": {
        "handlers": "sphere",
        "keys": {"radius": "radius", "center": "center"},
        "required": ["radius"],
    },
    "create_torus": {
        "handlers": "torus",
        "keys": {"major_radius": "majorRadius", "minor_radius": "minorRadius",
                 "center": "baseCenter", "axis": "axis"},
        "required": ["major_radius", "minor_radius"],
    },
}


class ExecutionRunner:
    # 节点间节流: 让 Fusion 事件循环喘息(密集 TMP/特征/属性调用是崩溃放大器,
    # 实测未保存文档+高密度操作触发 CmdDisabledManager/asset activation 崩溃)
    THROTTLE = float(__import__("os").environ.get("AIFUSION_THROTTLE", "0.35"))

    def __init__(self, session: WorkflowSession, backend=None) -> None:
        self.session = session
        self.backend = backend or FusionBackend(log=session.log)
        self.gate = ToolGate(session)
        self._running = False
        self._stop_requested = False

    # ------------------------------------------------------------------
    def start(self) -> dict:
        """按拓扑顺序启动执行流水线。

        执行前自动保存当前文档(需求 §9 文档级备份的第一步)——
        实测: 未保存文档在高密度 API 操作下会触发 Fusion UI 命令管理器
        (CmdDisabledManager/asset activation) 内部异常导致崩溃(e.g. CER
        Ns::CommandExecutorException); 保存后可显著降低风险(未保存文档在
        崩溃时全部丢失)。
        """
        self.session.start_execution()
        # 确保活动参数化设计文档(冷启动/无设计时引导用户先打开设计)
        try:
            ed = self.backend.ensure_design()
            if not isinstance(ed, dict) or not ed.get("ok"):
                err = ed.get("error") if isinstance(ed, dict) else str(ed)
                self.session.log.critical(f"无参数化设计: {err}", category="checkpoint")
                return {"ok": False, "state": self.session.state.state,
                        "error": f"请先在 Fusion 中打开/新建参数化设计文档: {err}"}
            self.session.log.info(f"活动设计就绪: {ed}", category="checkpoint")
        except Exception as e:
            self.session.log.warning(f"ensure_design 异常: {e}", category="checkpoint")
        try:
            save_r = self.backend.call("execute", {
                "featureType": "document", "action": "save"})
            ok_save = bool(isinstance(save_r, dict) and save_r.get("success"))
            self.session.log.info(f"执行前文档保存: {ok_save}", category="checkpoint")
            if not ok_save:
                # 未保存文档 + 高密度建模 → Fusion 可能崩溃(CER: SSL/许可层);
                # 守卫: 拒执行并引导保存
                self.session.state.transition_or_none("PAUSED", "save guard")
                return {"ok": False, "state": self.session.state.state,
                        "error": "执行前文档保存失败。为降低 Fusion 崩溃风险(未保存文档+高密度建模)，"
                                 "请先在 Fusion 中保存文档(文件→保存)后再执行。"}
        except Exception as e:
            self.session.log.warning(f"执行前文档保存失败: {e}", category="checkpoint")
        self._running = True
        self._stop_requested = False
        return {"ok": True, "state": self.session.state.state}

    def _node_output_tools(self, node: PlanNode) -> list[str]:
        """节点对应的后端工具调用（MVP: 单工具/节点）。"""
        return [node.type]

    # ------------------------------------------------------------------
    def execute_node(self, node: PlanNode, force: bool = False) -> dict:
        """执行单个节点。失败时标记 failed，返回错误详情。"""
        if self.THROTTLE > 0:
            time.sleep(self.THROTTLE)  # 节流: 降低 Fusion API 密集度(防崩溃放大)
        log = self.session.log
        node.status = "running"
        log.info(f"执行节点 {node.id}: {node.label}", actor="executor",
                 category="execute", data={"risk": node.risk})

        # 1) 门控
        verdict = self.gate.evaluate(
            tool=node.type, state=self.session.state.state,
            plan=self.session.plan, node_id=node.id,
        )
        if not verdict["allowed"]:
            node.status = "failed"
            log.error(f"节点 {node.id} 被门控拦截: {verdict['reason']}",
                      actor="gate", category="gate", data=verdict)
            return {"ok": False, "node": node.id, "gated": True, "reason": verdict["reason"]}

        # 2) 确认点未确认
        if node.requires_confirmation and not getattr(node, "requires_confirmation_ok", False):
            node.status = "paused"
            return {"ok": False, "node": node.id, "awaiting_confirmation": True,
                    "reason": f"节点 {node.id} 需要用户确认后才能执行"}

        # 3) 检查点（仅标记的检查点节点做全快照; 普通节点不做, 减少读压力）
        checkpoint = None
        if node.checkpoint:
            checkpoint = self._create_checkpoint(node)

        # 4) 执行
        try:
            result = self._invoke_node(node)
        except Exception as exc:  # 防御: 后端抛异常视为失败
            node.status = "failed"
            log.error(f"节点 {node.id} 异常: {exc}", actor="executor", category="execute")
            return {"ok": False, "node": node.id, "error": str(exc)}

        # 5) 结果判定
        if self._is_ok(result):
            node.status = "passed"
            log.info(f"节点 {node.id} 通过", actor="validator", category="execute")
            if node.checkpoint or verdict.get("checkpoints"):
                self._complete_checkpoint(checkpoint, result)
            self._tag_entity(result, node)  # AI 属性标签(BOM 追溯 §12.1)
            return {"ok": True, "node": node.id, "result": result,
                    "checkpoint": (checkpoint or {}).get("id")}
        node.status = "failed"
        msg = str(result.get("error") or result.get("message") or result)
        log.error(f"节点 {node.id} 失败: {msg}", actor="executor", category="execute")
        return {"ok": False, "node": node.id, "error": msg, "result": result}

    # ------------------------------------------------------------------
    def run_all(self) -> dict:
        """顺序执行整个计划(自动进入 VALIDATING 由调用方决定)。"""
        plan = self.session.plan
        if plan is None:
            return {"ok": False, "error": "会话无计划"}
        order = plan.topological_order()
        summary = []
        for nid in order:
            if self._stop_requested:
                self.session.pause()
                return {"ok": False, "stopped": True, "summary": summary}
            node = plan.node(nid)
            if node is None or node.status == "skipped":
                continue
            # 确认点: 跳过等待(不阻塞其它独立分支), 由用户单独确认
            if node.requires_confirmation and not getattr(node, "requires_confirmation_ok", False):
                summary.append({"ok": False, "node": nid,
                                "awaiting_confirmation": True,
                                "reason": "确认点等待用户确认"})
                continue
            r = self.execute_node(node)
            summary.append(r)
            if not r.get("ok"):
                # 真实失败 → 中断(其下游依赖无法继续)
                break
        passed = sum(1 for n in plan.nodes if n.status == "passed")
        failed = sum(1 for n in plan.nodes if n.status == "failed")
        return {"ok": passed > 0 and failed == 0,
                "passed": passed, "failed": failed,
                "summary": summary}

    def stop(self) -> None:
        self._stop_requested = True

    # ------------------------------------------------------------------
    def retry(self, node_id: str) -> dict:
        """重试失败节点（需求 §7.3: 单独执行/重跑）。"""
        plan = self.session.plan
        node = plan.node(node_id)
        if node is None:
            return {"ok": False, "error": f"未知节点 {node_id}"}
        if node.status != "failed":
            return {"ok": False, "note": "节点未失败，无需重试", "node": node_id}
        return self.execute_node(node, force=True)

    def pause(self) -> dict:
        self._stop_requested = True
        self.session.pause()
        return {"ok": True, "state": self.session.state.state}

    def resume(self) -> dict:
        self._stop_requested = False
        self.session.resume()
        return {"ok": True, "state": self.session.state.state}

    def cancel(self) -> dict:
        self._stop_requested = True
        self.session.cancel()
        return {"ok": True, "state": self.session.state.state}

    def rollback(self, checkpoint_id: str) -> dict:
        """回滚到检查点（需求 §9）。"""
        cp = self.session.get_checkpoint(checkpoint_id)
        if cp is None:
            return {"ok": False, "error": f"未知检查点 {checkpoint_id}"}
        r = self.backend.rollback(cp)
        self.session.log.warning(f"回滚到检查点 {checkpoint_id}", actor="executor",
                                 category="rollback", data=r)
        self.session.state.transition_or_none("ROLLED_BACK", f"rollback to {checkpoint_id}")
        # 标记检查点之后的节点为 pending
        for n in self.session.plan.nodes:
            n.status = "pending"
        return r

    def backup(self, label: str = "手动备份") -> dict:
        """手动创建文档级备份检查点(需求 §9): 参数+实体清单+timeline 快照。"""
        snap = self.backend.snapshot()
        cp = self.session.add_checkpoint(
            label=label,
            data={
                "kind": "document_backup",
                "timeline_index": snap.get("timeline_index"),
                "timeline_marker_index": None,
                "parameters": snap.get("parameters", {}),
                "entities": snap.get("entities", []),
                "body_names": snap.get("body_names", []),
            },
        )
        self.session.log.info(f"文档级备份已创建: {cp['id']} ({label})",
                              category="checkpoint")
        return {"ok": True, "checkpoint": cp}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _tag_entity(self, result: dict, node: PlanNode) -> None:
        """原语实体创建成功后写入 AI 属性标签(BOM 追溯, 需求 §12.1)。"""
        if node.type not in ("create_box", "create_cylinder", "create_sphere",
                             "create_torus", "create_coil"):
            return
        body_name = (result or {}).get("bodyName")
        if not body_name:
            inner = result.get("result") if isinstance(result, dict) else None
            if isinstance(inner, dict):
                body_name = inner.get("bodyName")
        if not body_name:
            return
        semantic_role = ""
        spec = self.session.spec
        if spec and node.outputs:
            f = spec.get_feature(node.outputs[0])
            if f is not None:
                semantic_role = f.semantic_role or f.label or ""
        attrs = {
            "intent_id": spec.intent_id if spec else "",
            "plan_node_id": node.id,
            "semantic_role": semantic_role,
            "created_by": "ai",
            "revision": "A",
        }
        try:
            tag = self.backend.set_ai_attributes(body_name, attrs)
            if isinstance(tag, dict) and not tag.get("error"):
                self.session.log.info(f"实体 {body_name} 已打 AI 属性标签",
                                      actor="executor", category="metadata", data=tag)
            else:
                self.session.log.warning(f"属性标签写入失败: {tag}", category="metadata")
        except Exception as e:
            self.session.log.warning(f"属性标签写入异常: {e}", category="metadata")

    # ------------------------------------------------------------------
    def _create_checkpoint(self, node: PlanNode) -> Optional[dict]:
        snap = self.backend.snapshot()
        cp = self.session.add_checkpoint(
            label=f"before {node.label}",
            data={"node_id": node.id, "timeline_index": snap.get("timeline_index"),
                  "parameters": snap.get("parameters", {}),
                  "entities": snap.get("entities", [])},
        )
        return cp

    def _complete_checkpoint(self, checkpoint: Optional[dict], result: dict) -> None:
        if checkpoint:
            checkpoint["data"]["after"] = {"entityToken": result.get("entityToken")}

    def _invoke_node(self, node: PlanNode) -> dict:
        """把 plan 节点映射到后端调用。"""
        plan = self.session.plan
        spec = self.session.spec
        args: dict[str, Any] = {"featureType": "script", }

        if node.type == "create_user_parameters":
            # 批量创建 User Parameters —— 实测契约(探针实证):
            #   {"featureType": "userParameter", "object": {"parameters": [{name, expression|value+unit}]}}
            batch = []
            skipped = []
            for pid in node.inputs:
                p = spec.get_parameter(pid) if spec else None
                if p is None:
                    batch.append({"id": pid, "error": "参数不存在"})
                    continue
                if p.value is None and not p.expression:
                    # 推断但无值(None)的参数无法创建, 跳过并记录(LLM 输出质量问题)
                    skipped.append({"id": pid, "name": p.name,
                                    "reason": "value is None (need LLM 输出确认值)"})
                    self.session.log.warning(f"参数 {p.name}({pid}) 无值, 跳过创建", actor="executor",
                                             category="execute")
                    continue
                entry: dict[str, Any] = {"name": p.name}
                if p.expression:
                    entry["expression"] = p.expression
                elif p.value is not None:
                    # 用表达式承载数值+单位, 保证 Fusion 内部 cm 换算正确
                    entry["expression"] = f"{p.value} {p.unit or 'mm'}"
                if p.label or p.description:
                    entry["comment"] = p.label or p.description
                batch.append({"id": pid, "name": p.name, "payload": entry})
            # 过滤掉缺 payload 的条目
            valid = [b for b in batch if "payload" in b]
            if len(valid) != len(batch):
                return {"ok": False,
                        "error": f"参数创建失败(存在缺失项): {[b.get('error') for b in batch if 'payload' not in b][:3]}"}
            if not valid:
                return {"ok": True, "note": "无可用参数可创建", "skipped": skipped}
            r = self.backend.call("create", {
                "featureType": "userParameter",
                "object": {"parameters": [b["payload"] for b in valid]},
            })
            if not self._is_ok(r):
                return {"ok": False, "error": str(r.get("error") or r.get("message") or r),
                        "result": r}
            return {"ok": True, "results": valid, "result": r, "skipped": skipped}

        if node.type == "pause":
            return {"ok": True, "paused": True}

        # 标准特征节点
        mapping = TOOL_TO_FEATURE_TYPE.get(node.type)
        if mapping is None:
            # 无映射 → 禁止执行
            return {"ok": False,
                    "error": f"节点类型 {node.type} 没有受限工具映射；"
                             "该操作需要任意脚本(默认禁用)或手动处理"}

        feature_type, _ = mapping
        # ---- 紧固件收尾: 零件合并(杆+锥段→螺栓实体) + 真实螺纹 ----
        if node.type == "fastener_finish":
            time.sleep(0.5)  # 收尾(合并+螺纹)前留出 Fusion 事件循环时间
            notes = []
            mg = self.backend.merge_fastener_groups()
            bolt = (mg or {}).get("bolt_body") if isinstance(mg, dict) else None
            nut = (mg or {}).get("nut_body") if isinstance(mg, dict) else None
            if not isinstance(mg, dict) or not mg.get("ok") or not bolt:
                # Fusion 2704 无文档体→临时体布尔 API; 合并降级为分段实体(note 不阻塞)
                notes.append("实体合并受限(Fusion 2704 布尔限制), 保持连续分段体表示")
                # 分段模式下螺纹目标: 按 semantic_role 找 bolt_rod/nut body
                for name, ent in getattr(self.backend, "_entities", {}).items():
                    if bolt is None and (ent.get("attributes") or {}).get("semantic_role") == "bolt_rod":
                        bolt = name
                    if nut is None and (ent.get("attributes") or {}).get("semantic_role") == "nut":
                        nut = name
            threads = []
            if bolt:
                threads.append({"part": "bolt", "result": self.backend.apply_thread(
                    bolt, is_internal=False, designation="M5x0.8", thread_class="6g")})
            if nut:
                threads.append({"part": "nut", "result": self.backend.apply_thread(
                    nut, is_internal=True, designation="M5x0.8", thread_class="6H")})
            self.session.log.info(
                f"紧固件收尾: merge={mg} notes={notes} threads={len(threads)}",
                actor="executor", category="execute")
            return {"ok": True, "node": node.id,
                    "result": {"merge": mg, "threads": threads, "notes": notes}}

        # ---- 锥台原语(沉头 90° 锥头部) ----
        if node.type == "create_cone":
            feature = spec.get_feature(node.outputs[0]) if spec and node.outputs else None
            if feature is None:
                return {"ok": False, "node": node.id, "error": "create_cone 缺少特征定义"}
            rb = self._feature_num(feature, "radius_big", 5.2)
            rs = self._feature_num(feature, "radius_small", 2.5)
            hh = self._feature_num(feature, "height", 2.7)
            center = feature.parameters.get("center", [0.0, 0.0, 0.0])
            if not isinstance(center, (list, tuple)) or len(center) != 3:
                center = [0.0, 0.0, 0.0]
            r = self.backend.create_cone(rb, rs, hh, center)
            if not isinstance(r, dict) or not r.get("ok"):
                return {"ok": False, "node": node.id,
                        "error": str(r.get("error") or r) if isinstance(r, dict) else str(r)}
            return {"ok": True, "node": node.id, "result": r}

        # ---- 原语特征桥接 (box/cylinder/sphere/torus) ----
        if spec and node.outputs:
            feature = spec.get_feature(node.outputs[0])
            if feature is not None:
                prim = self._primitive_args(node, feature)
                if prim.get("__error__"):
                    return {"ok": False, "node": node.id,
                            "error": prim["__error__"]}
                if prim:
                    return self.backend.call("create", prim)

        args: dict[str, Any] = {
            "featureType": feature_type,
            "name": node.label,
            "node_id": node.id,
        }
        # ---- 安装孔桥接(实验性): 四角贯通孔(sketch 圆 + extrude cut, fusion_api) ----
        if feature_type == "hole" and feature is not None:
            role = feature.semantic_role or ""
            if role == "mounting_holes":
                # 实验性功能: cut 链(ObjectCollection profiles + setAllExtent)在
                # Fusion 2704 存在内核崩溃风险, 仅在用户显式授权 execute 后尝试
                if not self.session.execute_enabled:
                    return {"ok": False, "node": node.id,
                            "error": "打孔(实验性)在 Fusion 2704 有稳定性风险; "
                                     "请先在设置中开启「执行授权」后重试该节点"}
                dia = 6.0
                dia_ref = (feature.parameters or {}).get("diameter")
                if dia_ref:
                    p = spec.get_parameter(dia_ref) if spec else None
                    if p is not None and isinstance(p.value, (int, float)):
                        dia = float(p.value)
                w = self._spec_num("width", 50.0)
                l = self._spec_num("depth", 10.0)
                if l <= 0:
                    l = self._spec_num("length", 10.0)
                try:
                    pts = _mounting_corners(w, l)
                except Exception:
                    pts = [(15, 2), (15, -2), (-15, 2), (-15, -2)]
                r = self.backend.create_cut_holes(pts, dia)
                if not isinstance(r, dict) or not r.get("ok"):
                    return {"ok": False, "node": node.id,
                            "error": str(r.get("error") or r) if isinstance(r, dict) else str(r)}
                return {"ok": True, "node": node.id, "result": r}

        # 几何特征(sketch 驱动链 / 孔等)尚未完成参数桥接 → 明确失败原因
        if feature_type in ("extrude", "revolve", "hole", "shell", "fillet", "chamfer"):
            if spec and node.outputs:
                f = spec.get_feature(node.outputs[0])
                if f is not None and f.parameters:
                    args["parameters"] = {
                        k: (spec.get_parameter(v).value if spec.get_parameter(v) else v)
                        for k, v in f.parameters.items()
                    }
            return {"ok": False, "node": node.id,
                    "error": (f"特征 {node.type} 需要草图轮廓/sketch 桥接(第二阶段), 尚未完成; "
                              f"替代策略: 使用 box/cylinder 等原语特征或人工建模"),
                    "hint": args}
        return self.backend.call("create", args)

    # ------------------------------------------------------------------
    def _spec_num(self, name: str, default: float) -> float:
        if self.session.spec:
            p = self.session.spec.get_parameter_by_name(name)
            if p is not None and isinstance(p.value, (int, float)):
                return float(p.value)
        return float(default)

    def _feature_num(self, feature, key: str, default: float) -> float:
        """解析特征参数数值(参数引用→其值; 字面→float; 缺省→default)。"""
        val = (feature.parameters or {}).get(key, default)
        if isinstance(val, str) and self.session.spec:
            p = self.session.spec.get_parameter(val)
            if p is not None and isinstance(p.value, (int, float)):
                return float(p.value)
            try:
                return float(val)
            except (TypeError, ValueError):
                return float(default)
        if isinstance(val, (int, float)):
            return float(val)
        return float(default)

    # ------------------------------------------------------------------
    def _primitive_args(self, node: PlanNode, feature) -> Optional[dict]:
        """把 feature.parameters 语义键解析为 handlers 原语契约。"""
        info = PRIMITIVE_GEOMETRY.get(node.type)
        if info is None:
            return {}
        resolved: dict[str, Any] = {}
        for k, v in (feature.parameters or {}).items():
            if isinstance(v, str) and self.session.spec and self.session.spec.get_parameter(v):
                p = self.session.spec.get_parameter(v)
                if p.value is not None:
                    resolved[k] = p.value
                else:
                    resolved[k] = v
            else:
                resolved[k] = v
        obj: dict[str, Any] = {}
        for sem_key, contract_key in info["keys"].items():
            if sem_key in resolved and resolved[sem_key] is not None:
                val = resolved[sem_key]
                if isinstance(val, (int, float)):
                    obj[contract_key] = f"{val} mm"
                elif isinstance(val, (list, tuple)):
                    # 坐标数组: 每个数值元素转 mm 表达式(避免 Fusion evaluateExpression
                    # 把裸数字当内部 cm 导致 10 倍错误), 字符串原样(已是表达式)
                    obj[contract_key] = [
                        (f"{v} mm" if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                        for v in val
                    ]
                else:
                    obj[contract_key] = val
        for req in info.get("required", []):
            mapped = info["keys"].get(req, req)
            if mapped not in obj:
                return {"__error__": f"原语节点 {node.id} 缺少几何参数 {req}"
                                     f"(feature.parameters 需含 {req})"}
        return {"featureType": info["handlers"], "object": obj}

    @staticmethod
    def _is_ok(result: Any) -> bool:
        if not isinstance(result, dict):
            return bool(result)
        if result.get("ok") is False:
            return False
        if "error" in result and not result.get("ok"):
            return False
        return True


__all__ = ["ExecutionRunner", "TOOL_TO_FEATURE_TYPE"]