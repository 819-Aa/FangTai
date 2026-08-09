"""跨模块共享的权威契约（Stage S1 锁定）。

- build.py：固定源 SourceManifest 与构建清单 BuildManifest。

字段的最终可执行权威是这里的 Pydantic Schema，见
docs/contracts/data-artifact-contracts.md §1；Schema 语义进入只读集合，
后续任务不得改变，只能按新 ADR 单独申请。
"""

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

__all__ = [
    "ArtifactEntry",
    "BuildManifest",
    "QualityGateReport",
    "SourceManifest",
    "SourceManifestMismatch",
    "canonical_json_hash",
    "source_manifest_hash",
    "verify_source_file",
]
