"""从审阅后的克重、可食部和营养映射构建菜品原始投料营养。"""

from __future__ import annotations

from collections import Counter

from food_agent_v2.b1.nutrition_calculator import calculate_raw_recipe_nutrition


def build_nutrition_features_from_views(
    views,
    *,
    measure_rules,
    quantity_decisions,
    edible_fractions,
    edible_fraction_decisions=None,
    nutrition_crosswalk,
):
    features = [
        calculate_raw_recipe_nutrition(
            view,
            measure_rules=measure_rules,
            quantity_decisions=quantity_decisions,
            edible_fractions=edible_fractions,
            edible_fraction_decisions=edible_fraction_decisions,
            nutrition_crosswalk=nutrition_crosswalk,
        )
        for view in views
    ]
    available_count = sum(item.available for item in features)
    reason_counts = Counter(
        item.reason for item in features if not item.available and item.reason is not None
    )
    return features, {
        "stage": "raw_recipe_nutrition",
        "status": "passed",
        "total_recipes": len(features),
        "available_count": available_count,
        "unavailable_count": len(features) - available_count,
        "reason_counts": dict(sorted(reason_counts.items())),
    }
