from pathlib import Path

import pytest

from food_agent_v2.b1.schemas import StepTask
from food_agent_v2.b1.source_manifest import (
    canonical_source_manifest,
    load_verified_recipe_source,
)
from food_agent_v2.b1.step_atomizer import atomize_recipe_steps, atomize_step
from food_agent_v2.b1.step_time_builder import split_steps
from food_agent_v2.b1.time_graph_profiler import (
    RecipeTimeProfile,
    TimeGraphCache,
    load_cached_recipe_time_graph,
    time_graph_cache_key,
    time_graph_pipeline_model_id,
)
from food_agent_v2.b1.time_review_decisions import (
    apply_time_review_decisions,
    load_time_review_decisions,
)
from food_agent_v2.core.paths import PROJECT_ROOT, RECIPES_RAW


def _write_decisions(path: Path, rows: tuple[str, ...]) -> None:
    path.write_text(
        "decision_id,recipe_id,source_step_index,atom_text,occurrence,action,duration_seconds,review_status,task_type,resources,depends_on_decision_ids\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )


def test_approved_duration_is_locked_and_selected_duplicate_is_ignored(tmp_path) -> None:
    atoms = atomize_step(
        recipe_id=675,
        source_step_index=1,
        text="盖上锅盖，盖上锅盖，烧煮20分钟",
    )
    decisions_path = tmp_path / "time_decisions.csv"
    _write_decisions(
        decisions_path,
        (
            "d1,675,1,盖上锅盖,2,ignore,,approved,,,",
            "d2,675,1,盖上锅盖,1,set_duration,5,approved,,,",
        ),
    )

    reviewed = apply_time_review_decisions(
        recipe_id=675,
        atoms=atoms,
        decisions=load_time_review_decisions(decisions_path),
    )

    assert [atom.text for atom in reviewed] == ["盖上锅盖", "烧煮20分钟"]
    assert reviewed[0].explicit_duration_seconds == 5
    assert reviewed[0].duration_locked is True
    assert reviewed[1].explicit_duration_seconds == 1200


def test_pending_decision_never_changes_atoms(tmp_path) -> None:
    atoms = atomize_step(
        recipe_id=1,
        source_step_index=1,
        text="开始预热",
    )
    decisions_path = tmp_path / "time_decisions.csv"
    _write_decisions(
        decisions_path,
        ("d1,1,1,开始预热,1,set_duration,300,pending,,,",),
    )

    reviewed = apply_time_review_decisions(
        recipe_id=1,
        atoms=atoms,
        decisions=load_time_review_decisions(decisions_path),
    )

    assert reviewed == atoms


def test_duplicate_active_target_is_rejected(tmp_path) -> None:
    decisions_path = tmp_path / "time_decisions.csv"
    _write_decisions(
        decisions_path,
        (
            "d1,1,1,开始预热,1,set_duration,300,approved,,,",
            "d2,1,1,开始预热,1,set_duration,600,modified,,,",
        ),
    )

    with pytest.raises(ValueError, match="重复目标"):
        load_time_review_decisions(decisions_path)


def test_unmatched_approved_decision_fails_closed(tmp_path) -> None:
    atoms = atomize_step(
        recipe_id=1,
        source_step_index=1,
        text="开始预热",
    )
    decisions_path = tmp_path / "time_decisions.csv"
    _write_decisions(
        decisions_path,
        ("d1,1,2,开始预热,1,set_duration,300,approved,,,",),
    )

    with pytest.raises(ValueError, match="未唯一匹配"):
        apply_time_review_decisions(
            recipe_id=1,
            atoms=atoms,
            decisions=load_time_review_decisions(decisions_path),
        )


def test_approved_task_semantics_and_dependencies_are_resolved_to_atom_ids(tmp_path) -> None:
    atoms = (
        *atomize_step(recipe_id=621, source_step_index=2, text="前段烤制"),
        *atomize_step(recipe_id=621, source_step_index=2, text="后段烤制"),
        *atomize_step(recipe_id=621, source_step_index=3, text="开门翻拌一次"),
    )
    decisions_path = tmp_path / "time_decisions.csv"
    _write_decisions(
        decisions_path,
        (
            "d-first,621,2,前段烤制,1,set_duration,210,approved,unattended_equipment,oven,",
            "d-second,621,2,后段烤制,1,set_duration,210,approved,unattended_equipment,oven,d-flip",
            "d-flip,621,3,开门翻拌一次,1,set_duration,30,approved,manual,cook,d-first",
        ),
    )

    reviewed = apply_time_review_decisions(
        recipe_id=621,
        atoms=atoms,
        decisions=load_time_review_decisions(decisions_path),
    )

    assert reviewed[0].reviewed_task_type == "unattended_equipment"
    assert reviewed[0].reviewed_resources == ("oven",)
    assert reviewed[0].reviewed_depends_on is None
    assert reviewed[1].reviewed_depends_on == (reviewed[2].atom_id,)
    assert reviewed[2].reviewed_task_type == "manual"
    assert reviewed[2].reviewed_resources == ("cook",)
    assert reviewed[2].reviewed_depends_on == (reviewed[0].atom_id,)


def test_invalid_reviewed_task_resource_combination_is_rejected(tmp_path) -> None:
    decisions_path = tmp_path / "time_decisions.csv"
    _write_decisions(
        decisions_path,
        (
            "d1,621,2,前段烤制,1,set_duration,210,approved,unattended_equipment,cook,",
        ),
    )

    with pytest.raises(ValueError, match="CSV"):
        load_time_review_decisions(decisions_path)


def test_owner_approved_recipe_time_decisions_match_current_source_atoms() -> None:
    decisions = load_time_review_decisions(
        PROJECT_ROOT / "data/review/recipe_time_graph_decisions.csv"
    )
    assert len(decisions) == 49

    rows = load_verified_recipe_source(RECIPES_RAW, canonical_source_manifest())
    target_recipe_ids = {decision.recipe_id for decision in decisions}
    assert target_recipe_ids == {
        65, 269, 305, 348, 408, 621, 659, 675, 718, 840, 855,
        860, 885, 984, 1039, 1092, 1138, 1139, 1246, 1449, 1763,
        1814, 1822, 1944,
    }

    for row in rows:
        if row.recipe_id not in target_recipe_ids:
            continue
        source_steps = split_steps(row.steps_raw)
        atoms = atomize_recipe_steps(
            recipe_id=row.recipe_id,
            steps=enumerate(source_steps, start=1),
        )
        apply_time_review_decisions(
            recipe_id=row.recipe_id,
            atoms=atoms,
            decisions=decisions,
        )


def test_408_reviewed_time_graph_keeps_a_task_atom_for_each_source_step() -> None:
    decisions = load_time_review_decisions(
        PROJECT_ROOT / "data/review/recipe_time_graph_decisions.csv"
    )
    rows = load_verified_recipe_source(RECIPES_RAW, canonical_source_manifest())
    recipe = next(row for row in rows if row.recipe_id == 408)
    source_steps = split_steps(recipe.steps_raw)
    atoms = atomize_recipe_steps(
        recipe_id=recipe.recipe_id,
        steps=enumerate(source_steps, start=1),
    )

    reviewed = apply_time_review_decisions(
        recipe_id=recipe.recipe_id,
        atoms=atoms,
        decisions=decisions,
    )

    assert {atom.source_step_index for atom in reviewed} == {1, 2, 3, 4, 5, 6}


def test_408_reviewed_time_graph_cache_covers_each_source_step_and_orders_final_runs(
    tmp_path,
) -> None:
    decisions = load_time_review_decisions(
        PROJECT_ROOT / "data/review/recipe_time_graph_decisions.csv"
    )
    rows = load_verified_recipe_source(RECIPES_RAW, canonical_source_manifest())
    recipe = next(row for row in rows if row.recipe_id == 408)
    atoms = atomize_recipe_steps(
        recipe_id=recipe.recipe_id,
        steps=enumerate(split_steps(recipe.steps_raw), start=1),
    )
    reviewed = apply_time_review_decisions(
        recipe_id=recipe.recipe_id,
        atoms=atoms,
        decisions=decisions,
    )

    tasks = tuple(
        StepTask(
            atom_id=atom.atom_id,
            text=atom.text,
            duration_seconds=(120, 180, 300, 120, 600, 600)[index],
            task_type=(
                "manual" if index < 4 else "attended_equipment"
            ),
            resources=(
                ("cook", "counter") if index < 4 else ("cook", "oven")
            ),
            depends_on=(
                (),
                (),
                (reviewed[0].atom_id, reviewed[1].atom_id),
                (reviewed[2].atom_id,),
                (reviewed[3].atom_id,),
                (reviewed[4].atom_id,),
            )[index],
        )
        for index, atom in enumerate(reviewed)
    )
    cache_path = tmp_path / "recipe_time_graphs.jsonl"
    cache = TimeGraphCache(cache_path)
    pipeline_model_id = time_graph_pipeline_model_id("deepseek-chat", "deepseek-chat")
    cache.put(
        time_graph_cache_key(recipe.recipe_id, reviewed, pipeline_model_id),
        RecipeTimeProfile(
            recipe_id=recipe.recipe_id,
            recipe_name=recipe.name,
            step_tasks=tasks,
        ),
    )

    profile = load_cached_recipe_time_graph(
        recipe_id=recipe.recipe_id,
        recipe_name=recipe.name,
        atoms=reviewed,
        generator_model_id="deepseek-chat",
        verifier_model_id="deepseek-chat",
        cache=TimeGraphCache(cache_path),
    )

    assert {task.atom_id for task in profile.step_tasks} == {
        atom.atom_id for atom in reviewed
    }
    atom_id_by_source_step = {
        atom.source_step_index: atom.atom_id for atom in reviewed
    }
    task_by_atom_id = {task.atom_id: task for task in profile.step_tasks}
    assert task_by_atom_id[atom_id_by_source_step[6]].depends_on == (
        atom_id_by_source_step[5],
    )
