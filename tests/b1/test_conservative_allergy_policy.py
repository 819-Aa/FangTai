import csv
from collections import Counter
from pathlib import Path

import pytest

from food_agent_v2.b1.health_relation_builder import generate_health_relation_candidates

REPO_ROOT = Path(__file__).resolve().parents[2]
DECISIONS_PATH = REPO_ROOT / "data" / "review" / "health_relation_decisions.csv"
BEFORE_DECISIONS_PATH = (
    REPO_ROOT
    / ".superpowers"
    / "sdd"
    / "2026-08-28-conservative-composite-allergy-policy"
    / "task-1-before"
    / "data__review__health_relation_decisions.csv"
)
FDA_AUTHORITY = (
    "https://www.fda.gov/food/buy-store-serve-safe-food/"
    "food-allergies-what-you-need-know"
)
CONSERVATIVE_POLICY = "project_owner-conservative-2026-08-28"

# Literal owner-approved matrix cells. Any omitted entry or broader matching is a bug.
APPROVED_RELATIONS = (
    ("allergy_dairy", 1599, "液态酥油"),
    ("allergy_dairy", 1697, "片状酥油"),
    ("allergy_soy", 181, "豆瓣酱"),
    ("allergy_soy", 753, "红油豆瓣酱"),
    ("allergy_soy", 903, "辣豆瓣酱"),
    ("allergy_soy", 1854, "六月鲜豆瓣酱"),
    ("allergy_soy", 1865, "大豆油"),
    ("allergy_wheat", 204, "甜面酱"),
    ("allergy_wheat", 351, "蛋挞皮"),
    ("allergy_wheat", 1569, "蛋挞胚"),
    ("allergy_wheat", 553, "郫县豆瓣"),
    ("allergy_wheat", 710, "郫县豆瓣酱"),
)

# Literal preservation boundaries: candidates must not infer a relation beyond approval.
PRESERVED_NO_HARD_CASES = (
    ("allergy_tree_nut", 1068, "白果仁"),
    ("allergy_tree_nut", 1253, "白果"),
    ("allergy_egg", 351, "蛋挞皮"),
    ("allergy_egg", 1569, "蛋挞胚"),
    ("allergy_soy", 553, "郫县豆瓣"),
    ("allergy_soy", 710, "郫县豆瓣酱"),
    ("allergy_soy", 1768, "青豆瓣"),
    ("allergy_soy", 1853, "蚕豆瓣"),
    ("allergy_egg", 99_101, "蛋挞"),
)

PRESERVED_CRAB_ROWS = {
    ("allergy_crab", 459): {
        "constraint_code": "allergy_crab",
        "ingredient_id": "459",
        "decision": "hard_exclude",
        "evidence": (
            f"authority={FDA_AUTHORITY};matched_patterns=蟹|蟹肉|蟹肉棒;"
            "review_policy=project_owner-batch-2026-08-10"
        ),
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-10",
    },
    ("allergy_crab", 1905): {
        "constraint_code": "allergy_crab",
        "ingredient_id": "1905",
        "decision": "hard_exclude",
        "evidence": (
            f"authority={FDA_AUTHORITY};matched_patterns=蟹|蟹柳;"
            "review_policy=project_owner-batch-2026-08-10"
        ),
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-10",
    },
}


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _approved_row(code: str, ingredient_id: int, name: str) -> dict[str, str]:
    return {
        "constraint_code": code,
        "ingredient_id": str(ingredient_id),
        "decision": "hard_exclude",
        "evidence": (
            f"authority={FDA_AUTHORITY};matched_patterns=exact_name:{name};"
            f"review_policy={CONSERVATIVE_POLICY}"
        ),
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-28",
    }


def _assert_approved_rows(rows: list[dict[str, str]]) -> None:
    by_key = {(row["constraint_code"], int(row["ingredient_id"])): row for row in rows}
    expected = {
        (code, ingredient_id): _approved_row(code, ingredient_id, name)
        for code, ingredient_id, name in APPROVED_RELATIONS
    }
    assert {key: by_key[key] for key in expected} == expected


def test_real_candidate_generator_returns_only_each_conservative_exact_name() -> None:
    ingredients = tuple(
        {"ingredient_id": ingredient_id, "name_canonical": name}
        for _, ingredient_id, name in APPROVED_RELATIONS
    )
    candidates = {
        (candidate.constraint_code, candidate.ingredient_id): candidate
        for candidate in generate_health_relation_candidates(
            ("allergy_dairy", "allergy_soy", "allergy_wheat", "allergy_tree_nut", "allergy_egg"),
            ingredients,
        )
    }

    for code, ingredient_id, name in APPROVED_RELATIONS:
        candidate = candidates[(code, ingredient_id)]
        assert candidate.suggested_decision == "hard_exclude"
        assert candidate.suggested_evidence == (
            f"authority={FDA_AUTHORITY};matched_patterns=exact_name:{name}"
        )


@pytest.mark.parametrize(("constraint_code", "ingredient_id", "ingredient_name"), PRESERVED_NO_HARD_CASES)
def test_real_candidate_generator_preserves_closed_exact_name_boundaries(
    constraint_code: str, ingredient_id: int, ingredient_name: str
) -> None:
    candidate = generate_health_relation_candidates(
        (constraint_code,),
        ({"ingredient_id": ingredient_id, "name_canonical": ingredient_name},),
    )[0]

    assert candidate.suggested_decision == "no_hard_relation"
    assert candidate.suggested_evidence == f"authority={FDA_AUTHORITY};matched_patterns=none"


def test_active_csv_has_the_twelve_approved_rows_and_preserves_crab_rows() -> None:
    rows = _read_rows(DECISIONS_PATH)
    _assert_approved_rows(rows)
    by_key = {(row["constraint_code"], int(row["ingredient_id"])): row for row in rows}
    assert {key: by_key[key] for key in PRESERVED_CRAB_ROWS} == PRESERVED_CRAB_ROWS


def test_conservative_csv_check_rejects_a_controlled_stale_row() -> None:
    rows = [dict(row) for row in _read_rows(DECISIONS_PATH)]
    stale_row = next(
        row
        for row in rows
        if (row["constraint_code"], int(row["ingredient_id"])) == ("allergy_soy", 181)
    )
    stale_row["decision"] = "no_hard_relation"
    stale_row["evidence"] = f"authority={FDA_AUTHORITY};matched_patterns=none"

    with pytest.raises(AssertionError):
        _assert_approved_rows(rows)


def test_active_csv_changes_only_the_twelve_approved_keys_and_has_final_totals() -> None:
    rows = _read_rows(DECISIONS_PATH)
    before_rows = _read_rows(BEFORE_DECISIONS_PATH)
    keys = [(row["constraint_code"], int(row["ingredient_id"])) for row in rows]
    by_key = {(row["constraint_code"], int(row["ingredient_id"])): row for row in rows}
    before_by_key = {
        (row["constraint_code"], int(row["ingredient_id"])): row for row in before_rows
    }
    changed_keys = {key for key in by_key if by_key[key] != before_by_key[key]}
    approved_keys = {(code, ingredient_id) for code, ingredient_id, _ in APPROVED_RELATIONS}

    assert len(rows) == 65_588
    assert len(keys) == len(set(keys)) == 65_588
    assert Counter(row["decision"] for row in rows) == {
        "hard_exclude": 1_084,
        "no_hard_relation": 64_504,
    }
    assert changed_keys == approved_keys
    assert len(changed_keys) == 12
