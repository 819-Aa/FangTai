from food_agent_v2.b6 import NutritionScoringService


def _vector(protein: float, sodium: float, fiber: float = 5.0) -> dict:
    return {
        "energy_kcal": 100,
        "protein_g": protein,
        "fat_g": 5,
        "carbohydrate_g": 10,
        "fiber_g": fiber,
        "sodium_mg": sodium,
        "calcium_mg": 20,
        "iron_mg": 2,
        "cholesterol_mg": 0,
    }


class FakeNutritionSource:
    def ready_build_id(self) -> str:
        return "build-b6"

    def records(self, artifact_name: str, build_id: str) -> list[dict]:
        return [
            {
                "recipe_id": 1,
                "available": False,
                "reason": "mapping_missing",
                "raw_nutrition_per_100g": None,
            },
            {
                "recipe_id": 2,
                "available": True,
                "reason": None,
                "raw_nutrition_per_100g": _vector(10, 500),
            },
            {
                "recipe_id": 3,
                "available": True,
                "reason": None,
                "raw_nutrition_per_100g": _vector(30, 100),
            },
            {
                "recipe_id": 4,
                "available": True,
                "reason": None,
                "raw_nutrition_per_100g": _vector(1000, 1),
            },
        ]


def _service() -> NutritionScoringService:
    service = NutritionScoringService(FakeNutritionSource())
    service.load()
    return service


def test_unavailable_feature_never_returns_a_numeric_score() -> None:
    result = _service().score_candidates((1,), ("high_protein",))[1]

    assert result.available is False
    assert result.reason == "mapping_missing"
    assert result.weighted_total is None


def test_goal_percentiles_only_use_current_safe_set() -> None:
    scores = _service().score_candidates((2, 3), ("high_protein",))

    assert set(scores) == {2, 3}
    assert scores[3].weighted_total == 1.0
    assert scores[2].weighted_total == 0.0
    # recipe 4 的极端值存在于仓库，但不在本次 safe 集合，不能进入 percentile。


def test_low_direction_and_multiple_explicit_goals_are_supported() -> None:
    scores = _service().score_candidates((2, 3), ("low_sodium", "high_protein"))

    assert scores[3].goal_scores == {"low_sodium": 1.0, "high_protein": 1.0}
    assert scores[3].weighted_total == 1.0
    assert scores[2].weighted_total == 0.0


def test_no_explicit_goal_means_no_nutrition_total() -> None:
    result = _service().score_candidates((2, 3), ())[2]

    assert result.available is True
    assert result.reason is None
    assert result.weighted_total is None
    assert result.goal_scores == {}
