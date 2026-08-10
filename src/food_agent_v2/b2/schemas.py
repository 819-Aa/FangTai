"""B2 用户健康档案领域 Schema（T10）。

约束、目标、临时约束与投影视图的类型化结构；临时食材禁忌必须绑定标准
ingredient_id；对模型只投影匿名 participant_ref 的最小视图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ConstraintScope(StrEnum):
    PERMANENT = "permanent"  # 不可覆盖
    SESSION = "session"      # 当前会话
    TURN = "turn"            # 仅本轮


class ConstraintEffect(StrEnum):
    HARD_EXCLUDE = "hard_exclude"
    SOFT_PREFER = "soft_prefer"


class IndicatorStatus(StrEnum):
    NORMAL = "normal"
    ABNORMAL = "abnormal"
    UNKNOWN = "unknown"


@dataclass
class CodedHealthConstraint:
    """编码化健康硬约束（constraint_code 必须属于封闭注册表）。"""

    constraint_code: str
    participant_ref: str
    source_refs: list[str]
    effect: ConstraintEffect = ConstraintEffect.HARD_EXCLUDE
    scope: ConstraintScope = ConstraintScope.PERMANENT
    projection_level: str = "b4_view"


@dataclass
class ExplicitFoodTabooConstraint:
    """显式食材禁忌约束（必须绑定标准 ingredient_id）。"""

    taboo_ingredient_id: int
    taboo_ingredient_name: str
    participant_ref: str
    source_refs: list[str]
    effect: ConstraintEffect = ConstraintEffect.HARD_EXCLUDE
    scope: ConstraintScope = ConstraintScope.PERMANENT
    projection_level: str = "b4_view"


@dataclass
class HealthGoal:
    """软健康目标（不进入硬约束）。"""

    goal_code: str
    participant_ref: str
    source_refs: list[str]
    effect: ConstraintEffect = ConstraintEffect.SOFT_PREFER
    scope: ConstraintScope = ConstraintScope.SESSION
    priority: str = "user_stated"


@dataclass
class TemporaryHealthConstraint:
    """B2 验证后的临时约束；明确食材禁忌必须携带 ingredient_id。"""

    constraint_id: str
    constraint_code: str | None
    taboo_ingredient_name: str | None
    taboo_ingredient_id: int | None
    participant_ref: str
    source_refs: list[str]
    scope: ConstraintScope
    effect: ConstraintEffect = ConstraintEffect.HARD_EXCLUDE


@dataclass
class ParticipantHealthConstraintSet:
    """单个参与者的完整有效约束集。"""

    participant_ref: str
    hard_constraints: list[CodedHealthConstraint | ExplicitFoodTabooConstraint] = field(default_factory=list)
    soft_goals: list[HealthGoal] = field(default_factory=list)
    unresolved_signals: list[dict] = field(default_factory=list)


@dataclass
class ProfileValidationResult:
    """单份固定档案的校验结果。"""

    user_id: int
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    derived_constraint_count: int = 0
