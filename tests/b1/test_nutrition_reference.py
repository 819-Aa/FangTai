import csv
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from food_agent_v2.b1.nutrition_reference import (
    IngredientNutritionReference,
    NutritionCrosswalkDecision,
    NutritionCrosswalkIndex,
    NutritionReferenceIndex,
    NutritionVector,
    build_nutrition_coverage_report,
    convert_china_composition_record,
    load_nutrition_crosswalk,
    load_nutrition_references,
    nutrition_form_from_occurrence,
    source_priority,
    write_nutrition_crosswalk_candidates,
)
from food_agent_v2.core.paths import PROJECT_ROOT


def _vector(**overrides) -> NutritionVector:
    values = {
        "energy_kcal": Decimal("100"),
        "protein_g": Decimal("10"),
        "fat_g": Decimal("2"),
        "carbohydrate_g": Decimal("15"),
        "fiber_g": Decimal("3"),
        "sodium_mg": Decimal("20"),
        "calcium_mg": Decimal("30"),
        "iron_mg": Decimal("1"),
        "cholesterol_mg": Decimal("4"),
    }
    values.update(overrides)
    return NutritionVector(**values)


def _reference(
    reference_id: str = "cn-1",
    *,
    name: str = "豆腐",
    form: str = "raw",
    source_dataset: str = "china_cdc",
    food_origin: str = "unknown",
    per_100g: NutritionVector | None = None,
    brand_name: str | None = None,
    specification: str | None = None,
) -> IngredientNutritionReference:
    return IngredientNutritionReference(
        reference_id=reference_id,
        canonical_name=name,
        form=form,
        per_100g=per_100g or _vector(),
        source_name="权威营养数据",
        source_url="https://example.invalid/reference/1",
        source_dataset=source_dataset,
        food_origin=food_origin,
        brand_name=brand_name,
        specification=specification,
    )


def test_nutrition_vector_always_serializes_exactly_nine_fixed_unit_fields() -> None:
    vector = NutritionVector()

    assert tuple(vector.model_dump()) == (
        "energy_kcal",
        "protein_g",
        "fat_g",
        "carbohydrate_g",
        "fiber_g",
        "sodium_mg",
        "calcium_mg",
        "iron_mg",
        "cholesterol_mg",
    )
    assert set(vector.model_dump().values()) == {None}
    with pytest.raises(ValidationError):
        NutritionVector(energy_kcal=Decimal("-1"))


def test_only_confirmed_food_origin_creates_structural_zeroes() -> None:
    plant = _reference(
        food_origin="plant",
        per_100g=_vector(cholesterol_mg=None, fiber_g=None, sodium_mg=None),
    )
    animal = _reference(
        "cn-2",
        name="牛肉",
        food_origin="animal",
        per_100g=_vector(cholesterol_mg=None, fiber_g=None, sodium_mg=None),
    )
    unknown = _reference(
        "cn-3",
        name="复合食品",
        food_origin="unknown",
        per_100g=_vector(cholesterol_mg=None, fiber_g=None, sodium_mg=None),
    )

    assert plant.per_100g.cholesterol_mg == Decimal("0")
    assert plant.per_100g.fiber_g is None
    assert animal.per_100g.fiber_g == Decimal("0")
    assert animal.per_100g.cholesterol_mg is None
    assert unknown.per_100g.cholesterol_mg is None
    assert unknown.per_100g.fiber_g is None
    assert plant.per_100g.sodium_mg is None


def test_crosswalk_requires_one_effective_mapping_per_ingredient_and_form() -> None:
    references = NutritionReferenceIndex((_reference(), _reference("cn-2")))
    decisions = (
        NutritionCrosswalkDecision(
            ingredient_id=10,
            ingredient_name="豆腐",
            form="raw",
            reference_id="cn-1",
            match_method="exact",
            review_status="approved",
        ),
        NutritionCrosswalkDecision(
            ingredient_id=10,
            ingredient_name="豆腐",
            form="raw",
            reference_id="cn-2",
            match_method="exact",
            review_status="modified",
        ),
    )

    with pytest.raises(ValueError, match="重复营养映射"):
        NutritionCrosswalkIndex(decisions, references)


def test_crosswalk_requires_exact_form_and_branded_identity() -> None:
    branded = _reference(
        "brand-1",
        name="某牌全脂牛奶",
        form="ready_to_drink",
        source_dataset="branded",
        brand_name="某牌",
        specification="250毫升",
    )
    references = NutritionReferenceIndex((branded,))

    with pytest.raises(ValueError, match="形态"):
        NutritionCrosswalkIndex(
            (
                NutritionCrosswalkDecision(
                    ingredient_id=1,
                    ingredient_name="某牌全脂牛奶",
                    form="powder",
                    reference_id="brand-1",
                    match_method="exact",
                    review_status="approved",
                    brand_name="某牌",
                    specification="250毫升",
                ),
            ),
            references,
        )

    with pytest.raises(ValueError, match="品牌"):
        NutritionCrosswalkIndex(
            (
                NutritionCrosswalkDecision(
                    ingredient_id=1,
                    ingredient_name="全脂牛奶",
                    form="ready_to_drink",
                    reference_id="brand-1",
                    match_method="approved_alias",
                    review_status="approved",
                    brand_name="某牌",
                    specification="250毫升",
                ),
            ),
            references,
        )


def test_source_priority_is_fixed() -> None:
    assert source_priority("china_cdc") < source_priority("usda_foundation")
    assert source_priority("usda_foundation") < source_priority("usda_sr_legacy")
    assert source_priority("usda_sr_legacy") < source_priority("usda_fndds")
    assert source_priority("usda_fndds") < source_priority("branded")


def test_loaders_keep_pending_crosswalk_ineffective_and_report_true_missingness(
    tmp_path,
) -> None:
    reference = _reference(
        per_100g=_vector(fiber_g=None, cholesterol_mg=None),
    )
    reference_path = tmp_path / "references.jsonl"
    reference_path.write_text(
        json.dumps(reference.model_dump(mode="json"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    crosswalk_path = tmp_path / "crosswalk.jsonl"
    crosswalk_path.write_text(
        json.dumps(
            {
                "ingredient_id": 10,
                "ingredient_name": "豆腐",
                "form": "raw",
                "reference_id": "cn-1",
                "match_method": "exact",
                "review_status": "pending",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    references = load_nutrition_references(reference_path)
    crosswalk = load_nutrition_crosswalk(crosswalk_path, references)
    report = build_nutrition_coverage_report(
        references,
        crosswalk,
        ingredient_form_keys=((10, "raw"), (20, "raw")),
    )

    assert crosswalk.get(10, "raw") is None
    assert report["approved_crosswalk_count"] == 0
    assert report["unresolved_ingredient_count"] == 2
    assert report["nutrient_non_null_rates"]["fiber_g"] == 0.0
    assert report["nutrient_non_null_rates"]["cholesterol_mg"] == 0.0


def test_china_composition_conversion_is_mechanical_and_does_not_invent_missing_values() -> None:
    reference = convert_china_composition_record(
        {
            "source_id": 259,
            "name": "小麦粉(标准粉)",
            "energy_kcal_per_100g": 353.25,
            "protein_g_per_100g": 11.2,
            "fat_g_per_100g": 1.5,
            "carb_g_per_100g": 73.6,
            "fiber_g_per_100g": None,
            "sodium_mg_per_100g": 3.1,
            "calcium_mg_per_100g": 31,
            "iron_mg_per_100g": 3.5,
            "source_name": "中国疾控食物成分表",
            "source_url": "https://example.invalid/259",
        }
    )

    assert reference.reference_id == "china-cdc-259"
    assert reference.per_100g.carbohydrate_g == Decimal("73.6")
    assert reference.per_100g.fiber_g is None
    assert reference.per_100g.cholesterol_mg is None
    assert reference.food_origin == "unknown"


def test_nutrition_candidate_writer_never_approves_and_keeps_state_mismatch_visible(
    tmp_path,
) -> None:
    references = NutritionReferenceIndex(
        (_reference(name="姜", form="unspecified"),)
    )
    output = tmp_path / "nutrition_candidates.csv"

    write_nutrition_crosswalk_candidates(
        ((10, "姜", "raw"), (20, "不存在", "raw")),
        references,
        output,
    )
    text = output.read_text(encoding="utf-8")

    assert "form_state_requires_review" in text
    assert "no_exact_name_match" in text
    assert "pending" in text
    assert "approved" not in text
    assert nutrition_form_from_occurrence("丝") == "unspecified"
    assert nutrition_form_from_occurrence("水发") == "hydrated"


def test_nutrition_candidate_writer_suggests_reference_aliases_without_approving(
    tmp_path,
) -> None:
    references = NutritionReferenceIndex(
        (
            _reference(name="梨(均值)", form="unspecified"),
            _reference(name="马铃薯[土豆，洋芋]", form="unspecified").model_copy(
                update={"reference_id": "cn-2"}
            ),
        )
    )
    output = tmp_path / "nutrition_candidates.csv"

    write_nutrition_crosswalk_candidates(
        ((10, "梨肉", "unspecified"), (20, "土豆", "unspecified")),
        references,
        output,
    )
    rows = list(csv.DictReader(output.open(encoding="utf-8")))

    assert [(row["ingredient_name"], row["candidate_name"]) for row in rows] == [
        ("梨肉", "梨(均值)"),
        ("土豆", "马铃薯[土豆，洋芋]"),
    ]
    assert {row["match_method"] for row in rows} == {"reference_alias"}
    assert {row["reason"] for row in rows} == {"alias_candidate_requires_review"}
    assert {row["review_status"] for row in rows} == {"pending"}


def test_repository_crosswalk_covers_retained_inputs_with_complete_references() -> None:
    reference_path = PROJECT_ROOT / "data/reference/ingredient_nutrition.jsonl"
    crosswalk_path = PROJECT_ROOT / "data/review/ingredient_nutrition_crosswalk.jsonl"
    retention_path = (
        PROJECT_ROOT / "data/review/ingredient_nutrition_retention_decisions.csv"
    )
    references = load_nutrition_references(reference_path)
    crosswalk = load_nutrition_crosswalk(crosswalk_path, references)
    decisions = [
        json.loads(line)
        for line in crosswalk_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with retention_path.open(encoding="utf-8", newline="") as handle:
        required_keys = {
            (
                int(row["ingredient_id"]),
                nutrition_form_from_occurrence(row["normalized_form"]),
            )
            for row in csv.DictReader(handle)
            if row["review_status"] in {"approved", "modified"}
            and row["retained_in_dish"].strip().lower() == "true"
        }

    decision_keys = {
        (int(row["ingredient_id"]), row["form"])
        for row in decisions
        if row["review_status"] in {"approved", "modified"}
    }
    assert len(decisions) == len(decision_keys) == crosswalk.approved_count == 1_786
    assert decision_keys == required_keys
    assert all(
        set(row)
        == {
            "ingredient_id",
            "ingredient_name",
            "form",
            "reference_id",
            "match_method",
            "review_status",
            "brand_name",
            "specification",
        }
        for row in decisions
    )
    assert all(
        crosswalk.get(ingredient_id, form).per_100g.complete
        for ingredient_id, form in required_keys
    )
