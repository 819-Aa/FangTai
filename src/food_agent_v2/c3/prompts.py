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
