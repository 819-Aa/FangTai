from pathlib import Path

import pytest

from food_agent_v2.b1.source_manifest import (
    canonical_source_manifest,
    load_verified_recipe_source,
)
from food_agent_v2.b1.step_atomizer import atomize_recipe_steps, atomize_step
from food_agent_v2.b1.step_time_builder import split_steps
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
    assert len(decisions) == 44

    rows = load_verified_recipe_source(RECIPES_RAW, canonical_source_manifest())
    target_recipe_ids = {decision.recipe_id for decision in decisions}
    assert target_recipe_ids == {
        65, 269, 305, 348, 408, 621, 659, 675, 718, 840, 855,
        860, 885, 1039, 1092, 1139, 1246, 1449, 1814, 1822, 1944,
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
