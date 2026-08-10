"""C2 菜单规划领域 Schema（T15）。

FeasibleMenu 携带内容寻址 plan_id 与 menu_hash；相同输入产生相同身份。
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_DISH_COUNT = 5
MIN_DISH_COUNT = 2
MAX_DISH_COUNT = 8

MAX_SOUP = 1
MAX_STAPLE = 1
MAX_DRINK = 1
MAX_DESSERT = 1


@dataclass
class MenuHardConstraints:
    """用户声明的硬约束（菜数/严格时间/严格食材/锁定/拒绝/槽位要求）。"""

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
    """一个可行菜单方案（内容寻址身份）。"""

    plan_id: str
    recipe_ids: list[int]
    menu_hash: str
    dominant_objective: str
    differing_recipe_ids: list[int] = field(default_factory=list)
    total_score: float = 0.0
    time_score: float = 0.0
    nutrition_score: float = 0.0
    preference_score: float = 0.0
    diversity_score: float = 0.0
    makespan_seconds: int | None = None
    strict_time_feasible: bool | str = "unknown"
    time_source: str = "task_graph"
