"""B6 营养评分（T13）。

- 营养特征缺失（不可用）时输出 `available: bool` 与 `reason`，不返回数值分
  （INV-022：禁止用 0.5/零值/均值伪装已计算）。
- 只做软排序，不参与健康硬筛选；营养值不进入 B4、SSE、前端。
- 数据来源为固定 nutrition_features Artifact（Repository），不再读取 JSONL。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from food_agent_v2.b3.repository import MySQLArtifactRecordSource


class NutritionRecordSource(Protocol):
    def ready_build_id(self) -> str: ...
    def records(self, artifact_name: str, build_id: str) -> list[dict]: ...


@dataclass
class NutritionScore:
    """单个营养维度评分；不可用时 raw_score=None。"""

    dimension: str
    raw_score: float | None
    available: bool
    reason: str | None


@dataclass
class NutritionScoreDecomposition:
    """营养评分分解；不可用时 available=False 且 weighted_total=None。"""

    recipe_id: int
    available: bool
    reason: str | None
    weighted_total: float | None
    goal_scores: dict[str, float] = field(default_factory=dict)
    dimension_scores: list[NutritionScore] = field(default_factory=list)
    coverage_ratio: float = 0.0
    overall_confidence: str = "low"


class NutritionScoringService:
    """营养评分服务（仅软排序；不可用不返回数值分）。"""

    # 置信度折扣（doc 06 §7.4）。
    CONFIDENCE_DISCOUNTS = {"high": 1.0, "medium": 0.7, "low": 0.3}

    # 各维度理想目标范围（基于通用膳食指南）。
    DIMENSION_GOALS = {
        "energy_kcal": (300, 800),
        "protein_g": (10, 40),
        "fat_g": (3, 25),
        "carb_g": (10, 80),
        "sodium_mg": (0, 800),
        "fiber_g": (1, 10),
    }

    def __init__(self, source: NutritionRecordSource | None = None) -> None:
        self._source = source or MySQLArtifactRecordSource()
        self._features: dict[int, dict] = {}
        self._loaded = False

    def load(self, path=None) -> None:
        """从固定 nutrition_features Artifact 加载（path 兼容旧签名）。"""
        build_id = self._source.ready_build_id()
        rows = self._source.records("nutrition_features", build_id)
        for rec in rows:
            self._features[int(rec["recipe_id"])] = rec
        self._loaded = True

    def get_nutrition_profile(self, recipe_id: int) -> dict | None:
        return self._features.get(recipe_id)

    def score_recipe(self, recipe_id: int) -> NutritionScoreDecomposition:
        feature = self._features.get(recipe_id)
        if feature is None or feature.get("available") is not True:
            reason = (feature or {}).get("reason") or "missing_nutrition_feature"
            return NutritionScoreDecomposition(
                recipe_id=recipe_id,
                available=False,
                reason=reason,
                weighted_total=None,
            )

        # 聚合 references[*].nutrients[] 为菜品级维度值（对齐 RecipeNutritionFeatures）。
        nutrients: dict[str, float] = {}
        for ref in feature.get("references", []):
            for nutrient in ref.get("nutrients", []):
                code = nutrient.get("nutrient_code")
                if code:
                    nutrients[code] = nutrients.get(code, 0.0) + float(nutrient.get("value", 0.0))

        dim_scores: list[NutritionScore] = []
        for dim, (lo, hi) in self.DIMENSION_GOALS.items():
            val = nutrients.get(dim)
            if val is None:
                dim_scores.append(
                    NutritionScore(dimension=dim, raw_score=None, available=False, reason="missing_dimension")
                )
                continue
            if lo <= val <= hi:
                raw = 1.0
            elif val < lo:
                raw = max(0.0, val / lo)
            else:
                raw = max(0.0, hi / val)
            dim_scores.append(
                NutritionScore(dimension=dim, raw_score=round(raw, 4), available=True, reason=None)
            )

        available_dims = [s.raw_score for s in dim_scores if s.available]
        if not available_dims:
            return NutritionScoreDecomposition(
                recipe_id=recipe_id,
                available=False,
                reason="no_available_dimensions",
                weighted_total=None,
                dimension_scores=dim_scores,
            )
        confidence = feature.get("confidence", "high")
        coverage = float(feature.get("coverage_ratio", 1.0))
        discount = self.CONFIDENCE_DISCOUNTS.get(confidence, 1.0)
        # 加权总分 = 可用维度均值 × 覆盖 × 置信度折扣（doc 06 §9.4）。
        weighted = (sum(available_dims) / len(available_dims)) * coverage * discount
        return NutritionScoreDecomposition(
            recipe_id=recipe_id,
            available=True,
            reason=None,
            weighted_total=round(weighted, 4),
            goal_scores={"overall": round(weighted, 4)},
            dimension_scores=dim_scores,
            coverage_ratio=coverage,
            overall_confidence=confidence,
        )

    def score_candidates(
        self, recipe_ids: list[int], goal_weights: dict[str, float] | None = None
    ) -> dict[int, NutritionScoreDecomposition]:
        return {rid: self.score_recipe(rid) for rid in recipe_ids}

    def get_score_summary(self, recipe_id: int) -> str | None:
        result = self.score_recipe(recipe_id)
        if not result.available or result.weighted_total is None:
            return None
        level = "高" if result.weighted_total >= 0.7 else ("中" if result.weighted_total >= 0.4 else "低")
        return f"营养评分: {level}"


# 模块级单例
_service: NutritionScoringService | None = None


def get_nutrition_service() -> NutritionScoringService:
    global _service
    if _service is None:
        _service = NutritionScoringService()
        _service.load()
    return _service
