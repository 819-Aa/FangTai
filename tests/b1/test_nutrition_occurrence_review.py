from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from food_agent_v2.b1.consumer_views import (
    BuildIdentity,
    IngredientIdentityFact,
    IngredientOccurrenceFact,
    NutritionOccurrenceInput,
    RecipeFact,
    RecipeNutritionInputView,
    RecipeStepBindingView,
    StructuredStep,
)
from food_agent_v2.b1.nutrition_occurrence_review import (
    NutritionOccurrenceReviewContext,
    generate_nutrition_occurrence_review,
    write_nutrition_occurrence_review,
)


def _context(
    *,
    name: str = "鸡肉",
    category: str = "肉类",
    step: str | None = "加入鸡肉炒熟",
    usage_requires_review: bool = True,
    retention_requires_review: bool = True,
    usage_code: str | None = None,
    retained_in_dish: bool | None = None,
) -> NutritionOccurrenceReviewContext:
    build = BuildIdentity(UUID(int=1), "a" * 64)
    occurrence = IngredientOccurrenceFact(
        occurrence_id="1-1",
        recipe_id=1,
        source_fragment="鸡肉 200g",
        name_clean=name,
        ingredient_id=7,
        consumption_role="edible",
        quantity_raw="200g",
        form="切块",
    )
    input_item = NutritionOccurrenceInput(
        occurrence_id="1-1",
        ingredient_id=7,
        quantity_raw="200g",
        unit_raw="g",
        quantity_status="explicit",
        ingredient_name=name,
        normalized_form="切块",
        usage_code=usage_code,  # type: ignore[arg-type]
        retained_in_dish=retained_in_dish,
        usage_requires_review=usage_requires_review,
        retention_requires_review=retention_requires_review,
    )
    nutrition_view = RecipeNutritionInputView(
        build_id=build.build_id,
        source_manifest_hash=build.source_manifest_hash,
        recipe_id=1,
        ingredients=(input_item,),
    )
    steps = () if step is None else (
        StructuredStep(step_index=1, raw_text=step, bound_occurrence_ids=("1-1",)),
    )
    step_view = RecipeStepBindingView(
        build_id=build.build_id,
        source_manifest_hash=build.source_manifest_hash,
        recipe_id=1,
        ingredient_ids=(7,),
        steps=steps,
    )
    return NutritionOccurrenceReviewContext(
        views=SimpleNamespace(nutrition_views=(nutrition_view,), step_views=(step_view,)),
        occurrences=(occurrence,),
        identities=(
            IngredientIdentityFact(
                ingredient_id=7, name_canonical=name, family_id=7, category=category
            ),
        ),
        recipes=(RecipeFact(recipe_id=1, name="测试菜", record_type="dish"),),
    )


def test_bound_solid_without_discard_risk_is_pending_retention_candidate() -> None:
    bundle = generate_nutrition_occurrence_review(_context())

    assert [
        (row.occurrence_id, row.candidate_retained_in_dish, row.review_status)
        for row in bundle.retention_candidates
    ] == [("1-1", True, "pending")]
    assert bundle.retention_exceptions == ()


def test_discard_and_no_binding_are_separate_retention_exceptions() -> None:
    discard = generate_nutrition_occurrence_review(_context(name="香料", step="煮后捞出香料"))
    missing = generate_nutrition_occurrence_review(_context(name="香料", step=None))

    assert discard.retention_exceptions[0].exception_codes == (
        "NUTRITION_RETENTION_DISCARD_RISK",
    )
    assert missing.retention_exceptions[0].exception_codes == (
        "NUTRITION_RETENTION_NO_STEP_BINDING",
    )


def test_resolved_seasoning_is_not_repeated_as_usage_candidate() -> None:
    bundle = generate_nutrition_occurrence_review(
        _context(
            name="盐",
            category="调料",
            usage_requires_review=False,
            usage_code="seasoning",
        )
    )

    assert bundle.usage_candidates == ()
    assert bundle.usage_exceptions == ()


@pytest.mark.parametrize(
    ("name", "candidate_usage_code"),
    (("食用油", "cooking_fat"), ("清水", "cooking_liquid")),
)
def test_controlled_oil_and_liquid_are_usage_candidates_only(
    name: str, candidate_usage_code: str
) -> None:
    bundle = generate_nutrition_occurrence_review(_context(name=name))

    assert [row.candidate_usage_code for row in bundle.usage_candidates] == [
        candidate_usage_code
    ]
    assert bundle.usage_exceptions == ()


def test_uncontrolled_usage_is_ambiguous_exception() -> None:
    bundle = generate_nutrition_occurrence_review(_context(name="鸡肉"))

    assert bundle.usage_exceptions[0].exception_codes == ("NUTRITION_USAGE_AMBIGUOUS",)


@pytest.mark.parametrize("name", ("食用油", "清水"))
def test_oil_and_liquid_retention_never_auto_true(name: str) -> None:
    bundle = generate_nutrition_occurrence_review(_context(name=name))

    assert bundle.retention_candidates == ()
    assert bundle.retention_exceptions[0].exception_codes == (
        "NUTRITION_RETENTION_AMBIGUOUS",
    )


def _bundle_fixture():
    fat = generate_nutrition_occurrence_review(_context(name="食用油"))
    solid = generate_nutrition_occurrence_review(_context(name="鸡肉"))
    return replace(
        fat,
        usage_exceptions=(replace(solid.usage_exceptions[0], occurrence_id="1-2"),),
        retention_candidates=(replace(solid.retention_candidates[0], occurrence_id="1-2"),),
    )


def test_writer_outputs_four_pending_only_atomic_csvs(tmp_path: Path) -> None:
    counts = write_nutrition_occurrence_review(_bundle_fixture(), tmp_path)

    assert counts == {
        "usage_candidates": 1,
        "usage_exceptions": 1,
        "retention_candidates": 1,
        "retention_exceptions": 1,
    }
    assert {path.name for path in tmp_path.iterdir()} == {
        "nutrition_usage_candidates.csv",
        "nutrition_usage_exceptions.csv",
        "nutrition_retention_candidates.csv",
        "nutrition_retention_exceptions.csv",
    }
    with (tmp_path / "nutrition_usage_candidates.csv").open(encoding="utf-8", newline="") as handle:
        assert tuple(csv.DictReader(handle).fieldnames or ()) == (
            "occurrence_id", "recipe_id", "recipe_name", "ingredient_id",
            "ingredient_name", "source_fragment", "normalized_form", "category",
            "quantity_raw", "bound_step_indexes", "bound_step_text",
            "candidate_usage_code", "evidence_codes", "review_status",
        )


def test_writer_escapes_formulas_in_every_text_field(tmp_path: Path) -> None:
    bundle = generate_nutrition_occurrence_review(
        _context(name="=食用油", step="@加入油", category="+油类")
    )

    write_nutrition_occurrence_review(bundle, tmp_path)

    text = (tmp_path / "nutrition_usage_candidates.csv").read_text(encoding="utf-8")
    assert "'=食用油" in text
    assert "'+油类" in text
    assert "'@加入油" in text


def test_writer_rejects_invalid_status_duplicate_and_cross_queue_before_replace(
    tmp_path: Path,
) -> None:
    bundle = _bundle_fixture()
    targets = tuple(tmp_path / name for name in (
        "nutrition_usage_candidates.csv", "nutrition_usage_exceptions.csv",
        "nutrition_retention_candidates.csv", "nutrition_retention_exceptions.csv",
    ))
    for target in targets:
        target.write_text(f"old:{target.name}", encoding="utf-8")

    invalid = replace(bundle.usage_candidates[0], review_status="approved")
    duplicate = replace(bundle, usage_candidates=(bundle.usage_candidates[0], bundle.usage_candidates[0]))
    cross_queue = replace(
        bundle,
        usage_exceptions=(
            replace(
                bundle.usage_exceptions[0],
                occurrence_id=bundle.usage_candidates[0].occurrence_id,
            ),
        ),
    )

    for broken in (
        replace(bundle, usage_candidates=(invalid,)),
        duplicate,
        cross_queue,
    ):
        with pytest.raises(ValueError):
            write_nutrition_occurrence_review(broken, tmp_path)
        assert [target.read_text(encoding="utf-8") for target in targets] == [
            f"old:{target.name}" for target in targets
        ]


def test_writer_rolls_back_every_queue_when_a_replace_fails(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle_fixture()
    targets = tuple(tmp_path / name for name in (
        "nutrition_usage_candidates.csv", "nutrition_usage_exceptions.csv",
        "nutrition_retention_candidates.csv", "nutrition_retention_exceptions.csv",
    ))
    for target in targets:
        target.write_text(f"old:{target.name}", encoding="utf-8")
    original_replace = Path.replace
    replacement_calls = 0

    def fail_third_queue_replace(path: Path, target: Path) -> Path:
        nonlocal replacement_calls
        if path.suffix == ".tmp":
            replacement_calls += 1
            if replacement_calls == 3:
                raise RuntimeError("injected replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_third_queue_replace)

    with pytest.raises(RuntimeError, match="injected replace failure"):
        write_nutrition_occurrence_review(bundle, tmp_path)

    assert [target.read_text(encoding="utf-8") for target in targets] == [
        f"old:{target.name}" for target in targets
    ]
