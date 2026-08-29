import csv
from collections import Counter
from pathlib import Path

import pytest

from food_agent_v2.b1.health_relation_builder import (
    CONSTRAINT_INGREDIENT_PATTERNS,
    _matching_patterns,
    generate_health_relation_candidates,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DECISIONS_PATH = REPO_ROOT / "data" / "review" / "health_relation_decisions.csv"
FDA_AUTHORITY = (
    "https://www.fda.gov/food/buy-store-serve-safe-food/"
    "food-allergies-what-you-need-know"
)

# Literal, owner-approved scope; this must not expand into substring matching.
APPROVED_EXACT_MATCHES = (
    ("allergy_tree_nut", 961, "榛果糖浆"),
    ("allergy_dairy", 2032, "婴儿奶粉"),
    ("allergy_shrimp", 423, "小青龙"),
    ("allergy_fish", 1578, "海参斑"),
    ("allergy_soy", 1216, "蒸鱼豆豉油"),
    ("allergy_soy", 1275, "厚百叶"),
    ("allergy_soy", 1519, "黑豆"),
    ("allergy_soy", 1608, "薄百叶"),
)

EXPECTED_REVIEW_ROWS = {
    ("allergy_tree_nut", 961): {
        "constraint_code": "allergy_tree_nut",
        "ingredient_id": "961",
        "decision": "hard_exclude",
        "evidence": "authority=https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know;matched_patterns=exact_name:榛果糖浆;review_policy=project_owner-batch-2026-08-28",
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-28",
    },
    ("allergy_dairy", 2032): {
        "constraint_code": "allergy_dairy",
        "ingredient_id": "2032",
        "decision": "hard_exclude",
        "evidence": "authority=https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know;matched_patterns=exact_name:婴儿奶粉;review_policy=project_owner-batch-2026-08-28",
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-28",
    },
    ("allergy_shrimp", 423): {
        "constraint_code": "allergy_shrimp",
        "ingredient_id": "423",
        "decision": "hard_exclude",
        "evidence": "authority=https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know;matched_patterns=exact_name:小青龙;review_policy=project_owner-batch-2026-08-28",
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-28",
    },
    ("allergy_fish", 1578): {
        "constraint_code": "allergy_fish",
        "ingredient_id": "1578",
        "decision": "hard_exclude",
        "evidence": "authority=https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know;matched_patterns=exact_name:海参斑;review_policy=project_owner-batch-2026-08-28",
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-28",
    },
    ("allergy_soy", 1216): {
        "constraint_code": "allergy_soy",
        "ingredient_id": "1216",
        "decision": "hard_exclude",
        "evidence": "authority=https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know;matched_patterns=exact_name:蒸鱼豆豉油;review_policy=project_owner-batch-2026-08-28",
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-28",
    },
    ("allergy_soy", 1275): {
        "constraint_code": "allergy_soy",
        "ingredient_id": "1275",
        "decision": "hard_exclude",
        "evidence": "authority=https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know;matched_patterns=exact_name:厚百叶;review_policy=project_owner-batch-2026-08-28",
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-28",
    },
    ("allergy_soy", 1519): {
        "constraint_code": "allergy_soy",
        "ingredient_id": "1519",
        "decision": "hard_exclude",
        "evidence": "authority=https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know;matched_patterns=exact_name:黑豆;review_policy=project_owner-batch-2026-08-28",
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-28",
    },
    ("allergy_soy", 1608): {
        "constraint_code": "allergy_soy",
        "ingredient_id": "1608",
        "decision": "hard_exclude",
        "evidence": "authority=https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know;matched_patterns=exact_name:薄百叶;review_policy=project_owner-batch-2026-08-28",
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-28",
    },
}

RETAINED_SOY_ROWS = {
    ("allergy_soy", 196): {
        "constraint_code": "allergy_soy",
        "ingredient_id": "196",
        "decision": "hard_exclude",
        "evidence": "authority=https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know;matched_patterns=豆腐;review_policy=project_owner-batch-2026-08-10",
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-10",
    },
    ("allergy_soy", 1638): {
        "constraint_code": "allergy_soy",
        "ingredient_id": "1638",
        "decision": "hard_exclude",
        "evidence": "authority=https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know;matched_patterns=豆腐;review_policy=project_owner-batch-2026-08-10",
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-10",
    },
}


def _read_decisions() -> list[dict[str, str]]:
    with DECISIONS_PATH.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _assert_expected_review_rows(rows: list[dict[str, str]]) -> None:
    by_key = {
        (row["constraint_code"], int(row["ingredient_id"])): row for row in rows
    }
    assert {
        key: by_key[key] for key in EXPECTED_REVIEW_ROWS
    } == EXPECTED_REVIEW_ROWS


def test_real_matcher_returns_each_new_exact_name_as_the_only_pattern() -> None:
    candidates = {
        (candidate.constraint_code, candidate.ingredient_id): candidate
        for candidate in generate_health_relation_candidates(
            tuple(dict.fromkeys(code for code, _, _ in APPROVED_EXACT_MATCHES)),
            tuple(
                {"ingredient_id": ingredient_id, "name_canonical": name}
                for _, ingredient_id, name in APPROVED_EXACT_MATCHES
            ),
        )
    }

    for constraint_code, ingredient_id, name in APPROVED_EXACT_MATCHES:
        candidate = candidates[(constraint_code, ingredient_id)]
        assert candidate.suggested_decision == "hard_exclude"
        assert candidate.suggested_evidence == (
            f"authority={FDA_AUTHORITY};matched_patterns=exact_name:{name}"
        )


def test_matcher_keeps_guard_before_exact_and_substring_matching() -> None:
    assert _matching_patterns(
        "allergy_fish", "鲍鱼", CONSTRAINT_INGREDIENT_PATTERNS["allergy_fish"]
    ) == ()


def test_csv_contains_the_eight_exact_approved_review_rows() -> None:
    _assert_expected_review_rows(_read_decisions())


def test_csv_guard_rejects_a_controlled_stale_row() -> None:
    stale_rows = [dict(row) for row in _read_decisions()]
    stale_row = next(
        row
        for row in stale_rows
        if (row["constraint_code"], int(row["ingredient_id"]))
        == ("allergy_soy", 1519)
    )
    stale_row["decision"] = "no_hard_relation"
    stale_row["evidence"] = f"authority={FDA_AUTHORITY};matched_patterns=none"

    with pytest.raises(AssertionError):
        _assert_expected_review_rows(stale_rows)


def test_csv_has_final_counts_unique_keys_and_unchanged_retained_soy_rows() -> None:
    rows = _read_decisions()
    keys = [(row["constraint_code"], int(row["ingredient_id"])) for row in rows]
    by_key = {(row["constraint_code"], int(row["ingredient_id"])): row for row in rows}

    assert len(rows) == 65_588
    assert len(keys) == len(set(keys)) == 65_588
    assert Counter(row["decision"] for row in rows) == {
        "hard_exclude": 1_084,
        "no_hard_relation": 64_504,
    }
    assert {key: by_key[key] for key in RETAINED_SOY_ROWS} == RETAINED_SOY_ROWS
