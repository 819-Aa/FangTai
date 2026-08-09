"""B3 菜谱消费者视图构建。

为 B4（健康审查）和 C1（RAG 检索）构建只读视图。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from food_agent_v2.core.paths import CLEANED_RECIPES, CLEANED_DIR
from food_agent_v2.b3.ingredient_parser import parse_ingredients, Occurrence
from food_agent_v2.b3.identity_resolver import get_resolver, IngredientIdentity


@dataclass
class RecipeHealthIngredientView:
    """B4 健康审查用的菜品食材视图。

    包含 required ∪ optional ∪ all alternative members ∪ verified composition children。
    """
    recipe_id: int
    ingredient_ids: list[int]                     # 完整并集
    ingredient_evidence_paths: dict[int, str]     # ingredient_id → 来源路径
    unresolved_occurrence_count: int              # 未解析的 occurence 数
    composition_expansion_status: str             # atomic | complete | invalid
    has_optional: bool
    has_alternatives: bool


@dataclass
class RecipeRetrievalBuildView:
    """C1 RAG 检索用的菜谱视图。不含健康字段和营养值。"""
    recipe_id: int
    name: str
    ingredient_display_names: list[str]            # 标准食材显示名
    searchable_fields: dict[str, str]              # 非健康检索字段
    step_summary: str | None
    time_reference: str | None
    ingredient_family_ids: list[int] = field(default_factory=list)


class RecipeViewBuilder:
    """菜谱视图构建服务。"""

    def __init__(self):
        self._resolver = get_resolver()
        self._recipes: dict[int, dict] = {}
        self._rag_docs: dict[int, dict] = {}
        self._loaded = False

    def load(self,
             recipes_path: Optional[Path] = None,
             rag_path: Optional[Path] = None) -> None:
        if recipes_path is None:
            recipes_path = CLEANED_RECIPES
        if rag_path is None:
            rag_path = CLEANED_DIR / "rag_documents.jsonl"

        with recipes_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    self._recipes[r["recipe_id"]] = r

        if rag_path.exists():
            with rag_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        d = json.loads(line)
                        self._rag_docs[d["recipe_id"]] = d

        self._loaded = True

    def get_recipe(self, recipe_id: int) -> dict | None:
        return self._recipes.get(recipe_id)

    def build_health_ingredient_view(self, recipe_id: int) -> RecipeHealthIngredientView | None:
        """为 B4 构建健康审查视图。"""
        recipe = self._recipes.get(recipe_id)
        if not recipe:
            return None

        raw = recipe.get("食材清单", "")
        occurrences = parse_ingredients(raw)

        ingredient_ids: list[int] = []
        evidence_paths: dict[int, str] = {}
        unresolved = 0
        has_optional = False
        has_alternatives = False

        for occ in occurrences:
            identity = self._resolver.resolve(occ.name_clean)
            if identity.identity == "resolved" and identity.ingredient_id:
                iid = identity.ingredient_id
                if iid not in ingredient_ids:
                    ingredient_ids.append(iid)
                evidence_paths[iid] = f"ingredient:{occ.name_clean}"
            else:
                unresolved += 1

            if occ.is_optional:
                has_optional = True
            if occ.choice_group_id:
                has_alternatives = True

        # 复合组成状态：当前版本所有菜品标记为 atomic（B3 复合组成验证待建设）
        expansion_status = "atomic"

        return RecipeHealthIngredientView(
            recipe_id=recipe_id,
            ingredient_ids=sorted(ingredient_ids),
            ingredient_evidence_paths=evidence_paths,
            unresolved_occurrence_count=unresolved,
            composition_expansion_status=expansion_status,
            has_optional=has_optional,
            has_alternatives=has_alternatives,
        )

    def build_retrieval_view(self, recipe_id: int) -> RecipeRetrievalBuildView | None:
        """为 C1 构建检索视图。不含健康字段和营养值。"""
        recipe = self._recipes.get(recipe_id)
        if not recipe:
            return None

        raw = recipe.get("食材清单", "")
        occurrences = parse_ingredients(raw)
        display_names: list[str] = []
        family_ids: list[int] = []

        for occ in occurrences:
            identity = self._resolver.resolve(occ.name_clean)
            if identity.identity == "resolved" and identity.ingredient_id:
                display_name = self._resolver.get_display_name(identity.ingredient_id)
                if display_name and display_name not in display_names:
                    display_names.append(display_name)

        # 从 RAG 文档获取检索字段
        rag_doc = self._rag_docs.get(recipe_id, {})
        searchable_fields = rag_doc.get("searchable_fields", {})

        return RecipeRetrievalBuildView(
            recipe_id=recipe_id,
            name=recipe["名称"],
            ingredient_display_names=display_names,
            searchable_fields=searchable_fields,
            step_summary=rag_doc.get("step_summary"),
            time_reference=rag_doc.get("time_reference"),
            ingredient_family_ids=family_ids,
        )

    def build_batch_health_views(self, recipe_ids: list[int]) -> dict[int, RecipeHealthIngredientView]:
        """批量构建健康审查视图。"""
        return {rid: self.build_health_ingredient_view(rid) for rid in recipe_ids
                if self.build_health_ingredient_view(rid) is not None}


# 模块级单例
_builder: RecipeViewBuilder | None = None


def get_view_builder() -> RecipeViewBuilder:
    global _builder
    if _builder is None:
        _builder = RecipeViewBuilder()
        _builder.load()
    return _builder
