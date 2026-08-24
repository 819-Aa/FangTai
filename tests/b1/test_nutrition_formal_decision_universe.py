from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

from food_agent_v2.b1 import nutrition_occurrence_rules
from food_agent_v2.b1.consumer_views import (
    BuildIdentity,
    IngredientIdentityFact,
    IngredientOccurrenceFact,
    RecipeFact,
    build_consumer_views,
)
from food_agent_v2.b1.nutrition_occurrence_rules import (
    load_nutrition_retention_decisions,
    load_nutrition_usage_decisions,
)

BUILD = BuildIdentity(UUID(int=1), "a" * 64)


@dataclass(frozen=True)
class _CurrentOccurrence:
    occurrence_id: str
    recipe_id: int
    ingredient_id: int
    ingredient_name: str
    normalized_form: str


@pytest.mark.parametrize(
    ("loader", "header", "row"),
    [
        (
            load_nutrition_usage_decisions,
            "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,usage_code,review_status",
            "1-1,2,10,盐,碎,seasoning,pending",
        ),
        (
            load_nutrition_usage_decisions,
            "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,usage_code,review_status",
            "1-1,1,11,盐,碎,seasoning,rejected",
        ),
        (
            load_nutrition_retention_decisions,
            "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,retained_in_dish,review_status",
            "1-1,1,10,糖,碎,true,pending",
        ),
        (
            load_nutrition_retention_decisions,
            "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,retained_in_dish,review_status",
            "1-1,1,10,盐,末,true,rejected",
        ),
    ],
)
def test_all_formal_rows_reject_stale_authoritative_metadata(
    loader, header: str, row: str, tmp_path: Path
) -> None:
    path = tmp_path / "decisions.csv"
    path.write_text(f"{header}\n{row}\n", encoding="utf-8")
    current = {
        "1-1": _CurrentOccurrence("1-1", 1, 10, "盐", "碎"),
    }

    with pytest.raises(ValueError, match="元数据不一致"):
        loader(path, current_occurrences=current)


def _final_nutrition_universe(
    *, target: IngredientOccurrenceFact, record_type: str = "dish"
):
    occurrences = (target,)
    identities = (
        IngredientIdentityFact(
            int(target.ingredient_id), target.name_clean, int(target.ingredient_id)
        ),
    )
    if record_type == "dish":
        occurrences = (
            IngredientOccurrenceFact("1-1", 1, "豆腐", "豆腐", 10, "edible"),
            target,
        )
        identities = (
            IngredientIdentityFact(10, "豆腐", 10),
            *identities,
        )
    views = build_consumer_views(
        build=BUILD,
        recipes=(RecipeFact(1, "测试菜", record_type, ("处理食材",)),),
        occurrences=occurrences,
        identities=identities,
    )
    metadata_from_views = (
        nutrition_occurrence_rules.nutrition_occurrence_metadata_from_views
    )
    return metadata_from_views(views.nutrition_views)


@pytest.mark.parametrize(
    ("target", "record_type"),
    [
        (
            IngredientOccurrenceFact(
                "1-2",
                1,
                "香菜可选",
                "香菜",
                20,
                "edible",
                condition_type="optional",
                selected_for_base=False,
            ),
            "dish",
        ),
        (
            IngredientOccurrenceFact(
                "1-2",
                1,
                "腌料",
                "腌料",
                20,
                "edible",
                is_process_material=True,
            ),
            "dish",
        ),
        (
            IngredientOccurrenceFact(
                "1-2",
                1,
                "牛腩",
                "牛腩",
                20,
                "edible",
                condition_type="one_of",
                choice_group_id="meat",
                selected_for_base=False,
            ),
            "dish",
        ),
        (
            IngredientOccurrenceFact("1-2", 1, "水", "水", 20, "edible"),
            "cooking_program",
        ),
    ],
    ids=("optional", "process-material", "unselected", "ineligible-recipe"),
)
@pytest.mark.parametrize(
    ("loader", "header", "tail"),
    [
        (
            load_nutrition_usage_decisions,
            "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,usage_code,review_status",
            "main,pending",
        ),
        (
            load_nutrition_retention_decisions,
            "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,retained_in_dish,review_status",
            "true,rejected",
        ),
    ],
    ids=("usage", "retention"),
)
def test_formal_rows_reject_occurrences_outside_final_nutrition_input_universe(
    loader,
    header: str,
    tail: str,
    target: IngredientOccurrenceFact,
    record_type: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "decisions.csv"
    path.write_text(
        f"{header}\n{target.occurrence_id},1,20,{target.name_clean},,{tail}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="未知 occurrence_id"):
        loader(
            path,
            current_occurrences=_final_nutrition_universe(
                target=target, record_type=record_type
            ),
        )
