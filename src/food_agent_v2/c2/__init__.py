"""C2 菜单规划（T15）—— 确定性多目标优化，3-5 个差异化方案。

硬约束优先（仅 B4 safe_recipe_ids）；内容寻址 plan_id/menu_hash；
严格时间只收 true；软维度不可用重归一化；无随机。
"""

from food_agent_v2.c2.planner import WEIGHT_STRATEGIES, MenuPlanner
from food_agent_v2.c2.schemas import (
    DEFAULT_DISH_COUNT,
    MAX_DESSERT,
    MAX_DRINK,
    MAX_SOUP,
    MAX_STAPLE,
    MIN_DISH_COUNT,
    FeasibleMenu,
    MenuHardConstraints,
)

__all__ = [
    "DEFAULT_DISH_COUNT",
    "FeasibleMenu",
    "MAX_DESSERT",
    "MAX_DRINK",
    "MAX_SOUP",
    "MAX_STAPLE",
    "MIN_DISH_COUNT",
    "MenuHardConstraints",
    "MenuPlanner",
    "WEIGHT_STRATEGIES",
]
