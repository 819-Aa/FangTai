import csv
import json

from food_agent_v2.b1.health_relation_builder import (
    ALLOWED_CONSTRAINT_CODES,
    build_approved_health_relation_artifacts,
    generate_health_relation_candidates,
    run_health_relation_stage,
)
from food_agent_v2.b1.health_relation_review import ApprovedHealthRelationDecision


def test_allowed_constraint_registry_is_closed_and_complete() -> None:
    assert len(ALLOWED_CONSTRAINT_CODES) == 38
    assert len(set(ALLOWED_CONSTRAINT_CODES)) == 38
    assert "allergy_pineapple" in ALLOWED_CONSTRAINT_CODES
    assert "disease_kidney" in ALLOWED_CONSTRAINT_CODES
    assert "indicator_high_bp" in ALLOWED_CONSTRAINT_CODES
    assert "group_pregnancy" in ALLOWED_CONSTRAINT_CODES


def test_candidates_are_exact_product_and_always_pending() -> None:
    codes = ("allergy_egg", "disease_kidney")
    ingredients = (
        {"ingredient_id": 1, "name_canonical": "鸡蛋"},
        {"ingredient_id": 2, "name_canonical": "胡萝卜"},
    )
    candidates = generate_health_relation_candidates(codes, ingredients)

    assert {(item.constraint_code, item.ingredient_id) for item in candidates} == {
        ("allergy_egg", 1),
        ("allergy_egg", 2),
        ("disease_kidney", 1),
        ("disease_kidney", 2),
    }
    assert all(item.review_status == "pending" for item in candidates)
    assert all(item.decision is None for item in candidates)
    assert all(item.reviewer is None for item in candidates)
    assert (
        next(
            item
            for item in candidates
            if item.constraint_code == "allergy_egg" and item.ingredient_id == 1
        ).suggested_decision
        == "hard_exclude"
    )
    assert (
        next(
            item
            for item in candidates
            if item.constraint_code == "disease_kidney" and item.ingredient_id == 1
        ).suggested_decision
        == "no_hard_relation"
    )


def test_freeze_preserves_negative_decisions_and_builds_complete_coverage() -> None:
    decisions = (
        ApprovedHealthRelationDecision(
            constraint_code="allergy_egg",
            ingredient_id=1,
            decision="hard_exclude",
            evidence="review:egg",
            reviewer="project_owner",
            reviewed_at="2026-08-10",
        ),
        ApprovedHealthRelationDecision(
            constraint_code="allergy_egg",
            ingredient_id=2,
            decision="no_hard_relation",
            evidence="review:carrot",
            reviewer="project_owner",
            reviewed_at="2026-08-10",
        ),
    )
    artifacts = build_approved_health_relation_artifacts(
        decisions,
        allowed_constraint_codes=("allergy_egg",),
        health_ingredient_ids=(1, 2),
    )

    assert len(artifacts.decisions) == 2
    assert [(item.constraint_code, item.ingredient_id) for item in artifacts.hard_relations] == [
        ("allergy_egg", 1)
    ]
    assert artifacts.coverage[0].coverage_status == "complete"
    assert artifacts.coverage[0].universe_ingredient_count == 2
    assert artifacts.coverage[0].reviewed_ingredient_count == 2
    assert artifacts.coverage[0].relation_count == 1


def test_stage_exports_only_health_view_product_and_blocks_at_h03(tmp_path) -> None:
    registry_path = tmp_path / "ingredient_registry.jsonl"
    registry_path.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False)
            for record in (
                {"ingredient_id": 1, "name_canonical": "鸡蛋"},
                {"ingredient_id": 2, "name_canonical": "胡萝卜"},
                {"ingredient_id": 3, "name_canonical": "未被菜品引用"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    health_views_path = tmp_path / "recipe_health_views.jsonl"
    health_views_path.write_text(
        json.dumps({"recipe_id": 1, "ingredient_ids": [1, 2]}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    decisions_path = tmp_path / "decisions.csv"
    with decisions_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(
            (
                "constraint_code",
                "ingredient_id",
                "decision",
                "evidence",
                "review_status",
                "reviewer",
                "reviewed_at",
            )
        )

    staging = tmp_path / "T08"
    report = run_health_relation_stage(
        ingredient_registry_path=registry_path,
        health_views_path=health_views_path,
        decisions_path=decisions_path,
        staging_dir=staging,
        builder_identity="codex_data_builder",
    )
    candidates = [
        json.loads(line)
        for line in (staging / "health_relation_candidates.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert report["status"] == "blocked"
    assert report["blocker"] == "HEALTH_RELATION_REVIEW_INCOMPLETE"
    assert report["health_ingredient_count"] == 2
    assert report["candidate_count"] == len(ALLOWED_CONSTRAINT_CODES) * 2
    assert all(item["review_status"] == "pending" for item in candidates)
    assert (staging / "health_relation_review_matrix.csv").exists()
    assert (staging / "health_relation_flagged_review.csv").exists()
    assert not (staging / "health_relations.jsonl").exists()
    assert not (staging / "health_relation_coverage.jsonl").exists()
