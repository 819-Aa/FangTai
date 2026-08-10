"""B3 食材身份解析器（T11）。

只经 Repository 消费固定 ingredient_registry + ingredient_aliases，不再导入
B1 解析器、不再读取 JSONL（INV-023）。解析只做精确匹配与已批准别名匹配，
不做子字符串/前缀模糊创建身份（R-016）。
"""

from __future__ import annotations

from food_agent_v2.b3.repository import RecipeCatalogRepository, default_mysql_repository


class IngredientIdentity:
    """单次解析结果。"""

    __slots__ = ("ingredient_id", "name_canonical", "identity", "match_source", "candidates")

    def __init__(self, ingredient_id, name_canonical, identity, match_source=None, candidates=None):
        self.ingredient_id = ingredient_id
        self.name_canonical = name_canonical
        self.identity = identity  # resolved | ambiguous | not_found
        self.match_source = match_source
        self.candidates = candidates or []

    def __repr__(self):
        return (
            f"IngredientIdentity(id={self.ingredient_id}, "
            f"name={self.name_canonical!r}, identity={self.identity})"
        )


class IngredientIdentityResolver:
    """由固定 ingredient_registry 与 ingredient_aliases 构建的只读身份解析。"""

    def __init__(self, repository: RecipeCatalogRepository | None = None, build_id: str | None = None) -> None:
        self._repository = repository or default_mysql_repository()
        self._build_id = build_id or self._repository.ready_build_id()
        self._exact_lookup: dict[str, dict] = {}
        self._alias_lookup: dict[str, int] = {}
        self._registry: list[dict] = []
        self._load()

    def _load(self) -> None:
        self._exact_lookup.clear()
        self._alias_lookup.clear()
        self._registry.clear()
        for record in self._repository.registry_entries(self._build_id):
            self._exact_lookup[record["name_canonical"]] = record
            self._registry.append(record)
        for alias in self._repository.alias_entries(self._build_id):
            self._alias_lookup[alias["alias"]] = int(alias["ingredient_id"])

    def resolve(self, raw_name: str) -> IngredientIdentity:
        clean_name = raw_name.strip()
        if not clean_name:
            return IngredientIdentity(None, None, "not_found")

        # 1. 精确匹配
        if clean_name in self._exact_lookup:
            record = self._exact_lookup[clean_name]
            return IngredientIdentity(record["ingredient_id"], clean_name, "resolved", "exact")

        # 2. 已批准别名匹配
        if clean_name in self._alias_lookup:
            return IngredientIdentity(self._alias_lookup[clean_name], clean_name, "resolved", "alias")

        # 3. 未匹配：不创建身份、不做模糊匹配（R-016）。
        return IngredientIdentity(None, clean_name, "not_found")

    def get_display_name(self, ingredient_id: int) -> str | None:
        for name, record in self._exact_lookup.items():
            if record["ingredient_id"] == ingredient_id:
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
    return _resolver
