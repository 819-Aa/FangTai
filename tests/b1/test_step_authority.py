from uuid import UUID

from food_agent_v2.b1.consumer_views import RecipeStepBindingView, StructuredStep
from food_agent_v2.b1.step_time_builder import build_step_profiles_from_views


def _view(raw_text: str) -> RecipeStepBindingView:
    return RecipeStepBindingView(
        build_id=UUID("11111111-1111-1111-1111-111111111111"),
        source_manifest_hash="a" * 64,
        recipe_id=1,
        ingredient_ids=(),
        steps=(StructuredStep(step_index=1, raw_text=raw_text),),
    )


def test_explicit_duration_can_produce_strict_boolean() -> None:
    profiles, _ = build_step_profiles_from_views(
        (_view("蒸10分钟"),),
        strict_limit_seconds_by_recipe={1: 900},
    )

    assert profiles[0].steps[0].duration_seconds == 600
    assert profiles[0].steps[0].time_source == "explicit"
    assert profiles[0].authority == "deterministic_high"
    assert profiles[0].strict_time_feasible is True


def test_llm_estimate_is_low_authority_and_strict_result_stays_unknown() -> None:
    profiles, _ = build_step_profiles_from_views(
        (_view("翻炒至熟"),),
        model_estimates={(1, 1): 300},
        strict_limit_seconds_by_recipe={1: 900},
    )

    assert profiles[0].steps[0].duration_seconds == 300
    assert profiles[0].steps[0].time_source == "llm_estimate"
    assert profiles[0].steps[0].confidence == "low"
    assert profiles[0].authority == "model_estimate"
    assert profiles[0].strict_time_feasible == "unknown"


def test_duration_range_is_not_double_counted() -> None:
    profiles, _ = build_step_profiles_from_views((_view("蒸10-20分钟"),))

    assert profiles[0].steps[0].duration_min_seconds == 600
    assert profiles[0].steps[0].duration_max_seconds == 1200
    assert profiles[0].steps[0].duration_seconds == 900
    assert profiles[0].steps[0].time_source == "derived_from_range"


def test_empty_steps_have_no_duration_and_never_gain_strict_authority() -> None:
    empty = RecipeStepBindingView(
        build_id=UUID("11111111-1111-1111-1111-111111111111"),
        source_manifest_hash="a" * 64,
        recipe_id=1,
        ingredient_ids=(),
        steps=(),
    )
    profiles, _ = build_step_profiles_from_views((empty,), strict_limit_seconds_by_recipe={1: 900})

    assert profiles[0].total_duration_seconds is None
    assert profiles[0].authority == "deterministic_partial"
    assert profiles[0].strict_time_feasible == "unknown"
