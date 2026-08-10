"""B4 健康规则与审查引擎（T12）。

确定性二元 PASS/EXCLUDE 评估；数据不完整时 fail-closed，不产出业务结论；
最终复核重新执行同一核心并绑定 menu_hash。
"""

from food_agent_v2.b2 import (
    CodedHealthConstraint,
    ConstraintScope,
    ExplicitFoodTabooConstraint,
    ParticipantHealthConstraintSet,
)
from food_agent_v2.b4.engine import (
    HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE,
    HEALTH_INGREDIENT_SET_INCOMPLETE,
    NUTRITION_HEALTH_BOUNDARY_VIOLATION,
    HealthRuleEngine,
)
from food_agent_v2.b4.repository import HealthDataRepository
from food_agent_v2.b4.schemas import (
    FinalValidationResult,
    HealthEvaluationReceipt,
    RecipeHealthResult,
)

__all__ = [
    "CodedHealthConstraint",
    "ConstraintScope",
    "ExplicitFoodTabooConstraint",
    "FinalValidationResult",
    "HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE",
    "HEALTH_INGREDIENT_SET_INCOMPLETE",
    "HealthDataRepository",
    "HealthEvaluationReceipt",
    "HealthRuleEngine",
    "NUTRITION_HEALTH_BOUNDARY_VIOLATION",
    "ParticipantHealthConstraintSet",
    "RecipeHealthResult",
]
