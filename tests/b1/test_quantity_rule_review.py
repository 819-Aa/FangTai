import csv
import os
from dataclasses import replace
from decimal import Decimal

import pytest

import food_agent_v2.b1.quantity_rule_review as quantity_rule_review
from food_agent_v2.b1.quantity_normalizer import MeasureRule, MeasureRuleIndex
from food_agent_v2.b1.quantity_review import QuantityReviewCandidate
from food_agent_v2.b1.quantity_rule_review import (
    generate_quantity_rule_candidates,
    nearest_rank,
    write_quantity_rule_candidates,
)


def _candidate(
    value: str,
    *,
    ingredient_id: int = 10,
    ingredient_name: str = "葱",
    form: str = "切段",
    unit: str | None = None,
    usage: str = "supporting",
    fuzzy: str | None = "small_amount",
    raw_quantity: str | None = None,
) -> QuantityReviewCandidate:
    return QuantityReviewCandidate(
        occurrence_id=f"r-{ingredient_id}-{form}-{unit}-{usage}-{fuzzy}-{value}",
        recipe_id=1,
        recipe_name="测试菜",
        ingredient_id=ingredient_id,
        ingredient_name=ingredient_name,
        normalized_form=form,
        normalized_unit=unit,
        usage_code=usage,
        fuzzy_token_class=fuzzy,
        raw_quantity=("少许" if fuzzy else f"1{unit or ''}")
        if raw_quantity is None
        else raw_quantity,
        step_context="",
        deterministic_calculation="not_available",
        candidate_basis="whole_recipe_context_model",
        candidate_grams=Decimal(value),
    )


def _fuzzy_rule(grams: str = "10") -> MeasureRule:
    return MeasureRule(
        schema_version="2.0.0",
        rule_id="fuzzy-onion",
        rule_type="fuzzy_single_value",
        ingredient_id=10,
        ingredient_name="葱",
        normalized_form="切段",
        normalized_unit=None,
        usage_code="supporting",
        fuzzy_token_class="small_amount",
        to_grams=Decimal(grams),
        mass_density_g_per_ml=None,
        review_status="approved",
    )


def test_rule_candidate_requires_five_samples_and_cv_at_most_point_15() -> None:
    rows = tuple(_candidate(value) for value in ("9", "10", "10", "10", "11"))

    result = generate_quantity_rule_candidates(rows, MeasureRuleIndex(()))

    assert result[0].sample_count == 5
    assert result[0].sample_standard_deviation > Decimal("0")
    assert result[0].coefficient_of_variation <= Decimal("0.15")
    assert result[0].is_stable is True
    assert result[0].review_status == "pending"


def test_rule_candidate_uses_nearest_rank_iqr_and_strict_30_percent_boundary() -> None:
    rows = tuple(_candidate(value) for value in ("11", "12", "13", "14", "15"))

    at_boundary = generate_quantity_rule_candidates(rows, MeasureRuleIndex((_fuzzy_rule(),)))[0]
    above_boundary = generate_quantity_rule_candidates(
        tuple(_candidate(value) for value in ("13.01",) * 5),
        MeasureRuleIndex((_fuzzy_rule(),)),
    )[0]

    assert (at_boundary.q1, at_boundary.q3, at_boundary.iqr) == (
        Decimal("12"),
        Decimal("14"),
        Decimal("2"),
    )
    assert "QTY_RULE_DEVIATION_GT_30PCT" not in at_boundary.exception_codes
    assert "QTY_RULE_DEVIATION_GT_30PCT" in above_boundary.exception_codes


def test_rule_candidate_flags_iqr_outliers_and_keeps_them_pending() -> None:
    result = generate_quantity_rule_candidates(
        tuple(_candidate(value) for value in ("9", "10", "10", "10", "30")),
        MeasureRuleIndex(()),
    )

    assert result[0].has_iqr_outlier is True
    assert result[0].exception_codes == ("QTY_RULE_IQR_OUTLIER",)
    assert result[0].review_status == "pending"


def test_rule_candidate_strictly_isolates_form_usage_and_fuzzy_groups() -> None:
    rows = (
        tuple(_candidate("2", form="切段") for _ in range(5))
        + tuple(_candidate("7", form="整根", usage="main") for _ in range(5))
    )

    result = generate_quantity_rule_candidates(rows, MeasureRuleIndex(()))

    assert len(result) == 2
    assert {item.normalized_form for item in result} == {"切段", "整根"}
    assert all("QTY_FORM_USAGE_AMBIGUOUS" in item.exception_codes for item in result)
    assert all(item.review_status == "pending" for item in result)


def test_high_impact_main_and_salt_oil_sugar_fuzzy_candidates_are_never_approved() -> None:
    rows = (
        tuple(_candidate("20", ingredient_name="鸡肉", usage="main") for _ in range(5))
        + tuple(_candidate("2", ingredient_id=11, ingredient_name="盐") for _ in range(5))
    )

    result = generate_quantity_rule_candidates(rows, MeasureRuleIndex(()))
    by_name = {item.ingredient_name: item for item in result}

    assert "QTY_MAIN_UNRESOLVED" in by_name["鸡肉"].exception_codes
    assert "QTY_HIGH_IMPACT_FUZZY" in by_name["盐"].exception_codes
    assert {item.review_status for item in result} == {"pending"}


def test_rule_candidate_writer_refuses_the_formal_measure_rule_filename(tmp_path) -> None:
    candidates = generate_quantity_rule_candidates(
        tuple(_candidate("2") for _ in range(5)), MeasureRuleIndex(())
    )

    with pytest.raises(ValueError, match="正式计量规则文件"):
        write_quantity_rule_candidates(candidates, tmp_path / "ingredient_measure_rules.csv")


@pytest.mark.parametrize("bad_value", ("0", "-1", "NaN", "Infinity", "-Infinity"))
def test_rule_candidates_reject_non_positive_or_non_finite_model_grams(
    bad_value: str,
) -> None:
    with pytest.raises(ValueError, match="candidate_grams"):
        generate_quantity_rule_candidates(
            (_candidate(bad_value),), MeasureRuleIndex(())
        )


def test_nearest_rank_rejects_non_finite_percentile() -> None:
    with pytest.raises(ValueError, match="分位数"):
        nearest_rank((Decimal("1"),), Decimal("NaN"))


def test_count_range_uses_its_validated_midpoint() -> None:
    result = generate_quantity_rule_candidates(
        (
            _candidate(
                "150",
                fuzzy=None,
                unit="个",
                raw_quantity="10-20个",
            ),
        ),
        MeasureRuleIndex(()),
    )

    assert result[0].to_grams == Decimal("10")


def test_count_expression_with_trailing_measurement_fails_closed() -> None:
    result = generate_quantity_rule_candidates(
        (
            _candidate(
                "2",
                fuzzy=None,
                unit="个",
                raw_quantity="1个2克",
            ),
        ),
        MeasureRuleIndex(()),
    )

    assert result == ()


def test_writer_checks_every_status_before_truncating_existing_output(tmp_path) -> None:
    output = tmp_path / "review.csv"
    output.write_text("preserve me", encoding="utf-8")
    candidate = generate_quantity_rule_candidates(
        tuple(_candidate("2") for _ in range(5)), MeasureRuleIndex(())
    )[0]

    with pytest.raises(ValueError, match="pending"):
        write_quantity_rule_candidates((replace(candidate, review_status="approved"),), output)

    assert output.read_text(encoding="utf-8") == "preserve me"


def test_writer_refuses_case_variant_of_formal_rule_file_before_opening(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(quantity_rule_review, "PROJECT_ROOT", tmp_path)
    formal = tmp_path / "data" / "review" / "ingredient_measure_rules.csv"
    formal.parent.mkdir(parents=True)
    formal.write_text("preserve formal", encoding="utf-8")
    candidates = generate_quantity_rule_candidates(
        tuple(_candidate("2") for _ in range(5)), MeasureRuleIndex(())
    )

    with pytest.raises(ValueError, match="正式计量规则文件"):
        write_quantity_rule_candidates(
            candidates, formal.with_name("INGREDIENT_MEASURE_RULES.CSV")
        )

    assert formal.read_text(encoding="utf-8") == "preserve formal"


def test_writer_refuses_hard_link_to_formal_rule_file_before_opening(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(quantity_rule_review, "PROJECT_ROOT", tmp_path)
    formal = tmp_path / "data" / "review" / "ingredient_measure_rules.csv"
    formal.parent.mkdir(parents=True)
    formal.write_text("preserve formal", encoding="utf-8")
    hard_link = tmp_path / "rule-alias.csv"
    os.link(formal, hard_link)
    candidates = generate_quantity_rule_candidates(
        tuple(_candidate("2") for _ in range(5)), MeasureRuleIndex(())
    )

    with pytest.raises(ValueError, match="正式计量规则文件"):
        write_quantity_rule_candidates(candidates, hard_link)

    assert formal.read_text(encoding="utf-8") == "preserve formal"


def test_writer_refuses_resolved_symlink_to_formal_rule_file_before_opening(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(quantity_rule_review, "PROJECT_ROOT", tmp_path)
    formal = tmp_path / "data" / "review" / "ingredient_measure_rules.csv"
    formal.parent.mkdir(parents=True)
    formal.write_text("preserve formal", encoding="utf-8")
    alias = tmp_path / "resolved-alias.csv"
    try:
        alias.symlink_to(formal)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    candidates = generate_quantity_rule_candidates(
        tuple(_candidate("2") for _ in range(5)), MeasureRuleIndex(())
    )

    with pytest.raises(ValueError, match="正式计量规则文件"):
        write_quantity_rule_candidates(candidates, alias)

    assert formal.read_text(encoding="utf-8") == "preserve formal"


def test_writer_escapes_formula_text_fields(tmp_path) -> None:
    candidate = generate_quantity_rule_candidates(
        tuple(_candidate("2", ingredient_name="\t=1+1") for _ in range(5)),
        MeasureRuleIndex(()),
    )[0]
    output = tmp_path / "review.csv"

    write_quantity_rule_candidates((candidate,), output)

    with output.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["ingredient_name"] == "'\t=1+1"


def test_single_sample_rule_candidate_has_blank_cv_and_remains_writable(tmp_path) -> None:
    candidate = generate_quantity_rule_candidates(
        (_candidate("2"),), MeasureRuleIndex(())
    )[0]
    output = tmp_path / "review.csv"

    write_quantity_rule_candidates((candidate,), output)

    with output.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert candidate.coefficient_of_variation is None
    assert candidate.is_stable is False
    assert row["coefficient_of_variation"] == ""
    assert row["review_status"] == "pending"


@pytest.mark.parametrize(
    "codes",
    (
        ("QTY_RULE_IQR_OUTLIER", "QTY_MAIN_UNRESOLVED"),
        ("QTY_RULE_IQR_OUTLIER", "QTY_RULE_IQR_OUTLIER"),
        ("QTY_UNKNOWN",),
        (1,),
    ),
)
def test_rule_writer_rejects_noncanonical_exception_codes_before_opening(
    tmp_path, codes
) -> None:
    output = tmp_path / "review.csv"
    output.write_text("preserve me", encoding="utf-8")
    candidate = generate_quantity_rule_candidates(
        tuple(_candidate("2") for _ in range(5)), MeasureRuleIndex(())
    )[0]

    with pytest.raises(ValueError, match="exception_codes"):
        write_quantity_rule_candidates((replace(candidate, exception_codes=codes),), output)

    assert output.read_text(encoding="utf-8") == "preserve me"


@pytest.mark.parametrize(
    "field",
    (
        "rule_type",
        "ingredient_name",
        "normalized_form",
        "normalized_unit",
        "usage_code",
        "fuzzy_token_class",
        "review_status",
    ),
)
def test_rule_writer_rejects_non_string_text_fields_before_opening(tmp_path, field) -> None:
    output = tmp_path / "review.csv"
    output.write_text("preserve me", encoding="utf-8")
    candidate = generate_quantity_rule_candidates(
        tuple(_candidate("2") for _ in range(5)), MeasureRuleIndex(())
    )[0]

    with pytest.raises(ValueError, match="文本字段"):
        write_quantity_rule_candidates((replace(candidate, **{field: 1}),), output)

    assert output.read_text(encoding="utf-8") == "preserve me"


def test_rule_writer_keeps_existing_queue_when_serialization_fails(tmp_path, monkeypatch) -> None:
    output = tmp_path / "review.csv"
    output.write_text("preserve me", encoding="utf-8")
    candidate = generate_quantity_rule_candidates(
        tuple(_candidate("2") for _ in range(5)), MeasureRuleIndex(())
    )[0]
    original = csv.DictWriter.writerow
    calls = 0

    def fail_on_data_row(writer, row):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected serialization failure")
        return original(writer, row)

    monkeypatch.setattr(csv.DictWriter, "writerow", fail_on_data_row)

    with pytest.raises(RuntimeError, match="injected serialization failure"):
        write_quantity_rule_candidates((candidate,), output)

    assert output.read_text(encoding="utf-8") == "preserve me"
