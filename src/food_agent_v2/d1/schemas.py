"""D1 API 请求/响应 Schema。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

# Simplified Pydantic-like schemas (FastAPI integration adds full Pydantic at runtime)

class RequestStatus(str, Enum):
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


class SSEEventType(str, Enum):
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
    """校验 POST /v1/recommendation-requests 的请求体。返回错误或 None。"""
    errors = []

    if not data.get("idempotency_key"):
        errors.append({"field": "idempotency_key", "issue": "required"})
    elif len(data.get("idempotency_key", "")) > 128:
        errors.append({"field": "idempotency_key", "issue": "too long"})

    participants = data.get("participants", [])
    if not participants:
        errors.append({"field": "participants", "issue": "at least one required"})
    else:
        refs = [p.get("participant_ref") for p in participants if p.get("participant_ref")]
        if len(refs) != len(set(refs)):
            errors.append({"field": "participants", "issue": "duplicate participant_ref"})

    if not data.get("message"):
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
    return datetime.now(timezone.utc).isoformat()
