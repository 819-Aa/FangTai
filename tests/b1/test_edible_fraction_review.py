from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

import pytest

from food_agent_v2.b1.consumer_views import NutritionOccurrenceInput, RecipeNutritionInputView
from food_agent_v2.b1.edible_fraction_review import (
    EdibleFractionDecision,
    EdibleFractionDecisionIndex,
    EdibleFractionReviewCandidate,
    EdibleFractionRule,
    EdibleFractionRuleIndex,
    generate_edible_fraction_candidates,
    load_edible_fraction_decisions,
    load_edible_fraction_rules,
    resolve_edible_fraction,
    write_edible_fraction_candidates,
)


def _occurrence(
    *, id: str = "1-1", ingredient_id: int = 7, form: str = "带皮"
) -> NutritionOccurrenceInput:
    return NutritionOccurrenceInput(
        occurrence_id=id,
        ingredient_id=ingredient_id,
        quantity_raw="100克",
        unit_raw="克",
        quantity_status="explicit",
        ingredient_name="苹果",
        normalized_form=form,
    )


def _rule(ingredient_id: int, normalized_form: str, edible_fraction: str) -> EdibleFractionRule:
    return EdibleFractionRule(
        ingredient_id=ingredient_id,
        ingredient_name="苹果",
        normalized_form=normalized_form,
        edible_fraction=Decimal(edible_fraction),
        review_status="approved",
    )


def _decision(
    occurrence_id: str,
    edible_fraction: str,
    review_status: str,
    *,
    decision_form: str = "带皮",
) -> EdibleFractionDecision:
    return EdibleFractionDecision(
        occurrence_id=occurrence_id,
        recipe_id=1,
        ingredient_id=7,
        ingredient_name="苹果",
        decision_form=decision_form,
        edible_fraction=Decimal(edible_fraction),
        review_status=review_status,
    )


def test_occurrence_decision_overrides_generic_fraction() -> None:
    resolution = resolve_edible_fraction(
        _occurrence(id="1-1", ingredient_id=7, form="带皮"),
        EdibleFractionRuleIndex((_rule(7, "带皮", "0.80"),)),
        EdibleFractionDecisionIndex((_decision("1-1", "0.65", "modified"),)),
    )

    assert resolution.edible_fraction == Decimal("0.65")
    assert resolution.requires_review is False


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-0.1", "1.01"])
def test_fractions_must_be_finite_and_in_the_edible_interval(value: str) -> None:
    with pytest.raises(ValueError, match="可食比例"):
        EdibleFractionRuleIndex((_rule(7, "带皮", value),))


def test_rule_key_uses_ingredient_id_and_normalized_form() -> None:
    with pytest.raises(ValueError, match="重复可食比例规则"):
        EdibleFractionRuleIndex(
            (
                _rule(7, "带皮", "0.8"),
                EdibleFractionRule(7, "另一名称", "带皮", Decimal("0.7"), "approved"),
            )
        )


def test_pending_rules_and_decisions_do_not_become_effective() -> None:
    resolution = resolve_edible_fraction(
        _occurrence(),
        EdibleFractionRuleIndex(
            (EdibleFractionRule(7, "苹果", "带皮", Decimal("0.8"), "pending"),)
        ),
        EdibleFractionDecisionIndex((_decision("1-1", "0.65", "pending"),)),
    )

    assert resolution.edible_fraction is None
    assert resolution.requires_review is True


def test_active_decision_metadata_drift_fails_closed_without_generic_fallback() -> None:
    decision = EdibleFractionDecision(
        occurrence_id="1-1",
        recipe_id=9,
        ingredient_id=7,
        ingredient_name="苹果",
        decision_form="带皮",
        edible_fraction=Decimal("0.65"),
        review_status="approved",
    )

    resolution = resolve_edible_fraction(
        _occurrence(),
        EdibleFractionRuleIndex((_rule(7, "带皮", "0.8"),)),
        EdibleFractionDecisionIndex((decision,)),
    )

    assert resolution.edible_fraction is None
    assert resolution.requires_review is True


def test_blank_form_rule_cannot_coexist_with_a_specific_form() -> None:
    with pytest.raises(ValueError, match="空白形态"):
        EdibleFractionRuleIndex((_rule(7, "", "1"), _rule(7, "带皮", "0.8")))


def test_loaders_require_the_formal_headers_and_reject_cooking_yield_fields(
    tmp_path: Path,
) -> None:
    rules = tmp_path / "rules.csv"
    rules.write_text(
        "ingredient_id,ingredient_name,normalized_form,edible_fraction,cooking_yield,review_status\n"
        "7,苹果,带皮,0.8,0.7,approved\n",
        encoding="utf-8",
    )
    decisions = tmp_path / "decisions.csv"
    decisions.write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,decision_form,edible_fraction,review_status\n"
        "1-1,1,7,苹果,带皮,0.65,modified\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="表头非法"):
        load_edible_fraction_rules(rules)
    index = load_edible_fraction_decisions(decisions)
    assert index.get_effective("1-1") is not None

    stale_decisions = tmp_path / "stale-decisions.csv"
    stale_decisions.write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,decision_form,edible_fraction,review_status\n"
        "1-1,1,7,苹果,带皮,带皮,0.65,modified\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="表头非法"):
        load_edible_fraction_decisions(stale_decisions)


def test_edible_fraction_loader_rejects_unknown_occurrence_ids_for_any_review_status(
    tmp_path: Path,
) -> None:
    decisions = tmp_path / "decisions.csv"
    decisions.write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,decision_form,edible_fraction,review_status\n"
        "9-9,9,7,苹果,带皮,0.65,pending\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="未知 occurrence_id"):
        load_edible_fraction_decisions(decisions, current_occurrence_ids={"1-1"})


def test_resolver_accepts_the_frozen_occurrence_input_type() -> None:
    assert get_type_hints(resolve_edible_fraction)["occurrence"] is NutritionOccurrenceInput


def test_decision_form_completes_a_blank_source_form() -> None:
    resolution = resolve_edible_fraction(
        _occurrence(form=""),
        EdibleFractionRuleIndex(()),
        EdibleFractionDecisionIndex((_decision("1-1", "0.65", "approved", decision_form="去皮"),)),
    )

    assert resolution.edible_fraction == Decimal("0.65")
    assert resolution.requires_review is False


def test_nonblank_source_form_conflicting_with_decision_fails_closed() -> None:
    resolution = resolve_edible_fraction(
        _occurrence(form="带皮"),
        EdibleFractionRuleIndex((_rule(7, "带皮", "0.8"),)),
        EdibleFractionDecisionIndex((_decision("1-1", "0.65", "approved", decision_form="去皮"),)),
    )

    assert resolution.edible_fraction is None
    assert resolution.requires_review is True


@pytest.mark.parametrize(
    ("first_status", "second_status"),
    [("pending", "pending"), ("approved", "pending"), ("rejected", "modified")],
)
def test_decision_occurrence_id_is_unique_across_all_review_statuses(
    first_status: str, second_status: str
) -> None:
    with pytest.raises(ValueError, match="重复"):
        EdibleFractionDecisionIndex(
            (
                _decision("1-1", "0.65", first_status),
                _decision("1-1", "0.8", second_status),
            )
        )


def test_generator_is_pending_only_excludes_nonretained_and_diagnoses_usage() -> None:
    view = RecipeNutritionInputView(
        build_id=UUID("00000000-0000-0000-0000-000000000001"),
        source_manifest_hash="a" * 64,
        recipe_id=1,
        ingredients=(
            _occurrence(id="1-1", form=""),
            NutritionOccurrenceInput(
                occurrence_id="1-2", ingredient_id=7, quantity_raw="1个", unit_raw=None,
                quantity_status="explicit", ingredient_name="苹果", normalized_form="带皮",
                retained_in_dish=False, usage_code="main",
            ),
            NutritionOccurrenceInput(
                occurrence_id="1-3", ingredient_id=7, quantity_raw="1个", unit_raw=None,
                quantity_status="explicit", ingredient_name="苹果", normalized_form="带皮",
                retained_in_dish=None, usage_code=None,
            ),
        ),
    )
    candidates = generate_edible_fraction_candidates(
        (view,), EdibleFractionRuleIndex(()), EdibleFractionDecisionIndex(())
    )

    assert [candidate.occurrence_id for candidate in candidates] == ["1-1", "1-3"]
    assert candidates[0].candidate_edible_fraction is None
    assert candidates[0].review_status == "pending"
    assert "blank_form" in candidates[0].exception_reasons
    assert "unresolved_usage" in candidates[0].exception_reasons
    assert "no_effective_rule_or_occurrence_decision" in candidates[0].exception_reasons
    assert "unresolved_usage" in candidates[1].exception_reasons


def test_usage_unresolved_does_not_skip_existing_effective_fraction() -> None:
    occurrence = NutritionOccurrenceInput(
        occurrence_id="1-1", ingredient_id=7, quantity_raw="1个", unit_raw=None,
        quantity_status="explicit", ingredient_name="苹果", normalized_form="带皮",
        usage_code="main", retained_in_dish=None, requires_review=False,
    )
    view = RecipeNutritionInputView(
        build_id=UUID("00000000-0000-0000-0000-000000000001"),
        source_manifest_hash="a" * 64, recipe_id=1, ingredients=(occurrence,),
    )
    candidates = generate_edible_fraction_candidates(
        (view,), EdibleFractionRuleIndex((_rule(7, "带皮", "0.8"),)), EdibleFractionDecisionIndex(())
    )

    assert len(candidates) == 1
    assert candidates[0].candidate_basis == "unresolved_usage"
    assert candidates[0].exception_reasons == ("unresolved_usage",)


def test_writer_escapes_formulas_and_preserves_old_queue_on_formal_alias(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "food_agent_v2.b1.edible_fraction_review.PROJECT_ROOT", tmp_path
    )
    candidate = EdibleFractionReviewCandidate(
        occurrence_id="1-1", recipe_id=1, ingredient_id=7, ingredient_name="=苹果",
        source_form="", decision_form="", quantity_raw="少许", usage_code="seasoning",
        retained_in_dish=True,
        exception_reasons=("blank_form", "no_effective_rule_or_occurrence_decision"),
    )
    output = tmp_path / "candidates.csv"
    write_edible_fraction_candidates((candidate,), output)
    assert "'=苹果" in output.read_text(encoding="utf-8")

    formal = tmp_path / "data" / "review" / "ingredient_edible_fraction_rules.csv"
    formal.parent.mkdir(parents=True)
    formal.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="正式"):
        write_edible_fraction_candidates((candidate,), formal)
    assert formal.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("usage_code", "garbage"),
        ("candidate_basis", "garbage"),
        ("candidate_basis", "unresolved_usage"),
        ("exception_reasons", ("blank_form", "blank_form")),
        ("exception_reasons", ("no_effective_rule_or_occurrence_decision", "blank_form")),
    ),
)
def test_writer_rejects_invalid_diagnostic_fields_before_opening(tmp_path, field, value) -> None:
    candidate = EdibleFractionReviewCandidate(
        occurrence_id="1-1", recipe_id=1, ingredient_id=7, ingredient_name="苹果",
        source_form="", decision_form="", quantity_raw="少许", usage_code="seasoning",
        retained_in_dish=True, exception_reasons=("blank_form",),
    )
    output = tmp_path / "queue.csv"
    original = b"preserve\r\n"
    output.write_bytes(original)

    with pytest.raises(ValueError):
        write_edible_fraction_candidates((replace(candidate, **{field: value}),), output)
    assert output.read_bytes() == original


@pytest.mark.parametrize(
    "filename",
    (
        "ingredient_measure_rules.csv",
        "ingredient_quantity_decisions.csv",
        "ingredient_nutrition_usage_decisions.csv",
        "ingredient_edible_fraction_rules.csv",
        "ingredient_edible_fraction_decisions.csv",
    ),
)
def test_edible_writer_refuses_all_formal_targets(tmp_path, monkeypatch, filename) -> None:
    monkeypatch.setattr(
        "food_agent_v2.b1.edible_fraction_review.PROJECT_ROOT", tmp_path
    )
    candidate = EdibleFractionReviewCandidate(
        occurrence_id="1-1", recipe_id=1, ingredient_id=7, ingredient_name="苹果",
        source_form="", decision_form="", retained_in_dish=True,
        exception_reasons=("blank_form", "no_effective_rule_or_occurrence_decision"),
    )
    formal = tmp_path / "data" / "review" / filename
    formal.parent.mkdir(parents=True, exist_ok=True)
    formal.write_text("preserve", encoding="utf-8")

    with pytest.raises(ValueError, match="正式"):
        write_edible_fraction_candidates((candidate,), formal)
    assert formal.read_text(encoding="utf-8") == "preserve"
