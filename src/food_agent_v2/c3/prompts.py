"""C3 提示词加载 —— 从 config/prompts.json 读取。"""

from __future__ import annotations

import json
from pathlib import Path


_PROMPTS: dict | None = None


def _load() -> dict:
    global _PROMPTS
    if _PROMPTS is not None:
        return _PROMPTS
    path = Path(__file__).resolve().parent.parent.parent.parent / "config" / "prompts.json"
    _PROMPTS = json.loads(path.read_text(encoding="utf-8"))
    return _PROMPTS


def get_prompt(role: str) -> str:
    """获取指定角色的 system prompt。"""
    prompts = _load()
    entry = prompts.get(role, {})
    return entry.get("system", "")


def get_model(role: str) -> str:
    """获取指定角色的推荐模型。"""
    prompts = _load()
    entry = prompts.get(role, {})
    return entry.get("model", "")


def get_test_cases(role: str) -> list[dict]:
    """获取指定角色的测试用例。"""
    prompts = _load()
    entry = prompts.get(role, {})
    return entry.get("test_cases", [])


# 向后兼容的模块级变量（供 runner.py 使用）
QUERY_UNDERSTANDING_SYSTEM = property(lambda self: get_prompt("query_understanding"))
HEALTH_MENU_PLANNING_SYSTEM = property(lambda self: get_prompt("health_menu_planning"))
MENU_DECISION_SYSTEM = property(lambda self: get_prompt("menu_decision"))
ANSWER_GENERATION_SYSTEM = property(lambda self: get_prompt("answer_generation"))
UNIFIED_REVIEW_SYSTEM = property(lambda self: get_prompt("unified_review"))

# runner.py 使用的字典
SYSTEM_PROMPTS = {
    "query_understanding": lambda: get_prompt("query_understanding"),
    "health_menu_planning": lambda: get_prompt("health_menu_planning"),
    "menu_decision": lambda: get_prompt("menu_decision"),
    "answer_generation": lambda: get_prompt("answer_generation"),
    "unified_review": lambda: get_prompt("unified_review"),
}
