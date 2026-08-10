"""T13 B6 软证据可用性测试。

营养特征不可用时输出 available=False + reason，不返回数值分（禁止 0.5/0.0
伪装）；可用时返回数值分；缺失维度不伪造。
"""

from food_agent_v2.b6 import NutritionScoringService


def _nutrient(code: str, value: float) -> dict:
    return {"nutrient_code": code, "value": value, "unit": "per_100g", "basis": "per_100g",
            "source_name": "s", "source_url": "u"}


class FakeNutritionSource:
    def ready_build_id(self) -> str:
        return "build-b6"

    def records(self, artifact_name: str, build_id: str) -> list[dict]:
        return [
            {
                "recipe_id": 1,
                "available": False,
                "reason": "missing_reference_mapping",
                "missing_ingredient_ids": [1, 2],
                "references": [],
            },
            {
                "recipe_id": 2,
                "available": True,
                "reason": "complete_reference_coverage",
                "references": [
                    {
                        "ingredient_id": 1, "reference_id": "r1", "reference_name": "ref1",
                        "match_method": "exact",
                        "nutrients": [
                            _nutrient("energy_kcal", 500), _nutrient("protein_g", 20),
                            _nutrient("fat_g", 10), _nutrient("carb_g", 40),
                            _nutrient("sodium_mg", 400), _nutrient("fiber_g", 5),
                        ],
                    }
                ],
                "missing_ingredient_ids": [],
            },
            {
                "recipe_id": 3,
                "available": True,
                "reason": "complete_reference_coverage",
                "references": [
                    {
                        "ingredient_id": 1, "reference_id": "r1", "reference_name": "ref1",
                        "match_method": "exact",
                        "nutrients": [_nutrient("energy_kcal", 500)],  # 其它维度缺失
                    }
                ],
                "missing_ingredient_ids": [],
            },
        ]


class TestSoftEvidenceAvailability:
    def _service(self) -> NutritionScoringService:
        service = NutritionScoringService(FakeNutritionSource())
        service.load()
        return service

    def test_unavailable_no_numeric_score(self) -> None:
        result = self._service().score_recipe(1)
        assert result.available is False
        assert result.reason == "missing_reference_mapping"
        assert result.weighted_total is None

    def test_unavailable_not_fabricated_as_neutral(self) -> None:
        result = self._service().score_recipe(1)
        # 不可用绝不伪装成 0.5 或 0.0。
        assert result.weighted_total is None
        assert result.weighted_total not in (0.5, 0.0)

    def test_available_computes_score(self) -> None:
        result = self._service().score_recipe(2)
        assert result.available is True
        assert result.reason is None
        assert result.weighted_total is not None
        assert 0.0 <= result.weighted_total <= 1.0

    def test_missing_dimension_not_fabricated(self) -> None:
        result = self._service().score_recipe(3)
        assert result.available is True
        dims = {s.dimension: s for s in result.dimension_scores}
        assert dims["protein_g"].available is False
        assert dims["protein_g"].raw_score is None

    def test_score_summary_none_when_unavailable(self) -> None:
        assert self._service().get_score_summary(1) is None
