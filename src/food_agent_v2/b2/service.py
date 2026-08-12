"""B2 用户健康档案服务（T10）。

读取固定档案 → 全校验 50 份 → 用封闭注册表派生约束（未知映射 fail-closed）
→ 临时信号验证（明确禁忌绑定标准 ingredient_id）→ 角色匿名投影。

MC-02：在线运行只经 UserProfileSource 端口读取唯一 ready MySQL 构建的固定档案
（data_builds + fixed_artifact_records），不再读取 JSONL/CSV/原始用户文件；
数据库不可用或 build 身份不一致即稳定失败，绝不回退文件。
"""

from __future__ import annotations

from food_agent_v2.b2.constraint_registry import (
    allergy_to_constraint_code,
    disease_to_constraint_code,
    indicator_to_constraint_code,
    is_allowed_code,
    special_stage_status,
)
from food_agent_v2.b2.repository import (
    MySQLUserProfileSource,
    UserProfileSource,
)
from food_agent_v2.b2.schemas import (
    CodedHealthConstraint,
    ConstraintScope,
    ExplicitFoodTabooConstraint,
    HealthGoal,
    IndicatorStatus,
    ParticipantHealthConstraintSet,
    ProfileValidationResult,
    TemporaryHealthConstraint,
)


class HealthProfileError(RuntimeError):
    """B2 档案处理确定性失败。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# ---- 确定性阈值（R-017：指标阈值是固定配置）----
BP_SYSTOLIC_HIGH = 140
BP_DIASTOLIC_HIGH = 90
GLUCOSE_HIGH = 7.0
URIC_ACID_MALE_HIGH = 420
URIC_ACID_FEMALE_HIGH = 360
CHOLESTEROL_HIGH = 5.2


def _normalize_metric_key(key: str) -> str:
    k = key.strip()
    for suffix in ("_mmol/L", "_mmol/l", "_mmHg", "_umol/L", "_mg/dL", "_g/L"):
        k = k.replace(suffix, "")
    return k.strip()


def _resolve_taboo_ingredient_id(resolver: object, name: str) -> int | None:
    """通过 B3 IngredientIdentityResolver 端口（或兼容 callable）解析标准 ingredient_id。

    B3 端口返回 IngredientIdentity(identity, ingredient_id)；兼容直接返回 int 的
    callable 与返回 identity 对象的 callable。
    """
    if resolver is None:
        return None
    resolve_fn = getattr(resolver, "resolve", None)
    result = resolve_fn(name) if callable(resolve_fn) else (resolver(name) if callable(resolver) else None)

    if result is not None and getattr(result, "identity", None) == "resolved":
        ingredient_id = getattr(result, "ingredient_id", None)
        if isinstance(ingredient_id, int):
            return ingredient_id
    if isinstance(result, int):
        return result
    return None


class UserHealthProfileService:
    """用户健康档案读取、约束派生与角色投影。

    默认生产 source 为 MySQLUserProfileSource（唯一 ready 构建只读）；
    单元测试注入 InMemory/Fake source，禁止服务走文件。
    """

    def __init__(self, source: UserProfileSource | None = None) -> None:
        self._users: dict[int, dict] = {}
        self._loaded = False
        self._source = source or MySQLUserProfileSource()

    def load(self, expected_build_id: str | None = None) -> None:
        """从固定档案来源加载；expected_build_id 与 ready build 不一致即失败。

        每次 load 前清空旧缓存；失败后不保留上一次用户数据（fail-closed）。
        """
        self._users = {}
        self._loaded = False
        records = self._source.load_users(expected_build_id=expected_build_id)
        for record in records:
            self._users[int(record["user_id"])] = record
        self._loaded = True

    def get_user(self, user_id: int) -> dict | None:
        return self._users.get(user_id)

    # ---- 指标状态（确定性阈值）----

    def get_indicator_status(self, user_id: int, indicator_name: str) -> IndicatorStatus:
        user = self._users.get(user_id)
        if not user:
            return IndicatorStatus.UNKNOWN
        metrics = user.get("health_metrics", {})
        data = metrics.get(indicator_name, {})
        value = data.get("value")
        if value is None:
            return IndicatorStatus.UNKNOWN

        normalized = _normalize_metric_key(indicator_name)

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
            "尿酸": (URIC_ACID_MALE_HIGH if user.get("gender") == "男" else URIC_ACID_FEMALE_HIGH, "ge"),
            "总胆固醇": (CHOLESTEROL_HIGH, "ge"),
        }
        threshold_info = thresholds.get(normalized)
        if threshold_info is None:
            return IndicatorStatus.UNKNOWN
        threshold, _op = threshold_info
        return IndicatorStatus.ABNORMAL if v >= threshold else IndicatorStatus.NORMAL

    # ---- 固定档案校验（50 份全部）----

    def validate_profile(self, user_id: int) -> ProfileValidationResult:
        user = self._users.get(user_id)
        if not user:
            return ProfileValidationResult(user_id=user_id, valid=False, errors=["档案不存在"])
        result = ProfileValidationResult(user_id=user_id, valid=True)
        uid = user.get("user_id")
        if not isinstance(uid, int):
            result.errors.append("user_id 缺失或非整数")
        if user.get("gender") not in ("男", "女"):
            result.errors.append(f"性别未知: {user.get('gender')!r}")
        if not isinstance(user.get("age"), int):
            result.errors.append("age 缺失或非整数")

        for allergy in user.get("allergies", []) or []:
            code = allergy_to_constraint_code(allergy)
            if not is_allowed_code(code):
                result.errors.append(f"过敏未映射到封闭代码: {allergy}")
        for disease in user.get("diseases", []) or []:
            code = disease_to_constraint_code(disease)
            if not is_allowed_code(code):
                result.errors.append(f"疾病未映射到封闭代码: {disease}")
        for stage in _as_list(user.get("special_group")):
            _, known = special_stage_status(stage)
            if not known:
                result.errors.append(f"特殊阶段不在显式规则中: {stage}")
        for metric_name in (user.get("health_metrics") or {}):
            if self.get_indicator_status(user_id, metric_name) == IndicatorStatus.ABNORMAL:
                code = indicator_to_constraint_code(metric_name)
                if not is_allowed_code(code):
                    result.errors.append(f"异常指标未映射到封闭代码: {metric_name}")

        # 备孕等已知但无批准代码 → warning，不视为无效。
        for stage in _as_list(user.get("special_group")):
            if stage == "备孕":
                result.warnings.append("备孕为已知阶段但封闭注册表无批准代码，需人工审查")

        result.valid = not result.errors
        if result.valid:
            # 仅在结构有效时派生计数，避免无效档案触发 fail-closed 异常。
            result.derived_constraint_count = len(
                self.derive_constraints(user_id, f"p{user_id}").hard_constraints
            )
        return result

    def validate_all(self) -> dict[str, list]:
        results = {uid: self.validate_profile(uid) for uid in sorted(self._users)}
        return {
            "profile_count": len(results),
            "valid_count": sum(1 for r in results.values() if r.valid),
            "invalid_count": sum(1 for r in results.values() if not r.valid),
            "profiles": [
                {
                    "user_id": r.user_id,
                    "valid": r.valid,
                    "errors": r.errors,
                    "warnings": r.warnings,
                    "derived_constraint_count": r.derived_constraint_count,
                }
                for r in results.values()
            ],
        }

    # ---- 约束派生（封闭注册表，未知映射 fail-closed）----

    def derive_constraints(self, user_id: int, participant_ref: str) -> ParticipantHealthConstraintSet:
        user = self._users.get(user_id)
        if not user:
            raise HealthProfileError("HEALTH_PROFILE_NOT_FOUND", f"user {user_id} 无固定档案")

        constraints: list[CodedHealthConstraint | ExplicitFoodTabooConstraint] = []
        soft_goals: list[HealthGoal] = []
        unresolved: list[dict] = []
        source_user = f"user:{user_id}"

        for allergy in user.get("allergies", []) or []:
            code = allergy_to_constraint_code(allergy)
            if not is_allowed_code(code):
                raise HealthProfileError(
                    "HEALTH_PROFILE_DATA_INVALID", f"过敏 {allergy} 未映射到封闭代码"
                )
            constraints.append(
                CodedHealthConstraint(
                    constraint_code=code,
                    participant_ref=participant_ref,
                    source_refs=[source_user, f"allergy:{allergy}"],
                    scope=ConstraintScope.PERMANENT,
                )
            )

        for disease in user.get("diseases", []) or []:
            code = disease_to_constraint_code(disease)
            if not is_allowed_code(code):
                raise HealthProfileError(
                    "HEALTH_PROFILE_DATA_INVALID", f"疾病 {disease} 未映射到封闭代码"
                )
            constraints.append(
                CodedHealthConstraint(
                    constraint_code=code,
                    participant_ref=participant_ref,
                    source_refs=[source_user, f"disease:{disease}"],
                    scope=ConstraintScope.PERMANENT,
                )
            )

        for metric_name in user.get("health_metrics", {}) or {}:
            if self.get_indicator_status(user_id, metric_name) == IndicatorStatus.ABNORMAL:
                code = indicator_to_constraint_code(metric_name)
                if not is_allowed_code(code):
                    raise HealthProfileError(
                        "HEALTH_PROFILE_DATA_INVALID", f"指标 {metric_name} 未映射到封闭代码"
                    )
                constraints.append(
                    CodedHealthConstraint(
                        constraint_code=code,
                        participant_ref=participant_ref,
                        source_refs=[source_user, f"metric:{metric_name}"],
                        scope=ConstraintScope.PERMANENT,
                    )
                )

        for taboo in user.get("taboo_ingredients", []) or []:
            unresolved.append(
                {
                    "participant_ref": participant_ref,
                    "kind": "explicit_food_taboo",
                    "name": taboo,
                    "reason": "固定档案禁忌需在初始化期解析为标准 ingredient_id",
                }
            )

        for goal in user.get("health_goals", []) or []:
            soft_goals.append(
                HealthGoal(
                    goal_code=goal,
                    participant_ref=participant_ref,
                    source_refs=[source_user, f"goal:{goal}"],
                    scope=ConstraintScope.SESSION,
                )
            )

        for stage in _as_list(user.get("special_group")):
            code, known = special_stage_status(stage)
            if not known:
                raise HealthProfileError(
                    "HEALTH_PROFILE_DATA_INVALID", f"特殊阶段 {stage} 不在显式规则中"
                )
            if code:
                constraints.append(
                    CodedHealthConstraint(
                        constraint_code=code,
                        participant_ref=participant_ref,
                        source_refs=[source_user, f"special_group:{stage}"],
                        scope=ConstraintScope.PERMANENT,
                    )
                )
            else:
                # 备孕等已知但无批准代码 → 待澄清/审查信号，不形成硬约束。
                unresolved.append(
                    {
                        "participant_ref": participant_ref,
                        "kind": "special_stage_without_approved_code",
                        "stage": stage,
                        "reason": "已知阶段但封闭注册表无批准代码",
                    }
                )

        return ParticipantHealthConstraintSet(
            participant_ref=participant_ref,
            hard_constraints=constraints,
            soft_goals=soft_goals,
            unresolved_signals=unresolved,
        )

    # ---- 临时信号验证（明确禁忌绑定标准 ingredient_id）----

    def validate_temporary_signal(
        self,
        signal: dict,
        participant_ref: str,
        *,
        ingredient_resolver: object | None = None,
    ) -> TemporaryHealthConstraint | None:
        signal_type = signal.get("type", "")
        signal_value = signal.get("value", "")

        if signal_type == "allergy":
            code = allergy_to_constraint_code(signal_value)
            if not is_allowed_code(code):
                raise HealthProfileError("HEALTH_SIGNAL_AMBIGUOUS", f"过敏 {signal_value} 无法封闭映射")
            return TemporaryHealthConstraint(
                constraint_id=f"temp_{participant_ref}_{code}",
                constraint_code=code,
                taboo_ingredient_name=None,
                taboo_ingredient_id=None,
                participant_ref=participant_ref,
                source_refs=[f"user_signal:{signal_value}"],
                scope=ConstraintScope.SESSION,
            )

        if signal_type == "disease":
            code = disease_to_constraint_code(signal_value)
            if not is_allowed_code(code):
                raise HealthProfileError("HEALTH_SIGNAL_AMBIGUOUS", f"疾病 {signal_value} 无法封闭映射")
            return TemporaryHealthConstraint(
                constraint_id=f"temp_{participant_ref}_{code}",
                constraint_code=code,
                taboo_ingredient_name=None,
                taboo_ingredient_id=None,
                participant_ref=participant_ref,
                source_refs=[f"user_signal:{signal_value}"],
                scope=ConstraintScope.SESSION,
            )

        if signal_type == "taboo":
            resolved_id = _resolve_taboo_ingredient_id(ingredient_resolver, signal_value)
            if resolved_id is None:
                raise HealthProfileError(
                    "HEALTH_SIGNAL_AMBIGUOUS", f"禁忌 {signal_value} 未解析到标准 ingredient_id"
                )
            return TemporaryHealthConstraint(
                constraint_id=f"temp_{participant_ref}_taboo_{resolved_id}",
                constraint_code=None,
                taboo_ingredient_name=signal_value,
                taboo_ingredient_id=resolved_id,
                participant_ref=participant_ref,
                source_refs=[f"user_signal:{signal_value}"],
                scope=ConstraintScope.TURN,
            )

        raise HealthProfileError("HEALTH_SIGNAL_AMBIGUOUS", f"无法识别的信号类型: {signal_type}")

    def check_permanent_constraint_override(self, constraint_code: str, action: str) -> bool:
        if action in ("remove", "relax"):
            return True  # 永久约束不可移除/放宽
        return False

    # ---- 角色匿名投影 ----

    def project_health_context(self, role: str, constraint_sets: list[ParticipantHealthConstraintSet]) -> list[dict]:
        """按角色投影，只暴露匿名 participant_ref 与最小约束摘要。"""
        if role not in ("query_view", "b4_view", "model_view"):
            raise HealthProfileError("HEALTH_CONTEXT_PROJECTION_FAILED", f"未知角色 {role}")
        projection: list[dict] = []
        for cs in constraint_sets:
            codes = sorted(
                {c.constraint_code for c in cs.hard_constraints if hasattr(c, "constraint_code") and c.constraint_code}
            )
            item: dict = {"participant_ref": cs.participant_ref}
            if role == "b4_view":
                item["constraint_codes"] = codes
                item["constraints"] = [
                    {
                        "constraint_code": c.constraint_code,
                        "scope": c.scope.value,
                        "effect": c.effect.value,
                        "source_refs": c.source_refs,
                    }
                    for c in cs.hard_constraints
                    if hasattr(c, "constraint_code") and c.constraint_code
                ]
                item["unresolved_signals"] = cs.unresolved_signals
            elif role == "model_view":
                item["constraint_codes"] = codes
            # query_view：仅匿名引用，不含约束细节。
            projection.append(item)
        return projection


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]
