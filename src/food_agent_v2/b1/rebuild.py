"""T09 fixed-source build orchestrator.

Full builds are isolated in a new empty staging directory.  No database or Qdrant
write occurs here; initialization is a separate H04-gated command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from food_agent_v2.b1.consumer_views import (
    BuildIdentity,
    build_consumer_views,
    identity_facts_from_records,
    occurrence_facts_from_records,
    publish_downstream_build_views,
    recipe_facts_from_source,
)
from food_agent_v2.b1.edible_fraction_review import load_edible_fraction_decisions
from food_agent_v2.b1.health_relation_builder import (
    run_health_relation_stage as build_health_relation_stage,
)
from food_agent_v2.b1.ingredient_identity import rebuild_ingredient_identities
from food_agent_v2.b1.nutrition_occurrence_rules import load_nutrition_usage_decisions
from food_agent_v2.b1.quality_gates import (
    artifact_entry,
    build_artifact_entries,
    verify_build_manifest,
    write_quality_gate_report,
)
from food_agent_v2.b1.recipe_classifier import (
    classify_all,
    enforce_classification_gate,
    load_overrides,
    write_classification_output,
)
from food_agent_v2.b1.review_inputs import (
    apply_condition_defaults,
    load_ingredient_condition_defaults,
    load_recipe_profile_enrichments,
)
from food_agent_v2.b1.source_manifest import (
    canonical_build_input_manifest,
    canonical_source_manifest,
    load_verified_recipe_source,
)
from food_agent_v2.b1.user_cleaning import clean_one, load_raw_users
from food_agent_v2.contracts.build import BuildManifest, QualityGateReport, source_manifest_hash
from food_agent_v2.core.paths import PROJECT_ROOT, RECIPES_RAW, USERS_RAW

V2_RUNTIME_ARTIFACTS = frozenset(
    {"rag_documents", "nutrition_features", "step_tasks", "recipe_nutrition_input_views"}
)

CLASSIFICATION_OVERRIDES = PROJECT_ROOT / "data" / "review" / "recipe_classification_overrides.csv"
INGREDIENT_OVERRIDES = PROJECT_ROOT / "data" / "review" / "ingredient_identity_overrides.csv"
HEALTH_DECISIONS = PROJECT_ROOT / "data" / "review" / "health_relation_decisions.csv"
RECIPE_PROFILE_ENRICHMENTS = PROJECT_ROOT / "data" / "review" / "recipe_profile_enrichment.jsonl"
NUTRITION_USAGE_DECISIONS = (
    PROJECT_ROOT / "data" / "review" / "ingredient_nutrition_usage_decisions.csv"
)
EDIBLE_FRACTION_DECISIONS = (
    PROJECT_ROOT / "data" / "review" / "ingredient_edible_fraction_decisions.csv"
)
INGREDIENT_CONDITION_DEFAULTS = (
    PROJECT_ROOT / "data" / "review" / "ingredient_condition_defaults.csv"
)


class DataPipelineError(RuntimeError):
    """Stable failure raised before a build can be considered publishable."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def artifact_schema_versions(artifacts: dict[str, object]) -> dict[str, str]:
    """仅四个重建的运行时 Artifact 升级为 2.0.0。"""
    return {
        name: "2.0.0" if name in V2_RUNTIME_ARTIFACTS else "1.0.0"
        for name in artifacts
    }


def _prepare_empty_staging(staging_dir: Path) -> Path:
    staging = Path(staging_dir).resolve()
    if staging.exists() and any(staging.iterdir()):
        raise DataPipelineError("STAGING_NOT_EMPTY", str(staging))
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def _git_builder_version() -> str:
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        if status.stdout.strip():
            raise DataPipelineError(
                "GIT_WORKTREE_NOT_CLEAN",
                "commit the reviewed build code and data decisions before data-rebuild",
            )
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except DataPipelineError:
        raise
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DataPipelineError("GIT_BUILDER_VERSION_UNAVAILABLE", str(exc)) from exc
    value = result.stdout.strip()
    if len(value) != 40:
        raise DataPipelineError("GIT_BUILDER_VERSION_INVALID", value)
    return value


def _read_jsonl(path: Path) -> tuple[dict, ...]:
    return tuple(
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )


def _write_jsonl(path: Path, records: list[dict] | tuple[dict, ...]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                + "\n"
            )
    return path


def _attach_build_identity(path: Path, *, build_id: str, manifest_hash: str) -> None:
    records = _read_jsonl(path)
    enriched = tuple(
        {
            **record,
            "build_id": build_id,
            "source_manifest_hash": manifest_hash,
        }
        for record in records
    )
    _write_jsonl(path, enriched)


def _write_source_rows(path: Path, rows, *, build_id: str, manifest_hash: str) -> Path:
    records = [
        {
            **asdict(row),
            "build_id": build_id,
            "source_manifest_hash": manifest_hash,
        }
        for row in rows
    ]
    return _write_jsonl(path, records)


def _write_user_profiles(path: Path, *, build_id: str, manifest_hash: str) -> tuple[Path, dict]:
    raw_users = load_raw_users(USERS_RAW)
    records = []
    for index, raw in enumerate(raw_users, start=1):
        profile = clean_one(raw, index)
        records.append(
            {
                "user_id": profile.user_id,
                "source_user_id": profile.source_user_id,
                "gender": profile.gender,
                "age": profile.age,
                "activity_level": profile.activity_level,
                "special_group": profile.special_group,
                "height_cm": profile.height_cm,
                "weight_kg": profile.weight_kg,
                "bmi": profile.bmi,
                "dietary_preferences": profile.dietary_preferences,
                "allergies": profile.allergies,
                "health_goals": profile.health_goals,
                "diseases": profile.diseases,
                "taboo_ingredients": profile.taboo_ingredients,
                "health_metrics": profile.health_metrics,
                "parse_quality": profile.parse_quality,
                "cleaning_notes": profile.cleaning_notes,
                "build_id": build_id,
                "source_manifest_hash": manifest_hash,
            }
        )
    _write_jsonl(path, records)
    return path, {
        "status": "passed" if len(records) == 50 else "failed",
        "profile_count": len(records),
    }


def run_ingredient_stage(
    staging_dir: Path,
    *,
    source_path: Path = RECIPES_RAW,
    overrides_path: Path = INGREDIENT_OVERRIDES,
) -> dict:
    """Backward-compatible standalone T06 build used during identity review."""
    staging = Path(staging_dir).resolve()
    rows = load_verified_recipe_source(source_path, canonical_source_manifest())
    report = rebuild_ingredient_identities(rows, overrides_path, staging)
    (staging / "ingredient_identity_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def run_health_relation_stage(staging_dir: Path) -> dict:
    """Backward-compatible standalone T08 build from sibling T06/T07 outputs."""
    staging = Path(staging_dir).resolve()
    return build_health_relation_stage(
        ingredient_registry_path=staging.parent / "T06" / "ingredient_registry.jsonl",
        health_views_path=staging.parent / "T07" / "recipe_health_views.jsonl",
        decisions_path=HEALTH_DECISIONS,
        staging_dir=staging,
        builder_identity="food-agent-v2:T08",
    )


def prepare_reviewed_consumer_inputs(rows, classifications, occurrences):
    """把已审画像和默认食材决定接入正式消费者视图构建输入。"""
    enrichments = load_recipe_profile_enrichments(
        RECIPE_PROFILE_ENRICHMENTS,
        known_recipe_ids={row.recipe_id for row in rows},
    )
    facts = recipe_facts_from_source(rows, classifications, enrichments)
    condition_defaults = load_ingredient_condition_defaults(
        INGREDIENT_CONDITION_DEFAULTS,
        known_recipe_names={fact.name for fact in facts},
    )
    reviewed_occurrences = apply_condition_defaults(
        occurrences,
        recipe_names={fact.recipe_id: fact.name for fact in facts},
        decisions=condition_defaults,
    )
    return facts, reviewed_occurrences


def build_fixed_data_staging(
    staging_dir: Path,
    *,
    build_id: UUID | None = None,
    builder_version: str | None = None,
) -> dict:
    """Build the complete fixed-data artifact graph and a verified BuildManifest."""
    staging = _prepare_empty_staging(staging_dir)
    resolved_build_id = build_id or uuid4()
    build_id_text = str(resolved_build_id)
    resolved_builder_version = builder_version or _git_builder_version()
    manifest_hash = source_manifest_hash(canonical_build_input_manifest())

    t04 = staging / "T04"
    t05 = staging / "T05"
    t06 = staging / "T06"
    t07 = staging / "T07"
    t08 = staging / "T08"

    rows = tuple(load_verified_recipe_source(RECIPES_RAW, canonical_source_manifest()))
    source_rows_path = _write_source_rows(
        t04 / "recipe_source_rows.jsonl",
        rows,
        build_id=build_id_text,
        manifest_hash=manifest_hash,
    )
    user_profiles_path, user_report = _write_user_profiles(
        t04 / "user_profiles.jsonl",
        build_id=build_id_text,
        manifest_hash=manifest_hash,
    )
    if user_report["status"] != "passed":
        raise DataPipelineError("FIXED_USER_COUNT_MISMATCH", str(user_report))

    classifications = classify_all(list(rows), load_overrides(CLASSIFICATION_OVERRIDES))
    classification_report = write_classification_output(list(rows), classifications, t05)
    enforce_classification_gate(classifications)
    classification_path = t05 / "recipe_classifications.jsonl"
    _attach_build_identity(
        classification_path,
        build_id=build_id_text,
        manifest_hash=manifest_hash,
    )

    t06_report = rebuild_ingredient_identities(list(rows), INGREDIENT_OVERRIDES, t06)
    if t06_report["status"] != "passed":
        raise DataPipelineError("T06_NOT_PASSED", str(t06_report))
    t06_artifact_files = (
        "ingredient_occurrences.jsonl",
        "ingredient_registry.jsonl",
        "ingredient_aliases.jsonl",
        "ingredient_forms.jsonl",
        "ingredient_crosswalk.jsonl",
        "recipe_ingredient_relations.jsonl",
    )
    for filename in t06_artifact_files:
        _attach_build_identity(
            t06 / filename,
            build_id=build_id_text,
            manifest_hash=manifest_hash,
        )

    identities = identity_facts_from_records(
        _read_jsonl(t06 / "ingredient_registry.jsonl"),
        _read_jsonl(t06 / "ingredient_aliases.jsonl"),
    )
    recipe_facts, occurrence_facts = prepare_reviewed_consumer_inputs(
        rows,
        tuple(classifications),
        occurrence_facts_from_records(_read_jsonl(t06 / "ingredient_occurrences.jsonl")),
    )
    nutrition_usage_decisions = load_nutrition_usage_decisions(
        NUTRITION_USAGE_DECISIONS,
        current_occurrence_ids={item.occurrence_id for item in occurrence_facts},
    )
    edible_fraction_decisions = load_edible_fraction_decisions(
        EDIBLE_FRACTION_DECISIONS,
        current_occurrence_ids={item.occurrence_id for item in occurrence_facts},
    )
    views = build_consumer_views(
        build=BuildIdentity(resolved_build_id, manifest_hash),
        recipes=recipe_facts,
        occurrences=occurrence_facts,
        identities=identities,
        nutrition_usage_decisions=nutrition_usage_decisions,
    )
    downstream_report = publish_downstream_build_views(
        views,
        identities,
        (),
        t07,
        edible_fraction_decisions=edible_fraction_decisions,
    )

    t08_report = build_health_relation_stage(
        ingredient_registry_path=t06 / "ingredient_registry.jsonl",
        health_views_path=t07 / "recipe_health_views.jsonl",
        decisions_path=HEALTH_DECISIONS,
        staging_dir=t08,
        builder_identity="food-agent-v2:T08",
    )
    if t08_report["status"] != "passed":
        raise DataPipelineError("T08_NOT_PASSED", str(t08_report))
    for filename in (
        "health_relation_decisions.jsonl",
        "health_relations.jsonl",
        "health_relation_coverage.jsonl",
    ):
        _attach_build_identity(
            t08 / filename,
            build_id=build_id_text,
            manifest_hash=manifest_hash,
        )

    artifact_paths = {
        "recipe_source_rows": source_rows_path,
        "recipe_classifications": classification_path,
        "user_profiles": user_profiles_path,
        "ingredient_occurrences": t06 / "ingredient_occurrences.jsonl",
        "ingredient_registry": t06 / "ingredient_registry.jsonl",
        "ingredient_aliases": t06 / "ingredient_aliases.jsonl",
        "ingredient_forms": t06 / "ingredient_forms.jsonl",
        "ingredient_crosswalk": t06 / "ingredient_crosswalk.jsonl",
        "recipe_ingredient_relations": t06 / "recipe_ingredient_relations.jsonl",
        "recipe_health_views": t07 / "recipe_health_views.jsonl",
        "recipe_step_binding_views": t07 / "recipe_step_binding_views.jsonl",
        "recipe_nutrition_input_views": t07 / "recipe_nutrition_input_views.jsonl",
        "recipe_retrieval_build_views": t07 / "recipe_retrieval_build_views.jsonl",
        "step_tasks": t07 / "step_time_profiles.jsonl",
        "nutrition_features": t07 / "nutrition_reference_views.jsonl",
        "rag_documents": t07 / "rag_documents.jsonl",
        "health_relation_decisions": t08 / "health_relation_decisions.jsonl",
        "health_relations": t08 / "health_relations.jsonl",
        "health_relation_coverage": t08 / "health_relation_coverage.jsonl",
    }
    quality_path = write_quality_gate_report(
        staging,
        artifact_paths,
        build_id=build_id_text,
        source_manifest_hash=manifest_hash,
    )
    artifact_entries = build_artifact_entries(staging, artifact_paths)
    quality_entry = artifact_entry(staging, quality_path)
    manifest = BuildManifest(
        build_id=resolved_build_id,
        source_manifest_hash=manifest_hash,
        builder_version=resolved_builder_version,
        schema_versions=artifact_schema_versions(artifact_entries),
        artifacts=artifact_entries,
        quality_gate_report=QualityGateReport(
            relative_path=quality_entry.relative_path,
            sha256=quality_entry.sha256,
            passed=True,
        ),
        created_at=datetime.now(UTC),
    )
    manifest_path = staging / "build_manifest.json"
    manifest_path.write_text(
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    verify_build_manifest(manifest_path)
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return {
        "status": "passed",
        "build_id": build_id_text,
        "source_manifest_hash": manifest_hash,
        "builder_version": resolved_builder_version,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "artifact_count": len(artifact_entries),
        "classification": classification_report,
        "T06": t06_report,
        "T07": downstream_report,
        "T08": t08_report,
        "user_profiles": user_report,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="food-agent-v2 data-rebuild")
    parser.add_argument("--stage", choices=("ingredients", "health-relations"))
    parser.add_argument("--staging-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.stage == "ingredients":
        report = run_ingredient_stage(args.staging_dir)
    elif args.stage == "health-relations":
        report = run_health_relation_stage(args.staging_dir)
    else:
        report = build_fixed_data_staging(args.staging_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
