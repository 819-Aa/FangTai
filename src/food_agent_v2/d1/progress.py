"""Closed public vocabulary for execution progress (never model/error prose)."""

import re

_INVOCATION = re.compile(r"[0-9a-f]{32}")
_REFERENCE = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|ev:\d+)")

NODE_TITLES = {
    "context_building": "上下文解析与需求对齐",
    "query_understanding": "意图分析与查询计划",
    "candidate_search": "菜品候选检索",
    "recipe_audit": "健康合规审查",
    "menu_combination": "营养与偏好组合",
    "final_validation": "全流程合规复核",
    "inquire_user": "需求澄清",
    "commit": "结果持久化",
    "processing": "请求处理",
}
STATUS_SUFFIX = {
    "running": "进行中",
    "done": "已完成",
    "warning": "需要确认",
    "error": "未完成",
}
TOOL_SUMMARIES = {
    "search_candidates": "候选检索已完成",
    "audit_recipe_health": "健康审查已完成",
    "combine_nutritional_menu": "菜单规划已结束",
    "validate_selected_menu_health": "菜单复核已完成",
    "processing": "工具执行已结束",
}
STAGE_NODES = {
    "context_ready": "context_building",
    "query_understanding": "query_understanding",
    "retrieval": "candidate_search",
    "health_evaluation": "recipe_audit",
    "health": "recipe_audit",  # historical API alias
    "menu_planning": "menu_combination",
    "constraint_inquiry": "inquire_user",
}


def public_node(node_id: str) -> str:
    return node_id if node_id in NODE_TITLES else "processing"


def public_tool(tool_name: str) -> str:
    return tool_name if tool_name in TOOL_SUMMARIES else "processing"


def validate_invocation(invocation: str) -> str:
    if not isinstance(invocation, str) or not _INVOCATION.fullmatch(invocation):
        raise ValueError("Invalid progress invocation identity")
    return invocation


def public_refs(refs: list[str]) -> list[str]:
    return [ref for ref in refs if isinstance(ref, str) and _REFERENCE.fullmatch(ref)]
