"""跨模块共享的权威契约（Stage S1 锁定）。

- build.py：固定源 SourceManifest 与构建清单 BuildManifest。
- artifacts.py：七类在线 Artifact 严格 Schema（禁额外字段）。
- receipts.py：工具回执与 request/node/input 绑定校验。
- status.py：请求状态、判定枚举与散列格式。

字段的最终可执行权威是这里的 Pydantic Schema，见
docs/contracts/data-artifact-contracts.md §1；Schema 语义进入只读集合，
后续任务不得改变，只能按新 ADR 单独申请。
"""

from food_agent_v2.contracts.artifacts import (
    AnswerArtifact,
    AnswerContent,
    ArtifactIntegrityError,
    FeasibleMenu,
    FeasibleMenuArtifact,
    FinalValidationArtifact,
    HealthEvaluationArtifact,
    MenuDecisionArtifact,
    MenuScoreDecomposition,
    ParticipantRecipeHealthResult,
    QueryPlanArtifact,
    RecipeGroupHealthResult,
    ReviewArtifact,
    UserVisibleAnalysis,
    validate_answer_menu_binding,
)
from food_agent_v2.contracts.build import (
    ArtifactEntry,
    BuildManifest,
    QualityGateReport,
    SourceManifest,
    SourceManifestMismatch,
    canonical_json_hash,
    source_manifest_hash,
    verify_source_file,
)
from food_agent_v2.contracts.receipts import (
    ReceiptBindingError,
    ToolReceipt,
    validate_receipt_binding,
)
from food_agent_v2.contracts.status import (
    HealthVerdict,
    RequestStatus,
    ReviewVerdict,
    Sha256Hash,
)

__all__ = [
    "AnswerArtifact",
    "AnswerContent",
    "ArtifactEntry",
    "ArtifactIntegrityError",
    "BuildManifest",
    "FeasibleMenu",
    "FeasibleMenuArtifact",
    "FinalValidationArtifact",
    "HealthEvaluationArtifact",
    "HealthVerdict",
    "MenuDecisionArtifact",
    "MenuScoreDecomposition",
    "ParticipantRecipeHealthResult",
    "QualityGateReport",
    "QueryPlanArtifact",
    "ReceiptBindingError",
    "RecipeGroupHealthResult",
    "RequestStatus",
    "ReviewArtifact",
    "ReviewVerdict",
    "Sha256Hash",
    "SourceManifest",
    "SourceManifestMismatch",
    "ToolReceipt",
    "UserVisibleAnalysis",
    "canonical_json_hash",
    "source_manifest_hash",
    "validate_answer_menu_binding",
    "validate_receipt_binding",
    "verify_source_file",
]
