"""C2 菜单规划 —— 多目标优化，3-5 个差异化方案。

硬约束优先（仅 B4 safe_recipe_ids），软评分排序（B5 时间 + B6 营养）。
随机重启 3 次取最佳，避免局部最优。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from food_agent_v2.b5 import TimeProfileService, get_time_service
from food_agent_v2.b6 import NutritionScoringService, get_nutrition_service

# 菜单结构硬约束
DEFAULT_DISH_COUNT = 5
MIN_DISH_COUNT = 2
MAX_DISH_COUNT = 8

# 槽位上限
MAX_SOUP = 1
MAX_STAPLE = 1
MAX_DRINK = 1
MAX_DESSERT = 1

# 方案生成：5 组权重偏移策略
WEIGHT_STRATEGIES = {
    "balanced":       {"diversity": 0.3, "time": 0.3, "nutrition": 0.3, "preference": 0.1},
    "preference_heavy": {"diversity": 0.2, "time": 0.2, "nutrition": 0.2, "preference": 0.4},
    "nutrition_heavy":  {"diversity": 0.15, "time": 0.2, "nutrition": 0.5, "preference": 0.15},
    "quick":           {"diversity": 0.15, "time": 0.55, "nutrition": 0.15, "preference": 0.15},
    "diverse":         {"diversity": 0.5, "time": 0.15, "nutrition": 0.15, "preference": 0.2},
}


@dataclass
class MenuHardConstraints:
    """用户声明的硬约束。"""
    dish_count: int = DEFAULT_DISH_COUNT
    strict_time_limit: int | None = None
    strict_ingredients: bool = False
    available_ingredient_ids: set[int] = field(default_factory=set)
    locked_recipe_ids: set[int] = field(default_factory=set)
    rejected_recipe_ids: set[int] = field(default_factory=set)
    require_soup: bool = False
    require_staple: bool = False
    require_drink: bool = False


@dataclass
class FeasibleMenu:
    """一个可行菜单方案。"""
    plan_id: str
    recipe_ids: list[int]
    dominant_objective: str              # balanced | preference_heavy | nutrition_heavy | quick | diverse
    differing_recipe_ids: list[int] = field(default_factory=list)
    total_score: float = 0.0
    time_score: float = 0.0
    nutrition_score: float = 0.0
    preference_score: float = 0.0
    diversity_score: float = 0.0
    makespan_seconds: int | None = None
    strict_time_feasible: str = "unknown"
    time_source: str = "task_graph"       # llm_estimate | task_graph | none


class MenuPlanner:
    """菜单规划服务。"""

    def __init__(self):
        self._b5: TimeProfileService | None = None
        self._b6: NutritionScoringService | None = None
        self._safe_recipe_ids: list[int] = []
        self._recipe_features: dict[int, dict] = {}  # recipe_id → features for scoring

    def set_safe_candidates(self, safe_ids: list[int]) -> None:
        self._safe_recipe_ids = list(safe_ids)

    def set_recipe_features(self, features: dict[int, dict]) -> None:
        self._recipe_features = features

    def _get_services(self) -> tuple[TimeProfileService, NutritionScoringService]:
        if self._b5 is None:
            self._b5 = get_time_service()
        if self._b6 is None:
            self._b6 = get_nutrition_service()
        return self._b5, self._b6

    def plan(
        self,
        constraints: MenuHardConstraints,
        target_count: int = 5,
    ) -> list[FeasibleMenu]:
        """生成差异化菜单方案。随机重启 3 次取最佳。"""
        b5, b6 = self._get_services()

        # 过滤安全候选
        candidates = list(self._safe_recipe_ids)
        if constraints.strict_ingredients and constraints.available_ingredient_ids:
            # 严格食材模式：过滤不含可用食材的菜
            candidates = [
                rid for rid in candidates
                if self._recipe_features.get(rid, {}).get("has_available_ingredients", True)
            ]

        # 锁定菜必须入选
        locked = list(constraints.locked_recipe_ids)
        # 拒绝菜不可入选
        candidates = [rid for rid in candidates if rid not in constraints.rejected_recipe_ids]

        if not candidates:
            return []

        # 自适应菜数
        dish_count = constraints.dish_count
        if dish_count == DEFAULT_DISH_COUNT and len(candidates) < 5:
            dish_count = max(MIN_DISH_COUNT, min(len(candidates), 4))

        strategies = list(WEIGHT_STRATEGIES.items())
        plans: list[FeasibleMenu] = []

        for strat_key, weights in strategies:
            if len(plans) >= target_count:
                break

            # 随机重启 3 次
            best: FeasibleMenu | None = None
            best_score = -1.0

            for _ in range(3):
                random.shuffle(candidates)
                menu = self._build_one_menu(
                    locked, candidates, dish_count, weights, strat_key, b5, b6,
                    constraints,
                )
                if menu and menu.total_score > best_score:
                    best = menu
                    best_score = menu.total_score

            if best:
                plans.append(best)

        # 时间约束（文档 08 §5.2）：有严格时间限制时优先选满足方案；
        # 若所有方案都超时（常见于 LLM 估算偏保守），软退回最快方案，不硬失败。
        if constraints.strict_time_limit and plans:
            limit_s = constraints.strict_time_limit * 60
            fitting = [p for p in plans
                       if p.strict_time_feasible is not False
                       and (p.makespan_seconds is None or p.makespan_seconds <= limit_s)]
            if fitting:
                plans = fitting
            else:
                # 无满足方案：保留最快方案（按 makespan 升序取最接近的）
                with_makespan = [p for p in plans if p.makespan_seconds is not None]
                if with_makespan:
                    plans = [min(with_makespan, key=lambda p: p.makespan_seconds)]
        if not plans:
            return []

        # 去重合并——任意两个方案至少 1 道菜不同
        unique_plans = self._deduplicate(plans)
        return unique_plans[:target_count]

    def _build_one_menu(
        self,
        locked: list[int],
        candidates: list[int],
        dish_count: int,
        weights: dict[str, float],
        strategy: str,
        b5: TimeProfileService,
        b6: NutritionScoringService,
        hard: MenuHardConstraints,
    ) -> FeasibleMenu | None:
        """构建单个菜单方案（贪心）。"""
        selected = list(locked)
        # 已选菜名集合：禁止同名菜进同一菜单（数据存在"红烧肉"等多个变体，
        # 同名会显得菜单重复）。locked 也参与去重。
        selected_names = set()
        # 槽位计数（文档 08 §8.1：汤/主食/饮品/甜品各 ≤1）
        slot_counts = {"soup": 0, "staple": 0, "drink": 0, "dessert": 0}
        for rid in selected:
            nm = (self._recipe_features.get(rid, {}) or {}).get("name", "")
            if nm:
                selected_names.add(nm)
                t = self._classify_dish_type(nm)
                if t in slot_counts:
                    slot_counts[t] += 1
        remaining = [r for r in candidates if r not in locked]

        if len(selected) >= dish_count:
            return self._score_menu(selected, weights, strategy, b5, b6, hard)

        # 多维度打分
        scores = self._score_all_candidates(remaining, b5, b6, weights, selected)
        ranked = sorted(scores, key=lambda x: -x[2])

        # 贪心选择：按综合分依次挑选（跳过同名菜；遵守槽位上限）
        slot_max = {"soup": MAX_SOUP, "staple": MAX_STAPLE,
                    "drink": MAX_DRINK, "dessert": MAX_DESSERT}
        for rid, _, _ in ranked:
            nm = (self._recipe_features.get(rid, {}) or {}).get("name", "")
            if nm and nm in selected_names:
                continue
            if nm:
                t = self._classify_dish_type(nm)
                if t in slot_max and slot_counts.get(t, 0) >= slot_max[t]:
                    continue  # 槽位已满，跳过
            selected.append(rid)
            if nm:
                selected_names.add(nm)
                if nm:
                    t = self._classify_dish_type(nm)
                    if t in slot_counts:
                        slot_counts[t] += 1
            if len(selected) >= dish_count:
                break

        if len(selected) < MIN_DISH_COUNT:
            return None

        return self._score_menu(selected[:dish_count], weights, strategy, b5, b6, hard)

    @staticmethod
    def _classify_dish_type(name: str) -> str:
        """基于菜名的启发式菜品类型分类（文档 08 §8.1 槽位用）。

        返回 soup | staple | drink | dessert | main。
        """
        n = name or ""
        # 饮品优先（茶/汁/奶昔/豆浆/饮料等），避免"甜汤/糖水"被当汤
        if any(m in n for m in ("奶昔", "豆浆", "奶茶", "果汁", "糖水", "气泡水",
                                "咖啡", "饮料", "汽水", "乳酸菌")) \
                or n.endswith(("茶", "汁", "饮")):
            return "drink"
        # 甜品
        if any(m in n for m in ("蛋糕", "布丁", "冰淇淋", "甜点", "点心", "曲奇",
                                "饼干", "蛋挞", "慕斯", "糖水", "醪糟", "汤圆",
                                "米糕", "发糕", "马拉糕")):
            return "dessert"
        # 汤羹
        if "汤" in n or "羹" in n:
            return "soup"
        # 主食
        if any(m in n for m in ("米饭", "炒饭", "煲仔饭", "面条", "米线", "凉皮",
                                "馒头", "包子", "饺子", "馄饨", "粥", "饼", "粉",
                                "饭团", "年糕", "烧麦", "面食")):
            return "staple"
        return "main"

    def _score_all_candidates(
        self, ids: list[int], b5: TimeProfileService,
        b6: NutritionScoringService, weights: dict[str, float],
        already_selected: list[int],
    ) -> list[tuple[int, float, float]]:
        """为所有候选打分。"""
        results = []
        for rid in ids:
            time_s = self._time_score(rid, b5)
            nutrition_s = self._nutrition_score(rid, b6)
            diversity_s = self._diversity_score(rid, already_selected)
            preference_s = self._preference_score(rid)

            total = (
                weights["time"] * time_s +
                weights["nutrition"] * nutrition_s +
                weights["diversity"] * diversity_s +
                weights["preference"] * preference_s
            )
            results.append((rid, total,
                           time_s + nutrition_s + diversity_s + preference_s))
        return results

    def _score_menu(
        self, recipe_ids: list[int], weights: dict[str, float],
        strategy: str, b5: TimeProfileService,
        b6: NutritionScoringService, hard: MenuHardConstraints,
    ) -> FeasibleMenu:
        """为完整菜单打分。"""
        time_s = sum(self._time_score(rid, b5) for rid in recipe_ids) / max(len(recipe_ids), 1)
        nutrition_s = sum(self._nutrition_score(rid, b6) for rid in recipe_ids) / max(len(recipe_ids), 1)
        pref_s = sum(self._preference_score(rid) for rid in recipe_ids) / max(len(recipe_ids), 1)

        # 多样性——基于食材族差异
        div_s = self._menu_diversity_score(recipe_ids)

        total = (
            weights["time"] * time_s +
            weights["nutrition"] * nutrition_s +
            weights["diversity"] * div_s +
            weights["preference"] * pref_s
        )

        sched = b5.compute_menu_schedule(recipe_ids, hard.strict_time_limit)

        return FeasibleMenu(
            plan_id=f"plan_{strategy}_{len(recipe_ids)}",
            recipe_ids=recipe_ids,
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

    def _time_score(self, rid: int, b5: TimeProfileService) -> float:
        profile = b5.get_recipe_time_profile(rid)
        if not profile:
            return 0.5
        # 时间越短分越高
        total = profile.total_active_seconds + profile.total_equipment_seconds
        if total <= 0:
            return 0.5
        # 归一化：1800s (30min) → 1.0, 7200s (2h) → 0.0
        return max(0.0, 1.0 - total / 7200)

    def _nutrition_score(self, rid: int, b6: NutritionScoringService) -> float:
        # T13：B6 在营养不可用时返回 weighted_total=None（不伪造中性分）。
        # 这里把不可用维度按 0 贡献处理；完整权重重归一化在 T15 的 C2 重写中完成。
        decomposition = b6.score_recipe(rid)
        if not decomposition.available or decomposition.weighted_total is None:
            return 0.0
        return decomposition.weighted_total

    def _diversity_score(self, rid: int, already_selected: list[int]) -> float:
        """单菜多样性——基于菜品名称和检索字段的差异程度。"""
        if not already_selected:
            return 0.5
        this_features = self._recipe_features.get(rid, {})
        this_name = this_features.get("name", "")

        scores = []
        for sid in already_selected:
            other_features = self._recipe_features.get(sid, {})
            other_name = other_features.get("name", "")
            # 名称包含度：名称越不同分越高
            name_diff = 0.0 if (this_name in other_name or other_name in this_name) else 0.5
            # 检索字段差异：字段重叠越少分越高
            this_fields = set(str(v) for v in this_features.get("fields", {}).values())
            other_fields = set(str(v) for v in other_features.get("fields", {}).values())
            field_overlap = len(this_fields & other_fields) / max(len(this_fields | other_fields), 1)
            scores.append(name_diff + (1.0 - field_overlap) * 0.5)
        return sum(scores) / max(len(scores), 1)

    def _menu_diversity_score(self, recipe_ids: list[int]) -> float:
        """菜单整体的多样性——名称和检索字段的平均差异度。"""
        if len(recipe_ids) <= 1:
            return 0.5
        scores = []
        for i, rid in enumerate(recipe_ids):
            others = recipe_ids[:i] + recipe_ids[i+1:]
            scores.append(self._diversity_score(rid, others))
        return sum(scores) / max(len(scores), 1)

    def _preference_score(self, rid: int) -> float:
        """偏好分——基于已知标签匹配。"""
        features = self._recipe_features.get(rid, {})
        return features.get("preference_score", 0.5)

    def _deduplicate(self, plans: list[FeasibleMenu]) -> list[FeasibleMenu]:
        """去重：任意两个方案至少 1 道菜不同。"""
        unique: list[FeasibleMenu] = []
        for plan in plans:
            is_dup = False
            for existing in unique:
                if set(plan.recipe_ids) == set(existing.recipe_ids):
                    is_dup = True
                    break
            if not is_dup:
                unique.append(plan)
        return unique

    def adjust_menu(
        self,
        menu: FeasibleMenu,
        replace_recipe_id: int,
        safe_ids: list[int],
        constraints: MenuHardConstraints,
    ) -> FeasibleMenu | None:
        """替换一道菜。最多尝试 3 次，makespan 增加则回退停止。"""
        b5, b6 = self._get_services()
        candidates = [r for r in safe_ids if r not in menu.recipe_ids
                     and r not in constraints.rejected_recipe_ids]

        if not candidates:
            return None

        original_makespan = menu.makespan_seconds or float("inf")

        for i, candidate in enumerate(candidates[:3]):
            new_ids = [r if r != replace_recipe_id else candidate for r in menu.recipe_ids]
            new_menu = self._score_menu(
                new_ids, WEIGHT_STRATEGIES.get(menu.dominant_objective, WEIGHT_STRATEGIES["balanced"]),
                menu.dominant_objective, b5, b6, constraints,
            )
            new_makespan = new_menu.makespan_seconds or float("inf")
            if new_makespan > original_makespan:
                # 回退并停止（§19.4 决议）
                return None
            new_menu.plan_id = f"{menu.plan_id}_adj_{i}"
            return new_menu

        return None
