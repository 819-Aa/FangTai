"""D1 API 请求/响应 Schema。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# Simplified Pydantic-like schemas (FastAPI integration adds full Pydantic at runtime)

class RequestStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    REVISING = "revising"
    COMPLETED = "completed"
    NEEDS_CLARIFICATION = "needs_clarification"
    NO_SAFE_MENU = "no_safe_menu"
    NO_FEASIBLE_MENU = "no_feasible_menu"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class SSEEventType(StrEnum):
    HEARTBEAT = "heartbeat"
    REQUEST_ACCEPTED = "request_accepted"
    ANALYSIS_READY = "analysis_ready"
    ANSWER_READY = "answer_ready"
    CLARIFICATION_NEEDED = "clarification_needed"
    RESULT_COMMITTED = "result_committed"
    ERROR = "error"
    REQUEST_CANCELLED = "request_cancelled"


# 禁止字段列表（D1 §7.1）
FORBIDDEN_RESPONSE_FIELDS = {
    "blood_pressure", "blood_sugar", "uric_acid", "cholesterol",
    "disease_name", "allergy_type", "condition_detail",
    "nutrition_value", "energy_kcal", "sodium_mg", "protein_g",
    "per_serving", "serving_size", "portion_count",
    "health_rule_score", "caution_reason",
    "participant_real_name", "user_id",
    "model_thought_chain", "internal_reasoning",
    "qdrant_collection", "mysql_connection_string", "redis_key",
}


def validate_create_request(data: dict) -> dict | None:
    """校验 POST /v1/recommendation-requests 的请求体。返回错误或 None。

    无效请求绝不启动任务：缺 idempotency_key、participant_ref、message，或
    participant_ref 重复/非字符串、message 非字符串、participants 非对象列表
    一律拒绝。
    """
    errors = []

    idem = data.get("idempotency_key")
    if not idem:
        errors.append({"field": "idempotency_key", "issue": "required"})
    elif not isinstance(idem, str) or len(idem) > 128:
        errors.append({"field": "idempotency_key", "issue": "too long or invalid"})

    participants = data.get("participants", [])
    if not isinstance(participants, list) or not participants:
        errors.append({"field": "participants", "issue": "at least one required"})
    else:
        refs: list[str] = []
        for i, p in enumerate(participants):
            if not isinstance(p, dict):
                errors.append({"field": f"participants[{i}]", "issue": "must be an object"})
                continue
            ref = p.get("participant_ref")
            if not ref or not isinstance(ref, str) or not ref.strip():
                errors.append({"field": f"participants[{i}].participant_ref", "issue": "required"})
            else:
                refs.append(ref.strip())
        if len(refs) != len(set(refs)):
            errors.append({"field": "participants", "issue": "duplicate participant_ref"})

    message = data.get("message")
    if not message or not isinstance(message, str):
        errors.append({"field": "message", "issue": "required"})

    return {"error": "VALIDATION_FAILED", "details": errors} if errors else None


def scan_forbidden_fields(obj: Any, path: str = "") -> list[str]:
    """递归扫描禁止字段（D1 §7.1）。"""
    violations = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key in FORBIDDEN_RESPONSE_FIELDS:
                violations.append(f"{path}.{key}" if path else key)
            violations.extend(scan_forbidden_fields(val, f"{path}.{key}" if path else key))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            violations.extend(scan_forbidden_fields(item, f"{path}[{i}]"))
    return violations


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
