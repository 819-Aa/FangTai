"""D1 API 请求/响应 Schema。"""

from __future__ import annotations

import re
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
    STRICT_TIME_INDETERMINATE = "strict_time_indeterminate"
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
    REQUEST_TERMINAL = "request_terminal"


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


PARTICIPANT_REF_RE = re.compile(r"^p([1-9]|[1-4][0-9]|50)$")
MAX_ANONYMOUS_PARTICIPANTS = 50


def resolve_participants(participants: list[dict]) -> tuple[list[dict], list[dict]]:
    """匿名 participant_ref → 内部固定 user_id 的确定性映射（D1 服务端边界）。

    合法引用 p1..p50 确定映射到固定 user_id 1..50（data/cleaned/user_profiles.jsonl
    共 50 条）。非法格式、越界、重复、携带 user_id 一律返回错误（启动工作流前 422）。

    返回 (enhanced_participants, errors)。enhanced 只用于服务端内部传给 C3
    （含 user_id），绝不进入公共响应/SSE/日志/前端。
    """
    errors: list[dict] = []
    enhanced: list[dict] = []
    seen: set[str] = set()
    for i, p in enumerate(participants):
        if not isinstance(p, dict):
            errors.append({"field": f"participants[{i}]", "issue": "must be an object"})
            continue
        if "user_id" in p:
            errors.append({"field": f"participants[{i}].user_id",
                           "issue": "公共接口只允许匿名 participant_ref，禁止提交 user_id"})
            continue
        ref = p.get("participant_ref")
        if not isinstance(ref, str) or not PARTICIPANT_REF_RE.match(ref):
            errors.append({"field": f"participants[{i}].participant_ref",
                           "issue": f"非法或越界引用: {ref!r}（仅 p1..p50）"})
            continue
        if ref in seen:
            errors.append({"field": f"participants[{i}].participant_ref",
                           "issue": f"重复引用: {ref}"})
            continue
        seen.add(ref)
        num = int(ref[1:])
        enhanced.append({
            "participant_ref": ref,
            "label": str(p.get("label") or ref),
            "user_id": num,  # 内部固定映射 pN → user_id N
        })
    return enhanced, errors


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
            if "user_id" in p:
                errors.append({"field": f"participants[{i}].user_id",
                               "issue": "公共接口只允许匿名 participant_ref，禁止提交 user_id"})
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


def strip_forbidden_fields(obj: Any) -> Any:
    """递归移除禁止字段（投影），返回副本，其余结构不变。"""
    if isinstance(obj, dict):
        return {k: strip_forbidden_fields(v) for k, v in obj.items()
                if k not in FORBIDDEN_RESPONSE_FIELDS}
    if isinstance(obj, list):
        return [strip_forbidden_fields(i) for i in obj]
    return obj


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
