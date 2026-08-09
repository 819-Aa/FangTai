"""B3 食材身份解析器。

将原始食材名解析为标准 ingredient_id。
解析策略：精确匹配 → 审核别名匹配 → 未匹配/歧义。

重要：查询名使用与 B1 建注册表时相同的 normalize_ingredient_name 做归一化，
确保 B1 的"红枣"（已剥离"20g"）能匹配 B3 查询的"红枣肉20g"。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from food_agent_v2.core.paths import CLEANED_DIR
# 复用 B1 的同一套归一化函数
from food_agent_v2.b1.ingredient_identity import normalize_ingredient_name


class IngredientIdentity:
    """单次解析结果。"""
    __slots__ = ("ingredient_id", "name_canonical", "identity", "match_source",
                 "candidates")

    def __init__(self, ingredient_id: int | None, name_canonical: str | None,
                 identity: str, match_source: str | None = None,
                 candidates: list[dict] | None = None):
        self.ingredient_id = ingredient_id
        self.name_canonical = name_canonical
        self.identity = identity  # resolved | ambiguous | not_found
        self.match_source = match_source
        self.candidates = candidates or []

    def __repr__(self):
        return (f"IngredientIdentity(id={self.ingredient_id}, "
                f"name={self.name_canonical!r}, identity={self.identity})")


class IngredientIdentityResolver:
    """食材身份解析服务。由 B1 的 ingredient_registry.jsonl 初始化。

    使用与 B1 建注册表时相同的 normalize_ingredient_name 函数做查询归一化，
    确保"红枣肉20g"类型的查询名能匹配注册表中已剥离数量的"红枣肉"。
    """

    def __init__(self):
        self._exact_lookup: dict[str, dict] = {}       # 精确名 → registry entry
        self._loaded = False
        self._registry: list[dict] = []                # 完整注册表（保留给其他用途）

    def load(self, registry_path: Optional[Path] = None) -> None:
        """加载食材注册表。"""
        if registry_path is None:
            registry_path = CLEANED_DIR / "ingredient_registry.jsonl"

        self._exact_lookup.clear()
        self._registry.clear()

        with registry_path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                self._exact_lookup[rec["name_canonical"]] = rec
                self._registry.append(rec)

        self._loaded = True

    def resolve(self, raw_name: str) -> IngredientIdentity:
        """解析单个食材名。"""
        if not self._loaded:
            raise RuntimeError("Resolver not loaded. Call load() first.")

        clean_name = raw_name.strip()
        if not clean_name:
            return IngredientIdentity(None, None, "not_found")

        # 1. 精确匹配
        if clean_name in self._exact_lookup:
            rec = self._exact_lookup[clean_name]
            return IngredientIdentity(rec["ingredient_id"], clean_name, "resolved", "exact")

        # 2. 使用 B1 的归一化函数剥离数量/单位/括号/前缀后重试
        normalized = normalize_ingredient_name(clean_name)
        if normalized and normalized != clean_name:
            if normalized in self._exact_lookup:
                rec = self._exact_lookup[normalized]
                return IngredientIdentity(rec["ingredient_id"], normalized, "resolved", "normalized")

        # 3. 未匹配（设计 §10.4：只做精确匹配，不做子字符串/前缀模糊匹配）
        return IngredientIdentity(None, clean_name, "not_found")

    def get_display_name(self, ingredient_id: int) -> str | None:
        """根据 ingredient_id 获取标准显示名。"""
        for name, rec in self._exact_lookup.items():
            if rec["ingredient_id"] == ingredient_id:
                return name
        return None

    @property
    def ingredient_count(self) -> int:
        return len(self._exact_lookup)


# 模块级单例
_resolver: IngredientIdentityResolver | None = None


def get_resolver() -> IngredientIdentityResolver:
    global _resolver
    if _resolver is None:
        _resolver = IngredientIdentityResolver()
        _resolver.load()
    return _resolver
