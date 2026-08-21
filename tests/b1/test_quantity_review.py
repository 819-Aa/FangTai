from decimal import Decimal
from uuid import UUID

from food_agent_v2.b1.consumer_views import (
    NutritionOccurrenceInput,
    RecipeFact,
    RecipeNutritionInputView,
)
from food_agent_v2.b1.quantity_normalizer import MeasureRuleIndex, QuantityDecisionIndex
from food_agent_v2.b1.quantity_review import (
    LLMQuantityEstimator,
    RecipeQuantityReviewContext,
    build_quantity_review_contexts,
    generate_quantity_candidates,
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


def test_review_contexts_only_include_unresolved_quantities() -> None:
    view = RecipeNutritionInputView(
        build_id=UUID("00000000-0000-0000-0000-000000000001"),
        source_manifest_hash="a" * 64,
        recipe_id=1,
        ingredients=(
            NutritionOccurrenceInput("1-1", 10, "100克", "克", "explicit", "面粉"),
            NutritionOccurrenceInput("1-2", 20, "少许", None, "unknown", "盐"),
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
