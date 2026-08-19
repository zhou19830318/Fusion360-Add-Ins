"""结构化执行日志（需求 §3 / §8 / §15: 执行日志）。

每个事件：
  ts          ISO 时间戳
  session_id  会话
  state       状态机当前状态
  level       info | warning | error | critical
  actor       user | ai | system | executor | validator
  source      node_id / intent / plan / tool / ...
  category    intent | clarify | plan | execute | validate | rollback | gate | server
  message     人类可读
  data        结构化上下文(关键参数不落明文密钥)
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional


class ExecutionLog:
    def __init__(self, session_id: str = "", max_entries: int = 2000) -> None:
        self.session_id = session_id
        self._entries: list[dict] = []
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._file: Optional[str] = None

    def attach_file(self, path: str) -> None:
        """将日志持久化到文件（JSON Lines 追加写）。"""
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._file = path
        except Exception:
            self._file = None

    def _flush(self, entry: dict) -> None:
        if self._file:
            try:
                with open(self._file, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def add(
        self,
        message: str,
        level: str = "info",
        actor: str = "system",
        source: str = "",
        category: str = "log",
        data: Optional[dict] = None,
    ) -> dict:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "session_id": self.session_id,
            "level": level,
            "actor": actor,
            "source": source,
            "category": category,
            "message": message,
            "data": data or {},
        }
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._max_entries:
                self._entries = self._entries[-self._max_entries:]
        self._flush(entry)
        return entry

    def info(self, msg: str, **kw: Any) -> dict:
        return self.add(msg, level="info", **kw)

    def warning(self, msg: str, **kw: Any) -> dict:
        return self.add(msg, level="warning", **kw)

    def error(self, msg: str, **kw: Any) -> dict:
        return self.add(msg, level="error", **kw)

    def critical(self, msg: str, **kw: Any) -> dict:
        return self.add(msg, level="critical", **kw)

    @property
    def entries(self) -> list[dict]:
        with self._lock:
            return list(self._entries)

    def filter(self, category: Optional[str] = None, level: Optional[str] = None,
               source: Optional[str] = None) -> list[dict]:
        out = []
        for e in self.entries:
            if category and e["category"] != category:
                continue
            if level and e["level"] != level:
                continue
            if source and e["source"] != source:
                continue
            out.append(e)
        return out

    def as_json(self) -> str:
        return json.dumps(self.entries, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 持久化（可选；MVP 默认内存 + 追加写文件）
    # ------------------------------------------------------------------
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for e in self.entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    @classmethod
    def load(cls, path: str) -> "ExecutionLog":
        log = cls()
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            log._entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        return log


__all__ = ["ExecutionLog"]