"""reliability API Blueprint（需求 §13 后端 API 的 MVP 子集）。

路径前缀 /api/reliability，避免与现有 /api/chat /api/models /api/config 冲突。

依赖注入（避免 server.py ↔ reliability 循环导入）:
  llm_caller:      callable(messages, kw) -> str   文本 LLM 调用（server.py 提供）
  backend_factory: callable(session_aware) -> backend   Fusion/Fake 后端
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from flask import Blueprint, Response, jsonify, request

from .. import __version__
from ..clarify.classifier import apply_answers, has_blocking_questions, rank_questions
from ..executor.backend import FakeBackend, FusionBackend
from ..executor.runner import ExecutionRunner
from ..intent.llm import generate_intent_with_llm
from ..intent.parser import RuleIntentParser
from ..ir.design_spec import (
    DesignSpec, Feature, Parameter,
    apply_parameter_change, impact_analysis, lock_parameter,
)
from ..metadata.registry import get_registry, prompt_hash
from ..planner.generator import apply_user_edits, generate_plan
from ..planner.plan import summarize_plan
from ..validation.geometry import GeometryValidator
from ..validation.intent_check import IntentValidator
from ..workflow.session import WorkflowSession

sessions: dict[str, WorkflowSession] = {}
# 会话级持久后端(避免每次请求重建空 FakeBackend / FusionBackend 导致状态丢失)
_session_backends: dict[str, Any] = {}

# 审计日志目录: reliability/logs/
_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
_ACCESS_LOG = _LOGS_DIR / "http_access.jsonl"


def _merge_spec(old: DesignSpec, new: DesignSpec) -> DesignSpec:
    """多轮澄清合并: 以新解析结果为基, 保留旧 spec 中用户已确认/锁定的参数与特征。

    规则(需求 §6.3 用户确认的参数不能被静默覆盖):
      - 参数按 name 匹配: 旧参数 source in (user, derived, constraint) 且
        status in (confirmed, locked) → 覆盖新值; 其余新参数追加;
      - 旧 spec 中用户确认但新解析遗漏的参数保留;
      - 特征按 id 去重: 旧特征保留, 新特征补充;
      - goal.summary 拼接多轮意图。
    """
    old_params = {p.name: p for p in old.parameters}
    for p in new.parameters:
        op = old_params.get(p.name)
        # 仅【锁定】参数不可被覆盖(需求 §6.5); 普通确认参数允许新一轮用户输入生效
        if op is not None and op.locked:
            p.value = op.value
            p.unit = op.unit
            p.expression = op.expression or p.expression
            p.status = "locked"
            p.locked = True
            p.source = op.source
    new_names = {p.name for p in new.parameters}
    for name, op in old_params.items():
        if name not in new_names and (op.locked or op.status in ("confirmed", "locked")):
            new.parameters.append(op)
    # 特征去重: 旧特征保留(含 enabled/role), 新特征补充
    new_ids = {f.id for f in new.features}
    for of in old.features:
        if of.id not in new_ids:
            new.features.append(of)
    if old.goal.get("summary") and new.goal.get("summary") and \
            old.goal.get("summary") not in new.goal.get("summary"):
        new.goal["summary"] = f"{old.goal.get('summary')}；{new.goal['summary']}"
    new._reindex()
    return new


def _default_backend(session: WorkflowSession):
    try:
        import adsk.core  # noqa: F401
        return FusionBackend(log=session.log)
    except Exception:
        return FakeBackend(log=session.log)


def make_reliability_blueprint(
    llm_caller: Optional[Callable] = None,
    backend_factory: Optional[Callable] = None,
) -> Blueprint:
    bp = Blueprint("reliability", __name__, url_prefix="/api/reliability")
    rf = RuleIntentParser()

    # ── HTTP 审计: 每个请求落盘(JSON Lines) ──
    @bp.before_request
    def _audit_request():
        try:
            entry: dict[str, Any] = {
                "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
                "method": request.method,
                "path": request.path,
            }
            if request.method in ("POST", "PATCH"):
                body = request.get_json(silent=True)
                if isinstance(body, dict):
                    # 去掉超长字段, 避免日志膨胀; 不记录密钥类字段(无)
                    slim = {k: (v if not isinstance(v, str) or len(v) <= 200 else v[:200] + "…")
                            for k, v in body.items()}
                    entry["body"] = slim
            try:
                _LOGS_DIR.mkdir(exist_ok=True)
                with open(_ACCESS_LOG, "a", encoding="utf-8") as _fh:
                    _fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass
        except Exception:
            pass

    @bp.get("/ping")
    def ping():
        """存活探测: 标记当前服务器是否加载了可靠性层(AIFusion 启动时区分新旧实例)。"""
        return jsonify({"ok": True, "module": "reliability", "version": __version__,
                        "sessions": len(sessions)})

    def _backend(sid: str):
        session = sessions.get(sid)
        if session is None:
            return None, None
        if sid not in _session_backends:
            _session_backends[sid] = (
                backend_factory(session) if backend_factory else _default_backend(session))
        return session, _session_backends[sid]

    @bp.post("/dev/create_probe")
    def dev_create_probe():
        """排查用直通探针: 直接把请求转发给 Fusion handlers 执行(绕过可靠流程门控,
        仅排障用)。请求体: {"tool": "create", "args": {"featureType": ..., ...}}"""
        body = request.get_json(force=True) or {}
        tool = body.get("tool", "create")
        args = body.get("args", {})
        backend = FusionBackend()  # 始终直通真实 handlers(非会话后端,非 FakeBackend)
        r = backend.call(tool, args)
        return jsonify({"ok": True, "tool": tool, "args": args, "result": r})

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    @bp.post("/session")
    def create_session():
        body = request.get_json(force=True) or {}
        s = WorkflowSession(execute_enabled=bool(body.get("execute_enabled", False)))
        sessions[s.session_id] = s
        try:
            s.log.attach_file(str(_LOGS_DIR / f"session_{s.session_id}.jsonl"))
        except Exception:
            pass
        return jsonify({"ok": True, "session_id": s.session_id, "state": s.state.state})

    @bp.get("/session/<sid>")
    def get_session(sid):
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "session not found"}), 404
        return jsonify(s.to_dict())

    @bp.get("/sessions")
    def list_sessions():
        """列出当前进程内的会话（用于审计/排障）。"""
        out = []
        for sid, s in sessions.items():
            out.append({
                "session_id": sid,
                "state": s.state.state,
                "intent_id": s.spec.intent_id if s.spec else None,
                "plan_id": s.plan.plan_id if s.plan else None,
                "log_count": len(s.log.entries),
                "created_checkpoints": len(s.checkpoints),
            })
        return jsonify({"count": len(out), "sessions": out})

    # ------------------------------------------------------------------
    # Intent
    # ------------------------------------------------------------------
    @bp.post("/intent/parse")
    def intent_parse():
        body = request.get_json(force=True)
        sid = body.get("session_id")
        text = (body.get("user_text") or "").strip()
        if not sid or not text:
            return jsonify({"error": "session_id and user_text required"}), 400
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "session not found"}), 404

        # 多轮澄清复用(reuse_session): 已有 spec 时仅用本轮文本重新解析,
        # 再做参数级合并(避免旧文本中的旧值干扰本轮修改)
        old_spec = s.spec if (body.get("reuse_session") and s.spec is not None) else None
        parse_text = text

        s.begin(text)
        use_llm = body.get("use_llm", True)
        spec: Optional[DesignSpec] = None
        llm_errors = None
        attempts = 0
        if use_llm and llm_caller is not None:
            ok, data, errs, attempts = generate_intent_with_llm(
                llm_caller, parse_text, language=body.get("language", "zh-CN"))
            if ok:
                spec = DesignSpec.from_dict(data)
            else:
                llm_errors = errs
                s.log.warning(f"LLM intent 校验失败, 回退规则解析: {errs}",
                              category="intent", actor="ai")
        if spec is None:
            spec = rf.parse(parse_text)
            if llm_errors:
                spec.metadata["llm_errors"] = llm_errors
                spec.metadata["fallback"] = "rule"
        spec.metadata["source_prompt_hash"] = prompt_hash(text)
        if old_spec is not None:
            spec = _merge_spec(old_spec, spec)
            s.log.info(f"多轮澄清合并完成: {len(spec.parameters)} 参数", actor="system",
                       category="intent")
        s.spec = spec

        # 状态推进
        questions = rank_questions(spec)
        blocking = has_blocking_questions(spec)
        if blocking:
            s.to_clarify("存在阻断性歧义")
            return jsonify({
                "ok": True, "intent_id": spec.intent_id, "state": s.state.state,
                "spec": spec.to_dict(),
                "questions": questions,
                "llm_attempts": attempts,
            })
        s.spec_ready()
        return jsonify({
            "ok": True, "intent_id": spec.intent_id, "state": s.state.state,
            "spec": spec.to_dict(), "questions": questions,
        })

    @bp.post("/clarify/answer")
    def clarify_answer():
        body = request.get_json(force=True)
        sid = body.get("session_id")
        answers = body.get("answers") or {}
        s = sessions.get(sid)
        if not s or not s.spec:
            return jsonify({"error": "session or spec not found"}), 404
        applied = apply_answers(s.spec, answers)
        s.log.info(f"澄清回答已应用: {applied}", actor="user", category="clarify")
        # 从 CLARIFY 回到 UNDERSTAND 重新评估
        s.state.transition_or_none("UNDERSTAND", "clarification answered")
        if has_blocking_questions(s.spec):
            s.to_clarify("仍有阻断性歧义")
        else:
            s.state.transition_or_none("SPEC_READY", "spec complete after clarification")
        return jsonify({
            "ok": True, "applied": applied, "state": s.state.state,
            "questions": rank_questions(s.spec) if s.state.state == "CLARIFY" else [],
            "spec": s.spec.to_dict(),
        })

    # ------------------------------------------------------------------
    # 设计规格 / 参数
    # ------------------------------------------------------------------
    @bp.get("/spec/<sid>")
    def get_spec(sid):
        s = sessions.get(sid)
        if not s or not s.spec:
            return jsonify({"error": "spec not found"}), 404
        return jsonify(s.spec.to_dict())

    @bp.get("/design/<sid>/parameters")
    def get_parameters(sid):
        s = sessions.get(sid)
        if not s or not s.spec:
            return jsonify({"error": "spec not found"}), 404
        return jsonify({"parameters": [p.to_dict() for p in s.spec.parameters]})

    @bp.patch("/design/<sid>/parameters")
    def update_parameters(sid):
        body = request.get_json(force=True)
        s = sessions.get(sid)
        if not s or not s.spec:
            return jsonify({"error": "spec not found"}), 404
        updates = body.get("updates") or []
        results = []
        for u in updates:
            actor = u.get("actor", "user")
            r = apply_parameter_change(s.spec, u.get("id"), u.get("value"), actor,
                                       reason=u.get("reason", ""))
            results.append(r)
            s.log.info(f"参数修改 {u.get('id')}: {json.dumps(r, ensure_ascii=False)[:200]}",
                       actor=actor, category="parameter")
        return jsonify({"ok": True, "results": results})

    @bp.post("/design/<sid>/parameters/lock")
    def lock_parameter_ep(sid):
        body = request.get_json(force=True)
        s = sessions.get(sid)
        if not s or not s.spec:
            return jsonify({"error": "spec not found"}), 404
        r = lock_parameter(s.spec, body.get("id"), True)
        return jsonify(r)

    @bp.post("/design/<sid>/parameters/unlock")
    def unlock_parameter_ep(sid):
        body = request.get_json(force=True)
        s = sessions.get(sid)
        if not s or not s.spec:
            return jsonify({"error": "spec not found"}), 404
        r = lock_parameter(s.spec, body.get("id"), False)
        return jsonify(r)

    @bp.post("/design/<sid>/parameters/impact")
    def parameter_impact(sid):
        body = request.get_json(force=True)
        s = sessions.get(sid)
        if not s or not s.spec:
            return jsonify({"error": "spec not found"}), 404
        r = impact_analysis(s.spec, body.get("id"), body.get("value"))
        return jsonify(r)

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------
    @bp.post("/plan/generate")
    def plan_generate():
        body = request.get_json(force=True)
        sid = body.get("session_id")
        s = sessions.get(sid)
        if not s or not s.spec:
            return jsonify({"error": "session/spec not found"}), 404
        # 阻断性澄清必须已解决
        if has_blocking_questions(s.spec):
            return jsonify({"error": "存在未解决的阻断性澄清", "questions": rank_questions(s.spec)}), 409
        if s.state.state == "SPEC_READY":
            s.state.transition_or_none("PLAN_READY", "plan generating")
        try:
            plan = generate_plan(s.spec)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 500
        s.plan = plan
        s.log.info(f"计划已生成: {len(plan.nodes)} 节点", category="plan")
        return jsonify({
            "ok": True, "session_id": sid, "plan": plan.to_dict(),
            "summary": summarize_plan(plan), "state": s.state.state,
        })

    @bp.get("/plan/<sid>")
    def get_plan(sid):
        """计划详情(GET)。"""
        s = sessions.get(sid)
        if not s or not s.plan:
            return jsonify({"error": "plan not found"}), 404
        return jsonify({
            "ok": True, "session_id": sid,
            "plan": s.plan.to_dict(),
            "summary": summarize_plan(s.plan),
            "state": s.state.state,
        })

    @bp.patch("/plan/<sid>")
    def plan_edit(sid):
        body = request.get_json(force=True)
        s = sessions.get(sid)
        if not s or not s.plan:
            return jsonify({"error": "plan not found"}), 404
        try:
            new_plan = apply_user_edits(s.plan, body.get("edits") or [])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        s.plan = new_plan
        return jsonify({"ok": True, "plan": new_plan.to_dict()})

    @bp.post("/plan/<sid>/approve")
    def plan_approve(sid):
        s = sessions.get(sid)
        if not s or not s.plan:
            return jsonify({"error": "plan not found"}), 404
        s.review_approved()
        return jsonify({"ok": True, "state": s.state.state})

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    @bp.post("/execution/start/<sid>")
    def execution_start(sid):
        s = sessions.get(sid)
        if not s or not s.plan:
            return jsonify({"error": "plan not found"}), 404
        session, backend = _backend(sid)
        runner = ExecutionRunner(session, backend)
        r = runner.start()
        return jsonify({"ok": True, "state": session.state.state,
                        "next": [n.to_dict() for n in session.plan.ready_nodes()]})

    @bp.post("/execution/run_all/<sid>")
    def execution_run_all(sid):
        s = sessions.get(sid)
        if not s or not s.plan:
            return jsonify({"error": "plan not found"}), 404
        session, backend = _backend(sid)
        runner = ExecutionRunner(session, backend)
        runner.start()
        res = runner.run_all()
        return jsonify({"ok": True, "execution": res, "state": session.state.state,
                        "plan": session.plan.to_dict()})

    @bp.post("/execution/step/<sid>/<node_id>")
    def execution_step(sid, node_id):
        s = sessions.get(sid)
        if not s or not s.plan:
            return jsonify({"error": "plan not found"}), 404
        session, backend = _backend(sid)
        runner = ExecutionRunner(session, backend)
        node = session.plan.node(node_id)
        if node is None:
            return jsonify({"error": f"unknown node {node_id}"}), 404
        r = runner.execute_node(node)
        return jsonify({"ok": True, "step": r, "plan": session.plan.to_dict()})

    @bp.post("/execution/retry/<sid>/<node_id>")
    def execution_retry(sid, node_id):
        s = sessions.get(sid)
        if not s or not s.plan:
            return jsonify({"error": "plan not found"}), 404
        session, backend = _backend(sid)
        runner = ExecutionRunner(session, backend)
        r = runner.retry(node_id)
        return jsonify({"ok": True, "retry": r, "plan": session.plan.to_dict()})

    @bp.post("/execution/retry_failed/<sid>")
    def execution_retry_failed(sid):
        """重试所有失败节点（MVP 前端一键重试）。"""
        s = sessions.get(sid)
        if not s or not s.plan:
            return jsonify({"error": "plan not found"}), 404
        session, backend = _backend(sid)
        runner = ExecutionRunner(session, backend)
        results = []
        for n in session.plan.nodes:
            if n.status == "failed":
                results.append(runner.retry(n.id))
        ok = all(r.get("ok") for r in results)
        return jsonify({"ok": True, "retry": {
            "ok": ok, "passed": sum(1 for n in session.plan.nodes if n.status == "passed"),
            "failed": sum(1 for n in session.plan.nodes if n.status == "failed"),
            "summary": results,
        }, "state": session.state.state})

    @bp.post("/execution/confirm/<sid>/<node_id>")
    def execution_confirm(sid, node_id):
        """确认点放行（需求 §7.3 设置确认点 / §3.2 REVIEW 用户干预）。
        确认后若节点处于 paused，立即执行该节点。"""
        s = sessions.get(sid)
        if not s or not s.plan:
            return jsonify({"error": "plan not found"}), 404
        node = s.plan.node(node_id)
        if node is None:
            return jsonify({"error": f"unknown node {node_id}"}), 404
        node.requires_confirmation_ok = True
        s.log.info(f"节点 {node_id} 已获用户确认", actor="user", category="execute")
        if node.status == "paused":
            backend = _session_backends.get(sid)
            runner = ExecutionRunner(s, backend)
            r = runner.execute_node(node)
            return jsonify({"ok": True, "executed": r, "plan": s.plan.to_dict()})
        return jsonify({"ok": True, "confirmed": True, "node": node_id,
                        "plan": s.plan.to_dict()})

    @bp.post("/execution/pause/<sid>")
    def execution_pause(sid):
        s = sessions.get(sid)
        session, backend = _backend(sid)
        runner = ExecutionRunner(session, backend)
        return jsonify(runner.pause())

    @bp.post("/execution/resume/<sid>")
    def execution_resume(sid):
        s = sessions.get(sid)
        session, backend = _backend(sid)
        runner = ExecutionRunner(session, backend)
        return jsonify(runner.resume())

    @bp.post("/execution/cancel/<sid>")
    def execution_cancel(sid):
        s = sessions.get(sid)
        session, backend = _backend(sid)
        runner = ExecutionRunner(session, backend)
        return jsonify(runner.cancel())

    @bp.post("/execution/backup/<sid>")
    def execution_backup(sid):
        """手动文档级备份检查点(需求 §9): 参数+实体清单+timeline 快照。"""
        body = request.get_json(force=True) or {}
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "session not found"}), 404
        session, backend = _backend(sid)
        runner = ExecutionRunner(session, backend)
        return jsonify(runner.backup(body.get("label", "手动备份")))

    @bp.post("/execution/rollback/<sid>/<checkpoint_id>")
    def execution_rollback(sid, checkpoint_id):
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "session not found"}), 404
        session, backend = _backend(sid)
        runner = ExecutionRunner(session, backend)
        return jsonify(runner.rollback(checkpoint_id))

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @bp.post("/validate/geometry/<sid>")
    def validate_geometry(sid):
        s = sessions.get(sid)
        if not s or not s.spec:
            return jsonify({"error": "session not found"}), 404
        session, backend = _backend(sid)
        session.to_validation()
        v = GeometryValidator(backend, plan=session.plan)
        report = v.run()
        session.validation_reports[report.validation_id] = report.to_dict()
        _apply_validation_state(session, report.status)
        return jsonify({"ok": True, "report": report.to_dict(), "state": session.state.state})

    @bp.post("/validate/intent/<sid>")
    def validate_intent(sid):
        s = sessions.get(sid)
        if not s or not s.spec:
            return jsonify({"error": "session not found"}), 404
        session, backend = _backend(sid)
        ctx = backend.model_context()
        v = IntentValidator(session.spec, plan=session.plan, model_context=ctx)
        report = v.run()
        session.validation_reports[report.validation_id] = report.to_dict()
        _apply_validation_state(session, report.status)
        return jsonify({"ok": True, "report": report.to_dict(), "state": session.state.state})

    # ------------------------------------------------------------------
    # Templates (需求 §13): DesignSpec 模板 CRUD + apply
    # ------------------------------------------------------------------
    def _template_path(name: str):
        return _LOGS_DIR.parent / "templates" / f"{name}.json"

    @bp.get("/templates")
    def templates_list():
        tdir = _LOGS_DIR.parent / "templates"
        out = []
        try:
            tdir.mkdir(exist_ok=True)
            for f in sorted(tdir.glob("*.json")):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    out.append({"name": f.stem, "description": d.get("description", ""),
                                "parameters": len((d.get("spec") or {}).get("parameters", [])),
                                "features": len((d.get("spec") or {}).get("features", []))})
                except Exception:
                    continue
        except Exception:
            pass
        return jsonify({"ok": True, "count": len(out), "templates": out})

    @bp.post("/templates")
    def templates_save():
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip()
        spec = body.get("spec")
        if not name or not isinstance(spec, dict):
            return jsonify({"error": "name and spec required"}), 400
        try:
            tdir = _LOGS_DIR.parent / "templates"
            tdir.mkdir(exist_ok=True)
            payload = {"description": body.get("description", ""),
                       "spec": {k: spec.get(k) for k in
                                ("goal", "environment", "parameters", "features",
                                 "constraints", "assumptions") if k in spec}}
            path = _template_path(name)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
            return jsonify({"ok": True, "name": name, "path": str(path)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @bp.get("/templates/<name>")
    def templates_get(name):
        path = _template_path(name)
        if not path.is_file():
            return jsonify({"error": "template not found"}), 404
        return jsonify({"ok": True, "name": name,
                        **json.loads(path.read_text(encoding="utf-8"))})

    @bp.delete("/templates/<name>")
    def templates_delete(name):
        path = _template_path(name)
        if path.is_file():
            path.unlink()
            return jsonify({"ok": True, "deleted": name})
        return jsonify({"error": "template not found"}), 404

    @bp.post("/templates/<name>/apply")
    def templates_apply(name):
        """把模板套用到会话: 合并参数/特征/environment 到现有 spec(不覆盖用户已确认项)。"""
        body = request.get_json(force=True) or {}
        sid = body.get("session_id")
        s = sessions.get(sid)
        if not s or not s.spec:
            return jsonify({"error": "session/spec not found"}), 404
        path = _template_path(name)
        if not path.is_file():
            return jsonify({"error": "template not found"}), 404
        tpl = json.loads(path.read_text(encoding="utf-8")).get("spec", {})
        spec = s.spec
        existing_ids = {p.id for p in spec.parameters}
        for tp in tpl.get("parameters", []):
            if tp.get("id") not in existing_ids:
                spec.add_parameter(Parameter.from_dict(tp))
        feats = {f.id for f in spec.features}
        for tf in tpl.get("features", []):
            if tf.get("id") not in feats:
                spec.features.append(Feature.from_dict(tf))
        env = tpl.get("environment") or {}
        if env.get("manufacturing_method"):
            cur = spec.environment.get("manufacturing_method")
            if not cur or cur == "unknown":
                spec.environment["manufacturing_method"] = env["manufacturing_method"]
        spec._reindex()
        s.log.info(f"模板 {name} 已套用", actor="user", category="intent")
        if s.state.state == "UNDERSTAND":
            s.state.transition_or_none("SPEC_READY", "template applied")
        return jsonify({"ok": True, "template": name,
                        "parameters": len(spec.parameters),
                        "features": len(spec.features),
                        "spec": spec.to_dict()})

    # ------------------------------------------------------------------
    # Log / Registry / 授权
    # ------------------------------------------------------------------
    @bp.get("/log/<sid>")
    def get_log(sid):
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "session not found"}), 404
        return jsonify({"entries": s.log.entries})

    @bp.get("/bom/<sid>")
    def get_bom(sid):
        """BOM 采集(需求 §12): 设计内实体 + AI 属性标签; 支持 ?view= 预置视图。"""
        view = request.args.get("view", "all")
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "session not found"}), 404
        session, backend = _backend(sid)
        rows = []
        if hasattr(backend, "collect_bom"):
            try:
                rows = backend.collect_bom()
            except Exception:
                rows = []
        try:
            from ..executor.fusion_api import BOM_VIEWS, filter_bom
            filtered = filter_bom(rows, view)
            views = list(BOM_VIEWS.keys())
        except Exception:
            filtered, views = rows, ["all"]
        return jsonify({"ok": True, "session_id": sid,
                        "backend": type(backend).__name__,
                        "view": view, "views": views,
                        "count": len(filtered), "bom": filtered})

    @bp.get("/bom/<sid>/export")
    def get_bom_export(sid):
        """BOM 导出(CSV, UTF-8 BOM 兼容 Excel)。"""
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "session not found"}), 404
        session, backend = _backend(sid)
        rows = []
        if hasattr(backend, "collect_bom"):
            try:
                rows = backend.collect_bom()
            except Exception:
                rows = []
        import io
        import csv as _csv
        buf = io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(["实体", "体积mm3", "质量g", "AI属性"])
        for row in rows:
            attrs = row.get("attributes") or {}
            ai = "; ".join(f"{k}={v}" for k, v in attrs.items())
            writer.writerow([
                row.get("body_name", ""),
                row.get("volume_mm3") if row.get("volume_mm3") is not None else "",
                row.get("mass_g") if row.get("mass_g") is not None else "",
                ai,
            ])
        data = "\ufeff" + buf.getvalue()  # UTF-8 BOM
        return Response(
            data, mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=bom.csv"},
        )

    @bp.get("/registry")
    def registry_list():
        return jsonify(get_registry().to_dict())

    @bp.post("/authorize/<sid>")
    def authorize(sid):
        body = request.get_json(force=True) or {}
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "session not found"}), 404
        s.set_execute_enabled(bool(body.get("execute", False)))
        return jsonify({"ok": True, "execute_enabled": s.execute_enabled})

    # 可选后端选择(测试等)
    @bp.get("/_backend/<sid>")
    def backend_info(sid):
        session, backend = _backend(sid)
        if session is None:
            return jsonify({"error": "session not found"}), 404
        return jsonify({"backend": type(backend).__name__,
                        "available": getattr(backend, "available", True)})

    return bp


def _apply_validation_state(session: WorkflowSession, status: str) -> None:
    """需求 §3.1: VALIDATING → PASSED→COMPLETED / WARNING→USER_DECISION /
    FAILED→REPAIR_PLANNING。"""
    if status == "passed":
        session.state.transition_or_none("COMPLETED", "validation passed")
    elif status == "warning":
        session.state.transition_or_none("USER_DECISION", "validation warnings")
    else:
        session.state.transition_or_none("REPAIR_PLANNING", "validation failed")


__all__ = ["make_reliability_blueprint", "sessions"]