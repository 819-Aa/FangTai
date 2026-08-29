"""T09 fixed-data quality gates and BuildManifest verification.

The build report is produced before the manifest and binds the complete fixed-data
universe.  ``verify_build_manifest`` is the only gate accepted by initialization.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from food_agent_v2.b1.health_relation_builder import ALLOWED_CONSTRAINT_CODES
from food_agent_v2.b1.source_manifest import (
    artifact_schema_version,
    canonical_build_input_manifest,
    canonical_source_manifest,
)
from food_agent_v2.contracts.build import (
    FIXED_ARTIFACT_NAMES,
    FIXED_RECIPE_BUNDLE_CONTAINS_COUNT,
    FIXED_RECIPE_DEPENDENCY_COUNT,
    ArtifactEntry,
    BuildManifest,
    SourceManifestMismatch,
    source_manifest_hash,
    verify_source_file,
)
from food_agent_v2.core.paths import PROJECT_ROOT, RECIPES_RAW

REQUIRED_ARTIFACTS = FIXED_ARTIFACT_NAMES

ALLOWED_RECORD_TYPES = {
    "dish",
    "preparation",
    "meal_bundle",
    "cooking_program",
    "test_record",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
APPROVED_NUTRITION_AVAILABLE_BASELINE = 0
TIME_REVIEW_DECISIONS = (
    PROJECT_ROOT / "data" / "review" / "recipe_time_graph_decisions.csv"
)


class DataQualityError(ValueError):
    """Stable, machine-readable build or verification failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# Backward-compatible public name used by the pre-T09 entrypoint.
GateFailure = DataQualityError


@dataclass(frozen=True)
class GateResult:
    code: str
    passed: bool
    detail: str

    def as_dict(self) -> dict:
        return {"code": self.code, "passed": self.passed, "detail": self.detail}


def _fail(code: str, message: str) -> None:
    raise DataQualityError(code, message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                _fail("ARTIFACT_JSON_INVALID", f"{path}:{line_number}: {exc}")
            if not isinstance(value, dict):
                _fail("ARTIFACT_RECORD_INVALID", f"{path}:{line_number} is not an object")
            records.append(value)
    return records


def _jsonl_count(path: Path) -> int:
    with path.open("r", encoding="utf-8") as handle:
        return sum(bool(line.strip()) for line in handle)


def artifact_entry(staging_root: Path, path: Path) -> ArtifactEntry:
    root = staging_root.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        _fail("ARTIFACT_PATH_ESCAPE", f"{resolved} is outside {root}")
        raise AssertionError from exc
    if not resolved.is_file():
        _fail("ARTIFACT_MISSING", str(resolved))
    return ArtifactEntry(
        relative_path=relative.as_posix(),
        row_count=_jsonl_count(resolved),
        sha256=_sha256(resolved),
    )


def build_artifact_entries(
    staging_root: Path,
    artifact_paths: dict[str, Path],
) -> dict[str, ArtifactEntry]:
    return {
        artifact_name: artifact_entry(staging_root, path)
        for artifact_name, path in artifact_paths.items()
    }


def _unique(records: list[dict], field: str, *, artifact: str) -> set:
    values = [record.get(field) for record in records]
    if None in values or len(values) != len(set(values)):
        _fail("ARTIFACT_PRIMARY_KEY_INVALID", f"{artifact}.{field} is missing or duplicated")
    return set(values)


def _recursive_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested
            for item in value.values()
            for nested in _recursive_keys(item)
        }
    if isinstance(value, (list, tuple)):
        return {nested for item in value for nested in _recursive_keys(item)}
    return set()


def _valid_nonnegative_number(value) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def _assert_build_identity(
    artifact_name: str,
    records: Iterable[dict],
    *,
    build_id: str,
    source_manifest_hash: str,
) -> None:
    for index, record in enumerate(records, start=1):
        if (
            str(record.get("build_id", "")) != build_id
            or record.get("source_manifest_hash") != source_manifest_hash
        ):
            _fail(
                "ARTIFACT_BUILD_MISMATCH",
                f"{artifact_name} row {index} does not belong to build {build_id}",
            )


def evaluate_staging_quality(
    artifact_paths: dict[str, Path],
    *,
    build_id: str,
    source_manifest_hash: str,
) -> tuple[list[GateResult], dict[str, int]]:
    """Run all S2 semantic gates against a complete staging artifact set."""
    missing = set(REQUIRED_ARTIFACTS) - set(artifact_paths)
    if missing:
        _fail("REQUIRED_ARTIFACT_MISSING", f"missing={sorted(missing)}")

    artifacts = {name: _read_jsonl(artifact_paths[name]) for name in REQUIRED_ARTIFACTS}
    for name, records in artifacts.items():
        _assert_build_identity(
            name,
            records,
            build_id=build_id,
            source_manifest_hash=source_manifest_hash,
        )

    gates: list[GateResult] = []

    def gate(code: str, condition: bool, detail: str) -> None:
        gates.append(GateResult(code, condition, detail))
        if not condition:
            _fail(code, detail)

    source_rows = artifacts["recipe_source_rows"]
    source_ids = _unique(source_rows, "recipe_id", artifact="recipe_source_rows")
    gate(
        "G01_SOURCE_ROW_CONSERVATION",
        source_ids == set(range(1, 2001)),
        f"rows={len(source_rows)}, ids={len(source_ids)}",
    )

    classifications = artifacts["recipe_classifications"]
    classification_ids = _unique(
        classifications,
        "recipe_id",
        artifact="recipe_classifications",
    )
    pending_classifications = [
        item["recipe_id"] for item in classifications if item.get("review_status") != "approved"
    ]
    unknown_types = sorted(
        {item.get("record_type") for item in classifications} - ALLOWED_RECORD_TYPES
    )
    gate(
        "G02_CLASSIFICATION_CLOSED",
        classification_ids == source_ids and not pending_classifications and not unknown_types,
        f"rows={len(classifications)}, pending={len(pending_classifications)}, "
        f"unknown_types={unknown_types}",
    )

    users = artifacts["user_profiles"]
    user_ids = _unique(users, "user_id", artifact="user_profiles")
    gate(
        "G03_FIXED_USER_PROFILES",
        user_ids == set(range(1, 51)),
        f"rows={len(users)}, ids={len(user_ids)}",
    )

    registry = artifacts["ingredient_registry"]
    ingredient_ids = _unique(registry, "ingredient_id", artifact="ingredient_registry")
    crosswalk = artifacts["ingredient_crosswalk"]
    pending_identity = [
        item.get("source_key") for item in crosswalk if item.get("review_status") == "pending"
    ]
    gate(
        "G04_INGREDIENT_IDENTITY_FROZEN",
        bool(registry) and not pending_identity,
        f"registry={len(registry)}, pending={len(pending_identity)}",
    )

    occurrences = artifacts["ingredient_occurrences"]
    _unique(occurrences, "occurrence_id", artifact="ingredient_occurrences")
    unresolved = [
        item["occurrence_id"]
        for item in occurrences
        if item.get("consumption_role") == "edible"
        and item.get("resolved_ingredient_id") not in ingredient_ids
    ]
    relations = artifacts["recipe_ingredient_relations"]
    dangling_relations = [
        item
        for item in relations
        if item.get("recipe_id") not in source_ids
        or item.get("ingredient_id") not in ingredient_ids
    ]
    gate(
        "G05_INGREDIENT_REFERENCES_COMPLETE",
        not unresolved and not dangling_relations,
        f"occurrences={len(occurrences)}, unresolved={len(unresolved)}, "
        f"dangling={len(dangling_relations)}",
    )

    view_names = (
        "recipe_health_views",
        "recipe_step_binding_views",
        "recipe_nutrition_input_views",
        "recipe_retrieval_build_views",
    )
    view_recipe_sets = {
        name: _unique(artifacts[name], "recipe_id", artifact=name) for name in view_names
    }
    eligible_ids = view_recipe_sets["recipe_health_views"]
    gate(
        "G06_CONSUMER_VIEW_ISOMORPHISM",
        len({frozenset(ids) for ids in view_recipe_sets.values()}) == 1
        and len(eligible_ids) == 1932,
        f"counts={{{', '.join(f'{name}:{len(ids)}' for name, ids in view_recipe_sets.items())}}}",
    )

    health_views = artifacts["recipe_health_views"]
    health_ingredient_ids = {
        int(ingredient_id)
        for view in health_views
        for ingredient_id in view.get("ingredient_ids", [])
    }
    decisions = artifacts["health_relation_decisions"]
    decision_keys = {(item.get("constraint_code"), item.get("ingredient_id")) for item in decisions}
    expected_decision_keys = {
        (code, ingredient_id)
        for code in ALLOWED_CONSTRAINT_CODES
        for ingredient_id in health_ingredient_ids
    }
    pending_health = [item for item in decisions if item.get("review_status") != "approved"]
    gate(
        "G07_HEALTH_REVIEW_MATRIX_COMPLETE",
        len(decisions) == len(decision_keys)
        and decision_keys == expected_decision_keys
        and not pending_health,
        f"ingredients={len(health_ingredient_ids)}, expected={len(expected_decision_keys)}, "
        f"actual={len(decisions)}, pending={len(pending_health)}",
    )

    health_relations = artifacts["health_relations"]
    hard_decision_keys = {
        (item["constraint_code"], item["ingredient_id"])
        for item in decisions
        if item.get("decision") == "hard_exclude"
    }
    hard_relation_keys = {
        (item.get("constraint_code"), item.get("ingredient_id")) for item in health_relations
    }
    coverage = artifacts["health_relation_coverage"]
    coverage_codes = _unique(
        coverage,
        "constraint_code",
        artifact="health_relation_coverage",
    )
    coverage_valid = all(
        item.get("coverage_status") == "complete"
        and item.get("universe_ingredient_count") == len(health_ingredient_ids)
        and item.get("reviewed_ingredient_count") == len(health_ingredient_ids)
        for item in coverage
    )
    gate(
        "G08_HEALTH_RELATION_PARITY",
        hard_relation_keys == hard_decision_keys
        and coverage_codes == set(ALLOWED_CONSTRAINT_CODES)
        and coverage_valid,
        f"hard_decisions={len(hard_decision_keys)}, relations={len(hard_relation_keys)}, "
        f"coverage={len(coverage_codes)}",
    )

    for artifact_name in ("step_tasks", "nutrition_features", "rag_documents"):
        derived_ids = _unique(artifacts[artifact_name], "recipe_id", artifact=artifact_name)
        gate(
            f"G09_{artifact_name.upper()}_COVERAGE",
            derived_ids == eligible_ids,
            f"expected={len(eligible_ids)}, actual={len(derived_ids)}",
        )

    nutrition_invalid = [
        item.get("recipe_id")
        for item in artifacts["nutrition_features"]
        if item.get("available") is False and not item.get("reason")
    ]
    gate(
        "G10_NUTRITION_UNAVAILABLE_EXPLICIT",
        not nutrition_invalid,
        f"invalid={len(nutrition_invalid)}",
    )

    forbidden_health_fields = {"allergen_types", "health_features", "risk_tags"}
    rag_health_leakage = [
        item.get("recipe_id")
        for item in artifacts["rag_documents"]
        if forbidden_health_fields & set(item.get("searchable_fields", {}))
    ]
    gate(
        "G11_RAG_HEALTH_BOUNDARY",
        not rag_health_leakage,
        f"leakage={len(rag_health_leakage)}",
    )

    from food_agent_v2.b1.consumer_views import (
        label_tags_from_raw_labels,
        meal_tags_from_raw_labels,
    )
    from food_agent_v2.b1.review_inputs import load_recipe_profile_enrichments

    source_by_id = {int(item["recipe_id"]): item for item in source_rows}
    enrichments = load_recipe_profile_enrichments(
        PROJECT_ROOT / "data" / "review" / "recipe_profile_enrichment.jsonl",
        known_recipe_ids=source_ids,
    )
    retrieval_by_id = {
        int(item["recipe_id"]): item
        for item in artifacts["recipe_retrieval_build_views"]
    }
    rag_label_errors: list[int] = []
    for item in artifacts["rag_documents"]:
        recipe_id = int(item["recipe_id"])
        retrieval = retrieval_by_id[recipe_id]
        labels_raw = str(source_by_id[recipe_id].get("labels_raw", ""))
        expected_label_tags = label_tags_from_raw_labels(labels_raw)
        raw_meal_tags = meal_tags_from_raw_labels(labels_raw)
        enrichment = enrichments.get(recipe_id)
        expected_meal_tags = raw_meal_tags or (
            tuple(enrichment.meal_tags) if enrichment is not None else ()
        )
        retrieval_meal_tags = tuple(retrieval.get("meal_tags", ()))
        if (
            not expected_meal_tags
            or retrieval_meal_tags != expected_meal_tags
            or tuple(item.get("meal_tags", ())) != expected_meal_tags
            or tuple(retrieval.get("label_tags", ())) != expected_label_tags
            or tuple(item.get("label_tags", ())) != expected_label_tags
        ):
            rag_label_errors.append(recipe_id)
    gate(
        "G12_RAG_LABEL_AND_MEAL_COVERAGE",
        not rag_label_errors,
        f"invalid={len(rag_label_errors)}",
    )

    forbidden_runtime_fields = {
        "confidence",
        "duration_confidence",
        "time_source",
        "authority",
        "minimum_seconds",
        "maximum_seconds",
        "duration_min_seconds",
        "duration_max_seconds",
        "match_method",
        "coverage_ratio",
        "evidence",
        "evidence_ref",
        "explanation",
    }
    runtime_boundary_errors = [
        (artifact_name, item.get("recipe_id"))
        for artifact_name in ("rag_documents", "nutrition_features", "step_tasks")
        for item in artifacts[artifact_name]
        if _recursive_keys(item) & forbidden_runtime_fields
    ]
    gate(
        "G13_RAG_RUNTIME_FIELD_BOUNDARY",
        not runtime_boundary_errors,
        f"invalid={len(runtime_boundary_errors)}",
    )

    from food_agent_v2.b1.nutrition_reference import NUTRIENT_FIELDS

    def nutrition_record_valid(item: dict) -> bool:
        available = item.get("available") is True
        weight = item.get("raw_edible_input_weight_g")
        total = item.get("raw_nutrition_total")
        per_100g = item.get("raw_nutrition_per_100g")
        if not available:
            return (
                item.get("available") is False
                and weight is None
                and total is None
                and per_100g is None
                and bool(item.get("reason"))
            )
        if item.get("reason") is not None or not _valid_nonnegative_number(weight):
            return False
        if float(weight) <= 0 or not isinstance(total, dict) or not isinstance(per_100g, dict):
            return False
        return all(
            set(vector) == set(NUTRIENT_FIELDS)
            and all(_valid_nonnegative_number(vector[field]) for field in NUTRIENT_FIELDS)
            for vector in (total, per_100g)
        )

    nutrition_all_or_nothing_errors = [
        item.get("recipe_id")
        for item in artifacts["nutrition_features"]
        if not nutrition_record_valid(item)
    ]
    gate(
        "G14_NUTRITION_ALL_OR_NOTHING",
        not nutrition_all_or_nothing_errors,
        f"invalid={len(nutrition_all_or_nothing_errors)}",
    )

    nutrition_available_count = sum(
        item.get("available") is True for item in artifacts["nutrition_features"]
    )
    gate(
        "G15_NUTRITION_COVERAGE_NON_REGRESSION",
        nutrition_available_count >= APPROVED_NUTRITION_AVAILABLE_BASELINE,
        f"baseline={APPROVED_NUTRITION_AVAILABLE_BASELINE}, actual={nutrition_available_count}",
    )

    from food_agent_v2.b1.schemas import StepTask
    from food_agent_v2.b1.step_atomizer import atomize_recipe_steps
    from food_agent_v2.b1.step_time_builder import split_steps
    from food_agent_v2.b1.time_graph_profiler import validate_time_graph
    from food_agent_v2.b1.time_review_decisions import (
        apply_time_review_decisions,
        load_time_review_decisions,
    )
    from food_agent_v2.b5.scheduler import schedule_task_graphs

    try:
        time_review_decisions = load_time_review_decisions(TIME_REVIEW_DECISIONS)
    except ValueError as exc:
        _fail("G16_TIME_GRAPH_COMPLETE_AND_ACYCLIC", f"time decisions invalid: {exc}")
    time_graph_errors: list[int] = []
    for item in artifacts["step_tasks"]:
        recipe_id = int(item["recipe_id"])
        try:
            source_row = source_by_id[recipe_id]
            source_steps = split_steps(str(source_row.get("steps_raw", "")))
            atoms = atomize_recipe_steps(
                recipe_id=recipe_id,
                steps=enumerate(source_steps, start=1),
            )
            atoms = apply_time_review_decisions(
                recipe_id=recipe_id,
                atoms=atoms,
                decisions=time_review_decisions,
            )
            tasks = tuple(StepTask.model_validate(task) for task in item.get("step_tasks", ()))
            if not atoms or not tasks:
                raise ValueError("步骤覆盖不足")
            validate_time_graph(atoms, tasks)
            schedule_task_graphs({recipe_id: tasks})
        except Exception:
            time_graph_errors.append(recipe_id)
    gate(
        "G16_TIME_GRAPH_COMPLETE_AND_ACYCLIC",
        not time_graph_errors,
        f"invalid={len(time_graph_errors)}",
    )

    health_forbidden = {
        "step_tasks",
        "active_seconds",
        "estimated_elapsed_seconds",
        "raw_nutrition_total",
        "raw_nutrition_per_100g",
    }
    rag_forbidden = health_forbidden | {
        "verdict",
        "constraint_code",
        "participant_ref",
    }
    nutrition_forbidden = {
        "step_tasks",
        "duration_seconds",
        "task_type",
        "verdict",
        "constraint_code",
        "participant_ref",
    }
    time_forbidden = {
        "raw_nutrition_total",
        "raw_nutrition_per_100g",
        "label_tags",
        "meal_tags",
        "verdict",
        "constraint_code",
        "participant_ref",
    }
    consumer_boundary_errors = sum(
        bool(_recursive_keys(item) & forbidden)
        for artifact_name, forbidden in (
            ("recipe_health_views", health_forbidden),
            ("rag_documents", rag_forbidden),
            ("nutrition_features", nutrition_forbidden),
            ("step_tasks", time_forbidden),
        )
        for item in artifacts[artifact_name]
    )
    gate(
        "G17_B4_B5_B6_CONSUMER_BOUNDARY",
        consumer_boundary_errors == 0,
        f"invalid={consumer_boundary_errors}",
    )

    from food_agent_v2.b1.review_inputs import load_recipe_dependencies

    classification_types = {
        int(item["recipe_id"]): str(item["record_type"])
        for item in classifications
    }
    try:
        expected_dependencies = load_recipe_dependencies(
            PROJECT_ROOT / "data" / "review" / "recipe_dependencies.jsonl",
            known_recipe_ids=source_ids,
            record_types=classification_types,
        )
        expected_dependency_keys = {
            (
                item.parent_recipe_id,
                item.dependency_recipe_id,
                item.relation_type,
                item.review_status,
            )
            for item in expected_dependencies
        }
        actual_dependency_records = artifacts["recipe_dependencies"]
        actual_dependency_keys = {
            (
                int(item["parent_recipe_id"]),
                int(item["dependency_recipe_id"]),
                str(item["relation_type"]),
                str(item["review_status"]),
            )
            for item in actual_dependency_records
        }
        dependency_input_valid = True
    except (KeyError, TypeError, ValueError):
        expected_dependency_keys = set()
        actual_dependency_records = artifacts["recipe_dependencies"]
        actual_dependency_keys = set()
        dependency_input_valid = False
    bundle_contains_count = sum(
        item.get("relation_type") == "bundle_contains"
        for item in actual_dependency_records
    )
    gate(
        "G18_RECIPE_DEPENDENCY_CLOSURE",
        dependency_input_valid
        and actual_dependency_keys == expected_dependency_keys
        and len(actual_dependency_records)
        == len(actual_dependency_keys)
        == FIXED_RECIPE_DEPENDENCY_COUNT
        and bundle_contains_count == FIXED_RECIPE_BUNDLE_CONTAINS_COUNT,
        f"expected={len(expected_dependency_keys)}, actual={len(actual_dependency_records)}, "
        f"unique={len(actual_dependency_keys)}, bundle_contains={bundle_contains_count}",
    )

    metrics = {name: len(records) for name, records in artifacts.items()}
    metrics["health_ingredient_universe"] = len(health_ingredient_ids)
    metrics["nutrition_available_count"] = nutrition_available_count
    return gates, metrics


def write_quality_gate_report(
    staging_root: Path,
    artifact_paths: dict[str, Path],
    *,
    build_id: str,
    source_manifest_hash: str,
) -> Path:
    """Write the pre-manifest S2 report. Failures are fail-closed and still diagnostic."""
    report_path = Path(staging_root) / "quality_gate_report.json"
    try:
        gates, metrics = evaluate_staging_quality(
            artifact_paths,
            build_id=build_id,
            source_manifest_hash=source_manifest_hash,
        )
    except DataQualityError as error:
        report = {
            "status": "failed",
            "passed": False,
            "build_id": build_id,
            "source_manifest_hash": source_manifest_hash,
            "failure_code": error.code,
            "failure_detail": error.message,
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        raise
    report = {
        "status": "passed",
        "passed": True,
        "build_id": build_id,
        "source_manifest_hash": source_manifest_hash,
        "gate_count": len(gates),
        "gates": [gate.as_dict() for gate in gates],
        "metrics": metrics,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report_path


def _resolve_manifest_artifact(root: Path, relative_path: str) -> Path:
    resolved = (root / relative_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        _fail("ARTIFACT_PATH_ESCAPE", relative_path)
    return resolved


def verify_build_manifest(
    manifest_path: Path,
    *,
    required_artifacts: tuple[str, ...] = REQUIRED_ARTIFACTS,
    run_semantic_gates: bool = True,
) -> BuildManifest:
    """Verify path confinement, hashes, row counts, build identity, and all S2 gates."""
    path = Path(manifest_path).resolve()
    if not path.is_file():
        _fail("BUILD_MANIFEST_MISSING", str(path))
    try:
        manifest = BuildManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, json.JSONDecodeError) as exc:
        _fail("BUILD_MANIFEST_INVALID", str(exc))

    root = path.parent.resolve()
    missing = set(required_artifacts) - set(manifest.artifacts)
    if missing:
        _fail("REQUIRED_ARTIFACT_MISSING", f"missing={sorted(missing)}")
    if required_artifacts == REQUIRED_ARTIFACTS and set(manifest.artifacts) != set(
        REQUIRED_ARTIFACTS
    ):
        _fail(
            "FIXED_ARTIFACT_SET_MISMATCH",
            f"固定构建必须恰好包含 {len(FIXED_ARTIFACT_NAMES)} 个 Artifact",
        )
    if not _SHA256_RE.match(manifest.source_manifest_hash):
        _fail("BUILD_MANIFEST_INVALID", "source_manifest_hash is not SHA-256")
    canonical_source = canonical_source_manifest()
    expected_source_hash = source_manifest_hash(canonical_build_input_manifest())
    if manifest.source_manifest_hash != expected_source_hash:
        _fail(
            "SOURCE_MANIFEST_HASH_MISMATCH",
            f"{manifest.source_manifest_hash} != {expected_source_hash}",
        )
    try:
        verify_source_file(RECIPES_RAW, canonical_source)
    except SourceManifestMismatch as exc:
        _fail(exc.code, exc.message)
    if set(manifest.schema_versions) != set(manifest.artifacts):
        _fail(
            "SCHEMA_VERSION_COVERAGE_MISMATCH",
            "schema_versions must cover exactly the declared artifacts",
        )
    invalid_schema_versions = {
        name: version
        for name, version in manifest.schema_versions.items()
        if version != artifact_schema_version(name)
    }
    if invalid_schema_versions:
        _fail("SCHEMA_VERSION_MISMATCH", str(invalid_schema_versions))

    artifact_paths: dict[str, Path] = {}
    for name, entry in manifest.artifacts.items():
        artifact_path = _resolve_manifest_artifact(root, entry.relative_path)
        if not artifact_path.is_file():
            _fail("ARTIFACT_MISSING", f"{name}: {entry.relative_path}")
        actual_hash = _sha256(artifact_path)
        if actual_hash != entry.sha256:
            _fail("ARTIFACT_HASH_MISMATCH", f"{name}: {actual_hash} != {entry.sha256}")
        actual_count = _jsonl_count(artifact_path)
        if actual_count != entry.row_count:
            _fail("ARTIFACT_COUNT_MISMATCH", f"{name}: {actual_count} != {entry.row_count}")
        artifact_paths[name] = artifact_path
        _assert_build_identity(
            name,
            _read_jsonl(artifact_path),
            build_id=str(manifest.build_id),
            source_manifest_hash=manifest.source_manifest_hash,
        )

    quality_path = _resolve_manifest_artifact(
        root,
        manifest.quality_gate_report.relative_path,
    )
    if not quality_path.is_file():
        _fail("QUALITY_GATE_REPORT_MISSING", str(quality_path))
    if _sha256(quality_path) != manifest.quality_gate_report.sha256:
        _fail("QUALITY_GATE_REPORT_HASH_MISMATCH", str(quality_path))
    try:
        quality_payload = json.loads(quality_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail("QUALITY_GATE_REPORT_INVALID", str(exc))
    if not manifest.quality_gate_report.passed or quality_payload.get("passed") is not True:
        _fail("QUALITY_GATES_NOT_PASSED", str(quality_path))
    if (
        quality_payload.get("build_id") != str(manifest.build_id)
        or quality_payload.get("source_manifest_hash") != manifest.source_manifest_hash
    ):
        _fail("QUALITY_REPORT_BUILD_MISMATCH", str(quality_path))

    if run_semantic_gates:
        evaluate_staging_quality(
            {name: artifact_paths[name] for name in required_artifacts},
            build_id=str(manifest.build_id),
            source_manifest_hash=manifest.source_manifest_hash,
        )
    return manifest


def run_all_gates(**_: int) -> dict:
    """Removed legacy count-only gate; callers must verify a complete BuildManifest."""
    _fail(
        "LEGACY_QUALITY_GATE_REMOVED",
        "use write_quality_gate_report() and verify_build_manifest()",
    )
