"""七类在线 Artifact 严格 Schema（T03）。

依据 docs/modules/09-agent-workflow.md、04-health-rule-engine.md、
08-menu-planning.md、12-answer-and-frontend.md。全部 Artifact 禁止额外字段
（extra=forbid），严格时间、软维度 availability、证据引用和所有散列均为
显式字段。未知 verdict 一律拒绝；答案菜单散列必须与最终校验一致（INV-005）。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from food_agent_v2.contracts.status import (
    HealthVerdict,
    ReviewVerdict,
    Sha256Hash,
)

DominantObjective = Literal["balanced", "preference", "nutrition", "quick", "diverse"]


class UserVisibleAnalysis(BaseModel):
    """用户可见的阶段分析摘要（不含健康结论与内部数值）。"""

    model_config = ConfigDict(extra="forbid")

    stage: str
    title: str
    summary: str
    evidence_refs: tuple[str, ...] = ()


class QueryPlanArtifact(BaseModel):
    """查询理解模型的输出：结构化检索需求 + B2/C1 分离的排除项。"""

    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    request_id: UUID
    participant_refs: tuple[str, ...]
    schema_version: Literal["2.0.0"] = "2.0.0"

    # 非健康检索需求（C1 消费）
    rewritten_query: str = ""
    meal_types: tuple[str, ...] = ()
    population_tags: tuple[str, ...] = ()
    dish_types: tuple[str, ...] = ()
    taste_tags: tuple[str, ...] = ()
    cuisine_tags: tuple[str, ...] = ()
    scenario_tags: tuple[str, ...] = ()
    include_ingredients: tuple[str, ...] = ()
    exclude_ingredients: tuple[str, ...] = ()
    nutrition_goal_codes: tuple[str, ...] = ()
    dish_count_requested: int | None = None

    # 健康排除走 B2/B4；普通食材排除由 C1 使用 exclude_ingredients。
    health_exclusions: tuple[str, ...] = ()

    # 时间：仅记录用户意图，严格可行性由 B5/C2 判定
    time_constraint_seconds: int | None = None
    time_constraint_policy: Literal["flexible", "hard"] = "flexible"

    evidence_refs: tuple[str, ...] = ()
    input_fingerprint: Sha256Hash
    content_hash: Sha256Hash

    @model_validator(mode="before")
    @classmethod
    def project_legacy_retrieval_fields(cls, value):
        """过渡期只接受已知 V1 字段，并把它们投影成 V2 输出。"""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        projections = (
            ("flavor_preferences", "taste_tags", True),
            ("cuisine_preferences", "cuisine_tags", True),
            ("preferred_ingredients", "include_ingredients", True),
            ("preference_exclusions", "exclude_ingredients", True),
            ("meal_type", "meal_types", False),
            ("scenario", "scenario_tags", False),
        )
        for old_name, new_name, is_collection in projections:
            old_value = data.pop(old_name, None)
            if new_name in data or old_value in (None, "", (), []):
                continue
            data[new_name] = tuple(old_value) if is_collection else (str(old_value),)
        data.pop("cooking_methods", None)
        data.pop("diversity_requirements", None)
        return data


class ParticipantRecipeHealthResult(BaseModel):
    """单个参与者对单道菜的健康判定结果。"""

    model_config = ConfigDict(extra="forbid")

    participant_ref: str
    recipe_id: int
    status: HealthVerdict
    constraint_refs: tuple[str, ...] = ()
    evaluated_ingredient_ids: tuple[int, ...] = ()
    ingredient_set_evidence_paths: tuple[str, ...] = ()
    coverage_refs: tuple[str, ...] = ()
    exclusion_hits: tuple[str, ...] = ()
    input_fingerprint: Sha256Hash


class RecipeGroupHealthResult(BaseModel):
    """全员交集后的单菜结果：只有全部参与者 PASS 才为 PASS。"""

    model_config = ConfigDict(extra="forbid")

    recipe_id: int
    participant_result_refs: tuple[str, ...]
    group_status: HealthVerdict


class HealthEvaluationArtifact(BaseModel):
    """B4 候选批次健康审查产物。safe_recipe_ids 与 B4 回执一致，模型不能删改。"""

    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    request_id: UUID
    participant_refs: tuple[str, ...]
    safe_recipe_ids: tuple[int, ...]
    excluded_recipe_ids: tuple[int, ...]
    participant_recipe_results: tuple[ParticipantRecipeHealthResult, ...]
    recipe_group_results: tuple[RecipeGroupHealthResult, ...]
    relation_evidence_refs: tuple[str, ...] = ()
    constraint_set_refs: tuple[str, ...] = ()
    coverage_refs: tuple[str, ...] = ()
    input_fingerprint: Sha256Hash
    content_hash: Sha256Hash


class MenuScoreDecomposition(BaseModel):
    """菜单软评分分解。不可用维度必须显式记录并重新归一化权重。"""

    model_config = ConfigDict(extra="forbid")

    total_score: float
    rag_score: float
    preference_score: float
    nutrition_score: float
    time_score: float
    diversity_score: float
    historical_score: float
    unavailable_dimensions: tuple[str, ...] = ()
    weights_applied: dict[str, float] = {}


class FeasibleMenu(BaseModel):
    """单个可行菜单方案，时间字段均为任务图产生的预计值。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    recipe_ids: tuple[int, ...]
    menu_hash: Sha256Hash
    score_decomposition: MenuScoreDecomposition
    differing_recipe_ids: tuple[int, ...] = ()
    dominant_objective: DominantObjective = "balanced"
    estimated_time_feasible: bool
    estimated_makespan_seconds: int
    user_visible_analysis: UserVisibleAnalysis | None = None

    @model_validator(mode="before")
    @classmethod
    def project_legacy_time_fields(cls, value):
        """过渡期读取 V1 时间字段，但序列化时只保留 V2 预计值。"""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy_feasible = data.pop("strict_time_feasible", None)
        legacy_makespan = data.pop("makespan_seconds", None)
        if "estimated_time_feasible" not in data and legacy_feasible is not None:
            data["estimated_time_feasible"] = legacy_feasible is True
        if "estimated_makespan_seconds" not in data and legacy_makespan is not None:
            data["estimated_makespan_seconds"] = int(legacy_makespan)
        return data


class FeasibleMenuArtifact(BaseModel):
    """C2 生成的 3-5 个可行方案，全部菜品 ∈ safe_recipe_ids。"""

    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    request_id: UUID
    safe_recipe_ids_ref: str
    constraint_set_refs: tuple[str, ...] = ()
    menus: tuple[FeasibleMenu, ...]
    common_to_all: tuple[int, ...] = ()
    exclusive_to_plan: dict[str, tuple[int, ...]] = {}
    trade_off_summary: str | None = None
    hard_constraints_applied: tuple[str, ...] = ()
    soft_objectives_applied: tuple[str, ...] = ()
    input_fingerprint: Sha256Hash
    content_hash: Sha256Hash


class MenuDecisionArtifact(BaseModel):
    """菜单决策模型的输出：从已有 plan_id 中选择并经最终校验 PASS。"""

    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    request_id: UUID
    plan_id: str
    recipe_ids: tuple[int, ...]
    menu_hash: Sha256Hash
    feasible_menu_artifact_ref: str
    final_validation_ref: str
    participant_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    content_hash: Sha256Hash


class FinalValidationArtifact(BaseModel):
    """B4 最终菜单健康复核产物（INV-001）。"""

    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    request_id: UUID
    plan_id: str
    menu_artifact_ref: str
    participant_refs: tuple[str, ...]
    recipe_ids: tuple[int, ...]
    participant_recipe_results: tuple[ParticipantRecipeHealthResult, ...]
    relation_evidence_refs: tuple[str, ...] = ()
    menu_hash: Sha256Hash
    input_fingerprint: Sha256Hash
    status: HealthVerdict


class AnswerContent(BaseModel):
    """回答正文：结论在前，引用已验证事实，不含内部数值/疾病名/身份。"""

    model_config = ConfigDict(extra="forbid")

    conclusion: str
    menu_summary: str
    reasoning_summary: str = ""
    health_note: str = ""
    time_note: str = ""
    step_highlights: str = ""


class AnswerArtifact(BaseModel):
    """回答模型的输出（INV-005）：只能描述所选且经最终校验通过的菜单。"""

    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    request_id: UUID
    plan_id: str
    menu_ref: str
    final_validation_ref: str
    recipe_ids: tuple[int, ...]
    menu_hash: Sha256Hash
    content: AnswerContent
    user_visible_analysis: tuple[UserVisibleAnalysis, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    content_hash: Sha256Hash


class ReviewArtifact(BaseModel):
    """统一审查的输出：PASS 或 REVISION_REQUIRED（含 target_node 与 issue_codes）。"""

    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    request_id: UUID
    status: ReviewVerdict
    target_node: str | None = None
    issue_codes: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    content_hash: Sha256Hash

    @model_validator(mode="after")
    def _revision_requires_target(self) -> "ReviewArtifact":
        if self.status == "REVISION_REQUIRED" and (not self.target_node or not self.issue_codes):
            raise ValueError("REVISION_REQUIRED 必须包含 target_node 与 issue_codes")
        return self


class ArtifactIntegrityError(Exception):
    """跨 Artifact 一致性违约（INV-001/INV-005）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def validate_answer_menu_binding(
    answer: AnswerArtifact,
    decision: MenuDecisionArtifact,
    final: FinalValidationArtifact,
) -> None:
    """回答不得改变已验证菜单（INV-005）。任何不一致即拒绝。"""
    problems: list[str] = []
    if answer.plan_id != decision.plan_id:
        problems.append("plan_id")
    if answer.menu_hash != decision.menu_hash or answer.menu_hash != final.menu_hash:
        problems.append("menu_hash")
    if set(answer.recipe_ids) - set(decision.recipe_ids):
        problems.append("recipe_ids")
    if final.status != "PASS":
        problems.append("final_validation_status")
    if problems:
        raise ArtifactIntegrityError("ANSWER_MENU_MISMATCH", f"不一致字段: {problems}")
