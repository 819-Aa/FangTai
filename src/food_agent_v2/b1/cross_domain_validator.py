"""对 BuildManifest 所声明 Artifact 执行跨域一致性复验。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from food_agent_v2.b1.quality_gates import (
    DataQualityError,
    evaluate_staging_quality,
)


def validate(
    artifact_paths: Mapping[str, Path],
    *,
    build_id: str,
    source_manifest_hash: str,
    report_path: Path | None = None,
) -> dict:
    """只接受 manifest Artifact 路径，不读取旧 cleaned 目录或旧时间字段。"""
    try:
        gates, metrics = evaluate_staging_quality(
            dict(artifact_paths),
            build_id=build_id,
            source_manifest_hash=source_manifest_hash,
        )
        report = {
            "stage": "cross_domain_validation",
            "status": "passed",
            "gate_count": len(gates),
            "gates": [gate.as_dict() for gate in gates],
            "metrics": metrics,
            "errors": [],
        }
    except DataQualityError as exc:
        report = {
            "stage": "cross_domain_validation",
            "status": "failed",
            "errors": [{"code": exc.code, "detail": exc.message}],
        }
    if report_path is not None:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return report
