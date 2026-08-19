"""验证报告结构（需求 §10.2-§10.3）。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

STATUS_PASSED = "passed"
STATUS_WARNING = "warning"
STATUS_FAILED = "failed"


@dataclass
class ValidationCheck:
    id: str
    type: str
    status: str            # passed | warning | failed
    severity: str = "info" # info | warning | error
    target: str = ""
    actual: Any = None
    expected: Any = None
    message: str = ""
    suggestions: list = field(default_factory=list)  # [{action, parameter, value}]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "type": self.type, "status": self.status,
            "severity": self.severity, "target": self.target,
            "actual": self.actual, "expected": self.expected,
            "message": self.message, "suggestions": list(self.suggestions),
        }


class ValidationReport:
    def __init__(self, validation_id: str = "") -> None:
        self.validation_id = validation_id or f"validation_{uuid.uuid4().hex[:8]}"
        self.checks: list[ValidationCheck] = []
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.meta: dict = {}

    def add(self, check: ValidationCheck) -> None:
        self.checks.append(check)

    @property
    def status(self) -> str:
        if any(c.status == STATUS_FAILED for c in self.checks):
            return STATUS_FAILED
        if any(c.status == STATUS_WARNING for c in self.checks):
            return STATUS_WARNING
        return STATUS_PASSED

    def to_dict(self) -> dict:
        return {
            "validation_id": self.validation_id,
            "status": self.status,
            "created_at": self.created_at,
            "meta": dict(self.meta),
            "checks": [c.to_dict() for c in self.checks],
        }


def make_check(status: str, ctype: str, message: str, **kw: Any) -> ValidationCheck:
    severity = "error" if status == STATUS_FAILED else ("warning" if status == STATUS_WARNING else "info")
    return ValidationCheck(
        id=f"check_{ctype}_{uuid.uuid4().hex[:6]}",
        type=ctype,
        status=status,
        severity=severity,
        message=message,
        **kw,
    )


__all__ = ["ValidationReport", "ValidationCheck", "make_check",
           "STATUS_PASSED", "STATUS_WARNING", "STATUS_FAILED"]