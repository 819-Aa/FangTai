"""B2 用户健康档案模块（T10）。

封闭约束代码注册表 + 服务：固定档案校验、约束派生（未知映射 fail-closed）、
临时信号验证（明确禁忌绑定标准 ingredient_id）、角色匿名投影。
"""

from food_agent_v2.b2.constraint_registry import (
    ALLOWED_CONSTRAINT_CODES,
    SPECIAL_STAGE_RULES,
    allergy_to_constraint_code,
    disease_to_constraint_code,
    indicator_to_constraint_code,
    is_allowed_code,
    special_group_to_constraint_code,
    special_stage_status,
)
from food_agent_v2.b2.schemas import (
    CodedHealthConstraint,
    ConstraintEffect,
    ConstraintScope,
    ExplicitFoodTabooConstraint,
    HealthGoal,
    IndicatorStatus,
    ParticipantHealthConstraintSet,
    ProfileValidationResult,
    TemporaryHealthConstraint,
)
from food_agent_v2.b2.service import (
    BP_DIASTOLIC_HIGH,
    BP_SYSTOLIC_HIGH,
    CHOLESTEROL_HIGH,
    GLUCOSE_HIGH,
    URIC_ACID_FEMALE_HIGH,
    URIC_ACID_MALE_HIGH,
    HealthProfileError,
    UserHealthProfileService,
)

# 遗留命名（R-017 修复：未知返回 None，不再动态造码）。
_allergy_to_constraint_code = allergy_to_constraint_code
_disease_to_constraint_code = disease_to_constraint_code

__all__ = [
    "ALLOWED_CONSTRAINT_CODES",
    "BP_DIASTOLIC_HIGH",
    "BP_SYSTOLIC_HIGH",
    "CHOLESTEROL_HIGH",
    "CodedHealthConstraint",
    "ConstraintEffect",
    "ConstraintScope",
    "ExplicitFoodTabooConstraint",
    "GLUCOSE_HIGH",
    "HealthGoal",
    "HealthProfileError",
    "IndicatorStatus",
    "ParticipantHealthConstraintSet",
    "ProfileValidationResult",
    "SPECIAL_STAGE_RULES",
    "TemporaryHealthConstraint",
    "URIC_ACID_FEMALE_HIGH",
    "URIC_ACID_MALE_HIGH",
    "UserHealthProfileService",
    "allergy_to_constraint_code",
    "disease_to_constraint_code",
    "indicator_to_constraint_code",
    "is_allowed_code",
    "special_group_to_constraint_code",
    "special_stage_status",
]
