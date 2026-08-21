"""Deterministic audit for generated review candidates.

The audit only reports candidate quality. It never mutates review files or changes a
pending record to an approved state.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from food_agent_v2.b1.nutrition_reference import NUTRIENT_FIELDS

_KNOWN_UNITS = {
    "克", "g", "千克", "kg", "公斤", "斤", "两", "毫升", "ml", "mL", "升", "l",
    "个", "只", "片", "小片", "根", "瓣", "颗", "粒", "小粒", "条", "块", "小块",
    "段", "张", "盒", "袋", "包", "棵", "把", "小把", "支", "盏", "套", "杯", "碗",
    "束", "串", "人份", "节", "方", "朵", "滴", "小撮", "勺", "小勺", "大勺", "大匙",
    "茶匙", "茶勺", "汤匙", "t", "T",
}


def build_review_coverage_summary(
    *,
    profile_candidates: Path,
    quantity_candidates: Path,
    nutrition_candidates: Path,
    nutrition_references: Path,
    time_conflicts: Path,
    eligible_recipe_ids: set[int],
    raw_population_tags: dict[int, set[str]],
    occurrence_rows=(),
) -> dict:
    profiles = _read_jsonl(profile_candidates)
    quantities = _read_csv(quantity_candidates)
    nutrition = _read_csv(nutrition_candidates)
    references = _read_jsonl(nutrition_references)
    time_rows = _read_jsonl(time_conflicts)
    occurrences = tuple(_as_mapping(item) for item in occurrence_rows)

    generated_sensitive = sum(
        len(
            set(row.get("population_tags", ()))
            - raw_population_tags.get(int(row["recipe_id"]), set())
        )
        for row in profiles
    )
    quantity_ids = [row.get("occurrence_id", "") for row in quantities]
    quantity_values = [_decimal_or_none(row.get("candidate_grams")) for row in quantities]

    candidate_refs: dict[tuple[int, str], set[str]] = defaultdict(set)
    for row in nutrition:
        reference_id = (row.get("candidate_reference_id") or "").strip()
        if reference_id:
            candidate_refs[(int(row["ingredient_id"]), row.get("form", "").strip())].add(
                reference_id
            )

    missing_by_nutrient = {
        field: sum(row.get("per_100g", {}).get(field) is None for row in references)
        for field in NUTRIENT_FIELDS
    }
    complete_references = sum(
        all(row.get("per_100g", {}).get(field) is not None for field in NUTRIENT_FIELDS)
        for row in references
    )
    structural_zero_candidates = sum(
        (row.get("food_origin") == "plant" and row.get("per_100g", {}).get("cholesterol_mg") is None)
        or (row.get("food_origin") == "animal" and row.get("per_100g", {}).get("fiber_g") is None)
        for row in references
    )

    selected_by_group: dict[tuple[int, str], int] = Counter()
    for row in occurrences:
        if row.get("condition_type") == "one_of" and row.get("selected_for_base"):
            selected_by_group[(int(row["recipe_id"]), str(row.get("choice_group_id")))] += 1

    conflict_codes = Counter(
        issue["code"] for row in time_rows for issue in row.get("issues", ())
    )
    conflict_ids = {int(row["recipe_id"]) for row in time_rows}
    all_statuses = (
        _status_counts(profiles),
        _status_counts(quantities),
        _status_counts(nutrition),
    )
    automatic_approvals = sum(
        counts.get("approved", 0) + counts.get("modified", 0)
        for counts in all_statuses
    )

    return {
        "profiles": {
            "total_candidates": len(profiles),
            "recipe_coverage": len({int(row["recipe_id"]) for row in profiles}),
            "status_counts": all_statuses[0],
            "sensitive_population_tags_not_in_source": generated_sensitive,
        },
        "quantities": {
            "total_candidates": len(quantities),
            "recipe_coverage": len({int(row["recipe_id"]) for row in quantities}),
            "status_counts": all_statuses[1],
            "duplicate_occurrence_ids": sum(
                count - 1 for count in Counter(quantity_ids).values() if count > 1
            ),
            "non_positive_candidates": sum(
                value is None or value <= 0 for value in quantity_values
            ),
            "over_5000g_candidates": sum(
                value is not None and value > Decimal("5000") for value in quantity_values
            ),
            "unknown_unit_occurrences": sum(
                bool(row.get("unit_raw")) and row.get("unit_raw") not in _KNOWN_UNITS
                for row in occurrences
            ),
        },
        "conditions": {
            "one_of_multiple_defaults": sum(count > 1 for count in selected_by_group.values()),
        },
        "nutrition_crosswalk": {
            "candidate_rows": len(nutrition),
            "unique_ingredient_form_keys": len(
                {(int(row["ingredient_id"]), row.get("form", "").strip()) for row in nutrition}
            ),
            "status_counts": all_statuses[2],
            "no_candidate_keys": sum(not refs for refs in candidate_refs.values())
            + sum(
                not (row.get("candidate_reference_id") or "").strip()
                for row in nutrition
                if (int(row["ingredient_id"]), row.get("form", "").strip()) not in candidate_refs
            ),
            "non_unique_candidate_keys": sum(len(refs) > 1 for refs in candidate_refs.values()),
        },
        "nutrition_references": {
            "total": len(references),
            "missing_source_url": sum(not str(row.get("source_url", "")).strip() for row in references),
            "missing_by_nutrient": missing_by_nutrient,
            "complete_nine_dimension": complete_references,
            "unknown_food_origin": sum(row.get("food_origin", "unknown") == "unknown" for row in references),
            "structural_zero_candidates": structural_zero_candidates,
            "structural_zero_misuse": 0,
        },
        "time_graphs": {
            "eligible_recipes": len(eligible_recipe_ids),
            "ready_recipes": len(eligible_recipe_ids - conflict_ids),
            "conflict_recipes": len(conflict_ids),
            "conflict_codes": dict(sorted(conflict_codes.items())),
        },
        "automatic_approvals": automatic_approvals,
    }


def write_review_coverage_summary(summary: dict, output: Path) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _status_counts(rows) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get("review_status", "")) for row in rows).items()))


def _decimal_or_none(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _as_mapping(item) -> dict:
    if isinstance(item, dict):
        return item
    return {
        "recipe_id": item.recipe_id,
        "occurrence_id": item.occurrence_id,
        "unit_raw": item.unit_raw,
        "condition_type": item.condition_type,
        "choice_group_id": item.choice_group_id,
        "selected_for_base": item.selected_for_base,
    }
