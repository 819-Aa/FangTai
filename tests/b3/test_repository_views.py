"""T11 B3 Repository 消费者视图测试。

用注入的 Fake 记录源验证：视图正确反序列化、未知 recipe_id fail-closed、
构建身份不一致 fail-closed、Builder 内存索引与 Resolver 别名解析。
"""

import pytest

from food_agent_v2.b3.identity_resolver import IngredientIdentityResolver
from food_agent_v2.b3.recipe_views import RecipeViewBuilder
from food_agent_v2.b3.repository import FixedDataRepository, RepositoryError

BUILD = "build-abc"


class FakeSource:
    """测试用固定 Artifact 记录源。"""

    def __init__(self) -> None:
        self._records = {
            "recipe_health_views": [
                {
                    "build_id": BUILD, "recipe_id": 1,
                    "ingredient_ids": [1, 2],
                    "ingredient_evidence_paths": ["recipe:1/occurrence:1-1/ingredient:1"],
                    "unresolved_occurrence_count": 0,
                    "composition_expansion_status": "atomic",
                    "catalog_eligibility": "eligible",
                },
                {
                    "build_id": BUILD, "recipe_id": 2,
                    "ingredient_ids": [3],
                    "ingredient_evidence_paths": [],
                    "unresolved_occurrence_count": 0,
                    "composition_expansion_status": "atomic",
                    "catalog_eligibility": "eligible",
                },
            ],
            "recipe_retrieval_build_views": [
                {
                    "build_id": BUILD, "recipe_id": 1, "name": "秋梨膏",
                    "ingredient_display_names": ["秋梨"], "ingredient_ids": [1],
                    "searchable_fields": {}, "step_summary": "总结",
                    "time_reference": "30分钟", "ingredient_family_ids": [9],
                },
            ],
            "recipe_step_binding_views": [
                {
                    "build_id": BUILD, "recipe_id": 1, "ingredient_ids": [1],
                    "steps": [{"step_index": 1, "raw_text": "步骤一"}],
                },
            ],
            "ingredient_registry": [
                {"build_id": BUILD, "ingredient_id": 1, "name_canonical": "秋梨"},
            ],
            "ingredient_aliases": [
                {"build_id": BUILD, "alias": "梨", "ingredient_id": 1},
            ],
        }

    def ready_build_id(self) -> str:
        return BUILD

    def records(self, artifact_name: str, build_id: str) -> list[dict]:
        if build_id != BUILD:
            raise AssertionError(f"意外的 build_id: {build_id}")
        return list(self._records.get(artifact_name, []))


@pytest.fixture
def repository() -> FixedDataRepository:
    return FixedDataRepository(FakeSource())


class TestRepositoryViews:
    def test_health_view(self, repository) -> None:
        views = repository.get_health_view([1, 2], BUILD)
        assert [v.recipe_id for v in views] == [1, 2]
        assert views[0].ingredient_ids == [1, 2]
        assert views[0].unresolved_occurrence_count == 0

    def test_unknown_recipe_fails_closed(self, repository) -> None:
        with pytest.raises(RepositoryError) as excinfo:
            repository.get_health_view([999], BUILD)
        assert excinfo.value.code == "UNKNOWN_RECIPE_ID"

    def test_retrieval_view(self, repository) -> None:
        view = repository.get_retrieval_view([1], BUILD)[0]
        assert view.name == "秋梨膏"
        assert view.ingredient_family_ids == [9]

    def test_time_view(self, repository) -> None:
        view = repository.get_time_view([1], BUILD)[0]
        assert view.ingredient_ids == [1]
        assert view.steps[0]["step_index"] == 1

    def test_build_identity_mismatch_fails(self) -> None:
        source = FakeSource()
        source._records["recipe_health_views"][0]["build_id"] = "other-build"
        repository = FixedDataRepository(source)
        with pytest.raises(RepositoryError) as excinfo:
            repository.get_health_view([1], BUILD)
        assert excinfo.value.code == "BUILD_IDENTITY_MISMATCH"

    def test_builder_in_memory_index(self, repository) -> None:
        builder = RecipeViewBuilder(repository)
        assert builder.build_health_ingredient_view(1) is not None
        assert builder.build_health_ingredient_view(999) is None
        retrieval = builder.build_retrieval_view(1)
        assert retrieval is not None and retrieval.name == "秋梨膏"
        assert builder.get_recipe(1)["名称"] == "秋梨膏"
        assert builder.get_recipe(999) is None

    def test_resolver_exact_and_alias(self, repository) -> None:
        resolver = IngredientIdentityResolver(repository)
        exact = resolver.resolve("秋梨")
        assert exact.identity == "resolved"
        assert exact.ingredient_id == 1
        alias = resolver.resolve("梨")
        assert alias.identity == "resolved"
        assert alias.ingredient_id == 1
        missing = resolver.resolve("某种不存在的食材")
        assert missing.identity == "not_found"
        assert missing.ingredient_id is None
