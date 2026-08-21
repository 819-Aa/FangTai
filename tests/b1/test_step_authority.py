from uuid import UUID

from food_agent_v2.b1.consumer_views import RecipeStepBindingView, StructuredStep
from food_agent_v2.b1.step_time_builder import build_step_profiles_from_views


def _view(*steps: str) -> RecipeStepBindingView:
    return RecipeStepBindingView(
        build_id=UUID("11111111-1111-1111-1111-111111111111"),
        source_manifest_hash="a" * 64,
        recipe_id=7,
        ingredient_ids=(),
        steps=tuple(
            StructuredStep(step_index=index, raw_text=text)
            for index, text in enumerate(steps, start=1)
        ),
    )


def test_build_view_publishes_atoms_without_runtime_authority_metadata() -> None:
    profiles, report = build_step_profiles_from_views((_view("蒸10分钟", "装盘享用"),))

    assert report["status"] == "passed"
    assert [atom.explicit_duration_seconds for atom in profiles[0].atoms] == [600, 0]
    assert all(atom.duration_locked for atom in profiles[0].atoms)
    atom_keys = set(profiles[0].model_dump(mode="json")["atoms"][0])
    assert "confidence" not in atom_keys
    assert "time_source" not in atom_keys
    assert "minimum_seconds" not in atom_keys
    assert "maximum_seconds" not in atom_keys


def test_missing_duration_remains_unlocked_for_whole_recipe_profiler() -> None:
    profiles, _ = build_step_profiles_from_views((_view("切成细丝"),))

    atom = profiles[0].atoms[0]
    assert atom.explicit_duration_seconds is None
    assert atom.duration_locked is False
