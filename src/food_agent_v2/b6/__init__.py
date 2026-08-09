"""B6 营养评分 —— 仅软排序，不参与健康硬筛选。

消费 B1 离线生成的 nutrition_profiles.jsonl，
提供分维度营养评分和加权总分。
营养值不进入 B4、不进入 D1 SSE 响应、不进入前端展示。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from food_agent_v2.core.paths import CLEANED_DIR


@dataclass
class NutritionScore:
    """单个营养维度的评分。"""
    dimension: str              # energy | protein | fat | sodium | carb | fiber
    raw_score: float            # [0, 1]，标准化后的分维度分
    confidence: str             # high | medium | low
    confidence_discount: float  # 置信度折扣因子


@dataclass
class NutritionScoreDecomposition:
    """营养评分分解。"""
    recipe_id: int
    goal_scores: dict[str, float]           # goal_code → score [0, 1]
    weighted_total: float                    # 加权总分 [0, 1]
    dimension_scores: list[NutritionScore] = field(default_factory=list)
    coverage_ratio: float = 1.0
    overall_confidence: str = "medium"


class NutritionScoringService:
    """营养评分服务。

    只做软排序——低置信度营养估算可以打折但不能作为排除理由。
    评分不进入 B4 输入、回答正文、SSE 事件或前端展示。
    """

    # 置信度折扣
    CONFIDENCE_DISCOUNTS = {"high": 1.0, "medium": 0.7, "low": 0.3}

    # 各维度的理想目标范围（基于通用膳食指南）
    # 评分方式：实际值在目标范围内 → 1.0，偏离则递减
    DIMENSION_GOALS = {
        "energy_kcal": (300, 800),          # 每道菜能量范围 (kcal)
        "protein_g": (10, 40),              # 蛋白质 (g)
        "fat_g": (3, 25),                   # 脂肪 (g)
        "carb_g": (10, 80),                 # 碳水 (g)
        "sodium_mg": (0, 800),              # 钠 (mg)
        "fiber_g": (1, 10),                 # 膳食纤维 (g)
    }

    def __init__(self):
        self._profiles: dict[int, dict] = {}
        self._loaded = False

    def load(self, path: Optional[Path] = None) -> None:
        if path is None:
            path = CLEANED_DIR / "nutrition_profiles.jsonl"
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                self._profiles[rec["recipe_id"]] = rec
        self._loaded = True

    def get_nutrition_profile(self, recipe_id: int) -> dict | None:
        return self._profiles.get(recipe_id)

    def score_recipe(self, recipe_id: int) -> NutritionScoreDecomposition:
        """对单道菜进行营养评分。"""
        profile = self._profiles.get(recipe_id, {})
        nutrients = profile.get("nutrient_values", {})
        confidence = profile.get("confidence", "low")
        coverage = profile.get("coverage_ratio", 0.0)

        discount = self.CONFIDENCE_DISCOUNTS.get(confidence, 0.3)
        dim_scores: list[NutritionScore] = []

        for dim, (lo, hi) in self.DIMENSION_GOALS.items():
            val = nutrients.get(dim)
            if val is None:
                dim_scores.append(NutritionScore(
                    dimension=dim, raw_score=0.5, confidence="low",
                    confidence_discount=0.3,
                ))
                continue

            # 计分：值在理想范围 → 1.0，偏离则按比例递减到 0
            if lo <= val <= hi:
                raw = 1.0
            elif val < lo:
                raw = max(0.0, val / lo)
            else:
                raw = max(0.0, hi / val)

            dim_scores.append(NutritionScore(
                dimension=dim, raw_score=round(raw, 4),
                confidence=confidence,
                confidence_discount=discount,
            ))

        # 维度均分 × 覆盖折扣 × 置信度折扣
        avg_raw = sum(s.raw_score for s in dim_scores) / max(len(dim_scores), 1)
        weighted = avg_raw * coverage * discount

        return NutritionScoreDecomposition(
            recipe_id=recipe_id,
            goal_scores={"overall": round(weighted, 4)},
            weighted_total=round(weighted, 4),
            dimension_scores=dim_scores,
            coverage_ratio=coverage,
            overall_confidence=confidence,
        )

    def score_candidates(
        self, recipe_ids: list[int], goal_weights: dict[str, float] | None = None
    ) -> dict[int, NutritionScoreDecomposition]:
        """批量评分。"""
        results = {}
        for rid in recipe_ids:
            results[rid] = self.score_recipe(rid)
        return results

    def get_score_summary(self, recipe_id: int) -> str | None:
        """公开评分摘要——不包含营养数值，仅供 C2 内部使用。"""
        result = self.score_recipe(recipe_id)
        if result.overall_confidence == "low":
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
