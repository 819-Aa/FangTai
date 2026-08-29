"""T09 staging build and one-time initialization acceptance tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from food_agent_v2.b1.database_loader import (
    InitializationError,
    PyMySQLFixedDataTarget,
    initialize_verified_fixed_data,
)
from food_agent_v2.b1.quality_gates import (
    REQUIRED_ARTIFACTS,
    DataQualityError,
    verify_build_manifest,
)
from food_agent_v2.b1.rebuild import (
    DataPipelineError,
    artifact_schema_versions,
    build_fixed_data_staging,
)
from food_agent_v2.b1.source_manifest import canonical_build_input_manifest
from food_agent_v2.contracts.build import source_manifest_hash


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
    def __init__(
        self,
        *,
        drop_last_point: bool = False,
        non_empty: bool = False,
        corrupt_first_payload: bool = False,
        reported_count_delta: int = 0,
    ):
        self.drop_last_point = drop_last_point
        self.non_empty = non_empty
        self.corrupt_first_payload = corrupt_first_payload
        self.reported_count_delta = reported_count_delta
        self.collections: dict[str, dict[int, dict]] = {}
        self.published: dict[str, str] = {}
        self.deleted: list[str] = []

    def target_is_empty(self, collection_name: str) -> bool:
        return not self.non_empty and collection_name not in self.published

    def create_staging_collection(self, collection_name: str) -> None:
        self.collections[collection_name] = {}

    def index_documents(self, collection_name: str, documents: list[dict]) -> int:
        from food_agent_v2.c1.qdrant_client import rag_document_payload

        selected = documents[:-1] if self.drop_last_point else documents
        self.collections[collection_name] = {
            int(item["recipe_id"]): rag_document_payload(item) for item in selected
        }
        if self.corrupt_first_payload and selected:
            first_id = int(selected[0]["recipe_id"])
            self.collections[collection_name][first_id]["meal_tags"] = ["早餐"]
        return len(documents) + self.reported_count_delta

    def point_ids(self, collection_name: str) -> set[int]:
        return set(self.collections.get(collection_name, {}))

    def point_payloads(self, collection_name: str) -> dict[int, dict]:
        return {
            point_id: dict(payload)
            for point_id, payload in self.collections.get(collection_name, {}).items()
        }

    def publish_collection(self, staging_name: str, final_name: str) -> None:
        self.published[final_name] = staging_name

    def delete_collection(self, collection_name: str) -> None:
        self.collections.pop(collection_name, None)
        for alias, target in list(self.published.items()):
            if alias == collection_name or target == collection_name:
                self.published.pop(alias, None)
        self.deleted.append(collection_name)


def test_fixed_build_schema_contract_has_exactly_three_v2_runtime_artifacts() -> None:
    versions = artifact_schema_versions({name: object() for name in REQUIRED_ARTIFACTS})

    assert len(versions) == 20
    assert {name for name, version in versions.items() if version == "2.0.0"} == {
        "rag_documents",
        "nutrition_features",
        "step_tasks",
    }


def test_manifest_missing_recipe_dependencies_is_rejected(
    verified_initialization_build: Path,
) -> None:
    assert len(REQUIRED_ARTIFACTS) == 20
    manifest = json.loads(verified_initialization_build.read_text(encoding="utf-8"))
    manifest["artifacts"].pop("recipe_dependencies", None)
    manifest["schema_versions"].pop("recipe_dependencies", None)
    incomplete_manifest = verified_initialization_build.parent / "missing-dependencies.json"
    incomplete_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(DataQualityError) as caught:
        verify_build_manifest(
            incomplete_manifest,
            required_artifacts=REQUIRED_ARTIFACTS,
            run_semantic_gates=False,
        )
    assert caught.value.code == "REQUIRED_ARTIFACT_MISSING"


def test_initializer_loads_every_shared_fixed_artifact(
    verified_initialization_build: Path,
) -> None:
    mysql = FakeMySQLTarget()
    vector = FakeVectorTarget()

    initialize_verified_fixed_data(
        verified_initialization_build,
        confirm_empty_v2=True,
        mysql_target=mysql,
        vector_target=vector,
        final_collection="recipe_retrieval_v2",
    )

    assert set(mysql.committed) == set(REQUIRED_ARTIFACTS)
    assert len(mysql.committed) == 20
    assert "recipe_dependencies" in mysql.committed


def test_mysql_schema_uses_v2_nutrition_and_time_columns() -> None:
    schema = (Path(__file__).resolve().parents[2] / "db" / "schema_mysql.sql").read_text(
        encoding="utf-8"
    )

    assert "raw_edible_input_weight_g" in schema
    assert "raw_nutrition_total" in schema
    assert "estimated_elapsed_seconds" in schema
    assert "schema_versions JSON NOT NULL" in schema
    assert "match_method" not in schema
    assert "confidence VARCHAR" not in schema


def test_mysql_adapter_populates_v2_runtime_projections_and_lossless_records() -> None:
    class RecordingCursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[tuple]]] = []

        def executemany(self, sql: str, rows: list[tuple]) -> None:
            self.calls.append((sql, rows))

    cursor = RecordingCursor()
    target = PyMySQLFixedDataTarget()
    target._connection = object()
    target._cursor = cursor
    target._build_id = "build-1"

    target.load_artifact("nutrition_features", [{
        "build_id": "build-1",
        "recipe_id": 1,
        "available": False,
        "raw_edible_input_weight_g": None,
        "raw_nutrition_total": None,
        "raw_nutrition_per_100g": None,
        "reason": "mapping_missing",
    }])
    target.load_artifact("step_tasks", [{
        "build_id": "build-1",
        "recipe_id": 1,
        "active_seconds": 120,
        "estimated_elapsed_seconds": 600,
        "step_tasks": [{"atom_id": "r1-s1-a1", "duration_seconds": 600}],
    }])

    sql = "\n".join(call[0] for call in cursor.calls)
    assert sql.count("fixed_artifact_records") == 2
    assert "raw_edible_input_weight_g" in sql
    assert "estimated_elapsed_seconds" in sql


def test_mysql_adapter_stores_recipe_dependencies_without_runtime_projection() -> None:
    class RecordingCursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[tuple]]] = []

        def executemany(self, sql: str, rows: list[tuple]) -> None:
            self.calls.append((sql, rows))

    cursor = RecordingCursor()
    target = PyMySQLFixedDataTarget()
    target._connection = object()
    target._cursor = cursor
    target._build_id = "build-1"

    target.load_artifact("recipe_dependencies", [{
        "build_id": "build-1",
        "source_manifest_hash": "a" * 64,
        "parent_recipe_id": 1462,
        "dependency_recipe_id": 298,
        "relation_type": "bundle_contains",
        "review_status": "approved",
    }])

    assert len(cursor.calls) == 1
    sql, rows = cursor.calls[0]
    assert "INSERT INTO fixed_artifact_records" in sql
    assert rows[0][1] == "recipe_dependencies"
    assert all(
        table not in sql
        for table in ("INSERT INTO recipes", "INSERT INTO nutrition_profiles", "INSERT INTO time_profiles")
    )


def test_build_refuses_nonempty_staging_directory(tmp_path: Path) -> None:
    staging = tmp_path / "not-empty"
    staging.mkdir()
    (staging / "foreign.txt").write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(DataPipelineError, match="STAGING_NOT_EMPTY"):
        build_fixed_data_staging(staging, builder_version="a" * 40)


def test_cross_build_artifact_is_rejected(tmp_path: Path) -> None:
    tampered_root = tmp_path / "cross-build"
    tampered_root.mkdir()
    tampered_artifact = tampered_root / "recipe_health_views.jsonl"
    manifest_hash = source_manifest_hash(canonical_build_input_manifest())
    tampered_artifact.write_text(
        json.dumps({
            "build_id": "44444444-4444-4444-4444-444444444444",
            "source_manifest_hash": manifest_hash,
        }) + "\n",
        encoding="utf-8",
    )
    quality_copy = tampered_root / "quality_gate_report.json"
    quality_copy.write_text(json.dumps({
        "passed": True,
        "build_id": "33333333-3333-3333-3333-333333333333",
        "source_manifest_hash": manifest_hash,
    }), encoding="utf-8")
    manifest = {
        "build_id": "33333333-3333-3333-3333-333333333333",
        "source_manifest_hash": manifest_hash,
        "builder_version": "1" * 40,
        "artifacts": {
            "recipe_health_views": {
                "relative_path": "recipe_health_views.jsonl",
                "row_count": 1,
                "sha256": hashlib.sha256(tampered_artifact.read_bytes()).hexdigest(),
            }
        },
        "schema_versions": {"recipe_health_views": "1.0.0"},
        "quality_gate_report": {
            "relative_path": "quality_gate_report.json",
            "sha256": hashlib.sha256(quality_copy.read_bytes()).hexdigest(),
            "passed": True,
        },
        "created_at": "2026-08-21T00:00:00Z",
    }
    (tampered_root / "build_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with pytest.raises(DataQualityError, match="ARTIFACT_BUILD_MISMATCH"):
        verify_build_manifest(
            tampered_root / "build_manifest.json",
            required_artifacts=("recipe_health_views",),
            run_semantic_gates=False,
        )


def test_mid_database_failure_rolls_back_and_removes_vector_staging(
    verified_initialization_build: Path,
) -> None:
    mysql = FakeMySQLTarget(fail_on_artifact="ingredient_registry")
    vector = FakeVectorTarget()

    with pytest.raises(InitializationError, match="DATABASE_INITIALIZATION_FAILED"):
        initialize_verified_fixed_data(
            verified_initialization_build,
            confirm_empty_v2=True,
            mysql_target=mysql,
            vector_target=vector,
            final_collection="recipe_retrieval_v2",
        )

    assert mysql.rolled_back is True
    assert mysql.committed == {}
    assert vector.collections == {}
    assert vector.published == {}


def test_second_initialization_is_blocked_before_writes(
    verified_initialization_build: Path,
) -> None:
    mysql = FakeMySQLTarget(non_empty=True)
    vector = FakeVectorTarget()

    with pytest.raises(InitializationError, match="V2_TARGET_NOT_EMPTY"):
        initialize_verified_fixed_data(
            verified_initialization_build,
            confirm_empty_v2=True,
            mysql_target=mysql,
            vector_target=vector,
            final_collection="recipe_retrieval_v2",
        )

    assert mysql.pending == {}
    assert vector.collections == {}


def test_unknown_builder_commit_is_rejected_before_writes(
    verified_initialization_build: Path,
) -> None:
    manifest = json.loads(verified_initialization_build.read_text(encoding="utf-8"))
    manifest["builder_version"] = "a" * 40
    invalid_manifest = verified_initialization_build.parent / "unknown-builder-manifest.json"
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
