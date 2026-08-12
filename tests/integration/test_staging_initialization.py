"""T09 staging build and one-time initialization acceptance tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from food_agent_v2.b1.database_loader import (
    InitializationError,
    initialize_verified_fixed_data,
)
from food_agent_v2.b1.quality_gates import DataQualityError, verify_build_manifest
from food_agent_v2.b1.rebuild import DataPipelineError, build_fixed_data_staging


class FakeMySQLTarget:
    def __init__(self, *, fail_on_artifact: str | None = None, non_empty: bool = False,
                 retain_artifacts: set[str] | None = None):
        self.fail_on_artifact = fail_on_artifact
        self.non_empty = non_empty
        self.pending: dict[str, list[dict]] = {}
        self.committed: dict[str, list[dict]] = {}
        self.build_metadata: dict = {}
        self.rolled_back = False
        self.retain_artifacts = retain_artifacts
        self.pending_counts: dict[str, int] = {}
        self.pending_keys: dict[tuple[str, str], set[int]] = {}

    def fixed_data_is_empty(self) -> bool:
        return not self.non_empty and not self.committed

    def begin(self, build_metadata: dict) -> None:
        self.pending = {}
        self.pending_counts = {}
        self.pending_keys = {}
        self.build_metadata = dict(build_metadata)

    def load_artifact(self, artifact_name: str, records: list[dict]) -> None:
        if artifact_name == self.fail_on_artifact:
            raise RuntimeError("injected database failure")
        self.pending_counts[artifact_name] = len(records)
        if artifact_name == "rag_documents" and records and "recipe_id" in records[0]:
            self.pending_keys[(artifact_name, "recipe_id")] = {
                int(record["recipe_id"]) for record in records
            }
        if self.retain_artifacts is None or artifact_name in self.retain_artifacts:
            self.pending[artifact_name] = records

    def artifact_counts(self) -> dict[str, int]:
        return dict(self.pending_counts)

    def artifact_key_values(self, artifact_name: str, field: str) -> set[int]:
        key = (artifact_name, field)
        if key in self.pending_keys:
            return set(self.pending_keys[key])
        return {int(record[field]) for record in self.pending.get(artifact_name, [])}

    def commit(self) -> None:
        self.committed = dict(self.pending)
        self.pending = {}

    def rollback(self) -> None:
        self.pending = {}
        self.pending_counts = {}
        self.pending_keys = {}
        self.rolled_back = True


class FakeVectorTarget:
    def __init__(self, *, drop_last_point: bool = False, non_empty: bool = False):
        self.drop_last_point = drop_last_point
        self.non_empty = non_empty
        self.collections: dict[str, dict[int, dict]] = {}
        self.published: dict[str, str] = {}
        self.deleted: list[str] = []

    def target_is_empty(self, collection_name: str) -> bool:
        return not self.non_empty and collection_name not in self.published

    def create_staging_collection(self, collection_name: str) -> None:
        self.collections[collection_name] = {}

    def index_documents(self, collection_name: str, documents: list[dict]) -> int:
        selected = documents[:-1] if self.drop_last_point else documents
        self.collections[collection_name] = {int(item["recipe_id"]): item for item in selected}
        return len(selected)

    def point_ids(self, collection_name: str) -> set[int]:
        return set(self.collections.get(collection_name, {}))

    def publish_collection(self, staging_name: str, final_name: str) -> None:
        self.published[final_name] = staging_name

    def delete_collection(self, collection_name: str) -> None:
        self.collections.pop(collection_name, None)
        for alias, target in list(self.published.items()):
            if alias == collection_name or target == collection_name:
                self.published.pop(alias, None)
        self.deleted.append(collection_name)


def test_full_fixed_build_manifest_passes_all_gates(verified_build: Path) -> None:
    payload = json.loads(verified_build.read_text(encoding="utf-8"))
    assert payload["quality_gate_report"]["passed"] is True
    assert payload["artifacts"]["recipe_source_rows"]["row_count"] == 2000
    assert payload["artifacts"]["recipe_classifications"]["row_count"] == 2000
    assert payload["artifacts"]["user_profiles"]["row_count"] == 50
    assert payload["artifacts"]["health_relation_coverage"]["row_count"] == 38
    assert payload["artifacts"]["rag_documents"]["row_count"] == 1914


def test_build_refuses_nonempty_staging_directory(tmp_path: Path) -> None:
    staging = tmp_path / "not-empty"
    staging.mkdir()
    (staging / "foreign.txt").write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(DataPipelineError, match="STAGING_NOT_EMPTY"):
        build_fixed_data_staging(staging, builder_version="a" * 40)


def test_cross_build_artifact_is_rejected(verified_build: Path, tmp_path: Path) -> None:
    root = verified_build.parent
    original_manifest = json.loads(verified_build.read_text(encoding="utf-8"))
    entry = original_manifest["artifacts"]["recipe_health_views"]
    original_artifact = root / entry["relative_path"]
    tampered_root = tmp_path / "cross-build"
    tampered_root.mkdir()
    tampered_artifact = tampered_root / "recipe_health_views.jsonl"
    lines = original_artifact.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["build_id"] = "44444444-4444-4444-4444-444444444444"
    lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    tampered_artifact.write_text("\n".join(lines) + "\n", encoding="utf-8")

    original_manifest["artifacts"] = {
        "recipe_health_views": {
            "relative_path": "recipe_health_views.jsonl",
            "row_count": len(lines),
            "sha256": hashlib.sha256(tampered_artifact.read_bytes()).hexdigest(),
        }
    }
    original_manifest["schema_versions"] = {"recipe_health_views": "1.0.0"}
    quality_source = root / original_manifest["quality_gate_report"]["relative_path"]
    quality_copy = tampered_root / "quality_gate_report.json"
    quality_copy.write_bytes(quality_source.read_bytes())
    original_manifest["quality_gate_report"]["relative_path"] = "quality_gate_report.json"
    (tampered_root / "build_manifest.json").write_text(
        json.dumps(original_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with pytest.raises(DataQualityError, match="ARTIFACT_BUILD_MISMATCH"):
        verify_build_manifest(
            tampered_root / "build_manifest.json",
            required_artifacts=("recipe_health_views",),
            run_semantic_gates=False,
        )


def test_mid_database_failure_rolls_back_and_removes_vector_staging(
    verified_build: Path,
) -> None:
    mysql = FakeMySQLTarget(fail_on_artifact="ingredient_registry")
    vector = FakeVectorTarget()

    with pytest.raises(InitializationError, match="DATABASE_INITIALIZATION_FAILED"):
        initialize_verified_fixed_data(
            verified_build,
            confirm_empty_v2=True,
            mysql_target=mysql,
            vector_target=vector,
            final_collection="recipe_retrieval_v2",
        )

    assert mysql.rolled_back is True
    assert mysql.committed == {}
    assert vector.collections == {}
    assert vector.published == {}


def test_second_initialization_is_blocked_before_writes(verified_build: Path) -> None:
    mysql = FakeMySQLTarget(non_empty=True)
    vector = FakeVectorTarget()

    with pytest.raises(InitializationError, match="V2_TARGET_NOT_EMPTY"):
        initialize_verified_fixed_data(
            verified_build,
            confirm_empty_v2=True,
            mysql_target=mysql,
            vector_target=vector,
            final_collection="recipe_retrieval_v2",
        )

    assert mysql.pending == {}
    assert vector.collections == {}


def test_unknown_builder_commit_is_rejected_before_writes(verified_build: Path) -> None:
    manifest = json.loads(verified_build.read_text(encoding="utf-8"))
    manifest["builder_version"] = "a" * 40
    invalid_manifest = verified_build.parent / "unknown-builder-manifest.json"
    invalid_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    mysql = FakeMySQLTarget()
    vector = FakeVectorTarget()

    try:
        with pytest.raises(InitializationError, match="BUILD_COMMIT_UNAVAILABLE"):
            initialize_verified_fixed_data(
                invalid_manifest,
                confirm_empty_v2=True,
                mysql_target=mysql,
                vector_target=vector,
                final_collection="recipe_retrieval_v2",
            )
    finally:
        invalid_manifest.unlink(missing_ok=True)

    assert mysql.pending == {}
    assert vector.collections == {}
