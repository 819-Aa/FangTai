"""Repository review-input regression coverage for the sesame-oil identity repair."""

import csv
import json
from pathlib import Path

import pytest

from food_agent_v2.core.paths import PROJECT_ROOT

HEALTH_DECISIONS = PROJECT_ROOT / "data" / "review" / "health_relation_decisions.csv"
NUTRITION_CROSSWALK = (
    PROJECT_ROOT / "data" / "review" / "ingredient_nutrition_crosswalk.jsonl"
)
RETENTION_DECISIONS = (
    PROJECT_ROOT / "data" / "review" / "ingredient_nutrition_retention_decisions.csv"
)
EDIBLE_FRACTION_DECISIONS = (
    PROJECT_ROOT / "data" / "review" / "ingredient_edible_fraction_decisions.csv"
)
QUANTITY_DECISIONS = (
    PROJECT_ROOT / "data" / "review" / "ingredient_quantity_decisions.csv"
)


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _jsonl_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _assert_active_sesame_review_inputs(
    health_rows: list[dict[str, str]], nutrition_rows: list[dict]
) -> None:
    assert not any(row["ingredient_id"] == "612" for row in health_rows), (
        "retired ID 612 must not remain in active health decisions"
    )
    sesame_health = [
        row
        for row in health_rows
        if row["ingredient_id"] == "66" and row["constraint_code"] == "allergy_sesame"
    ]
    assert [(row["decision"], row["review_status"]) for row in sesame_health] == [
        ("hard_exclude", "approved")
    ]
    assert not any(
        row["ingredient_id"] == "66"
        and row["constraint_code"] in {"allergy_fish", "allergy_seafood"}
        and row["decision"] == "hard_exclude"
        for row in health_rows
    )

    assert not any(row["ingredient_id"] == 612 for row in nutrition_rows), (
        "retired ID 612 must not remain in the active nutrition crosswalk"
    )
    sesame_nutrition = [
        row
        for row in nutrition_rows
        if row["ingredient_id"] == 66 and row["form"] == "unspecified"
    ]
    assert sesame_nutrition == [
        {
            "ingredient_id": 66,
            "ingredient_name": "芝麻油",
            "form": "unspecified",
            "reference_id": "usda-fdc-171016",
            "match_method": "approved_alias",
            "review_status": "approved",
            "brand_name": None,
            "specification": None,
        }
    ]


def test_active_review_inputs_keep_sesame_oil_and_retire_芝麻鱼() -> None:
    """A stale ID or surimi remap in active review data must fail this test."""
    _assert_active_sesame_review_inputs(
        _csv_rows(HEALTH_DECISIONS), _jsonl_rows(NUTRITION_CROSSWALK)
    )


def test_active_occurrence_review_inputs_rebind_215_11_once_to_芝麻油() -> None:
    """The three approved occurrence-review files must agree on the repaired identity."""
    retention = [row for row in _csv_rows(RETENTION_DECISIONS) if row["occurrence_id"] == "215-11"]
    edible_fraction = [
        row for row in _csv_rows(EDIBLE_FRACTION_DECISIONS) if row["occurrence_id"] == "215-11"
    ]
    quantity = [row for row in _csv_rows(QUANTITY_DECISIONS) if row["occurrence_id"] == "215-11"]

    assert retention == [
        {
            "occurrence_id": "215-11",
            "recipe_id": "215",
            "ingredient_id": "66",
            "ingredient_name": "芝麻油",
            "normalized_form": "",
            "retained_in_dish": "true",
            "review_status": "approved",
        }
    ]
    assert edible_fraction == [
        {
            "occurrence_id": "215-11",
            "recipe_id": "215",
            "ingredient_id": "66",
            "ingredient_name": "芝麻油",
            "decision_form": "按可食部估重",
            "edible_fraction": "1.0",
            "review_status": "approved",
        }
    ]
    assert len(quantity) == 1
    assert quantity[0]["ingredient_name"] == "芝麻油"
    assert quantity[0]["raw_quantity"] == "4毫升"
    assert quantity[0]["review_status"] == "approved"


def test_sesame_review_input_guard_rejects_controlled_stale_rows() -> None:
    """Mutation proof: the repository-data guard rejects both retired-ID and surimi states."""
    health_rows = _csv_rows(HEALTH_DECISIONS)
    nutrition_rows = _jsonl_rows(NUTRITION_CROSSWALK)

    with pytest.raises(AssertionError, match="retired ID 612"):
        _assert_active_sesame_review_inputs(
            [
                *health_rows,
                {
                    "constraint_code": "allergy_fish",
                    "ingredient_id": "612",
                    "decision": "hard_exclude",
                    "review_status": "approved",
                },
            ],
            nutrition_rows,
        )

    stale_nutrition = [
        {
            **row,
            "reference_id": "usda-fdc-173702",
        }
        if row["ingredient_id"] == 66 and row["form"] == "unspecified"
        else row
        for row in nutrition_rows
    ]
    with pytest.raises(AssertionError):
        _assert_active_sesame_review_inputs(health_rows, stale_nutrition)
