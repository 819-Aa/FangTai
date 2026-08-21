"""BM25、向量检索和最终候选共享的唯一过滤语义。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalFilters:
    meal_tags: tuple[str, ...] = ()
    population_tags: tuple[str, ...] = ()
    dish_type_tags: tuple[str, ...] = ()
    taste_tags: tuple[str, ...] = ()
    cuisine_tags: tuple[str, ...] = ()
    scenario_tags: tuple[str, ...] = ()
    include_ingredients: tuple[str, ...] = ()
    exclude_ingredients: tuple[str, ...] = ()

    def matches_payload(self, payload: dict) -> bool:
        must_fields = (
            ("meal_tags", self.meal_tags),
            ("population_tags", self.population_tags),
            ("dish_type_tags", self.dish_type_tags),
            ("taste_tags", self.taste_tags),
            ("cuisine_tags", self.cuisine_tags),
            ("scenario_tags", self.scenario_tags),
            ("ingredient_names", self.include_ingredients),
        )
        for field, required in must_fields:
            if required and not _overlaps(payload.get(field, ()), required):
                return False
        if self.exclude_ingredients and _overlaps(
            payload.get("ingredient_names", ()),
            self.exclude_ingredients,
        ):
            return False
        return True

    def to_qdrant_filter(self):
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        must = [
            FieldCondition(key=field, match=MatchAny(any=list(values)))
            for field, values in (
                ("meal_tags", self.meal_tags),
                ("population_tags", self.population_tags),
                ("dish_type_tags", self.dish_type_tags),
                ("taste_tags", self.taste_tags),
                ("cuisine_tags", self.cuisine_tags),
                ("scenario_tags", self.scenario_tags),
                ("ingredient_names", self.include_ingredients),
            )
            if values
        ]
        must_not = [
            FieldCondition(
                key="ingredient_names",
                match=MatchAny(any=list(self.exclude_ingredients)),
            )
        ] if self.exclude_ingredients else []
        return Filter(must=must, must_not=must_not)


def _overlaps(payload_values, required_values) -> bool:
    if isinstance(payload_values, str):
        observed = {payload_values}
    else:
        observed = {str(item) for item in (payload_values or ())}
    return bool(observed & {str(item) for item in required_values})
