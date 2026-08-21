"""T09 one-time initialization of a verified fixed-data build."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from food_agent_v2.b1.quality_gates import REQUIRED_ARTIFACTS, verify_build_manifest
from food_agent_v2.core.config import load_config
from food_agent_v2.core.paths import PROJECT_ROOT


class InitializationError(RuntimeError):
    """Stable H04/database/vector initialization failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class MySQLInitializationTarget(Protocol):
    def fixed_data_is_empty(self) -> bool: ...

    def begin(self, build_metadata: dict) -> None: ...

    def load_artifact(self, artifact_name: str, records: list[dict]) -> None: ...

    def artifact_counts(self) -> dict[str, int]: ...

    def artifact_key_values(self, artifact_name: str, field: str) -> set[int]: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class VectorInitializationTarget(Protocol):
    def target_is_empty(self, collection_name: str) -> bool: ...

    def create_staging_collection(self, collection_name: str) -> None: ...

    def index_documents(self, collection_name: str, documents: list[dict]) -> int: ...

    def point_ids(self, collection_name: str) -> set[int]: ...

    def point_payloads(self, collection_name: str) -> dict[int, dict]: ...

    def publish_collection(self, staging_name: str, final_name: str) -> None: ...

    def delete_collection(self, collection_name: str) -> None: ...


def _read_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _json_value(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_manifest_artifact(root: Path, manifest, name: str) -> list[dict]:
    return _read_jsonl((root / manifest.artifacts[name].relative_path).resolve())


def _verify_builder_commit(builder_version: str) -> None:
    """Require the declared builder Git commit to exist in this V2 repository."""
    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"{builder_version}^{{commit}}"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InitializationError(
            "BUILD_COMMIT_UNAVAILABLE",
            f"builder_version={builder_version}",
        ) from exc


def _write_initialization_report(path: Path, payload: dict) -> Path:
    """Atomically persist machine-readable post-initialization evidence."""
    report_path = Path(path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(report_path)
    return report_path


def initialize_verified_fixed_data(
    manifest_path: Path,
    *,
    confirm_empty_v2: bool,
    mysql_target: MySQLInitializationTarget | None = None,
    vector_target: VectorInitializationTarget | None = None,
    final_collection: str | None = None,
    report_path: Path | None = None,
) -> dict:
    """Initialize empty V2 stores from one verified build or roll everything back.

    MySQL remains uncommitted while the isolated Qdrant collection is built and checked.
    The final collection alias is published immediately before the MySQL commit; any
    exception removes both the alias and the explicitly named staging collection.
    """
    if not confirm_empty_v2:
        raise InitializationError(
            "H04_CONFIRMATION_REQUIRED",
            "--confirm-empty-v2 is required",
        )

    manifest_path = Path(manifest_path).resolve()
    manifest = verify_build_manifest(manifest_path)
    _verify_builder_commit(manifest.builder_version)
    cfg = load_config()
    mysql = mysql_target or PyMySQLFixedDataTarget()
    if vector_target is None:
        from food_agent_v2.c1.qdrant_client import QdrantInitializationTarget

        vector = QdrantInitializationTarget()
    else:
        vector = vector_target
    collection = final_collection or cfg.qdrant.collection

    if not mysql.fixed_data_is_empty():
        raise InitializationError("V2_TARGET_NOT_EMPTY", "MySQL fixed-data target is not empty")
    if not vector.target_is_empty(collection):
        raise InitializationError(
            "V2_TARGET_NOT_EMPTY",
            f"Qdrant target {collection!r} is not empty",
        )

    artifact_root = manifest_path.parent
    expected_counts = {name: manifest.artifacts[name].row_count for name in REQUIRED_ARTIFACTS}
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    build_metadata = {
        "build_id": str(manifest.build_id),
        "source_manifest_hash": manifest.source_manifest_hash,
        "builder_version": manifest.builder_version,
        "manifest_sha256": manifest_sha256,
        "quality_report_sha256": manifest.quality_gate_report.sha256,
        "schema_versions": dict(manifest.schema_versions),
    }
    staging_collection = f"{collection}__staging__{manifest.build_id.hex}"
    evidence_path = report_path or (
        manifest_path.parent / f"initialization_report_{manifest.build_id}.json"
    )
    phase = "database"
    vector_created = False
    vector_published = False
    try:
        mysql.begin(build_metadata)
        rag_documents: list[dict] | None = None
        for artifact_name in REQUIRED_ARTIFACTS:
            records = _read_manifest_artifact(artifact_root, manifest, artifact_name)
            mysql.load_artifact(artifact_name, records)
            if artifact_name == "rag_documents":
                # 仅该产物在 MySQL 导入后还需供 Qdrant 使用；其余逐类释放，
                # 避免 19 类/十余万行固定事实同时驻留内存。
                rag_documents = records
        actual_counts = mysql.artifact_counts()
        if actual_counts != expected_counts:
            raise InitializationError(
                "MYSQL_ARTIFACT_PARITY_FAILED",
                f"expected={expected_counts}, actual={actual_counts}",
            )
        if rag_documents is None:
            raise InitializationError(
                "DATABASE_INITIALIZATION_FAILED",
                "verified build is missing rag_documents",
            )
        expected_recipe_ids = {int(item["recipe_id"]) for item in rag_documents}
        mysql_recipe_ids = mysql.artifact_key_values("rag_documents", "recipe_id")
        if mysql_recipe_ids != expected_recipe_ids:
            raise InitializationError(
                "MYSQL_RECIPE_ID_PARITY_FAILED",
                f"expected={len(expected_recipe_ids)}, actual={len(mysql_recipe_ids)}",
            )

        phase = "vector"
        vector.create_staging_collection(staging_collection)
        vector_created = True
        indexed_count = vector.index_documents(staging_collection, rag_documents)
        if indexed_count != len(rag_documents):
            raise InitializationError(
                "VECTOR_INDEX_COUNT_FAILED",
                f"expected={len(rag_documents)}, reported={indexed_count}",
            )
        actual_ids = vector.point_ids(staging_collection)
        if actual_ids != mysql_recipe_ids:
            missing = sorted(mysql_recipe_ids - actual_ids)[:10]
            extra = sorted(actual_ids - mysql_recipe_ids)[:10]
            raise InitializationError(
                "VECTOR_INDEX_PARITY_FAILED",
                f"expected={len(mysql_recipe_ids)}, actual={len(actual_ids)}, "
                f"missing={missing}, extra={extra}",
            )
        from food_agent_v2.c1.qdrant_client import rag_document_payload

        expected_payloads = {
            int(document["recipe_id"]): rag_document_payload(document)
            for document in rag_documents
        }
        actual_payloads = vector.point_payloads(staging_collection)
        if actual_payloads != expected_payloads:
            mismatched = sorted(
                point_id
                for point_id in expected_recipe_ids
                if actual_payloads.get(point_id) != expected_payloads[point_id]
            )[:10]
            raise InitializationError(
                "VECTOR_PAYLOAD_PARITY_FAILED",
                f"expected={len(expected_payloads)}, actual={len(actual_payloads)}, "
                f"mismatched={mismatched}",
            )
        vector.publish_collection(staging_collection, collection)
        vector_published = True

        phase = "database_commit"
        mysql.commit()
    except Exception as exc:
        cleanup_errors: list[str] = []
        try:
            mysql.rollback()
        except Exception as cleanup_exc:
            cleanup_errors.append(f"mysql.rollback: {cleanup_exc}")
        if vector_published:
            try:
                vector.delete_collection(collection)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"qdrant.delete_alias: {cleanup_exc}")
        if vector_created:
            try:
                vector.delete_collection(staging_collection)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"qdrant.delete_staging: {cleanup_exc}")
        if cleanup_errors:
            failure = InitializationError(
                "INITIALIZATION_ROLLBACK_INCOMPLETE",
                f"original={exc}; cleanup={cleanup_errors}",
            )
        elif isinstance(exc, InitializationError):
            failure = exc
        else:
            code = (
                "DATABASE_INITIALIZATION_FAILED"
                if phase.startswith("database")
                else "VECTOR_INDEX_BUILD_FAILED"
            )
            failure = InitializationError(code, str(exc))
        failure_evidence = {
            **build_metadata,
            "status": "failed",
            "failure_code": failure.code,
            "failure_detail": failure.message,
            "phase": phase,
            "cleanup_errors": cleanup_errors,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        try:
            _write_initialization_report(evidence_path, failure_evidence)
        except OSError:
            pass
        if failure is exc:
            raise
        raise failure from exc

    result = {
        "status": "initialized",
        "build_id": str(manifest.build_id),
        "source_manifest_hash": manifest.source_manifest_hash,
        "builder_version": manifest.builder_version,
        "manifest_sha256": manifest_sha256,
        "quality_report_sha256": manifest.quality_gate_report.sha256,
        "schema_versions": dict(manifest.schema_versions),
        "mysql_artifact_counts": expected_counts,
        "qdrant_collection": collection,
        "qdrant_physical_collection": staging_collection,
        "qdrant_point_count": len(rag_documents),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    try:
        written_report = _write_initialization_report(evidence_path, result)
        result["initialization_report_path"] = str(written_report)
        # Re-write once so the persisted evidence also points to itself.
        _write_initialization_report(written_report, result)
    except OSError as exc:
        # Storage is already committed and published at this point.  Do not report a
        # false rollback; surface the evidence-write failure in the returned result.
        result["initialization_report_error"] = str(exc)
    return result


class PyMySQLFixedDataTarget:
    """Transactional MySQL adapter backed by generic immutable artifact rows.

    The generic table remains the lossless replay source. Runtime nutrition/time
    projections are populated in the same transaction so no partial build is observable.
    """

    def __init__(self) -> None:
        self._connection = None
        self._cursor = None
        self._build_id: str | None = None

    def _connect(self):
        if self._connection is not None:
            return self._connection
        import pymysql

        cfg = load_config().mysql
        self._connection = pymysql.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
            charset="utf8mb4",
            autocommit=False,
        )
        self._cursor = self._connection.cursor()
        return self._connection

    @property
    def cursor(self):
        self._connect()
        return self._cursor

    def fixed_data_is_empty(self) -> bool:
        cursor = self.cursor
        fixed_tables = (
            "data_builds",
            "fixed_artifact_records",
            "recipes",
            "ingredients",
            "ingredient_aliases",
            "health_relations",
            "nutrition_profiles",
            "time_profiles",
            "user_profiles",
        )
        for table in fixed_tables:
            cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
            if int(cursor.fetchone()[0]) != 0:
                return False
        return True

    def begin(self, build_metadata: dict) -> None:
        connection = self._connect()
        connection.begin()
        self._build_id = str(build_metadata["build_id"])
        self.cursor.execute(
            "INSERT INTO data_builds "
            "(build_id, source_manifest_hash, builder_version, manifest_sha256, "
            "quality_report_sha256, schema_versions, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'initializing')",
            (
                self._build_id,
                build_metadata["source_manifest_hash"],
                build_metadata["builder_version"],
                build_metadata["manifest_sha256"],
                build_metadata["quality_report_sha256"],
                json.dumps(
                    build_metadata["schema_versions"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )

    def load_artifact(self, artifact_name: str, records: list[dict]) -> None:
        if not self._build_id:
            raise InitializationError(
                "DATABASE_INITIALIZATION_FAILED",
                f"artifact {artifact_name} has no build identity",
            )
        if any(str(record.get("build_id")) != self._build_id for record in records):
            raise InitializationError(
                "DATABASE_INITIALIZATION_FAILED",
                f"artifact {artifact_name} has mixed build identity",
            )
        payloads = [
            (
                self._build_id,
                artifact_name,
                index,
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            )
            for index, record in enumerate(records, start=1)
        ]
        if payloads:
            self.cursor.executemany(
                "INSERT INTO fixed_artifact_records "
                "(build_id, artifact_name, record_index, payload) VALUES (%s, %s, %s, %s)",
                payloads,
            )
        self._load_runtime_projection(artifact_name, records)

    def _load_runtime_projection(self, artifact_name: str, records: list[dict]) -> None:
        if artifact_name == "recipe_source_rows":
            rows = [
                (
                    int(record["recipe_id"]),
                    record["name"],
                    record["ingredients_raw"],
                    record["steps_raw"],
                    record["labels_raw"],
                )
                for record in records
            ]
            if rows:
                self.cursor.executemany(
                    "INSERT INTO recipes "
                    "(recipe_id, name, ingredients_raw, steps_raw, labels_raw) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    rows,
                )
        elif artifact_name == "nutrition_features":
            rows = [
                (
                    int(record["recipe_id"]),
                    bool(record["available"]),
                    record.get("raw_edible_input_weight_g"),
                    _json_value(record.get("raw_nutrition_total")),
                    _json_value(record.get("raw_nutrition_per_100g")),
                    record.get("reason"),
                )
                for record in records
            ]
            if rows:
                self.cursor.executemany(
                    "INSERT INTO nutrition_profiles "
                    "(recipe_id, available, raw_edible_input_weight_g, "
                    "raw_nutrition_total, raw_nutrition_per_100g, reason) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    rows,
                )
        elif artifact_name == "step_tasks":
            rows = [
                (
                    int(record["recipe_id"]),
                    int(record["active_seconds"]),
                    int(record["estimated_elapsed_seconds"]),
                    _json_value(record["step_tasks"]),
                )
                for record in records
            ]
            if rows:
                self.cursor.executemany(
                    "INSERT INTO time_profiles "
                    "(recipe_id, active_seconds, estimated_elapsed_seconds, step_tasks) "
                    "VALUES (%s, %s, %s, %s)",
                    rows,
                )

    def artifact_counts(self) -> dict[str, int]:
        self.cursor.execute(
            "SELECT artifact_name, COUNT(*) FROM fixed_artifact_records GROUP BY artifact_name"
        )
        return {str(name): int(count) for name, count in self.cursor.fetchall()}

    def artifact_key_values(self, artifact_name: str, field: str) -> set[int]:
        if not field.replace("_", "").isalnum():
            raise InitializationError("DATABASE_INITIALIZATION_FAILED", "invalid JSON field")
        self.cursor.execute(
            "SELECT JSON_UNQUOTE(JSON_EXTRACT(payload, %s)) "
            "FROM fixed_artifact_records WHERE build_id=%s AND artifact_name=%s",
            (f"$.{field}", self._build_id, artifact_name),
        )
        values = {row[0] for row in self.cursor.fetchall()}
        if None in values:
            raise InitializationError(
                "DATABASE_INITIALIZATION_FAILED",
                f"artifact {artifact_name} is missing {field}",
            )
        return {int(value) for value in values}

    def commit(self) -> None:
        if self._build_id:
            counts = json.dumps(
                self.artifact_counts(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self.cursor.execute(
                "UPDATE data_builds SET status='ready', artifact_counts=%s, "
                "initialized_at=CURRENT_TIMESTAMP "
                "WHERE build_id=%s",
                (counts, self._build_id),
            )
        self._connection.commit()

    def rollback(self) -> None:
        if self._connection is not None:
            self._connection.rollback()


def load_database(confirm: bool = False, manifest_path: Path | None = None) -> dict:
    """Compatibility wrapper; the old mutable cleaned-directory loader is removed."""
    if not confirm:
        return {"status": "aborted", "reason": "run with confirm=True"}
    if manifest_path is None:
        raise InitializationError("BUILD_MANIFEST_MISSING", "manifest_path is required")
    return initialize_verified_fixed_data(
        manifest_path,
        confirm_empty_v2=True,
    )
