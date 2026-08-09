import csv

import pytest

from food_agent_v2.b1.health_relation_builder import generate_health_relation_candidates
from food_agent_v2.b1.health_relation_review import (
    HealthRelationReviewError,
    load_approved_health_relation_decisions,
)

FIELDS = (
    "constraint_code",
    "ingredient_id",
    "decision",
    "evidence",
    "review_status",
    "reviewer",
    "reviewed_at",
)


def _write(path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _row(code: str, ingredient_id: int, **overrides) -> dict:
    row = {
        "constraint_code": code,
        "ingredient_id": ingredient_id,
        "decision": "no_hard_relation",
        "evidence": f"manual-review:{code}:{ingredient_id}",
        "review_status": "approved",
        "reviewer": "project_owner",
        "reviewed_at": "2026-08-10",
    }
    row.update(overrides)
    return row


def test_complete_independently_signed_matrix_is_accepted(tmp_path) -> None:
    path = tmp_path / "decisions.csv"
    rows = [
        _row("allergy_egg", 1, decision="hard_exclude"),
        _row("allergy_egg", 2),
        _row("disease_kidney", 1),
        _row("disease_kidney", 2),
    ]
    _write(path, rows)

    decisions = load_approved_health_relation_decisions(
        path,
        allowed_constraint_codes=("allergy_egg", "disease_kidney"),
        health_ingredient_ids=(1, 2),
        builder_identity="codex_builder",
    )

    assert len(decisions) == 4
    assert {(item.constraint_code, item.ingredient_id) for item in decisions} == {
        ("allergy_egg", 1),
        ("allergy_egg", 2),
        ("disease_kidney", 1),
        ("disease_kidney", 2),
    }


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda rows: rows.pop(), "HEALTH_RELATION_REVIEW_INCOMPLETE"),
        (lambda rows: rows.append(dict(rows[0])), "HEALTH_RELATION_REVIEW_DUPLICATE"),
        (
            lambda rows: rows[0].update(constraint_code="unknown_code"),
            "HEALTH_RELATION_REVIEW_UNKNOWN_CONSTRAINT",
        ),
        (
            lambda rows: rows[0].update(ingredient_id=999),
            "HEALTH_RELATION_REVIEW_UNKNOWN_INGREDIENT",
        ),
        (
            lambda rows: rows[0].update(review_status="pending"),
            "HEALTH_RELATION_REVIEW_PENDING",
        ),
        (
            lambda rows: rows[0].update(evidence=""),
            "HEALTH_RELATION_REVIEW_MISSING_EVIDENCE",
        ),
        (
            lambda rows: rows[0].update(reviewer=""),
            "HEALTH_RELATION_REVIEW_MISSING_REVIEWER",
        ),
        (
            lambda rows: rows[0].update(reviewer="codex_builder"),
            "HEALTH_RELATION_REVIEW_SELF_SIGNED",
        ),
    ],
)
def test_invalid_or_incomplete_review_is_rejected(tmp_path, mutation, expected_code) -> None:
    path = tmp_path / "decisions.csv"
    rows = [
        _row("allergy_egg", 1),
        _row("allergy_egg", 2),
        _row("disease_kidney", 1),
        _row("disease_kidney", 2),
    ]
    mutation(rows)
    _write(path, rows)

    with pytest.raises(HealthRelationReviewError) as excinfo:
        load_approved_health_relation_decisions(
            path,
            allowed_constraint_codes=("allergy_egg", "disease_kidney"),
            health_ingredient_ids=(1, 2),
            builder_identity="codex_builder",
        )
    assert excinfo.value.code == expected_code


def test_machine_candidates_cannot_be_imported_as_approval(tmp_path) -> None:
    candidates = generate_health_relation_candidates(
        ("allergy_egg",),
        ({"ingredient_id": 1, "name_canonical": "鸡蛋"},),
    )
    path = tmp_path / "candidates.csv"
    _write(
        path,
        [
            {
                "constraint_code": candidates[0].constraint_code,
                "ingredient_id": candidates[0].ingredient_id,
                "decision": "",
                "evidence": candidates[0].suggested_evidence,
                "review_status": candidates[0].review_status,
                "reviewer": "",
                "reviewed_at": "",
            }
        ],
    )

    with pytest.raises(HealthRelationReviewError) as excinfo:
        load_approved_health_relation_decisions(
            path,
            allowed_constraint_codes=("allergy_egg",),
            health_ingredient_ids=(1,),
            builder_identity="codex_builder",
        )
    assert excinfo.value.code == "HEALTH_RELATION_REVIEW_PENDING"
