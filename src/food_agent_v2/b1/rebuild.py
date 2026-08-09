"""B1 数据管线编排器 —— REFACTOR 自 V1 rebuild.py。

V2 变更：
- 从子进程调用改为单进程函数调用（状态共享、结构化错误传递）
- 阶段间传递数据对象而非文件路径
- 每个阶段返回 (数据, 汇总报告)，下一阶段消费上一阶段的数据
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from food_agent_v2.b1.cross_domain_validator import validate
from food_agent_v2.b1.health_relation_builder import (
    build_health_relations,
)
from food_agent_v2.b1.health_relation_builder import (
    run_health_relation_stage as build_health_relation_stage,
)
from food_agent_v2.b1.ingredient_identity import (
    build_ingredient_registry,
    rebuild_ingredient_identities,
)
from food_agent_v2.b1.nutrition_feature_builder import build_nutrition_features
from food_agent_v2.b1.quality_gates import GateFailure, run_all_gates
from food_agent_v2.b1.rag_document_builder import build_rag_documents
from food_agent_v2.b1.recipe_cleaning import clean_recipes
from food_agent_v2.b1.source_manifest import (
    canonical_source_manifest,
    load_verified_recipe_source,
)
from food_agent_v2.b1.step_time_builder import build_step_profiles
from food_agent_v2.b1.user_cleaning import clean_users
from food_agent_v2.core.paths import PIPELINE_REPORTS_DIR, PROJECT_ROOT, RECIPES_RAW

STAGES = [
    ("recipe_cleaning", "菜品清洗"),
    ("user_cleaning", "用户档案清洗"),
    ("ingredient_registry", "食材注册表"),
    ("step_time", "步骤时间构建"),
    ("nutrition", "营养特征构建"),
    ("rag_documents", "RAG检索文档"),
    ("health_relations", "健康关系构建"),
    ("cross_domain", "跨域引用校验"),
    ("quality_gates", "数据质量门禁"),
]


def run_ingredient_stage(
    staging_dir: Path,
    *,
    source_path: Path = RECIPES_RAW,
    overrides_path: Path = PROJECT_ROOT / "data" / "review" / "ingredient_identity_overrides.csv",
) -> dict:
    """运行计划规定的独立 T06 阶段，并把机器可核验报告写入 staging。"""
    staging = Path(staging_dir).resolve()
    rows = load_verified_recipe_source(source_path, canonical_source_manifest())
    report = rebuild_ingredient_identities(rows, overrides_path, staging)
    (staging / "ingredient_identity_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def run_health_relation_stage(staging_dir: Path) -> dict:
    """运行 T08；只从同一 staging 根目录的 T06/T07 结构化产物构建。"""
    staging = Path(staging_dir).resolve()
    return build_health_relation_stage(
        ingredient_registry_path=staging.parent / "T06" / "ingredient_registry.jsonl",
        health_views_path=staging.parent / "T07" / "recipe_health_views.jsonl",
        decisions_path=PROJECT_ROOT / "data" / "review" / "health_relation_decisions.csv",
        staging_dir=staging,
        builder_identity="food-agent-v2:T08",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="food-agent-v2 data-rebuild")
    parser.add_argument("--stage", choices=("ingredients", "health-relations"))
    parser.add_argument("--staging-dir", type=Path)
    args = parser.parse_args(argv)

    if args.stage == "ingredients":
        if args.staging_dir is None:
            parser.error("--stage ingredients requires --staging-dir")
        report = run_ingredient_stage(args.staging_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "passed" else 2
    if args.stage == "health-relations":
        if args.staging_dir is None:
            parser.error("--stage health-relations requires --staging-dir")
        report = run_health_relation_stage(args.staging_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "passed" else 2
    if args.staging_dir is not None:
        parser.error("--staging-dir requires --stage")

    PIPELINE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    results: list[dict] = []
    data: dict[str, list[dict]] = {}

    # --- 阶段 1: 菜品清洗 ---
    t0 = time.perf_counter()
    recipes, summary = clean_recipes()
    data["recipes"] = recipes
    results.append(
        {
            "name": STAGES[0][1],
            "module": STAGES[0][0],
            "status": "passed",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    )
    print(f"[PASS] 菜品清洗 — {len(recipes)} recipes")

    # --- 阶段 2: 用户清洗 ---
    t0 = time.perf_counter()
    users, summary = clean_users()
    data["users"] = users
    results.append(
        {
            "name": STAGES[1][1],
            "module": STAGES[1][0],
            "status": "passed",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    )
    print(f"[PASS] 用户清洗 — {len(users)} users")

    # --- 阶段 3: 食材注册表 ---
    t0 = time.perf_counter()
    ingredients, summary = build_ingredient_registry(recipes)
    data["ingredients"] = ingredients
    results.append(
        {
            "name": STAGES[2][1],
            "module": STAGES[2][0],
            "status": "passed",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    )
    print(f"[PASS] 食材注册表 — {len(ingredients)} ingredients")

    # --- 阶段 4: 步骤时间 ---
    t0 = time.perf_counter()
    time_profiles, summary = build_step_profiles(recipes)
    data["time_profiles"] = time_profiles
    results.append(
        {
            "name": STAGES[3][1],
            "module": STAGES[3][0],
            "status": "passed",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    )
    print(f"[PASS] 步骤时间 — {len(time_profiles)} profiles")

    # --- 阶段 5: 营养特征 ---
    t0 = time.perf_counter()
    nutrition_profiles, summary = build_nutrition_features(recipes)
    data["nutrition_profiles"] = nutrition_profiles
    results.append(
        {
            "name": STAGES[4][1],
            "module": STAGES[4][0],
            "status": "passed",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    )
    print(f"[PASS] 营养特征 — {len(nutrition_profiles)} profiles")

    # --- 阶段 6: RAG 文档 ---
    t0 = time.perf_counter()
    rag_docs, summary = build_rag_documents(recipes)
    data["rag_docs"] = rag_docs
    results.append(
        {
            "name": STAGES[5][1],
            "module": STAGES[5][0],
            "status": "passed",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    )
    print(f"[PASS] RAG文档 — {len(rag_docs)} docs")

    # --- 阶段 7: 健康关系构建 ---
    t0 = time.perf_counter()
    health_relations, summary = build_health_relations(ingredients)
    data["health_relations"] = health_relations
    health_status = summary["status"]
    results.append(
        {
            "name": STAGES[6][1],
            "module": STAGES[6][0],
            "status": health_status,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "approval_gate": summary.get("approval_gate"),
        }
    )
    print(f"[{health_status.upper()}] 健康关系 — {len(health_relations)} pending candidate pairs")
    if health_status != "passed":
        report = {
            "status": "blocked",
            "blocker": "H03",
            "stages": results,
            "data_summary": {
                "recipes": len(recipes),
                "users": len(users),
                "ingredients": len(ingredients),
                "time_profiles": len(time_profiles),
                "nutrition_profiles": len(nutrition_profiles),
                "rag_docs": len(rag_docs),
            },
        }
        report_path = PIPELINE_REPORTS_DIR / "pipeline_run.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[BLOCKED] H03 independent health relation approval required: {report_path}")
        return 2

    # --- 阶段 8: 跨域校验 ---
    t0 = time.perf_counter()
    validation_report = validate(recipes, users)
    cross_errors = len(validation_report.get("errors", []))
    results.append(
        {
            "name": STAGES[7][1],
            "module": STAGES[7][0],
            "status": validation_report["status"],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "errors": cross_errors,
            "warnings": len(validation_report.get("warnings", [])),
        }
    )
    print(f"[{validation_report['status'].upper()}] 跨域校验 — {cross_errors} errors")

    # --- 阶段 9: 质量门禁 ---
    t0 = time.perf_counter()
    try:
        run_all_gates(
            recipe_count=len(recipes),
            user_count=len(users),
            ingredient_count=len(ingredients),
            time_profile_count=len(time_profiles),
            nutrition_count=len(nutrition_profiles),
            rag_count=len(rag_docs),
            cross_domain_errors=cross_errors,
        )
        results.append(
            {
                "name": STAGES[8][1],
                "module": STAGES[8][0],
                "status": "passed",
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            }
        )
        print("[PASS] 质量门禁 — all gates passed")
        pipeline_passed = True
    except GateFailure as e:
        results.append(
            {
                "name": STAGES[8][1],
                "module": STAGES[8][0],
                "status": "failed",
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                "error": str(e),
            }
        )
        print(f"[FAIL] 质量门禁 — {e}")
        pipeline_passed = False

    # 恢复离线 LLM 时间估算（data-rebuild 会重新生成 time_profiles，需把 partial 合并回来）
    try:
        from food_agent_v2.b1.llm_time_profiler import merge_estimates

        merged = merge_estimates()
        if merged:
            print(f"[INFO] 恢复 LLM 时间估算 {merged} 条")
    except Exception as e:
        print(f"[WARN] 合并 LLM 时间估算失败: {e}")

    # 写入管线运行报告
    elapsed_total = round((time.perf_counter() - started) * 1000, 2)
    report = {
        "status": "passed" if pipeline_passed else "failed",
        "elapsed_ms": elapsed_total,
        "stages": results,
        "data_summary": {
            "recipes": len(recipes),
            "users": len(users),
            "ingredients": len(ingredients),
            "time_profiles": len(time_profiles),
            "nutrition_profiles": len(nutrition_profiles),
            "rag_docs": len(rag_docs),
        },
    }

    report_path = PIPELINE_REPORTS_DIR / "pipeline_run.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'=' * 40}")
    print(f"Pipeline {'PASSED' if pipeline_passed else 'FAILED'} ({elapsed_total} ms)")
    print(f"Report: {report_path}")

    return 0 if pipeline_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
