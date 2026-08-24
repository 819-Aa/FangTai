import csv
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest

import food_agent_v2.b1.quantity_review as quantity_review
from food_agent_v2.b1.consumer_views import (
    NutritionOccurrenceInput,
    RecipeFact,
    RecipeNutritionInputView,
)
from food_agent_v2.b1.quantity_normalizer import MeasureRuleIndex, QuantityDecisionIndex
from food_agent_v2.b1.quantity_review import (
    LLMQuantityEstimator,
    QuantityEstimateCache,
    RecipeQuantityReviewContext,
    build_quantity_review_contexts,
    generate_quantity_candidates,
    load_quantity_review_candidates,
    write_quantity_candidates,
)


def _pending(occurrence_id: str, name: str) -> NutritionOccurrenceInput:
    return NutritionOccurrenceInput(
        occurrence_id=occurrence_id,
        ingredient_id=10,
        quantity_raw="少许",
        unit_raw=None,
        quantity_status="unknown",
        ingredient_name=name,
        normalized_form="",
        usage_code="seasoning",
        fuzzy_token_class="small_amount",
        retained_in_dish=True,
    )


def test_one_recipe_uses_one_estimator_call_and_keeps_candidates_pending(tmp_path) -> None:
    class _Estimator:
        def __init__(self) -> None:
            self.calls = 0

        def estimate(self, context):
            self.calls += 1
            return {"1-1": Decimal("2"), "1-2": Decimal("5")}

    estimator = _Estimator()
    context = RecipeQuantityReviewContext(
        recipe_id=1,
        recipe_name="测试菜",
        step_context="加入少许盐和若干葱花",
        ingredients=(_pending("1-1", "盐"), _pending("1-2", "葱花")),
    )

    candidates = generate_quantity_candidates((context,), estimator)
    output = tmp_path / "quantities.csv"
    write_quantity_candidates(candidates, output)

    assert estimator.calls == 1
    assert [item.candidate_grams for item in candidates] == [Decimal("2"), Decimal("5")]
    assert {item.review_status for item in candidates} == {"pending"}
    assert ",pending" in output.read_text(encoding="utf-8")


def test_quantity_estimates_resume_from_exact_content_cache(tmp_path) -> None:
    class _Estimator:
        def __init__(self) -> None:
            self.calls = 0

        def estimate(self, _context):
            self.calls += 1
            return {"1-1": Decimal("3")}

    context = RecipeQuantityReviewContext(
        recipe_id=1,
        recipe_name="测试菜",
        step_context="加入少许盐",
        ingredients=(_pending("1-1", "盐"),),
    )
    cache_path = tmp_path / "quantity-cache.jsonl"
    first_estimator = _Estimator()
    generate_quantity_candidates(
        (context,),
        first_estimator,
        cache=QuantityEstimateCache(cache_path),
        model_id="model-v1",
    )
    second_estimator = _Estimator()

    result = generate_quantity_candidates(
        (context,),
        second_estimator,
        cache=QuantityEstimateCache(cache_path),
        model_id="model-v1",
    )

    assert first_estimator.calls == 1
    assert second_estimator.calls == 0
    assert result[0].candidate_grams == Decimal("3")


def test_review_contexts_only_include_unresolved_quantities() -> None:
    view = RecipeNutritionInputView(
        build_id=UUID("00000000-0000-0000-0000-000000000001"),
        source_manifest_hash="a" * 64,
        recipe_id=1,
        ingredients=(
                NutritionOccurrenceInput(
                    "1-1",
                    10,
                    "100克",
                    "克",
                    "explicit",
                    "面粉",
                    usage_code="main",
                    retained_in_dish=True,
                ),
                NutritionOccurrenceInput(
                    "1-2",
                    20,
                    "少许",
                    None,
                    "unknown",
                    "盐",
                    usage_code="seasoning",
                    fuzzy_token_class="small_amount",
                    retained_in_dish=True,
                ),
        ),
    )
    fact = RecipeFact(
        recipe_id=1,
        name="测试饼",
        record_type="dish",
        step_segments=("面粉加水揉匀", "加入少许盐煎熟"),
    )

    contexts = build_quantity_review_contexts(
        (view,), (fact,), MeasureRuleIndex(()), QuantityDecisionIndex(())
    )

    assert len(contexts) == 1
    assert [item.occurrence_id for item in contexts[0].ingredients] == ["1-2"]
    assert contexts[0].step_context == "面粉加水揉匀\n加入少许盐煎熟"


def test_llm_estimator_requests_strict_whole_recipe_json_once() -> None:
    class _LLM:
        def __init__(self) -> None:
            self.calls = []

        def invoke(self, role, system_prompt, user_message, response_format=None):
            self.calls.append((role, system_prompt, user_message, response_format))
            return {"content": '{"grams":{"1-1":2.5,"1-2":"4"}}'}

    llm = _LLM()
    estimator = LLMQuantityEstimator(llm)
    context = RecipeQuantityReviewContext(
        recipe_id=1,
        recipe_name="测试菜",
        step_context="加入盐和葱",
        ingredients=(_pending("1-1", "盐"), _pending("1-2", "葱")),
    )

    result = estimator.estimate(context)

    assert result == {"1-1": Decimal("2.5"), "1-2": Decimal("4")}
    assert len(llm.calls) == 1
    assert llm.calls[0][0] == "quantity_estimation"


@pytest.mark.parametrize("grams", ("NaN", "Infinity", "-Infinity"))
def test_model_quantity_candidates_reject_non_finite_grams(grams: str) -> None:
    class _Estimator:
        def estimate(self, _context):
            return {"1-1": Decimal(grams)}

    context = RecipeQuantityReviewContext(
        recipe_id=1,
        recipe_name="测试菜",
        step_context="加入少许盐",
        ingredients=(_pending("1-1", "盐"),),
    )

    with pytest.raises(ValueError, match="候选克重必须为有限正数"):
        generate_quantity_candidates((context,), _Estimator())


def test_model_candidate_writer_validates_pending_status_before_truncation(tmp_path) -> None:
    candidate = generate_quantity_candidates(
        (
            RecipeQuantityReviewContext(
                recipe_id=1,
                recipe_name="测试菜",
                step_context="加入少许盐",
                ingredients=(_pending("1-1", "盐"),),
            ),
        ),
        type("Estimator", (), {"estimate": lambda _self, _context: {"1-1": Decimal("2")}})(),
    )[0]
    output = tmp_path / "model-candidates.csv"
    output.write_text("preserve me", encoding="utf-8")

    with pytest.raises(ValueError, match="pending"):
        write_quantity_candidates((replace(candidate, review_status="approved"),), output)

    assert output.read_text(encoding="utf-8") == "preserve me"


def test_model_candidate_writer_escapes_formula_text_fields(tmp_path) -> None:
    candidate = generate_quantity_candidates(
        (
            RecipeQuantityReviewContext(
                recipe_id=1,
                recipe_name="\n@formula",
                step_context="加入少许盐",
                ingredients=(_pending("1-1", "盐"),),
            ),
        ),
        type("Estimator", (), {"estimate": lambda _self, _context: {"1-1": Decimal("2")}})(),
    )[0]
    output = tmp_path / "model-candidates.csv"

    write_quantity_candidates((candidate,), output)

    with output.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["recipe_name"] == "'\n@formula"


def test_model_candidate_writer_refuses_formal_rule_target_before_opening(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(quantity_review, "PROJECT_ROOT", tmp_path)
    formal = tmp_path / "data" / "review" / "ingredient_measure_rules.csv"
    formal.parent.mkdir(parents=True)
    formal.write_text("preserve formal", encoding="utf-8")
    candidate = generate_quantity_candidates(
        (
            RecipeQuantityReviewContext(
                recipe_id=1,
                recipe_name="测试菜",
                step_context="加入少许盐",
                ingredients=(_pending("1-1", "盐"),),
            ),
        ),
        type("Estimator", (), {"estimate": lambda _self, _context: {"1-1": Decimal("2")}})(),
    )[0]

    with pytest.raises(ValueError, match="正式计量规则文件"):
        write_quantity_candidates((candidate,), formal)

    assert formal.read_text(encoding="utf-8") == "preserve formal"


@pytest.mark.parametrize(
    "field",
    (
        "occurrence_id",
        "recipe_name",
        "ingredient_name",
        "normalized_form",
        "normalized_unit",
        "usage_code",
        "fuzzy_token_class",
        "raw_quantity",
        "step_context",
        "deterministic_calculation",
        "candidate_basis",
        "review_status",
    ),
)
def test_model_writer_rejects_non_string_text_fields_before_opening(tmp_path, field) -> None:
    output = tmp_path / "model-candidates.csv"
    output.write_text("preserve me", encoding="utf-8")
    candidate = generate_quantity_candidates(
        (
            RecipeQuantityReviewContext(
                recipe_id=1,
                recipe_name="测试菜",
                step_context="加入少许盐",
                ingredients=(_pending("1-1", "盐"),),
            ),
        ),
        type("Estimator", (), {"estimate": lambda _self, _context: {"1-1": Decimal("2")}})(),
    )[0]

    with pytest.raises(ValueError, match="文本字段"):
        write_quantity_candidates((replace(candidate, **{field: 1}),), output)

    assert output.read_text(encoding="utf-8") == "preserve me"


def test_model_writer_keeps_existing_queue_when_serialization_fails(tmp_path, monkeypatch) -> None:
    output = tmp_path / "model-candidates.csv"
    output.write_text("preserve me", encoding="utf-8")
    candidate = generate_quantity_candidates(
        (
            RecipeQuantityReviewContext(
                recipe_id=1,
                recipe_name="测试菜",
                step_context="加入少许盐",
                ingredients=(_pending("1-1", "盐"),),
            ),
        ),
        type("Estimator", (), {"estimate": lambda _self, _context: {"1-1": Decimal("2")}})(),
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
        write_quantity_candidates((candidate,), output)

    assert output.read_text(encoding="utf-8") == "preserve me"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("occurrence_id", ""),
        ("recipe_id", True),
        ("recipe_name", "  "),
        ("ingredient_name", ""),
        ("raw_quantity", 1),
        ("step_context", 1),
        ("deterministic_calculation", ""),
        ("candidate_basis", ""),
        ("candidate_grams", Decimal("NaN")),
        ("ingredient_id", "=1+1"),
        ("normalized_form", None),
        ("normalized_unit", 1),
        ("usage_code", 1),
        ("fuzzy_token_class", 1),
        ("decision_grams", Decimal("0")),
        ("review_status", "approved"),
    ),
)
def test_model_writer_rejects_every_invalid_dataclass_field_before_opening(
    tmp_path, field, value
) -> None:
    output = tmp_path / "model-candidates.csv"
    original = b"preserve me\r\n"
    output.write_bytes(original)
    candidate = generate_quantity_candidates(
        (
            RecipeQuantityReviewContext(
                recipe_id=1,
                recipe_name="测试菜",
                step_context="加入少许盐",
                ingredients=(_pending("1-1", "盐"),),
            ),
        ),
        type("Estimator", (), {"estimate": lambda _self, _context: {"1-1": Decimal("2")}})(),
    )[0]

    with pytest.raises(ValueError):
        write_quantity_candidates((replace(candidate, **{field: value}),), output)

    assert output.read_bytes() == original


def test_model_writer_rejects_unknown_usage_enum_before_opening(tmp_path) -> None:
    output = tmp_path / "model-candidates.csv"
    original = b"preserve me\r\n"
    output.write_bytes(original)
    candidate = generate_quantity_candidates(
        (
            RecipeQuantityReviewContext(
                recipe_id=1,
                recipe_name="测试菜",
                step_context="加入少许盐",
                ingredients=(_pending("1-1", "盐"),),
            ),
        ),
        type("Estimator", (), {"estimate": lambda _self, _context: {"1-1": Decimal("2")}})(),
    )[0]

    with pytest.raises(ValueError, match="usage_code"):
        write_quantity_candidates((replace(candidate, usage_code="unknown"),), output)

    assert output.read_bytes() == original


def test_model_writer_accepts_cooking_liquid_usage_enum(tmp_path) -> None:
    candidate = generate_quantity_candidates(
        (
            RecipeQuantityReviewContext(
                recipe_id=1,
                recipe_name="测试菜",
                step_context="加入汤汁",
                ingredients=(_pending("1-1", "汤汁"),),
            ),
        ),
        type("Estimator", (), {"estimate": lambda _self, _context: {"1-1": Decimal("2")}})(),
    )[0]
    output = tmp_path / "model-candidates.csv"

    write_quantity_candidates((replace(candidate, usage_code="cooking_liquid"),), output)

    assert "cooking_liquid" in output.read_text(encoding="utf-8")


def _candidate_view(*, occurrence_id: str = "1-1", raw_quantity: str = "1个"):
    return RecipeNutritionInputView(
        build_id=UUID("00000000-0000-0000-0000-000000000001"),
        source_manifest_hash="a" * 64,
        recipe_id=1,
        ingredients=(
            NutritionOccurrenceInput(
                occurrence_id=occurrence_id,
                ingredient_id=10,
                quantity_raw=raw_quantity,
                unit_raw=None,
                quantity_status="explicit",
                ingredient_name="苹果",
                normalized_form="带皮",
                usage_code="main",
                fuzzy_token_class=None,
                retained_in_dish=True,
            ),
        ),
    )


def _candidate_file(path, *, occurrence_id="1-1", candidate_grams="120"):
    path.write_text(
        "occurrence_id,recipe_id,recipe_name,ingredient_name,raw_quantity,step_context,"
        "deterministic_calculation,candidate_basis,candidate_grams,decision_grams,review_status\n"
        f"{occurrence_id},1,测试菜,苹果,1个,加入苹果,not_available,whole_recipe_context_model,"
        f"{candidate_grams},,pending\n",
        encoding="utf-8",
    )


def test_quantity_rule_loader_enriches_legacy_rows_from_current_occurrence(tmp_path) -> None:
    source = tmp_path / "legacy.csv"
    _candidate_file(source)
    fact = RecipeFact(recipe_id=1, name="测试菜", record_type="dish")

    candidates = load_quantity_review_candidates(source, type("Views", (), {"nutrition_views": (_candidate_view(),)})(), (fact,))

    assert candidates[0].ingredient_id == 10
    assert candidates[0].normalized_form == "带皮"
    assert candidates[0].usage_code == "main"
    assert candidates[0].candidate_grams == Decimal("120")
    assert candidates[0].review_status == "pending"


@pytest.mark.parametrize("value", ("NaN", "Infinity", "0", "-1"))
def test_quantity_rule_loader_rejects_unknown_or_nonfinite_candidates(tmp_path, value) -> None:
    source = tmp_path / "legacy.csv"
    _candidate_file(source, candidate_grams=value)
    fact = RecipeFact(recipe_id=1, name="测试菜", record_type="dish")
    views = type("Views", (), {"nutrition_views": (_candidate_view(),)})()

    with pytest.raises(ValueError, match="有限正数"):
        load_quantity_review_candidates(source, views, (fact,))

    unknown = tmp_path / "unknown.csv"
    _candidate_file(unknown, occurrence_id="9-9")
    with pytest.raises(ValueError, match="未知 occurrence_id"):
        load_quantity_review_candidates(unknown, views, (fact,))


def test_quantity_rule_loader_rejects_extra_or_missing_cells(tmp_path) -> None:
    source = tmp_path / "extra.csv"
    _candidate_file(source)
    source.write_text(source.read_text(encoding="utf-8").replace("pending\n", "pending,extra\n"), encoding="utf-8")
    views = type("Views", (), {"nutrition_views": (_candidate_view(),)})()
    fact = RecipeFact(recipe_id=1, name="测试菜", record_type="dish")
    with pytest.raises(ValueError, match="列|表头"):
        load_quantity_review_candidates(source, views, (fact,))

    missing = tmp_path / "missing.csv"
    _candidate_file(missing)
    missing.write_text(missing.read_text(encoding="utf-8").replace(",,pending\n", ",\n"), encoding="utf-8")
    with pytest.raises(ValueError, match="列|非法"):
        load_quantity_review_candidates(missing, views, (fact,))


def test_quantity_rule_loader_rejects_duplicate_occurrence_ids(tmp_path) -> None:
    source = tmp_path / "duplicate.csv"
    _candidate_file(source)
    source.write_text(source.read_text(encoding="utf-8") + source.read_text(encoding="utf-8").splitlines(True)[1], encoding="utf-8")
    views = type("Views", (), {"nutrition_views": (_candidate_view(),)})()
    fact = RecipeFact(recipe_id=1, name="测试菜", record_type="dish")
    with pytest.raises(ValueError, match="重复"):
        load_quantity_review_candidates(source, views, (fact,))


def test_quantity_rule_loader_rejects_v2_metadata_conflict(tmp_path) -> None:
    source = tmp_path / "v2.csv"
    source.write_text(
        "occurrence_id,recipe_id,recipe_name,ingredient_id,ingredient_name,normalized_form,"
        "normalized_unit,usage_code,fuzzy_token_class,raw_quantity,step_context,"
        "deterministic_calculation,candidate_basis,candidate_grams,decision_grams,review_status\n"
        "1-1,1,测试菜,10,苹果,错误形态,,main,,1个,加入苹果,not_available,"
        "whole_recipe_context_model,120,,pending\n",
        encoding="utf-8",
    )
    views = type("Views", (), {"nutrition_views": (_candidate_view(),)})()
    fact = RecipeFact(recipe_id=1, name="测试菜", record_type="dish")
    with pytest.raises(ValueError, match="normalized_form"):
        load_quantity_review_candidates(source, views, (fact,))


@pytest.mark.parametrize("filename", (
    "ingredient_measure_rules.csv",
    "ingredient_quantity_decisions.csv",
    "ingredient_nutrition_usage_decisions.csv",
    "ingredient_nutrition_retention_decisions.csv",
    "ingredient_edible_fraction_rules.csv",
    "ingredient_edible_fraction_decisions.csv",
))
def test_quantity_writer_refuses_all_formal_targets(tmp_path, monkeypatch, filename) -> None:
    monkeypatch.setattr(quantity_review, "PROJECT_ROOT", tmp_path)
    formal = tmp_path / "data" / "review" / filename
    formal.parent.mkdir(parents=True, exist_ok=True)
    formal.write_text("preserve", encoding="utf-8")
    candidate = generate_quantity_candidates(
        (RecipeQuantityReviewContext(
            recipe_id=1, recipe_name="测试菜", step_context="加入盐",
            ingredients=(_pending("1-1", "盐"),),
        ),),
        type("Estimator", (), {"estimate": lambda _self, _context: {"1-1": Decimal("2")}})(),
    )[0]

    with pytest.raises(ValueError, match="正式"):
        write_quantity_candidates((candidate,), formal)
    assert formal.read_text(encoding="utf-8") == "preserve"
