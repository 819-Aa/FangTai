from __future__ import annotations

import csv
import json
from pathlib import Path

from food_agent_v2.b1.review_audit import build_review_coverage_summary


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_review_audit_reports_failures_without_approving_candidates(tmp_path) -> None:
    profiles = tmp_path / "profiles.jsonl"
    quantities = tmp_path / "quantities.csv"
    nutrition = tmp_path / "nutrition.csv"
    references = tmp_path / "references.jsonl"
    food_origins = tmp_path / "food_origins.csv"
    usda_candidates = tmp_path / "usda_candidates.csv"
    time_conflicts = tmp_path / "time.jsonl"
    _write_jsonl(
        profiles,
        [
            {"recipe_id": 1, "population_tags": ["老人"], "review_status": "pending"},
            {"recipe_id": 2, "population_tags": [], "review_status": "pending"},
        ],
    )
    _write_csv(
        food_origins,
        (
            "reference_id",
            "canonical_name",
            "form",
            "candidate_food_origin",
            "review_status",
        ),
        [
            {
                "reference_id": "ref-1",
                "canonical_name": "梨",
                "form": "unspecified",
                "candidate_food_origin": "plant",
                "review_status": "pending",
            },
            {
                "reference_id": "usda-1",
                "canonical_name": "Salt, table",
                "form": "unspecified",
                "candidate_food_origin": "mixed",
                "review_status": "pending",
            },
        ],
    )
    _write_csv(
        usda_candidates,
        (
            "ingredient_id",
            "ingredient_name",
            "form",
            "candidate_reference_id",
            "candidate_name",
            "candidate_form",
            "source_dataset",
            "match_method",
            "candidate_rank",
            "reason",
            "review_status",
        ),
        [
            {
                "ingredient_id": 20,
                "ingredient_name": "盐",
                "form": "unspecified",
                "candidate_reference_id": "usda-1",
                "candidate_name": "Salt, table",
                "candidate_form": "unspecified",
                "source_dataset": "usda_sr_legacy",
                "match_method": "multilingual_embedding_model_review",
                "candidate_rank": 1,
                "reason": "usda_identity_and_form_requires_owner_review",
                "review_status": "pending",
            }
        ],
    )
    quantity_fields = (
        "occurrence_id",
        "recipe_id",
        "candidate_grams",
        "decision_grams",
        "review_status",
    )
    _write_csv(
        quantities,
        quantity_fields,
        [
            {"occurrence_id": "1-1", "recipe_id": 1, "candidate_grams": "6001", "decision_grams": "", "review_status": "pending"},
            {"occurrence_id": "1-1", "recipe_id": 1, "candidate_grams": "10", "decision_grams": "", "review_status": "pending"},
            {"occurrence_id": "2-1", "recipe_id": 2, "candidate_grams": "0", "decision_grams": "", "review_status": "pending"},
        ],
    )
    nutrition_fields = (
        "ingredient_id",
        "ingredient_name",
        "form",
        "candidate_reference_id",
        "review_status",
    )
    _write_csv(
        nutrition,
        nutrition_fields,
        [
            {"ingredient_id": 10, "ingredient_name": "梨", "form": "unspecified", "candidate_reference_id": "ref-1", "review_status": "pending"},
            {"ingredient_id": 10, "ingredient_name": "梨", "form": "unspecified", "candidate_reference_id": "ref-2", "review_status": "pending"},
            {"ingredient_id": 20, "ingredient_name": "盐", "form": "unspecified", "candidate_reference_id": "", "review_status": "pending"},
        ],
    )
    _write_jsonl(
        references,
        [
            {
                "reference_id": "ref-1",
                "canonical_name": "梨",
                "form": "unspecified",
                "food_origin": "plant",
                "source_name": "authority",
                "source_url": "https://example.test/1",
                "source_dataset": "china_cdc",
                "per_100g": {
                    "energy_kcal": "50",
                    "protein_g": "1",
                    "fat_g": "0",
                    "carbohydrate_g": "10",
                    "fiber_g": None,
                    "sodium_mg": "1",
                    "calcium_mg": "2",
                    "iron_mg": "0.1",
                    "cholesterol_mg": None,
                },
            },
            {
                "reference_id": "ref-2",
                "canonical_name": "梨二",
                "form": "unspecified",
                "food_origin": "unknown",
                "source_name": "authority",
                "source_url": "",
                "source_dataset": "china_cdc",
                "per_100g": {},
            },
        ],
    )
    _write_jsonl(
        time_conflicts,
        [{"recipe_id": 3, "issues": [{"code": "DEPENDENCY_CYCLE", "atom_ids": ["a"]}]}],
    )

    summary = build_review_coverage_summary(
        profile_candidates=profiles,
        quantity_candidates=quantities,
        nutrition_candidates=nutrition,
        nutrition_references=references,
        food_origin_candidates=food_origins,
        usda_nutrition_candidates=usda_candidates,
        time_conflicts=time_conflicts,
        eligible_recipe_ids={1, 2, 3},
        raw_population_tags={1: set(), 2: set()},
        occurrence_rows=(
            {"recipe_id": 1, "occurrence_id": "1-1", "unit_raw": "℃", "condition_type": "one_of", "choice_group_id": "g", "selected_for_base": True},
            {"recipe_id": 1, "occurrence_id": "1-2", "unit_raw": "克", "condition_type": "one_of", "choice_group_id": "g", "selected_for_base": True},
        ),
    )

    assert summary["profiles"]["status_counts"] == {"pending": 2}
    assert summary["profiles"]["sensitive_population_tags_not_in_source"] == 1
    assert summary["quantities"]["duplicate_occurrence_ids"] == 1
    assert summary["quantities"]["non_positive_candidates"] == 1
    assert summary["quantities"]["over_5000g_candidates"] == 1
    assert summary["quantities"]["unknown_unit_occurrences"] == 1
    assert summary["conditions"]["one_of_multiple_defaults"] == 1
    assert summary["nutrition_crosswalk"]["non_unique_candidate_keys"] == 1
    assert summary["nutrition_references"]["missing_source_url"] == 1
    assert summary["nutrition_references"]["complete_nine_dimension"] == 0
    assert summary["nutrition_references"]["structural_zero_candidates"] == 1
    assert summary["food_origins"]["candidate_rows"] == 2
    assert summary["food_origins"]["selected_reference_without_origin_candidate"] == 1
    assert summary["usda_fallback"]["selected_candidates"] == 1
    assert summary["usda_fallback"]["combined_unresolved_keys"] == 0
    assert summary["time_graphs"]["ready_recipes"] == 2
    assert summary["time_graphs"]["conflict_codes"] == {"DEPENDENCY_CYCLE": 1}
    assert summary["automatic_approvals"] == 0
