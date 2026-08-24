"""按原始可食投料克重计算整菜九维营养。"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from food_agent_v2.b1.edible_fraction_review import (
    EdibleFractionDecisionIndex,
    resolve_edible_fraction,
)
from food_agent_v2.b1.nutrition_reference import (
    NUTRIENT_FIELDS,
    NutritionVector,
    nutrition_form_from_occurrence,
)
from food_agent_v2.b1.quantity_normalizer import normalize_quantity

NutritionUnavailableReason = Literal[
    "retention_unresolved",
    "usage_unresolved_for_fuzzy",
    "quantity_unapproved",
    "edible_fraction_missing",
    "mapping_missing",
    "nutrient_incomplete",
    "no_nutrition_ingredients",
    "no_retained_ingredients",
]


class RecipeNutritionFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_id: str
    source_manifest_hash: str
    recipe_id: int
    available: bool
    raw_edible_input_weight_g: Decimal | None
    raw_nutrition_total: NutritionVector | None
    raw_nutrition_per_100g: NutritionVector | None
    reason: NutritionUnavailableReason | None

    @model_validator(mode="after")
    def _all_or_nothing(self):
        values_present = (
            self.raw_edible_input_weight_g is not None
            and self.raw_nutrition_total is not None
            and self.raw_nutrition_per_100g is not None
        )
        if self.available != values_present:
            raise ValueError("营养可用性必须满足 all-or-nothing")
        if self.available and self.reason is not None:
            raise ValueError("可用营养不得带失败 reason")
        if not self.available and self.reason is None:
            raise ValueError("不可用营养必须带稳定 reason")
        return self


def calculate_raw_recipe_nutrition(
    view,
    *,
    measure_rules,
    quantity_decisions,
    edible_fractions,
    edible_fraction_decisions=None,
    nutrition_crosswalk,
) -> RecipeNutritionFeatures:
    if not view.ingredients:
        return _unavailable(view, "no_nutrition_ingredients")

    if any(
        occurrence.retention_requires_review or occurrence.retained_in_dish is None
        for occurrence in view.ingredients
    ):
        return _unavailable(view, "retention_unresolved")
    retained_occurrences = tuple(
        occurrence for occurrence in view.ingredients if occurrence.retained_in_dish
    )
    if not retained_occurrences:
        return _unavailable(view, "no_retained_ingredients")

    total_edible_g = Decimal("0")
    totals = {field: Decimal("0") for field in NUTRIENT_FIELDS}
    for occurrence in retained_occurrences:
        normalized = normalize_quantity(occurrence, measure_rules, quantity_decisions)
        if normalized.standardized_grams is None or normalized.requires_review:
            return _unavailable(view, normalized.reason or "quantity_unapproved")
        resolution = resolve_edible_fraction(
            occurrence,
            edible_fractions,
            edible_fraction_decisions or EdibleFractionDecisionIndex(()),
        )
        if resolution.edible_fraction is None or resolution.requires_review:
            return _unavailable(view, "edible_fraction_missing")
        reference = nutrition_crosswalk.get(
            occurrence.ingredient_id,
            nutrition_form_from_occurrence(occurrence.form),
        )
        if reference is None:
            return _unavailable(view, "mapping_missing")
        if not reference.per_100g.complete:
            return _unavailable(view, "nutrient_incomplete")

        edible_g = normalized.standardized_grams * resolution.edible_fraction
        total_edible_g += edible_g
        for field in NUTRIENT_FIELDS:
            nutrient = getattr(reference.per_100g, field)
            totals[field] += edible_g / Decimal("100") * nutrient

    if total_edible_g <= 0:
        return _unavailable(view, "quantity_unapproved")
    per_100g = {field: value / total_edible_g * Decimal("100") for field, value in totals.items()}
    return RecipeNutritionFeatures(
        build_id=str(view.build_id),
        source_manifest_hash=view.source_manifest_hash,
        recipe_id=view.recipe_id,
        available=True,
        raw_edible_input_weight_g=_round_2(total_edible_g),
        raw_nutrition_total=_round_vector(totals),
        raw_nutrition_per_100g=_round_vector(per_100g),
        reason=None,
    )


def _round_vector(values: dict[str, Decimal]) -> NutritionVector:
    return NutritionVector(**{field: _round_2(values[field]) for field in NUTRIENT_FIELDS})


def _round_2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _unavailable(view, reason: NutritionUnavailableReason) -> RecipeNutritionFeatures:
    return RecipeNutritionFeatures(
        build_id=str(view.build_id),
        source_manifest_hash=view.source_manifest_hash,
        recipe_id=view.recipe_id,
        available=False,
        raw_edible_input_weight_g=None,
        raw_nutrition_total=None,
        raw_nutrition_per_100g=None,
        reason=reason,
    )
