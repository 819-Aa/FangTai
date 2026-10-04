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
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class SSEEventType(StrEnum):
    HEARTBEAT = "heartbeat"
    REQUEST_ACCEPTED = "request_accepted"
    ANSWER_STARTED = "answer_started"
    ANALYSIS_READY = "analysis_ready"
    ANSWER_READY = "answer_ready"
    CLARIFICATION_NEEDED = "clarification_needed"
    RESULT_COMMITTED = "result_committed"
    ERROR = "error"
    REQUEST_CANCELLED = "request_cancelled"
    REQUEST_TERMINAL = "request_terminal"
    REQUEST_RECOVERY_REQUIRED = "request_recovery_required"
    # 流式与现代 Agent 交互扩展事件（RFC 2026-09-30）
    TEXT_DELTA = "text_delta"
    THOUGHT_NODE = "thought_node"
    TOOL_TRACE = "tool_trace"
    MENU_ARTIFACT = "menu_artifact"



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

# 自由文本敏感关键词（D1 §7.1，R05/C03 规范）：涵盖英文禁止字段与中文临床体征、疾病名称、过敏史
FORBIDDEN_TEXT_KEYWORDS = set(FORBIDDEN_RESPONSE_FIELDS) | {
    "血压", "血糖", "尿酸", "血脂", "胆固醇",
    "高血压", "低血压", "高血糖", "低血糖", "高尿酸", "高血脂",
    "糖尿病", "痛风", "冠心病", "心绞痛", "心肌梗塞", "脂肪肝", "肾病", "哮喘",
    "过敏史", "过敏原详情", "花生过敏", "海鲜过敏", "芒果过敏", "虾过敏", "蟹过敏",
    "病历", "病史", "患病",
}


PARTICIPANT_REF_RE = re.compile(r"^p([1-9]|[1-4][0-9]|50)$")
HEX_64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
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

    clar_resp = data.get("clarification_response")
    if clar_resp is not None:
        if not isinstance(clar_resp, dict):
            errors.append({"field": "clarification_response", "issue": "must be an object"})
        else:
            qid = clar_resp.get("question_id")
            opt_id = clar_resp.get("option_id")
            if not qid or not isinstance(qid, str) or not qid.strip():
                errors.append({"field": "clarification_response.question_id", "issue": "required non-empty string"})
            if opt_id is None or not isinstance(opt_id, int) or isinstance(opt_id, bool):
                errors.append({"field": "clarification_response.option_id", "issue": "required integer"})
            for forbidden_subfield in ("modifications", "private_snapshot", "query_plan_snapshot"):
                if forbidden_subfield in clar_resp:
                    errors.append({"field": f"clarification_response.{forbidden_subfield}", "issue": "forbidden internal field"})

    # R07: 单菜替换动作与结构化版本标识校验
    action = data.get("action")
    if action is not None:
        if action not in ("recommend", "replace_dish"):
            errors.append({"field": "action", "issue": "must be 'recommend' or 'replace_dish'"})
        elif action == "replace_dish":
            target_id = data.get("target_recipe_id")
            if target_id is None or not isinstance(target_id, int) or isinstance(target_id, bool) or target_id <= 0:
                errors.append({"field": "target_recipe_id", "issue": "required positive integer for replace_dish"})
            plan_id = data.get("source_plan_id")
            if not plan_id or not isinstance(plan_id, str) or not plan_id.strip():
                errors.append({"field": "source_plan_id", "issue": "required non-empty string for replace_dish"})
            mhash = data.get("source_menu_hash")
            if not mhash or not isinstance(mhash, str) or not HEX_64_RE.match(mhash):
                errors.append({"field": "source_menu_hash", "issue": "required 64-char hexadecimal hash for replace_dish"})

    return {"error": "VALIDATION_FAILED", "details": errors} if errors else None


def scan_forbidden_fields(obj: Any, path: str = "") -> list[str]:
    """递归扫描禁止字段与敏感自由文本（D1 §7.1，Section 3 规范）。"""
    violations = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key in FORBIDDEN_RESPONSE_FIELDS:
                violations.append(f"{path}.{key}" if path else key)
            violations.extend(scan_forbidden_fields(val, f"{path}.{key}" if path else key))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            violations.extend(scan_forbidden_fields(item, f"{path}[{i}]"))
    elif isinstance(obj, str):
        lower_val = obj.lower()
        for forbidden in FORBIDDEN_TEXT_KEYWORDS:
            if forbidden in lower_val:
                violations.append(f"{path} contains sensitive keyword '{forbidden}'")
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
