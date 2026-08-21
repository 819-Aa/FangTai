"""Small verified-build fixture for storage-boundary integration tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from food_agent_v2.b1.quality_gates import REQUIRED_ARTIFACTS
from food_agent_v2.contracts.build import ArtifactEntry, BuildManifest, QualityGateReport

BUILD_ID = UUID("33333333-3333-3333-3333-333333333333")
SOURCE_HASH = "b" * 64


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def verified_initialization_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal manifest already considered semantically verified.

    Full-data rebuild tests intentionally wait for the human-reviewed time/profile inputs;
    storage transaction tests only need the post-verification manifest boundary.
    """
    records = {
        name: {"build_id": str(BUILD_ID), "source_manifest_hash": SOURCE_HASH}
        for name in REQUIRED_ARTIFACTS
    }
    records["recipe_source_rows"].update({
        "recipe_id": 1,
        "name": "清蒸鱼",
        "ingredients_raw": "鱼500克",
        "steps_raw": "蒸10分钟",
        "labels_raw": "晚餐",
    })
    records["nutrition_features"].update({
        "recipe_id": 1,
        "available": False,
        "raw_edible_input_weight_g": None,
        "raw_nutrition_total": None,
        "raw_nutrition_per_100g": None,
        "reason": "mapping_missing",
    })
    records["step_tasks"].update({
        "recipe_id": 1,
        "recipe_name": "清蒸鱼",
        "active_seconds": 0,
        "estimated_elapsed_seconds": 600,
        "step_tasks": [{
            "atom_id": "r1-s1-a1",
            "text": "蒸10分钟",
            "duration_seconds": 600,
            "task_type": "unattended_equipment",
            "resources": ["steamer"],
            "depends_on": [],
        }],
    })
    records["rag_documents"].update({
        "recipe_id": 1,
        "name": "清蒸鱼",
        "label_tags": ["晚餐"],
        "meal_tags": ["晚餐"],
        "population_tags": ["老人"],
        "dish_type_tags": ["主菜"],
        "taste_tags": ["清淡"],
        "cuisine_tags": ["家常"],
        "cooking_method_tags": ["蒸"],
        "texture_tags": ["软嫩"],
        "scenario_tags": ["家庭"],
        "ingredient_names": ["鱼"],
        "ingredient_ids": [1],
        "catalog_eligibility": "eligible",
        "source_row_sha256": "c" * 64,
        "searchable_text": "清蒸鱼 鱼 晚餐 老人 清淡",
    })

    artifacts: dict[str, ArtifactEntry] = {}
    for name, record in records.items():
        path = tmp_path / f"{name}.jsonl"
        path.write_text(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        artifacts[name] = ArtifactEntry(
            relative_path=path.name,
            row_count=1,
            sha256=_sha256(path),
        )

    quality_path = tmp_path / "quality_gate_report.json"
    quality_path.write_text(
        json.dumps({
            "passed": True,
            "build_id": str(BUILD_ID),
            "source_manifest_hash": SOURCE_HASH,
        }),
        encoding="utf-8",
    )
    builder_version = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest = BuildManifest(
        build_id=BUILD_ID,
        source_manifest_hash=SOURCE_HASH,
        builder_version=builder_version,
        schema_versions={
            name: (
                "2.0.0"
                if name in {"rag_documents", "nutrition_features", "step_tasks"}
                else "1.0.0"
            )
            for name in REQUIRED_ARTIFACTS
        },
        artifacts=artifacts,
        quality_gate_report=QualityGateReport(
            relative_path=quality_path.name,
            sha256=_sha256(quality_path),
            passed=True,
        ),
        created_at=datetime.now(UTC),
    )
    manifest_path = tmp_path / "build_manifest.json"
    manifest_path.write_text(
        manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )

    def verified_manifest(path: Path) -> BuildManifest:
        return BuildManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))

    monkeypatch.setattr(
        "food_agent_v2.b1.database_loader.verify_build_manifest",
        verified_manifest,
    )
    return manifest_path
