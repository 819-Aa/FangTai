"""T15 C2 菜单硬约束测试。

空安全候选 → 无可行；菜数不足 → 无可行（不自动降）；严格时间只收 true；
锁定/拒绝菜生效。
"""

from types import SimpleNamespace

from food_agent_v2.c2 import MenuHardConstraints, MenuPlanner


class FakeB5:
    def __init__(self, per_recipe_seconds: int = 300) -> None:
        self._seconds = per_recipe_seconds

    def get_recipe_time_profile(self, rid: int):
        return SimpleNamespace(total_active_seconds=200, total_equipment_seconds=100)

    def compute_menu_schedule(self, recipe_ids, time_limit_minutes=None):
        makespan = self._seconds * len(recipe_ids)
        if time_limit_minutes is not None:
            feasible = makespan <= time_limit_minutes * 60
        else:
            feasible = "unknown"
        return SimpleNamespace(
            makespan_seconds=makespan, strict_time_feasible=feasible, time_source="task_graph"
        )


class FakeB6:
    def __init__(self, available: bool = True, score: float = 0.8) -> None:
        self._available = available
        self._score = score

    def score_recipe(self, rid: int):
        return SimpleNamespace(
            available=self._available,
            weighted_total=self._score if self._available else None,
        )

    def score_candidates(self, safe_recipe_ids, goal_codes=()):
        return {rid: self.score_recipe(rid) for rid in safe_recipe_ids}


def make_planner(count: int = 8, b6_available: bool = True) -> MenuPlanner:
    ids = list(range(1, count + 1))
    planner = MenuPlanner(b5=FakeB5(), b6=FakeB6(available=b6_available))
    planner.set_safe_candidates(ids)
    planner.set_recipe_features(
        {rid: {"name": f"菜{rid}", "preference_score": 0.5, "fields": {}} for rid in ids}
    )
    return planner


def make_typed_planner() -> MenuPlanner:
    planner = MenuPlanner(b5=FakeB5(), b6=FakeB6())
    planner.set_safe_candidates([1, 2, 3, 4, 5, 6])
    planner.set_recipe_features({
        1: {"name": "番茄蛋汤", "preference_score": 0.2, "fields": {}},
        2: {"name": "紫菜汤", "preference_score": 0.1, "fields": {}},
        3: {"name": "红柚果茶", "preference_score": 1.0, "fields": {}},
        4: {"name": "榴莲冰淇淋", "preference_score": 1.0, "fields": {}},
        5: {"name": "青椒肉丝", "preference_score": 0.8, "fields": {}},
        6: {"name": "清炒时蔬", "preference_score": 0.7, "fields": {}},
    })
    return planner


class TestMenuHardConstraints:
    def test_empty_safe_candidates_no_feasible(self) -> None:
        planner = MenuPlanner(b5=FakeB5(), b6=FakeB6())
        planner.set_safe_candidates([])
        assert planner.plan(MenuHardConstraints(dish_count=5)) == []

    def test_insufficient_candidates_no_feasible(self) -> None:
        planner = make_planner(count=3)
        assert planner.plan(MenuHardConstraints(dish_count=5)) == []

    def test_exact_dish_count(self) -> None:
        planner = make_planner(count=10)
        plans = planner.plan(MenuHardConstraints(dish_count=4))
        assert plans
        assert all(len(p.recipe_ids) == 4 for p in plans)

    def test_strict_time_only_true(self) -> None:
        # 宽松时限 → true，有方案。
        loose = make_planner(count=8)
        plans = loose.plan(MenuHardConstraints(dish_count=4, strict_time_limit=60))
        assert plans
        assert all(p.strict_time_feasible is True for p in plans)
        # 极紧时限 → false，无可行菜单（无软退回）。
        tight = make_planner(count=8)
        assert tight.plan(MenuHardConstraints(dish_count=4, strict_time_limit=1)) == []

    def test_rejected_recipe_excluded(self) -> None:
        planner = make_planner(count=6)
        plans = planner.plan(MenuHardConstraints(dish_count=3, rejected_recipe_ids={1, 2, 3}))
        for plan in plans:
            assert not (set(plan.recipe_ids) & {1, 2, 3})

    def test_locked_recipe_included(self) -> None:
        planner = make_planner(count=6)
        plans = planner.plan(MenuHardConstraints(dish_count=3, locked_recipe_ids={1}))
        assert all(1 in plan.recipe_ids for plan in plans)

    def test_locked_exceeds_dish_count_no_feasible(self) -> None:
        planner = make_planner(count=6)
        assert planner.plan(MenuHardConstraints(dish_count=2, locked_recipe_ids={1, 2, 3})) == []

    def test_locked_recipe_not_in_safe_no_feasible(self) -> None:
        # 锁定菜不在 B4 安全候选集 → 无可行菜单（08 §9.1 LOCKED_RECIPE_NOT_SAFE）。
        planner = make_planner(count=6)
        assert planner.plan(MenuHardConstraints(dish_count=3, locked_recipe_ids={99})) == []

    def test_required_soup_is_present(self) -> None:
        planner = make_typed_planner()
        plans = planner.plan(MenuHardConstraints(dish_count=3, require_soup=True))
        assert plans
        assert all(
            sum(planner._classify_dish_type(
                planner._recipe_features[rid]["name"]) == "soup"
                for rid in plan.recipe_ids) == 1
            for plan in plans
        )

    def test_drink_and_dessert_do_not_replace_meal_dishes_by_default(self) -> None:
        planner = make_typed_planner()
        plans = planner.plan(MenuHardConstraints(dish_count=3, require_soup=True))
        assert plans
        for plan in plans:
            types = {
                planner._classify_dish_type(planner._recipe_features[rid]["name"])
                for rid in plan.recipe_ids
            }
            assert "drink" not in types
            assert "dessert" not in types
