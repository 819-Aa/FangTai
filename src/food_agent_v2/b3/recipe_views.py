"""B3 菜谱消费者视图（T11）。

B3 只经 Repository 消费固定 Artifact 构建只读视图，不再解析原始食材字符串，
不再读取 data/cleaned JSONL（INV-023）。未知 recipe_id / 构建身份不一致 fail-closed。
"""

from __future__ import annotations

from food_agent_v2.b3.repository import (
    RecipeCatalogRepository,
    RecipeHealthIngredientView,
    RecipeRetrievalView,
    RecipeTimeView,
    default_mysql_repository,
)

# 兼容旧命名。
RecipeRetrievalBuildView = RecipeRetrievalView

__all__ = [
    "RecipeCatalogRepository",
    "RecipeHealthIngredientView",
    "RecipeRetrievalBuildView",
    "RecipeRetrievalView",
    "RecipeTimeView",
    "RecipeViewBuilder",
    "get_view_builder",
]


class RecipeViewBuilder:
    """菜谱只读视图构建：启动时经 Repository 批量加载为内存索引。"""

    def __init__(self, repository: RecipeCatalogRepository | None = None) -> None:
        self._repository = repository or default_mysql_repository()
        self._build_id = self._repository.ready_build_id()
        self._health: dict[int, RecipeHealthIngredientView] = {
            v.recipe_id: v for v in self._repository.all_health_views(self._build_id)
        }
        self._retrieval: dict[int, RecipeRetrievalView] = {
            v.recipe_id: v for v in self._repository.all_retrieval_views(self._build_id)
        }
        self._loaded = True

    @property
    def build_id(self) -> str:
        return self._build_id

    def get_recipe(self, recipe_id: int) -> dict | None:
        """返回含菜名的最小记录（供 C2 类型分类等只读用途）。"""
        view = self._retrieval.get(int(recipe_id))
        if view is None:
            return None
        return {"recipe_id": view.recipe_id, "名称": view.name}

    def build_health_ingredient_view(self, recipe_id: int) -> RecipeHealthIngredientView | None:
        return self._health.get(int(recipe_id))

    def build_retrieval_view(self, recipe_id: int) -> RecipeRetrievalView | None:
        return self._retrieval.get(int(recipe_id))

    def build_batch_health_views(self, recipe_ids: list[int]) -> dict[int, RecipeHealthIngredientView]:
        return {int(rid): self._health[int(rid)] for rid in recipe_ids if int(rid) in self._health}


# 模块级单例
_builder: RecipeViewBuilder | None = None


def get_view_builder() -> RecipeViewBuilder:
    global _builder
    if _builder is None:
        _builder = RecipeViewBuilder()
    return _builder
