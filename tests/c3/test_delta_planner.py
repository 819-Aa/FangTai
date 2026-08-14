"""DeltaPlanner 最小修改规划测试（L1 Task 6）。"""

from __future__ import annotations

from food_agent_v2.c3.delta_planner import DeltaPlanner
from food_agent_v2.c3.fast_intent import IntentDelta


def test_add_constraint_preserves_current_count_and_all_still_valid_dishes():
    plan = DeltaPlanner().plan(
        current_recipe_ids=(1, 2, 3),
        intent=IntentDelta(intent="add_constraint", flavor_preferences=("清淡",)),
        safe_recipe_ids={1, 2, 3, 4},
    )
    assert plan.dish_count == 3
    assert plan.locked_recipe_ids == (1, 2, 3)


def test_add_constraint_drops_invalid_dish():
    # 菜 2 违反新约束（不在 safe），仅锁定 1、3
    plan = DeltaPlanner().plan(
        current_recipe_ids=(1, 2, 3),
        intent=IntentDelta(intent="add_constraint"),
        safe_recipe_ids={1, 3, 4},
    )
    assert plan.dish_count == 3
    assert plan.locked_recipe_ids == (1, 3)


def test_replace_changes_only_named_recipe():
    plan = DeltaPlanner().plan(
        current_recipe_ids=(1, 2, 3),
        intent=IntentDelta(intent="replace", target_recipe_id=2),
        safe_recipe_ids={1, 3, 4, 5},
    )
    assert plan.dish_count == 3
    assert plan.locked_recipe_ids == (1, 3)
    assert plan.rejected_recipe_ids == (2,)


def test_reject_plan_rejects_all_current():
    plan = DeltaPlanner().plan(
        current_recipe_ids=(1, 2, 3),
        intent=IntentDelta(intent="reject_plan"),
        safe_recipe_ids={4, 5},
    )
    assert plan.dish_count == 3
    assert plan.rejected_recipe_ids == (1, 2, 3)


def test_restore_binds_existing_committed_version():
    plan = DeltaPlanner(menu_history={"v1": (4, 5, 6)}).restore(version="v1")
    assert plan.restore_version == "v1"
    assert plan.restore_recipe_ids == (4, 5, 6)
