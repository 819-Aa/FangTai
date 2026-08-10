"""C2 确定性菜单规划器（T15）。

硬规则：
- 只收 B4 有效安全候选（空集即无可行菜单）；
- 默认/明确菜数精确执行（不足即无可行，不自动降菜数）；
- 严格时间只收 true；false/unknown 一律不接受（无软退回）；
- 不改菜：plan 与最终校验必须同一内容（内容寻址 plan_id/menu_hash）；
- 软维度不可用时重归一化权重；
- 相同输入产生稳定 plan_id/menu_hash（无 random）。
"""

from __future__ import annotations

from collections.abc import Sequence

from food_agent_v2.b5 import TimeProfileService, get_time_service
from food_agent_v2.b6 import NutritionScoringService, get_nutrition_service
from food_agent_v2.c2.schemas import (
    MAX_DESSERT,
    MAX_DRINK,
    MAX_SOUP,
    MAX_STAPLE,
    FeasibleMenu,
    MenuHardConstraints,
)
from food_agent_v2.contracts.build import canonical_json_hash

WEIGHT_STRATEGIES: dict[str, dict[str, float]] = {
    "balanced": {"diversity": 0.3, "time": 0.3, "nutrition": 0.3, "preference": 0.1},
    "preference_heavy": {"diversity": 0.2, "time": 0.2, "nutrition": 0.2, "preference": 0.4},
    "nutrition_heavy": {"diversity": 0.15, "time": 0.2, "nutrition": 0.5, "preference": 0.15},
    "quick": {"diversity": 0.15, "time": 0.55, "nutrition": 0.15, "preference": 0.15},
    "diverse": {"diversity": 0.5, "time": 0.15, "nutrition": 0.15, "preference": 0.2},
}


class MenuPlanner:
    """确定性菜单规划服务（无随机重启，内容寻址身份）。"""

    def __init__(
        self,
        b5: TimeProfileService | None = None,
        b6: NutritionScoringService | None = None,
    ) -> None:
        self._b5 = b5
        self._b6 = b6
        self._safe_recipe_ids: list[int] = []
        self._recipe_features: dict[int, dict] = {}

    def set_safe_candidates(self, safe_ids: Sequence[int]) -> None:
        self._safe_recipe_ids = list(safe_ids)

    def set_recipe_features(self, features: dict[int, dict]) -> None:
        self._recipe_features = features

    def _get_services(self) -> tuple[TimeProfileService, NutritionScoringService]:
        if self._b5 is None:
            self._b5 = get_time_service()
        if self._b6 is None:
            self._b6 = get_nutrition_service()
        return self._b5, self._b6

    def plan(self, constraints: MenuHardConstraints, target_count: int = 5) -> list[FeasibleMenu]:
        """生成确定性差异方案；不满足硬约束时返回空（无可行菜单）。"""
        # 只收有效 B4 安全候选。
        if not self._safe_recipe_ids:
            return []

        locked = list(constraints.locked_recipe_ids)
        # 锁定菜必须属于安全候选集，否则无可行菜单（08 §9.1：LOCKED_RECIPE_NOT_SAFE）。
        if any(r not in self._safe_recipe_ids for r in locked):
            return []
        if len(locked) > constraints.dish_count:
            return []  # 锁定菜超过菜数

        candidates = [r for r in self._safe_recipe_ids if r not in constraints.rejected_recipe_ids]
        if constraints.strict_ingredients and constraints.available_ingredient_ids:
            candidates = [
                r for r in candidates
                if self._recipe_features.get(r, {}).get("has_available_ingredients", True)
            ]

        available_remaining = [r for r in candidates if r not in locked]
        if len(locked) + len(available_remaining) < constraints.dish_count:
            return []  # 候选不足，无法精确凑满菜数

        b5, b6 = self._get_services()
        plans: list[FeasibleMenu] = []
        for strategy in WEIGHT_STRATEGIES:  # 确定性顺序
            if len(plans) >= target_count:
                break
            menu = self._build_one_menu(
                locked, candidates, constraints.dish_count, strategy, b5, b6, constraints
            )
            if menu is None:
                continue
            # 严格时间只收 true；有严格时限时 false/unknown 不接受。
            if constraints.strict_time_limit is not None and menu.strict_time_feasible is not True:
                continue
            plans.append(menu)

        unique = self._deduplicate(plans)
        return unique[:target_count]

    def _build_one_menu(
        self,
        locked: list[int],
        candidates: list[int],
        dish_count: int,
        strategy: str,
        b5: TimeProfileService,
        b6: NutritionScoringService,
        hard: MenuHardConstraints,
    ) -> FeasibleMenu | None:
        """确定性贪心构建单个方案（无 random）。"""
        weights = WEIGHT_STRATEGIES[strategy]
        selected = list(locked)
        selected_names = {self._recipe_features.get(rid, {}).get("name", "") for rid in locked}
        slot_counts = {"soup": 0, "staple": 0, "drink": 0, "dessert": 0}
        for rid in locked:
            nm = self._recipe_features.get(rid, {}).get("name", "")
            if nm:
                t = self._classify_dish_type(nm)
                if t in slot_counts:
                    slot_counts[t] += 1

        remaining = [r for r in candidates if r not in locked]
        scores = self._score_all_candidates(remaining, b5, b6, weights, selected)
        ranked = sorted(scores, key=lambda x: (-x[1], x[0]))  # 策略加权总分降序，同分按 recipe_id 稳定

        slot_max = {"soup": MAX_SOUP, "staple": MAX_STAPLE, "drink": MAX_DRINK, "dessert": MAX_DESSERT}
        for rid, _, _ in ranked:
            if len(selected) >= dish_count:
                break
            nm = self._recipe_features.get(rid, {}).get("name", "")
            if nm and nm in selected_names:
                continue
            if nm:
                t = self._classify_dish_type(nm)
                if t in slot_max and slot_counts.get(t, 0) >= slot_max[t]:
                    continue
            selected.append(rid)
            if nm:
                selected_names.add(nm)
                t = self._classify_dish_type(nm)
                if t in slot_counts:
                    slot_counts[t] += 1

        if len(selected) != dish_count:
            return None  # 无法精确凑满菜数

        return self._score_menu(selected, weights, strategy, b5, b6, hard)

    def _score_menu(
        self,
        recipe_ids: list[int],
        weights: dict[str, float],
        strategy: str,
        b5: TimeProfileService,
        b6: NutritionScoringService,
        hard: MenuHardConstraints,
    ) -> FeasibleMenu:
        time_s = sum(self._time_score(rid, b5) for rid in recipe_ids) / max(len(recipe_ids), 1)
        nutrition_available = all(
            self._nutrition_decomposition(rid, b6).available for rid in recipe_ids
        )
        nutrition_s = sum(self._nutrition_score(rid, b6) for rid in recipe_ids) / max(len(recipe_ids), 1)
        pref_s = sum(self._preference_score(rid) for rid in recipe_ids) / max(len(recipe_ids), 1)
        div_s = self._menu_diversity_score(recipe_ids)

        dims = {
            "time": time_s,
            "nutrition": nutrition_s if nutrition_available else None,
            "preference": pref_s,
            "diversity": div_s,
        }
        total = self._weighted_renormalized(weights, dims)

        sched = b5.compute_menu_schedule(recipe_ids, hard.strict_time_limit)
        plan_id = canonical_json_hash(
            {"strategy": strategy, "recipe_ids": sorted(recipe_ids)}
        )
        menu_hash = canonical_json_hash(
            {"plan_id": plan_id, "recipe_ids": sorted(recipe_ids)}
        )
        return FeasibleMenu(
            plan_id=plan_id,
            recipe_ids=list(recipe_ids),
            menu_hash=menu_hash,
            dominant_objective=strategy,
            total_score=round(total, 4),
            time_score=round(time_s, 4),
            nutrition_score=round(nutrition_s, 4),
            preference_score=round(pref_s, 4),
            diversity_score=round(div_s, 4),
            makespan_seconds=sched.makespan_seconds,
            strict_time_feasible=sched.strict_time_feasible,
            time_source=sched.time_source,
        )

    @staticmethod
    def _weighted_renormalized(weights: dict[str, float], dims: dict[str, float | None]) -> float:
        """不可用维度禁用，剩余权重重新归一化（INV-022）。"""
        available = [(name, value) for name, value in dims.items() if value is not None]
        if not available:
            return 0.0
        weight_sum = sum(weights[name] for name, _ in available)
        if weight_sum <= 0:
            return 0.0
        return sum(weights[name] * value for name, value in available) / weight_sum

    def _nutrition_decomposition(self, rid: int, b6: NutritionScoringService):
        return b6.score_recipe(rid)

    def _time_score(self, rid: int, b5: TimeProfileService) -> float:
        profile = b5.get_recipe_time_profile(rid)
        if not profile:
            return 0.5
        total = profile.total_active_seconds + profile.total_equipment_seconds
        if total <= 0:
            return 0.5
        return max(0.0, 1.0 - total / 7200)

    def _nutrition_score(self, rid: int, b6: NutritionScoringService) -> float:
        decomposition = b6.score_recipe(rid)
        if not decomposition.available or decomposition.weighted_total is None:
            return 0.0
        return decomposition.weighted_total

    def _diversity_score(self, rid: int, already_selected: list[int]) -> float:
        if not already_selected:
            return 0.5
        this_features = self._recipe_features.get(rid, {})
        this_name = this_features.get("name", "")
        scores = []
        for sid in already_selected:
            other_features = self._recipe_features.get(sid, {})
            other_name = other_features.get("name", "")
            name_diff = 0.0 if (this_name in other_name or other_name in this_name) else 0.5
            this_fields = set(str(v) for v in this_features.get("fields", {}).values())
            other_fields = set(str(v) for v in other_features.get("fields", {}).values())
            field_overlap = len(this_fields & other_fields) / max(len(this_fields | other_fields), 1)
            scores.append(name_diff + (1.0 - field_overlap) * 0.5)
        return sum(scores) / max(len(scores), 1)

    def _menu_diversity_score(self, recipe_ids: list[int]) -> float:
        if len(recipe_ids) <= 1:
            return 0.5
        scores = []
        for i, rid in enumerate(recipe_ids):
            others = recipe_ids[:i] + recipe_ids[i + 1:]
            scores.append(self._diversity_score(rid, others))
        return sum(scores) / max(len(scores), 1)

    def _preference_score(self, rid: int) -> float:
        return self._recipe_features.get(rid, {}).get("preference_score", 0.5)

    def _score_all_candidates(
        self,
        ids: list[int],
        b5: TimeProfileService,
        b6: NutritionScoringService,
        weights: dict[str, float],
        already_selected: list[int],
    ) -> list[tuple[int, float, float]]:
        results = []
        for rid in ids:
            time_s = self._time_score(rid, b5)
            nutrition_available = self._nutrition_decomposition(rid, b6).available
            nutrition_s = self._nutrition_score(rid, b6)
            diversity_s = self._diversity_score(rid, already_selected)
            preference_s = self._preference_score(rid)
            dims = {
                "time": time_s,
                "nutrition": nutrition_s if nutrition_available else None,
                "preference": preference_s,
                "diversity": diversity_s,
            }
            total = self._weighted_renormalized(weights, dims)
            results.append((rid, total, time_s + (nutrition_s or 0.0) + diversity_s + preference_s))
        return results

    @staticmethod
    def _classify_dish_type(name: str) -> str:
        n = name or ""
        if any(m in n for m in ("奶昔", "豆浆", "奶茶", "果汁", "糖水", "气泡水",
                                "咖啡", "饮料", "汽水", "乳酸菌")) or n.endswith(("茶", "汁", "饮")):
            return "drink"
        if any(m in n for m in ("蛋糕", "布丁", "冰淇淋", "甜点", "点心", "曲奇",
                                "饼干", "蛋挞", "慕斯", "醪糟", "汤圆", "米糕", "发糕", "马拉糕")):
            return "dessert"
        if "汤" in n or "羹" in n:
            return "soup"
        if any(m in n for m in ("米饭", "炒饭", "煲仔饭", "面条", "米线", "凉皮",
                                "馒头", "包子", "饺子", "馄饨", "粥", "饼", "粉",
                                "饭团", "年糕", "烧麦", "面食")):
            return "staple"
        return "main"

    def _deduplicate(self, plans: list[FeasibleMenu]) -> list[FeasibleMenu]:
        unique: list[FeasibleMenu] = []
        for plan in plans:
            if not any(set(plan.recipe_ids) == set(existing.recipe_ids) for existing in unique):
                unique.append(plan)
        return unique

    def adjust_menu(
        self,
        menu: FeasibleMenu,
        replace_recipe_id: int,
        safe_ids: list[int],
        constraints: MenuHardConstraints,
    ) -> FeasibleMenu | None:
        """替换一道菜（确定性，无 random）。新增方案 makespan 超原方案则回退。"""
        b5, b6 = self._get_services()
        candidates = [
            r for r in safe_ids
            if r not in menu.recipe_ids and r not in constraints.rejected_recipe_ids
        ]
        if not candidates:
            return None
        original_makespan = menu.makespan_seconds or float("inf")
        weights = WEIGHT_STRATEGIES.get(menu.dominant_objective, WEIGHT_STRATEGIES["balanced"])
        # 确定性候选顺序：按 score 降序（同分按 recipe_id）。
        scored = sorted(
            ((r, self._weighted_renormalized(
                weights,
                {
                    "time": self._time_score(r, b5),
                    "nutrition": self._nutrition_score(r, b6)
                    if self._nutrition_decomposition(r, b6).available else None,
                    "preference": self._preference_score(r),
                    "diversity": self._diversity_score(r, menu.recipe_ids),
                },
            )) for r in candidates),
            key=lambda x: (-x[1], x[0]),
        )
        for candidate, _ in scored[:3]:
            new_ids = [r if r != replace_recipe_id else candidate for r in menu.recipe_ids]
            new_menu = self._score_menu(
                new_ids, weights, menu.dominant_objective, b5, b6, constraints
            )
            if (new_menu.makespan_seconds or float("inf")) > original_makespan:
                return None
            new_menu.plan_id = canonical_json_hash(
                {"base_plan": menu.plan_id, "replaced": replace_recipe_id, "with": candidate}
            )
            new_menu.menu_hash = canonical_json_hash(
                {"plan_id": new_menu.plan_id, "recipe_ids": sorted(new_ids)}
            )
            return new_menu
        return None
