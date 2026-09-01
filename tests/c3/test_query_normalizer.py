from __future__ import annotations

import json

import pytest

from food_agent_v2.c3.fast_intent import FastIntentRouter
from food_agent_v2.c3.orchestrator import DeterministicRecommendationOrchestrator
from food_agent_v2.c3.query_normalizer import QueryNormalizer


class _FakeLLM:
    def __init__(self, responses=None, error: Exception | None = None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def invoke(self, role, system_prompt, user_message, **kwargs):
        self.calls.append((role, system_prompt, user_message, kwargs))
        if self.error is not None:
            raise self.error
        content = self.responses.pop(0) if self.responses else "{}"
        return {"content": content}


def test_closed_rewrite_extracts_population_meal_negation_and_time() -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "老人 晚餐 豆腐",
                    "meal_types": ["晚餐"],
                    "population_tags": ["老人"],
                    "include_ingredients": ["豆腐"],
                    "exclude_ingredients": ["辣椒"],
                    "max_time_minutes": 30,
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize(
        "给老人推荐晚餐，不要辣，想吃豆腐，30分钟内",
        ("p1",),
    )

    assert len(llm.calls) == 1
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.population_tags == ("老人",)
    assert rewrite.include_ingredients == ("豆腐",)
    assert rewrite.exclude_ingredients == ("辣椒",)
    assert rewrite.max_time_minutes == 30
    assert rewrite.time_constraint_seconds == 1800


def test_invalid_output_uses_deterministic_fallback_after_one_call() -> None:
    llm = _FakeLLM(responses=["not json", "[]", '{"recipe_ids":[1]}'])

    rewrite = QueryNormalizer(llm).normalize(
        "给我推荐老人吃的晚餐，不要辣，30分钟内",
        ("p1",),
    )

    assert len(llm.calls) == 1
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.population_tags == ("老人",)
    assert rewrite.exclude_ingredients == ("辣椒",)
    assert rewrite.max_time_minutes == 30


def test_invalid_semantic_hard_filters_fall_back_after_one_call() -> None:
    invalid_rewrite = json.dumps(
        {
            "retrieval_query": "两人 晚餐 清淡 健康",
            "meal_types": ["晚餐"],
            "population_tags": ["两人"],
            "taste_tags": ["清淡"],
            "exclude_ingredients": ["海鲜"],
            "health_constraints": ["健康"],
        },
        ensure_ascii=False,
    )
    llm = _FakeLLM(responses=[invalid_rewrite, invalid_rewrite, invalid_rewrite])

    rewrite = QueryNormalizer(llm).normalize(
        "两人晚餐，都不要海鲜，清淡健康",
        ("p1", "p2"),
    )

    assert len(llm.calls) == 1
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.taste_tags == ("清淡",)
    assert rewrite.exclude_ingredients == ("海鲜",)
    assert rewrite.population_tags == ()
    assert rewrite.health_constraints == ()


def test_non_health_taboo_does_not_count_as_health_constraint() -> None:
    invalid_rewrite = json.dumps(
        {
            "retrieval_query": "晚餐",
            "meal_types": ["晚餐"],
            "health_constraints": ["不能吃海鲜"],
        },
        ensure_ascii=False,
    )
    llm = _FakeLLM(responses=[invalid_rewrite, invalid_rewrite, invalid_rewrite])

    rewrite = QueryNormalizer(llm).normalize(
        "晚餐不要海鲜",
        ("p1",),
    )

    assert len(llm.calls) == 1
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.exclude_ingredients == ("海鲜",)
    assert rewrite.health_constraints == ()


def test_indicator_only_health_text_falls_back_after_one_call() -> None:
    invalid_rewrite = json.dumps(
        {
            "retrieval_query": "晚餐 胆固醇",
            "meal_types": ["晚餐"],
            "health_constraints": ["注意胆固醇"],
        },
        ensure_ascii=False,
    )
    llm = _FakeLLM(responses=[invalid_rewrite, invalid_rewrite, invalid_rewrite])

    rewrite = QueryNormalizer(llm).normalize(
        "晚餐注意胆固醇",
        ("p1",),
    )

    assert len(llm.calls) == 1
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.health_constraints == ()


def test_model_failure_fallback_preserves_include_and_exclude() -> None:
    llm = _FakeLLM(error=TimeoutError())

    rewrite = QueryNormalizer(llm).normalize("不要辣椒，想吃豆腐", ("p1",))

    assert len(llm.calls) == 1
    assert rewrite.include_ingredients == ("豆腐",)
    assert rewrite.exclude_ingredients == ("辣椒",)


def test_previous_query_plan_not_free_text_history_is_sent_to_model() -> None:
    llm = _FakeLLM(responses=['{"retrieval_query":"清淡晚餐"}'])
    previous = {"meal_types": ["晚餐"], "exclude_ingredients": ["辣椒"]}

    QueryNormalizer(llm).normalize("再清淡一点", ("p1",), previous_query_plan=previous)

    payload = json.loads(llm.calls[0][2])
    assert payload["previous_query_plan"] == previous
    assert "history" not in payload


def test_exact_previous_health_constraint_may_be_carried_by_model() -> None:
    previous_health = "我花生过敏"
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "清淡",
                    "taste_tags": ["清淡"],
                    "health_constraints": [previous_health],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize(
        "再清淡一点",
        ("p1",),
        previous_query_plan={"health_constraints": [previous_health]},
    )

    assert rewrite.health_constraints == (previous_health,)


def test_sparse_model_rewrite_inherits_all_previous_plan_semantics() -> None:
    """模型成功但省略旧字段时，增量轮仍必须保留上一轮硬约束。"""
    previous = {
        "rewritten_query": "晚餐 豆腐",
        "meal_types": ["晚餐"],
        "population_tags": ["老人"],
        "dish_types": ["汤"],
        "taste_tags": ["家常"],
        "cuisine_tags": ["粤式"],
        "scenario_tags": ["家庭"],
        "include_ingredients": ["豆腐"],
        "exclude_ingredients": ["花生"],
        "nutrition_goal_codes": ["low_sodium"],
        "health_exclusions": ["p1:过敏:花生"],
        "dish_count_requested": 3,
        "time_constraint_seconds": 1800,
        "time_constraint_policy": "hard",
    }
    llm = _FakeLLM(responses=[json.dumps({
        "retrieval_query": "清淡",
        "taste_tags": ["清淡"],
    }, ensure_ascii=False)])

    rewrite = QueryNormalizer(llm).normalize(
        "再清淡一点", ("p1",), previous_query_plan=previous)

    assert rewrite.retrieval_query == "晚餐 豆腐 清淡"
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.population_tags == ("老人",)
    assert rewrite.dish_types == ("汤",)
    assert rewrite.taste_tags == ("清淡", "家常")
    assert rewrite.cuisine_tags == ("粤式",)
    assert rewrite.scenario_tags == ("家庭",)
    assert rewrite.include_ingredients == ("豆腐",)
    assert rewrite.exclude_ingredients == ("花生",)
    assert rewrite.nutrition_goal_codes == ("low_sodium",)
    assert rewrite.health_constraints == ("p1:过敏:花生",)
    assert rewrite.dish_count == 3
    assert rewrite.max_time_minutes == 30


def test_model_timeout_inherits_previous_plan_and_current_hard_scalars_win() -> None:
    """模型失败也保留旧计划，但本轮明确菜数/时限优先。"""
    previous = {
        "rewritten_query": "晚餐 豆腐",
        "meal_types": ["晚餐"],
        "exclude_ingredients": ["花生"],
        "health_exclusions": ["p1:过敏:花生"],
        "dish_count_requested": 3,
        "time_constraint_seconds": 1800,
        "time_constraint_policy": "hard",
    }

    rewrite = QueryNormalizer(_FakeLLM(error=TimeoutError())).normalize(
        "再清淡一点，四菜一汤，45分钟内",
        ("p1",),
        previous_query_plan=previous,
    )

    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.exclude_ingredients == ("花生",)
    assert rewrite.health_constraints == ("p1:过敏:花生",)
    assert rewrite.dish_count == 5
    assert rewrite.max_time_minutes == 45


def test_documented_meal_alias_is_grounded() -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "晚餐 豆腐",
                    "meal_types": ["晚餐"],
                    "include_ingredients": ["豆腐"],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize("晚饭想吃豆腐", ("p1",))

    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.include_ingredients == ("豆腐",)


def test_model_meal_supplement_is_grounded_by_tonight_alias() -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "晚餐",
                    "meal_types": ["晚餐"],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize("今晚吃什么", ("p1",))

    assert len(llm.calls) == 1
    assert rewrite.retrieval_query == "晚餐"
    assert rewrite.meal_types == ("晚餐",)


def test_model_include_requires_an_independent_ingredient_mention() -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "洋葱",
                    "include_ingredients": ["葱"],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize("想吃洋葱", ("p1",))

    assert rewrite.retrieval_query == "洋葱"
    assert rewrite.include_ingredients == ("洋葱",)


def test_negated_meal_alias_invalidates_canonical_meal_grounding() -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "家常",
                    "meal_types": ["晚餐"],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize(
        "今晚不想吃晚饭，推荐家常菜",
        ("p1",),
    )

    assert rewrite.retrieval_query == "家常"
    assert rewrite.meal_types == ()
    assert rewrite.taste_tags == ("家常",)


def test_negated_controlled_facet_is_not_grounded_by_raw_mention() -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "家常",
                    "taste_tags": ["辣"],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize("不要辣，推荐家常菜", ("p1",))

    assert rewrite.taste_tags == ("家常",)
    assert rewrite.exclude_ingredients == ("辣椒",)


def test_valid_health_constraints_remain_allowed_in_semantic_rewrite() -> None:
    message = "给老人推荐晚餐，高血压也能吃"
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "老人 晚餐 高血压",
                    "meal_types": ["晚餐"],
                    "population_tags": ["老人"],
                    "health_constraints": [message],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize(message, ("p1",))

    assert len(llm.calls) == 1
    assert rewrite.population_tags == ("老人",)
    assert rewrite.health_constraints == (message,)


def test_explicit_disease_constraints_like_wei_bing_remain_allowed() -> None:
    message = "胃病的人晚餐吃什么"
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "晚餐 胃病",
                    "meal_types": ["晚餐"],
                    "health_constraints": [message],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize(message, ("p1",))

    assert len(llm.calls) == 1
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.health_constraints == (message,)


def test_explicit_allergy_constraints_remain_allowed() -> None:
    message = "晚餐不要花生，我花生过敏"
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "晚餐 花生过敏",
                    "meal_types": ["晚餐"],
                    "exclude_ingredients": ["花生"],
                    "health_constraints": [message],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize(message, ("p1",))

    assert len(llm.calls) == 1
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.exclude_ingredients == ("花生",)
    assert rewrite.health_constraints == (message,)


def test_e02_fallback_preserves_menu_structure_taste_and_hard_time() -> None:
    llm = _FakeLLM(error=TimeoutError())

    rewrite = QueryNormalizer(llm).normalize(
        "推荐三菜一汤，家常口味，45分钟内",
        ("p1",),
    )

    assert len(llm.calls) == 1
    assert rewrite.dish_count == 4
    assert rewrite.dish_types == ("汤",)
    assert rewrite.taste_tags == ("家常",)
    assert rewrite.max_time_minutes == 45


def test_sparse_model_arrays_merge_with_deterministic_explicit_fields() -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "家常 日常 晚餐 豆腐",
                    "meal_types": ["晚餐"],
                    "scenario_tags": ["日常"],
                    "max_time_minutes": 60,
                    "dish_count": 5,
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize(
        "日常晚餐推荐三菜一汤，家常口味，想吃豆腐，45分钟内",
        ("p1",),
    )

    assert len(llm.calls) == 1
    assert rewrite.retrieval_query == "家常 日常 晚餐 豆腐"
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.dish_types == ("汤",)
    assert rewrite.taste_tags == ("家常",)
    assert rewrite.scenario_tags == ("日常",)
    assert rewrite.include_ingredients == ("豆腐",)
    assert rewrite.max_time_minutes == 45
    assert rewrite.dish_count == 4


def test_controlled_vocab_accepts_soup_and_homestyle() -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "家常 汤",
                    "dish_types": ["汤"],
                    "taste_tags": ["家常"],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize("想吃家常汤", ("p1",))

    assert len(llm.calls) == 1
    assert rewrite.dish_types == ("汤",)
    assert rewrite.taste_tags == ("家常",)
    assert rewrite.include_ingredients == ()


def test_dish_request_is_not_misread_as_hard_ingredient() -> None:
    rewrite = QueryNormalizer(_FakeLLM(error=TimeoutError())).normalize("想吃拉面", ("p1",))

    assert rewrite.include_ingredients == ()


@pytest.mark.parametrize(
    ("field", "value"),
    (("meal_types", "dinner"), ("dish_types", "三菜一汤")),
)
def test_invalid_controlled_vocab_falls_back_without_retry(field: str, value: str) -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "汤",
                    field: [value],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize("今晚吃啥", ("p1",))

    assert len(llm.calls) == 1
    assert rewrite.retrieval_query == "今晚吃啥"
    assert rewrite.meal_types == ()
    assert rewrite.dish_types == ()


def test_contaminated_positive_query_falls_back_and_keeps_health_fields() -> None:
    message = "晚餐不要花生，我花生过敏"
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "晚餐 不要花生 花生过敏",
                    "meal_types": ["晚餐"],
                    "exclude_ingredients": ["花生"],
                    "health_constraints": [message],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize(message, ("p1",))

    assert len(llm.calls) == 1
    assert all(token not in rewrite.retrieval_query for token in ("花生", "过敏", "不要"))
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.exclude_ingredients == ("花生",)
    assert rewrite.health_constraints == (message,)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("meal_types", ["晚餐"]),
        ("population_tags", ["老人"]),
        ("dish_types", ["汤"]),
        ("taste_tags", ["清淡"]),
        ("cuisine_tags", ["川味"]),
        ("scenario_tags", ["宴客"]),
        ("include_ingredients", ["花生"]),
        ("exclude_ingredients", ["花生"]),
        ("nutrition_goal_codes", ["low_sodium"]),
        ("max_time_minutes", 45),
        ("dish_count", 4),
    ),
)
def test_ungrounded_model_field_forces_whole_result_fallback(field: str, value) -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {"retrieval_query": "家常", field: value},
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize("推荐家常菜", ("p1",))

    assert rewrite.retrieval_query == "家常"
    assert rewrite.meal_types == ()
    assert rewrite.population_tags == ()
    assert rewrite.dish_types == ()
    assert rewrite.taste_tags == ("家常",)
    assert rewrite.cuisine_tags == ()
    assert rewrite.scenario_tags == ()
    assert rewrite.include_ingredients == ()
    assert rewrite.exclude_ingredients == ()
    assert rewrite.nutrition_goal_codes == ()
    assert rewrite.max_time_minutes is None
    assert rewrite.dish_count is None


@pytest.mark.parametrize("message", ("我不能吃花生", "别吃花生"))
def test_cannot_eat_language_preserves_raw_health_and_exclusion(message: str) -> None:
    rewrite = QueryNormalizer(_FakeLLM(error=TimeoutError())).normalize(message, ("p1",))

    assert rewrite.exclude_ingredients == ("花生",)
    assert rewrite.health_constraints == (message,)
    assert "花生" not in rewrite.retrieval_query


@pytest.mark.parametrize("llm", (_FakeLLM(error=TimeoutError()), _FakeLLM(responses=["not json"])))
def test_not_want_to_eat_does_not_parse_embedded_want_as_include(llm: _FakeLLM) -> None:
    rewrite = QueryNormalizer(llm).normalize("不想吃花生，想吃豆腐", ("p1",))

    assert rewrite.exclude_ingredients == ("花生",)
    assert rewrite.include_ingredients == ("豆腐",)
    assert rewrite.retrieval_query == "豆腐"
    assert "花生" not in rewrite.retrieval_query
    assert "不想吃" not in rewrite.retrieval_query


def test_model_positive_query_with_not_want_to_eat_falls_back_to_safe_query() -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "不想吃花生 豆腐",
                    "include_ingredients": ["豆腐"],
                    "exclude_ingredients": ["花生"],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize("不想吃花生，想吃豆腐", ("p1",))

    assert rewrite.retrieval_query == "豆腐"
    assert rewrite.include_ingredients == ("豆腐",)
    assert rewrite.exclude_ingredients == ("花生",)


def test_allergy_only_fallback_derives_allergen_and_rejects_positive_contamination() -> None:
    message = "我花生过敏"
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "花生",
                    "exclude_ingredients": ["花生"],
                    "health_constraints": [message],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize(message, ("p1",))

    assert rewrite.retrieval_query == "家常菜"
    assert rewrite.exclude_ingredients == ("花生",)
    assert rewrite.health_constraints == (message,)


@pytest.mark.parametrize("message", ("不要汤", "不能吃汤"))
def test_negated_soup_is_absent_from_fallback_positive_semantics(message: str) -> None:
    rewrite = QueryNormalizer(_FakeLLM(error=TimeoutError())).normalize(message, ("p1",))

    assert rewrite.dish_types == ()
    assert "汤" not in rewrite.retrieval_query


def test_ambiguous_include_phrase_does_not_create_hard_ingredient() -> None:
    rewrite = QueryNormalizer(_FakeLLM(error=TimeoutError())).normalize(
        "想吃清淡的豆腐",
        ("p1",),
    )

    assert rewrite.include_ingredients == ()


def test_excluded_term_does_not_contaminate_distinct_longer_query_token() -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "洋葱",
                    "exclude_ingredients": ["葱"],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize("不要葱，可以吃洋葱", ("p1",))

    assert rewrite.retrieval_query == "洋葱"
    assert rewrite.exclude_ingredients == ("葱",)


@pytest.mark.parametrize("model_query", ("花生汤", "花生酱拌面"))
def test_excluded_term_at_compound_token_start_forces_fallback(model_query: str) -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": model_query,
                    "exclude_ingredients": ["花生"],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize("不要花生，想吃汤", ("p1",))

    assert rewrite.retrieval_query == "汤"
    assert rewrite.exclude_ingredients == ("花生",)


def test_excluded_term_inside_compound_model_query_forces_clean_fallback() -> None:
    llm = _FakeLLM(
        responses=[
            json.dumps(
                {
                    "retrieval_query": "老北京花生酱拌面",
                    "include_ingredients": ["豆腐"],
                    "exclude_ingredients": ["花生"],
                },
                ensure_ascii=False,
            )
        ]
    )

    rewrite = QueryNormalizer(llm).normalize("不要花生，想吃豆腐", ("p1",))

    assert rewrite.retrieval_query == "豆腐"
    assert rewrite.exclude_ingredients == ("花生",)


def test_deterministic_fallback_removes_excluded_term_inside_compound_query() -> None:
    rewrite = QueryNormalizer(_FakeLLM(error=TimeoutError())).normalize(
        "不要花生，想吃老北京花生酱拌面",
        ("p1",),
    )

    assert rewrite.exclude_ingredients == ("花生",)
    assert "花生" not in rewrite.retrieval_query
    assert rewrite.retrieval_query == "家常菜"


@pytest.mark.parametrize("message", ("我花生过敏", "晚餐我花生过敏"))
def test_allergy_extraction_uses_local_noun_after_sentence_prefix(message: str) -> None:
    rewrite = QueryNormalizer(_FakeLLM(error=TimeoutError())).normalize(message, ("p1",))

    assert rewrite.exclude_ingredients == ("花生",)


def test_validated_rewrite_is_the_only_query_plan_and_retrieval_source() -> None:
    orchestrator = DeterministicRecommendationOrchestrator(llm=object())
    routed = FastIntentRouter.route("给我推荐老人吃的晚餐", ("p1",))
    rewrite = QueryNormalizer(
        _FakeLLM(
            responses=[
                json.dumps(
                    {
                        "retrieval_query": "老人 晚餐",
                        "meal_types": ["晚餐"],
                        "population_tags": ["老人"],
                    },
                    ensure_ascii=False,
                )
            ]
        )
    ).normalize("给我推荐老人吃的晚餐", ("p1",))

    intent = orchestrator._apply_semantic_rewrite(
        routed,
        rewrite,
        ("p1",),
        has_current_menu=False,
    )
    plan = orchestrator._build_query_plan(
        intent,
        "11111111-1111-1111-1111-111111111111",
        ["p1"],
    )

    assert plan.rewritten_query == "老人 晚餐"
    assert plan.meal_types == ("晚餐",)
    assert plan.population_tags == ("老人",)
    assert orchestrator._retrieval_query(intent) == "老人 晚餐"
