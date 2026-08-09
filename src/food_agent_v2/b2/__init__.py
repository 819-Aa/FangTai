"""B2 用户健康档案模块 —— 事实读取、约束派生、投影。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from food_agent_v2.core.paths import CLEANED_USERS


# ---- 约束代码枚举 ----
# 仅列出与健康食材全集有对应关系的约束代码。
# 完整注册表由 B4 的离线覆盖审核维护。

class ConstraintScope(str, Enum):
    PERMANENT = "permanent"   # 不可覆盖
    SESSION = "session"       # 当前会话
    TURN = "turn"             # 仅本轮


class ConstraintEffect(str, Enum):
    HARD_EXCLUDE = "hard_exclude"
    SOFT_PREFER = "soft_prefer"


class IndicatorStatus(str, Enum):
    NORMAL = "normal"
    ABNORMAL = "abnormal"
    UNKNOWN = "unknown"


# ---- 确定性阈值 ----
# 收缩压 ≥ 140 或 舒张压 ≥ 90 → abnormal
BP_SYSTOLIC_HIGH = 140
BP_DIASTOLIC_HIGH = 90

# 空腹血糖 ≥ 7.0 mmol/L → abnormal
GLUCOSE_HIGH = 7.0

# 尿酸 ≥ 420 μmol/L（男）/ ≥ 360 μmol/L（女）→ abnormal
URIC_ACID_MALE_HIGH = 420
URIC_ACID_FEMALE_HIGH = 360

# 总胆固醇 ≥ 5.2 mmol/L → abnormal
CHOLESTEROL_HIGH = 5.2


@dataclass
class CodedHealthConstraint:
    """编码化的健康硬约束。"""
    constraint_code: str
    participant_ref: str
    source_refs: list[str]          # 来源事实引用
    effect: ConstraintEffect = ConstraintEffect.HARD_EXCLUDE
    scope: ConstraintScope = ConstraintScope.PERMANENT
    projection_level: str = "b4_view"  # b4_view | answer_view | query_view


@dataclass
class ExplicitFoodTabooConstraint:
    """显式食材禁忌约束。"""
    taboo_ingredient_id: int | None
    taboo_ingredient_name: str
    participant_ref: str
    source_refs: list[str]
    effect: ConstraintEffect = ConstraintEffect.HARD_EXCLUDE
    scope: ConstraintScope = ConstraintScope.PERMANENT
    projection_level: str = "b4_view"


@dataclass
class HealthGoal:
    """软健康目标。"""
    goal_code: str
    participant_ref: str
    source_refs: list[str]
    effect: ConstraintEffect = ConstraintEffect.SOFT_PREFER
    scope: ConstraintScope = ConstraintScope.SESSION
    priority: str = "user_stated"


@dataclass
class TemporaryHealthConstraint:
    """B2 验证后的临时健康约束。"""
    constraint_id: str
    constraint_code: str | None
    taboo_ingredient_name: str | None
    participant_ref: str
    source_refs: list[str]
    scope: ConstraintScope                 # session | turn
    effect: ConstraintEffect = ConstraintEffect.HARD_EXCLUDE


@dataclass
class ParticipantHealthConstraintSet:
    """单个参与者的完整有效约束集。"""
    participant_ref: str
    hard_constraints: list[CodedHealthConstraint | ExplicitFoodTabooConstraint] = field(default_factory=list)
    soft_goals: list[HealthGoal] = field(default_factory=list)
    unresolved_signals: list[dict] = field(default_factory=list)  # 待 B2 验证的原始信号


class UserHealthProfileService:
    """用户健康档案读取与约束派生服务。"""

    def __init__(self):
        self._users: dict[int, dict] = {}
        self._loaded = False

    def load(self, path: Optional[Path] = None) -> None:
        if path is None:
            path = CLEANED_USERS
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    u = json.loads(line)
                    self._users[u["user_id"]] = u
        self._loaded = True

    def get_user(self, user_id: int) -> dict | None:
        return self._users.get(user_id)

    def get_indicator_status(self, user_id: int, indicator_name: str) -> IndicatorStatus:
        """查询单个指标的状态。

        键名归一化：清洗数据中的指标键常带单位后缀（如"空腹血糖_mmol/L"、
        "血压_mmHg"），此处剥离单位后再判断；血压值为"141/89"复合格式时拆分判断。
        """
        user = self._users.get(user_id)
        if not user:
            return IndicatorStatus.UNKNOWN

        metrics = user.get("health_metrics", {})
        data = metrics.get(indicator_name, {})
        value = data.get("value")

        if value is None:
            return IndicatorStatus.UNKNOWN

        normalized = _normalize_metric_key(indicator_name)

        # 血压：可能为 "141/89" 复合格式
        if "血压" in normalized:
            try:
                parts = str(value).replace(" ", "").split("/")
                if len(parts) == 2:
                    sys_val = float(parts[0])
                    dia_val = float(parts[1])
                    if sys_val >= BP_SYSTOLIC_HIGH or dia_val >= BP_DIASTOLIC_HIGH:
                        return IndicatorStatus.ABNORMAL
                    return IndicatorStatus.NORMAL
            except (ValueError, TypeError):
                return IndicatorStatus.UNKNOWN

        try:
            v = float(value)
        except (ValueError, TypeError):
            return IndicatorStatus.UNKNOWN

        thresholds = {
            "空腹血糖": (GLUCOSE_HIGH, "ge"),
            "血糖": (GLUCOSE_HIGH, "ge"),
            "尿酸": (URIC_ACID_MALE_HIGH
                     if user.get("gender") == "男"
                     else URIC_ACID_FEMALE_HIGH, "ge"),
            "总胆固醇": (CHOLESTEROL_HIGH, "ge"),
        }

        threshold_info = thresholds.get(normalized)
        if threshold_info is None:
            return IndicatorStatus.UNKNOWN

        threshold, op = threshold_info
        if op == "ge":
            return IndicatorStatus.ABNORMAL if v >= threshold else IndicatorStatus.NORMAL
        return IndicatorStatus.UNKNOWN

    def derive_constraints(self, user_id: int, participant_ref: str) -> ParticipantHealthConstraintSet:
        """从用户档案派生完整约束集。"""
        user = self._users.get(user_id)
        if not user:
            return ParticipantHealthConstraintSet(participant_ref=participant_ref)

        constraints: list[CodedHealthConstraint | ExplicitFoodTabooConstraint] = []
        soft_goals: list[HealthGoal] = []
        source_user = f"user:{user_id}"

        # 1. 过敏 → hard_exclude constraint_code
        for allergy in user.get("allergies", []):
            code = _allergy_to_constraint_code(allergy)
            constraints.append(CodedHealthConstraint(
                constraint_code=code,
                participant_ref=participant_ref,
                source_refs=[source_user, f"allergy:{allergy}"],
                scope=ConstraintScope.PERMANENT,
            ))

        # 2. 疾病 → hard_exclude（即使当前指标正常也不失效）
        for disease in user.get("diseases", []):
            code = _disease_to_constraint_code(disease)
            constraints.append(CodedHealthConstraint(
                constraint_code=code,
                participant_ref=participant_ref,
                source_refs=[source_user, f"disease:{disease}"],
                scope=ConstraintScope.PERMANENT,
            ))

        # 3. 异常指标 → hard_exclude
        for metric_name in user.get("health_metrics", {}):
            status = self.get_indicator_status(user_id, metric_name)
            if status == IndicatorStatus.ABNORMAL:
                code = _indicator_to_constraint_code(metric_name)
                constraints.append(CodedHealthConstraint(
                    constraint_code=code,
                    participant_ref=participant_ref,
                    source_refs=[source_user, f"metric:{metric_name}"],
                    scope=ConstraintScope.PERMANENT,
                ))

        # 4. 禁忌食材 → ExplicitFoodTabooConstraint
        for taboo in user.get("taboo_ingredients", []):
            constraints.append(ExplicitFoodTabooConstraint(
                taboo_ingredient_id=None,
                taboo_ingredient_name=taboo,
                participant_ref=participant_ref,
                source_refs=[source_user, f"taboo:{taboo}"],
                scope=ConstraintScope.PERMANENT,
            ))

        # 5. 健康目标 → soft_prefer
        for goal in user.get("health_goals", []):
            soft_goals.append(HealthGoal(
                goal_code=goal,
                participant_ref=participant_ref,
                source_refs=[source_user, f"goal:{goal}"],
                scope=ConstraintScope.SESSION,
            ))

        # 6. 特殊人群/慢病 → 派生约束
        special = user.get("special_group")
        if special:
            items = special if isinstance(special, list) else [special]
            for item in items:
                code = _special_group_to_constraint_code(str(item))
                if code:
                    constraints.append(CodedHealthConstraint(
                        constraint_code=code,
                        participant_ref=participant_ref,
                        source_refs=[source_user, f"special_group:{item}"],
                        scope=ConstraintScope.PERMANENT,
                    ))

        return ParticipantHealthConstraintSet(
            participant_ref=participant_ref,
            hard_constraints=constraints,
            soft_goals=soft_goals,
        )

    def validate_temporary_signal(self, signal: dict, participant_ref: str) -> TemporaryHealthConstraint | None:
        """验证模型提出的临时健康信号。"""
        signal_type = signal.get("type", "")
        signal_value = signal.get("value", "")

        if signal_type == "allergy":
            code = _allergy_to_constraint_code(signal_value)
            return TemporaryHealthConstraint(
                constraint_id=f"temp_{participant_ref}_{code}",
                constraint_code=code,
                taboo_ingredient_name=None,
                participant_ref=participant_ref,
                source_refs=[f"user_signal:{signal_value}"],
                scope=ConstraintScope.SESSION,
            )

        if signal_type == "disease":
            return TemporaryHealthConstraint(
                constraint_id=f"temp_{participant_ref}_disease",
                constraint_code=_disease_to_constraint_code(signal_value),
                taboo_ingredient_name=None,
                participant_ref=participant_ref,
                source_refs=[f"user_signal:{signal_value}"],
                scope=ConstraintScope.SESSION,
            )

        if signal_type == "taboo":
            return TemporaryHealthConstraint(
                constraint_id=f"temp_{participant_ref}_taboo",
                constraint_code=None,
                taboo_ingredient_name=signal_value,
                participant_ref=participant_ref,
                source_refs=[f"user_signal:{signal_value}"],
                scope=ConstraintScope.TURN,
            )

        # 模糊信号 → 不创建约束，返回 None（由 C3 判断进入 needs_clarification）
        return None

    def check_permanent_constraint_override(
        self, constraint_code: str, action: str
    ) -> bool:
        """检查是否尝试覆盖永久约束。返回 True 表示覆盖被拒绝。"""
        if action == "remove":
            return True   # 永久约束不可移除
        if action == "relax":
            return True   # 永久约束不可放宽
        return False


# ---- 约束代码映射 ----

def _allergy_to_constraint_code(allergy: str) -> str:
    """过敏食材名 → constraint_code。"""
    mapping = {
        "花生": "allergy_peanut",
        "坚果": "allergy_tree_nut",
        "牛奶": "allergy_dairy",
        "鸡蛋": "allergy_egg",
        "海鲜": "allergy_seafood",
        "虾": "allergy_shrimp",
        "蟹": "allergy_crab",
        "螃蟹": "allergy_crab",
        "鱼": "allergy_fish",
        "贝类": "allergy_shellfish",
        "贝壳": "allergy_shellfish",
        "大豆": "allergy_soy",
        "豆制品": "allergy_soy",
        "豆类": "allergy_soy",
        "小麦": "allergy_wheat",
        "芝麻": "allergy_sesame",
        "芒果": "allergy_mango",
        "菠萝": "allergy_pineapple",
        "啤酒": "allergy_alcohol",
        "酒精": "allergy_alcohol",
    }
    return mapping.get(allergy.strip(), f"allergy_{allergy.strip()}")


def _disease_to_constraint_code(disease: str) -> str:
    """疾病名 → constraint_code。"""
    mapping = {
        "高血压": "disease_hypertension",
        "高血脂": "disease_hyperlipidemia",
        "高胆固醇": "disease_hypercholesterolemia",
        "高血糖": "disease_hyperglycemia",
        "糖尿病": "disease_diabetes",
        "高尿酸": "disease_hyperuricemia",
        "痛风": "disease_gout",
        "肾病": "disease_kidney",
        "脂肪肝": "disease_fatty_liver",
        "冠心病": "disease_chd",
        "肥胖": "disease_obesity",
        "贫血": "disease_anemia",
        "骨质疏松": "disease_osteoporosis",
        "甲亢": "disease_hyperthyroidism",
        "甲减": "disease_hypothyroidism",
    }
    return mapping.get(disease.strip(), f"disease_{disease.strip()}")


def _normalize_metric_key(key: str) -> str:
    """归一化指标键名：剥离单位后缀（_mmol/L、_mmHg、_umol/L 等）。"""
    k = key.strip()
    for suffix in ("_mmol/L", "_mmol/l", "_mmHg", "_umol/L", "_mg/dL", "_g/L"):
        k = k.replace(suffix, "")
    return k.strip()


def _indicator_to_constraint_code(indicator: str) -> str:
    """异常指标 → constraint_code。键名先归一化（剥离单位后缀）。"""
    normalized = _normalize_metric_key(indicator)
    mapping = {
        "血压": "indicator_high_bp",
        "收缩压": "indicator_high_bp",
        "舒张压": "indicator_high_bp",
        "空腹血糖": "indicator_high_glucose",
        "血糖": "indicator_high_glucose",
        "尿酸": "indicator_high_uric_acid",
        "总胆固醇": "indicator_high_cholesterol",
    }
    return mapping.get(normalized, f"indicator_{normalized}")


def _special_group_to_constraint_code(group: str) -> str | None:
    """特殊人群/慢病 → constraint_code。

    数据中高血压/高血糖/高尿酸等慢病存放在"特殊人群"字段，
    必须映射到 B4 有审核关系的 disease_* 约束码，否则慢病健康约束会整体丢失。
    """
    mapping = {
        "孕妇": "group_pregnancy",
        "哺乳期": "group_lactation",
        "儿童": "group_child",
        "老人": "group_elderly",
        "高血压": "disease_hypertension",
        "高血糖": "disease_diabetes",       # B1 仅维护 disease_diabetes 的关系
        "糖尿病": "disease_diabetes",
        "高尿酸": "disease_hyperuricemia",
        "痛风": "disease_gout",
        "高血脂": "disease_hyperlipidemia",
        "高胆固醇": "disease_hypercholesterolemia",
        "肥胖": "disease_obesity",
    }
    return mapping.get(group.strip())
