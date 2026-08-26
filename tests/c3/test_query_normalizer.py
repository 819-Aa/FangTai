from __future__ import annotations

import json

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


def test_invalid_output_retries_twice_then_uses_deterministic_fallback() -> None:
    llm = _FakeLLM(responses=["not json", "[]", '{"recipe_ids":[1]}'])

    rewrite = QueryNormalizer(llm).normalize(
        "给我推荐老人吃的晚餐，不要辣，30分钟内",
        ("p1",),
    )

    assert len(llm.calls) == 3
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.population_tags == ("老人",)
    assert rewrite.exclude_ingredients == ("辣椒",)
    assert rewrite.max_time_minutes == 30


def test_invalid_semantic_hard_filters_retry_then_fall_back_to_safe_plan() -> None:
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

    assert len(llm.calls) == 3
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

    assert len(llm.calls) == 3
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.exclude_ingredients == ("海鲜",)
    assert rewrite.health_constraints == ()


def test_indicator_only_health_text_retries_then_falls_back() -> None:
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

    assert len(llm.calls) == 3
    assert rewrite.meal_types == ("晚餐",)
    assert rewrite.health_constraints == ()


def test_model_failure_fallback_preserves_include_and_exclude() -> None:
    llm = _FakeLLM(error=TimeoutError())

    rewrite = QueryNormalizer(llm).normalize("不要辣椒，想吃豆腐", ("p1",))

    assert len(llm.calls) == 3
    assert rewrite.include_ingredients == ("豆腐",)
    assert rewrite.exclude_ingredients == ("辣椒",)


def test_previous_query_plan_not_free_text_history_is_sent_to_model() -> None:
    llm = _FakeLLM(responses=['{"retrieval_query":"清淡晚餐"}'])
    previous = {"meal_types": ["晚餐"], "exclude_ingredients": ["辣椒"]}

    QueryNormalizer(llm).normalize("再清淡一点", ("p1",), previous_query_plan=previous)

    payload = json.loads(llm.calls[0][2])
    assert payload["previous_query_plan"] == previous
    assert "history" not in payload


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
