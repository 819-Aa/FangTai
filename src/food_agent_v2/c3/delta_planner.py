"""C3 多轮 delta 规划器（L1.3）—— 最小修改原则的执行计划。

消费已验证的 IntentDelta + 当前已提交菜单 + 健康审查后的 safe 集，产出
DeltaExecutionPlan（锁定/拒绝/恢复），供 orchestrator 生成最小修改的新菜单。
不做菜名 → recipe_id 的解析（target_recipe_id 由上游 FastIntentRouter /
QueryNormalizer 解析后传入）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from food_agent_v2.c3.fast_intent import IntentDelta


@dataclass(frozen=True)
class DeltaExecutionPlan:
    """最小修改执行计划：锁定/拒绝/恢复的精确 recipe_id 集合。"""

    intent: str
    dish_count: int
    locked_recipe_ids: tuple[int, ...] = ()
    rejected_recipe_ids: tuple[int, ...] = ()
    target_recipe_id: int | None = None
    restore_version: str | None = None
    restore_recipe_ids: tuple[int, ...] = ()


class DeltaPlanner:
    """确定性 delta 规划器。menu_history 为 {version: recipe_ids} 映射。"""

    def __init__(self, menu_history: dict[str, tuple[int, ...]] | None = None):
        self._menu_history = menu_history or {}

    def plan(self, current_recipe_ids: Sequence[int], intent: IntentDelta,
             safe_recipe_ids: Sequence[int]) -> DeltaExecutionPlan:
        current = list(current_recipe_ids)
        safe = set(safe_recipe_ids)

        if intent.intent == "add_constraint":
            # 保留满足所有新约束的当前菜（不无故推翻已确认方案）
            locked = tuple(rid for rid in current if rid in safe)
            return DeltaExecutionPlan(
                intent="add_constraint", dish_count=len(current),
                locked_recipe_ids=locked)

        if intent.intent == "replace":
            # 仅解锁目标菜，其余保留
            target = intent.target_recipe_id
            locked = tuple(rid for rid in current if rid != target and rid in safe)
            rejected = (target,) if target is not None else ()
            return DeltaExecutionPlan(
                intent="replace", dish_count=len(current),
                locked_recipe_ids=locked, rejected_recipe_ids=rejected,
                target_recipe_id=target)

        if intent.intent == "reject_plan":
            # 整套否定：当前 IDs 全部拒绝，保留参与者/有效约束
            return DeltaExecutionPlan(
                intent="reject_plan", dish_count=len(current),
                rejected_recipe_ids=tuple(current))

        # 默认：无前文 delta（当作首次推荐，无锁定）
        return DeltaExecutionPlan(intent=intent.intent, dish_count=len(current))

    def restore(self, version: str) -> DeltaExecutionPlan:
        recipe_ids = tuple(self._menu_history.get(version, ()))
        return DeltaExecutionPlan(
            intent="restore", dish_count=len(recipe_ids),
            restore_version=version, restore_recipe_ids=recipe_ids)
