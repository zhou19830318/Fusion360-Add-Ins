"""AI 对象注册表（需求 §12.1 对象元数据，MVP：内存实现）。

为 AI 创建/修改的对象记录:
  ai_feature_id / semantic_role / intent_id / plan_node_id /
  source_prompt_hash / parameter_refs / created_by / revision

后端 Fusion attributes 写入(vendorAttributes)作为后续阶段能力;
MVP 先保证内存注册与 API 查询路径可用。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Optional


def prompt_hash(user_text: str) -> str:
    return "sha256:" + hashlib.sha256((user_text or "").encode("utf-8")).hexdigest()


class EntityRegistry:
    def __init__(self) -> None:
        self._records: list[dict] = []
        self._by_token: dict[str, dict] = {}

    def register(
        self,
        entity_token: str,
        semantic_role: str = "",
        intent_id: str = "",
        plan_node_id: str = "",
        source_prompt_hash: str = "",
        parameter_refs: Optional[list] = None,
        kind: str = "body",
        name: str = "",
    ) -> dict:
        rec = {
            "ai_feature_id": f"feature_{uuid.uuid4().hex[:8]}",
            "entity_token": entity_token,
            "kind": kind,
            "semantic_role": semantic_role,
            "intent_id": intent_id,
            "plan_node_id": plan_node_id,
            "source_prompt_hash": source_prompt_hash,
            "parameter_refs": list(parameter_refs or []),
            "created_by": "ai",
            "revision": "A",
            "name": name,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._records.append(rec)
        if entity_token:
            self._by_token[entity_token] = rec
        return rec

    def unregister_by_plan_node(self, plan_node_id: str) -> list[str]:
        removed = [r for r in self._records if r["plan_node_id"] == plan_node_id]
        self._records = [r for r in self._records if r["plan_node_id"] != plan_node_id]
        for r in removed:
            self._by_token.pop(r.get("entity_token"), None)
        return [r["entity_token"] for r in removed]

    def by_intent(self, intent_id: str) -> list[dict]:
        return [r for r in self._records if r["intent_id"] == intent_id]

    def list(self) -> list[dict]:
        return list(self._records)

    def clear(self) -> None:
        self._records = []
        self._by_token = {}

    def to_dict(self) -> dict:
        return {"count": len(self._records), "records": list(self._records)}


# 全局单例（进程内; Fusion Server 与 API 共用）
_registry = EntityRegistry()


def get_registry() -> EntityRegistry:
    return _registry


__all__ = ["EntityRegistry", "get_registry", "prompt_hash"]