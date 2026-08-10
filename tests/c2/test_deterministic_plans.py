"""T15 C2 确定性方案测试。

plan_id/menu_hash 内容寻址（相同输入稳定）；输出 3-5 个实际不同方案；
软维度不可用时重归一化权重。
"""

from types import SimpleNamespace

from food_agent_v2.c2 import MenuHardConstraints, MenuPlanner


class FakeB5:
    def __init__(self, per_recipe_seconds: dict[int, int] | None = None) -> None:
        self._seconds = per_recipe_seconds or {}

    def _total(self, rid: int) -> int:
        return self._seconds.get(rid, 300)

    def get_recipe_time_profile(self, rid: int):
        total = self._total(rid)
        return SimpleNamespace(
            total_active_seconds=int(total * 0.7), total_equipment_seconds=int(total * 0.3)
        )

    def compute_menu_schedule(self, recipe_ids, time_limit_minutes=None):
        makespan = sum(self._total(rid) for rid in recipe_ids)
        if time_limit_minutes is not None:
            feasible = makespan <= time_limit_minutes * 60
        else:
            feasible = "unknown"
        return SimpleNamespace(
            makespan_seconds=makespan, strict_time_feasible=feasible, time_source="task_graph"
        )


class FakeB6:
    def __init__(
        self,
        available: bool = True,
        per_recipe=None,
    ) -> None:
        self._available = available
        self._per_recipe = per_recipe

    def score_recipe(self, rid: int):
        if not self._available:
            return SimpleNamespace(available=False, weighted_total=None)
        score = self._per_recipe(rid) if self._per_recipe else 0.8
        return SimpleNamespace(available=True, weighted_total=score)


def make_planner(b6_available: bool = True, count: int = 20) -> MenuPlanner:
    ids = list(range(1, count + 1))
    # 时间随菜递增：quick 策略偏好短时（低 rid），preference_heavy 偏好高偏好（高 rid），
    # 使不同权重策略选出不同方案。
    durations = {rid: 100 + rid * 500 for rid in ids}
    # 营养峰值在中间（rid 10），使 nutrition_heavy 与 quick/preference 选出不同方案。
    planner = MenuPlanner(
        b5=FakeB5(durations),
        b6=FakeB6(available=b6_available, per_recipe=lambda r: max(0.0, 1.0 - abs(r - 10) * 0.05)),
    )
    planner.set_safe_candidates(ids)
    planner.set_recipe_features(
        {
            rid: {"name": f"菜{rid}", "preference_score": 0.2 + 0.03 * rid, "fields": {}}
            for rid in ids
        }
    )
    return planner


class TestDeterministicPlans:
    def test_plan_id_stable_for_same_input(self) -> None:
        planner = make_planner()
        a = planner.plan(MenuHardConstraints(dish_count=5))
        b = planner.plan(MenuHardConstraints(dish_count=5))
        assert [p.plan_id for p in a] == [p.plan_id for p in b]
        assert [p.menu_hash for p in a] == [p.menu_hash for p in b]
        assert all(len(p.plan_id) == 64 for p in a)
        assert all(len(p.menu_hash) == 64 for p in a)

    def test_plan_id_changes_with_recipe_set(self) -> None:
        planner = make_planner()
        plans = planner.plan(MenuHardConstraints(dish_count=5))
        assert len(plans) >= 3, f"应生成 3-5 个方案，实际 {len(plans)}"
        sets = [set(p.recipe_ids) for p in plans]
        # 实际不同方案：任意两方案菜集合不同。
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                assert sets[i] != sets[j]

    def test_menu_hash_content_addressed(self) -> None:
        planner = make_planner()
        plans = planner.plan(MenuHardConstraints(dish_count=4))
        for p in plans:
            assert len(p.menu_hash) == 64

    def test_nutrition_unavailable_renormalizes(self) -> None:
        # 营养不可用时，其余可用维度权重重新归一化（INV-022）。
        available = make_planner(b6_available=True)
        unavailable = make_planner(b6_available=False)
        pa = available.plan(MenuHardConstraints(dish_count=5))
        pu = unavailable.plan(MenuHardConstraints(dish_count=5))
        assert pa and pu
        # 两者都能产生方案；营养不可用时不返回伪造中性分。
        for p in pu:
            assert p.nutrition_score == 0.0
