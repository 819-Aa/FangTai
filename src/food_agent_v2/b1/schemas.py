"""B1 输出物 Schema 定义。

B1 生成数据，Schema 由 B1 自身定义（B2-B6 在运行时消费这些数据的同时定义自己的领域 Schema）。
B1 的 Schema 关注输出结构、跨域引用一致性和数据质量元数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

# ---- 固定 2,000 条源数据的权威常量（data-artifact-contracts.md §2.1）----
SOURCE_ID = "fixed-recipes-2000"
SOURCE_ENCODING = "GBK"
SOURCE_RELATIVE_PATH = "data/raw/recipes_sample_2000.csv"
SOURCE_BYTE_SIZE = 1_138_083
SOURCE_SHA256 = "B2177DC6CDCAE24FC5671C8DADA44295228F4301E3CE620ED11D51B1ABFE4371"
SOURCE_ROW_COUNT = 2_000
SOURCE_HEADERS = ("名称", "食材清单", "烹饪步骤", "label")


class RecordType(StrEnum):
    """B1 识别的记录分类。详情归 B3 定义。"""
    DISH = "dish"
    PREPARATION = "preparation"
    MEAL_BUNDLE = "meal_bundle"
    COOKING_PROGRAM = "cooking_program"
    TEST_RECORD = "test_record"
    UNKNOWN = "unknown"


class StepType(StrEnum):
    ACTIVE = "active"        # 切配等需要人主动操作
    EQUIPMENT = "equipment"  # 炒、煎、烤、蒸等占用设备
    PASSIVE = "passive"      # 醒面、冷藏、腌制等不占用人与设备


class MatchMethod(StrEnum):
    EXACT = "exact"          # ingredient_id 精确匹配
    ALIAS = "alias"          # 审核别名匹配
    ESTIMATE = "estimate"    # 可解释估算
    NOT_FOUND = "not_found"  # 无匹配


class IngredientIdentity(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class CompositionStatus(StrEnum):
    ATOMIC = "atomic"        # 无复合组成，或全部子项已独立解析
    COMPLETE = "complete"    # 所有子项身份已解析
    INVALID = "invalid"      # 存在循环引用或不可解析的子项


class RelationReview(StrEnum):
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"


class StepAtom(BaseModel):
    """确定性步骤原子；仅在离线时间图构建期间使用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    atom_id: str
    source_step_index: int
    text: str
    explicit_duration_seconds: int | None
    duration_locked: bool

    @model_validator(mode="after")
    def validate_explicit_duration(self) -> "StepAtom":
        if self.source_step_index < 1:
            raise ValueError("source_step_index 必须从 1 开始")
        if not self.atom_id or not self.text.strip():
            raise ValueError("atom_id 和 text 不得为空")
        if self.explicit_duration_seconds is not None and self.explicit_duration_seconds < 0:
            raise ValueError("显式时长不得为负")
        if self.duration_locked != (self.explicit_duration_seconds is not None):
            raise ValueError("duration_locked 必须与显式时长是否存在一致")
        return self


class StepTask(BaseModel):
    """发布给 B5 的最终单值时间任务。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    atom_id: str
    text: str
    duration_seconds: int
    task_type: Literal[
        "manual",
        "attended_equipment",
        "unattended_equipment",
        "passive",
        "non_task",
    ]
    resources: tuple[str, ...]
    depends_on: tuple[str, ...]

    @model_validator(mode="after")
    def validate_duration(self) -> "StepTask":
        if self.task_type == "non_task":
            if self.duration_seconds != 0:
                raise ValueError("non_task 时长必须为 0")
        elif self.duration_seconds <= 0:
            raise ValueError("真实任务时长必须为正整数")
        return self


@dataclass
class OccurrenceRecord:
    """单个食材 occurence 的解析结果。"""
    raw_text: str
    name_clean: str                     # 去除数量前缀后的食材名
    ingredient_id: int | None = None   # 解析后的标准食材 ID
    identity: IngredientIdentity = IngredientIdentity.NOT_FOUND
    is_optional: bool = False
    belongs_to_choice_group: int | None = None  # ChoiceGroup ID
    quantity_raw: str | None = None
    unit_raw: str | None = None
    alternatives: list[str] = field(default_factory=list)  # 替代食材原文


@dataclass
class StepTaskRecord:
    """单步结构化步骤。"""
    step_index: int
    description_raw: str
    description_clean: str
    step_type: StepType = StepType.ACTIVE
    duration_seconds: int | None = None
    duration_confidence: str | None = None  # high | medium | low | null
    depends_on: list[int] = field(default_factory=list)   # 前置 step_index
    equipment_type: str | None = None
    mutex_key: str | None = None


@dataclass
class RecipeCleaningOutput:
    """单道菜的清洗输出。"""
    source_row_number: int
    recipe_id: int                     # 稳定 1-2000
    name: str
    ingredients_raw: str
    steps_raw: str
    labels_raw: str
    record_type: RecordType = RecordType.UNKNOWN
    variant_index: int = 1
    variant_count: int = 1
    label_empty: bool = False
    steps_empty: bool = False
    cleaning_notes: list[str] = field(default_factory=list)


@dataclass
class UserCleaningOutput:
    """单个用户档案的清洗输出。"""
    source_user_id: int               # 1-50
    user_id: int                      # 稳定 ID
    gender: str | None = None
    age: int | None = None
    activity_level: str | None = None
    special_group: str | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    bmi: float | None = None
    dietary_preferences: list[str] = field(default_factory=list)
    allergies: list[str] = field(default_factory=list)
    health_goals: list[str] = field(default_factory=list)
    health_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    diseases: list[str] = field(default_factory=list)
    taboo_ingredients: list[str] = field(default_factory=list)
    raw_facts: dict[str, Any] = field(default_factory=dict)
    parse_quality: dict[str, Any] = field(default_factory=dict)
    cleaning_notes: list[str] = field(default_factory=list)


@dataclass
class IngredientRecord:
    """标准食材记录。"""
    ingredient_id: int
    name_canonical: str
    category: str | None = None
    family_id: int | None = None      # 食材族 ID
    aliases: list[str] = field(default_factory=list)
    is_edible: bool = True            # 非食用材料 = False
    composition: dict[str, Any] | None = None  # 复合组成的子食材及比例


@dataclass
class HealthRelationRecord:
    """单条约束—食材关系。"""
    constraint_code: str
    ingredient_id: int
    review_status: RelationReview = RelationReview.PENDING
    hard_filter: bool = True          # 默认为硬约束
    evidence_ref: str | None = None
    notes: str | None = None


@dataclass
class CoverageAuditRecord:
    """覆盖审核状态。"""
    constraint_code: str
    expected_ingredient_count: int     # 健康食材全集中应审核的数量
    reviewed_count: int               # 实际已审核（approved + rejected）的数量
    approved_count: int
    pending_count: int
    rejected_count: int
    is_complete: bool                 # reviewed_count == expected_ingredient_count


@dataclass
class NutritionFeatureRecord:
    """单道菜的营养特征。"""
    recipe_id: int
    match_method: MatchMethod          # 整体匹配方法
    confidence: str                    # high | medium | low
    coverage_ratio: float              # 数量覆盖比例 [0, 1]
    nutrient_values: dict[str, float] = field(default_factory=dict)  # 内部使用，不进入回答


@dataclass
class RagDocumentRecord:
    """RAG 检索文档。"""
    recipe_id: int
    document_id: str
    name: str
    searchable_text: str               # 不含健康字段和营养值
    searchable_fields: dict[str, str] = field(default_factory=dict)
    step_summary: str | None = None
    time_reference: str | None = None


@dataclass
class PipelineStage:
    """单个管线阶段的执行记录。"""
    name: str
    module: str
    status: str                        # passed | failed
    elapsed_ms: float = 0
    return_code: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineRun:
    """一次完整管线执行的记录。"""
    status: str                        # passed | failed
    elapsed_ms: float = 0
    stages: list[PipelineStage] = field(default_factory=list)


class ClassificationReview(StrEnum):
    """分类审核状态：发布门禁要求 pending=0。"""
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"


@dataclass
class SourceRecipeRow:
    """固定源单行的事实记录（recipe_id 严格等于行号 1..2000）。"""
    recipe_id: int                     # row_index，禁止名称排序/变体重编号
    source_row_number: int             # 源文件中的行号
    name: str
    ingredients_raw: str
    steps_raw: str
    labels_raw: str
    row_sha256: str                    # 源行规范散列（审计/守恒证明）


@dataclass
class RecipeClassification:
    """单行菜品分类：封闭类型 + 规则证据 + 审核状态。"""
    recipe_id: int
    record_type: RecordType            # dish|preparation|meal_bundle|cooking_program|test_record
    rule_evidence: list[str] = field(default_factory=list)
    confidence: str = "high"           # high | medium | low
    review_status: ClassificationReview = ClassificationReview.APPROVED


@dataclass
class ClassificationOverride:
    """人工对争议分类的签名覆盖。"""
    recipe_id: int
    record_type: RecordType
    reason: str
    reviewer: str
    reviewed_at: str
