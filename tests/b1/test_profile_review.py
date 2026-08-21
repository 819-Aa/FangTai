from __future__ import annotations

from food_agent_v2.b1.consumer_views import RecipeFact
from food_agent_v2.b1.profile_review import (
    LLMProfileEstimator,
    ProfileCandidateCache,
    ProfileReviewInput,
    generate_profile_candidates,
)


def _input() -> ProfileReviewInput:
    return ProfileReviewInput(
        recipe_id=1,
        name="清蒸鱼",
        record_type="dish",
        ingredients_raw="鱼500克、姜10克",
        steps_raw="放入蒸锅蒸10分钟",
        label_tags=("晚餐", "老人", "清淡"),
    )


def _fact() -> RecipeFact:
    return RecipeFact(
        recipe_id=1,
        name="清蒸鱼",
        record_type="dish",
        meal_tags=("晚餐",),
        population_tags=("老人",),
        taste_tags=("清淡",),
    )


class _Estimator:
    model_id = "profile-model-v1"

    def __init__(self) -> None:
        self.calls = 0

    def estimate_batch(self, inputs):
        self.calls += 1
        return {item.recipe_id: {
            "meal_tags": ["午餐", "晚餐"],
            "dish_type_tags": ["主菜"],
            "taste_tags": ["咸鲜"],
            "cuisine_tags": ["家常"],
            "cooking_method_tags": ["蒸"],
            "texture_tags": ["软嫩"],
            "scenario_tags": ["日常"],
        } for item in inputs}


def test_profile_model_candidates_merge_raw_tags_and_never_invent_population(tmp_path) -> None:
    records = generate_profile_candidates(
        (_input(),),
        (_fact(),),
        _Estimator(),
        cache=ProfileCandidateCache(tmp_path / "cache.jsonl"),
    )

    assert records[0]["meal_tags"] == ["晚餐", "午餐"]
    assert records[0]["taste_tags"] == ["清淡", "咸鲜"]
    assert records[0]["population_tags"] == ["老人"]
    assert records[0]["review_status"] == "pending"


def test_profile_model_uses_exact_content_cache_on_resume(tmp_path) -> None:
    cache_path = tmp_path / "cache.jsonl"
    first = _Estimator()
    generate_profile_candidates(
        (_input(),), (_fact(),), first, cache=ProfileCandidateCache(cache_path)
    )
    second = _Estimator()

    generate_profile_candidates(
        (_input(),), (_fact(),), second, cache=ProfileCandidateCache(cache_path)
    )

    assert first.calls == 1
    assert second.calls == 0


def test_llm_profile_estimator_rejects_out_of_vocabulary_tags() -> None:
    class _LLM:
        def invoke(self, *_args, **_kwargs):
            return {"content": '{"profiles":[{"recipe_id":1,"meal_tags":["早餐"],'
                    '"dish_type_tags":["神秘菜"],"taste_tags":[],"cuisine_tags":[],'
                    '"cooking_method_tags":[],"texture_tags":[],"scenario_tags":[]}]}'}

    estimator = LLMProfileEstimator(_LLM(), model_id="model")

    try:
        estimator.estimate_batch((_input(),))
    except ValueError as exc:
        assert "封闭词表" in str(exc)
    else:
        raise AssertionError("越界画像标签必须被拒绝")
