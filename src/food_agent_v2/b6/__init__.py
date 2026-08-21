"""B6 仅对本次 B4 安全集合执行显式营养目标软排序。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from food_agent_v2.b3.repository import MySQLArtifactRecordSource


class NutritionRecordSource(Protocol):
    def ready_build_id(self) -> str: ...
    def records(self, artifact_name: str, build_id: str) -> list[dict]: ...


@dataclass
class NutritionScore:
    dimension: str
    raw_score: float | None
    available: bool
    reason: str | None


@dataclass
class NutritionScoreDecomposition:
    recipe_id: int
    available: bool
    reason: str | None
    weighted_total: float | None
    goal_scores: dict[str, float] = field(default_factory=dict)
    dimension_scores: list[NutritionScore] = field(default_factory=list)


GoalDirection = Literal["higher", "lower"]
GOAL_SPECS: dict[str, tuple[str, GoalDirection]] = {
    "high_protein": ("protein_g", "higher"),
    "low_sodium": ("sodium_mg", "lower"),
    "high_fiber": ("fiber_g", "higher"),
    "low_fat": ("fat_g", "lower"),
    "high_calcium": ("calcium_mg", "higher"),
    "high_iron": ("iron_mg", "higher"),
}


class NutritionScoringService:
    def __init__(self, source: NutritionRecordSource | None = None) -> None:
        self._source = source or MySQLArtifactRecordSource()
        self._features: dict[int, dict] = {}
        self._loaded = False

    def load(self, path=None) -> None:
        build_id = self._source.ready_build_id()
        rows = self._source.records("nutrition_features", build_id)
        self._features = {int(record["recipe_id"]): record for record in rows}
        self._loaded = True

    def get_nutrition_profile(self, recipe_id: int) -> dict | None:
        return self._features.get(recipe_id)

    def score_candidates(
        self,
        safe_recipe_ids: tuple[int, ...],
        goal_codes: tuple[str, ...] = (),
    ) -> dict[int, NutritionScoreDecomposition]:
        unknown_goals = set(goal_codes) - set(GOAL_SPECS)
        if unknown_goals:
            raise ValueError(f"不支持的营养目标: {sorted(unknown_goals)}")
        safe_ids = tuple(dict.fromkeys(int(item) for item in safe_recipe_ids))
        results = {recipe_id: self._base_result(recipe_id) for recipe_id in safe_ids}
        if not goal_codes:
            return results

        values_by_goal: dict[str, dict[int, float]] = {}
        for goal_code in goal_codes:
            dimension, _direction = GOAL_SPECS[goal_code]
            values_by_goal[goal_code] = {
                recipe_id: float(feature["raw_nutrition_per_100g"][dimension])
                for recipe_id in safe_ids
                if (feature := self._features.get(recipe_id)) is not None
                and feature.get("available") is True
                and isinstance(feature.get("raw_nutrition_per_100g"), dict)
                and feature["raw_nutrition_per_100g"].get(dimension) is not None
            }

        for recipe_id, result in results.items():
            if not result.available:
                continue
            goal_scores: dict[str, float] = {}
            dimension_scores: list[NutritionScore] = []
            for goal_code in goal_codes:
                dimension, direction = GOAL_SPECS[goal_code]
                values = values_by_goal[goal_code]
                if recipe_id not in values:
                    result.available = False
                    result.reason = "nutrient_dimension_missing"
                    result.weighted_total = None
                    dimension_scores.append(
                        NutritionScore(dimension, None, False, "missing_dimension")
                    )
                    continue
                score = _percentile(values[recipe_id], tuple(values.values()), direction)
                goal_scores[goal_code] = score
                dimension_scores.append(NutritionScore(dimension, score, True, None))
            result.goal_scores = goal_scores
            result.dimension_scores = dimension_scores
            if result.available and len(goal_scores) == len(goal_codes):
                result.weighted_total = round(
                    sum(goal_scores.values()) / len(goal_scores),
                    4,
                )
        return results

    def score_recipe(
        self,
        recipe_id: int,
        goal_codes: tuple[str, ...] = (),
    ) -> NutritionScoreDecomposition:
        return self.score_candidates((recipe_id,), goal_codes)[recipe_id]

    def _base_result(self, recipe_id: int) -> NutritionScoreDecomposition:
        feature = self._features.get(recipe_id)
        if feature is None or feature.get("available") is not True:
            return NutritionScoreDecomposition(
                recipe_id=recipe_id,
                available=False,
                reason=(feature or {}).get("reason") or "missing_nutrition_feature",
                weighted_total=None,
            )
        return NutritionScoreDecomposition(
            recipe_id=recipe_id,
            available=True,
            reason=None,
            weighted_total=None,
        )

    def get_score_summary(
        self,
        recipe_id: int,
        goal_codes: tuple[str, ...] = (),
    ) -> str | None:
        result = self.score_recipe(recipe_id, goal_codes)
        if not result.available or result.weighted_total is None:
            return None
        level = "高" if result.weighted_total >= 0.7 else (
            "中" if result.weighted_total >= 0.4 else "低"
        )
        return f"营养目标匹配度: {level}"


def _percentile(value: float, population: tuple[float, ...], direction: GoalDirection) -> float:
    if not population:
        raise ValueError("percentile population 不能为空")
    if len(population) == 1:
        return 1.0
    less = sum(item < value for item in population)
    equal = sum(item == value for item in population)
    average_rank = less + (equal - 1) / 2
    higher_score = average_rank / (len(population) - 1)
    score = higher_score if direction == "higher" else 1.0 - higher_score
    return round(score, 4)


_service: NutritionScoringService | None = None


def get_nutrition_service() -> NutritionScoringService:
    global _service
    if _service is None:
        _service = NutritionScoringService()
        _service.load()
    return _service
