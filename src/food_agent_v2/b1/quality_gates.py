"""B1 数据质量门禁 —— V2 新增。

所有 B1 离线构建阶段通过后，运行质量门禁。门禁失败 → 管线停止，整改后重跑。
"""

from __future__ import annotations

import json
from pathlib import Path

from food_agent_v2.core.paths import PIPELINE_REPORTS_DIR


class GateFailure(Exception):
    """质量门禁失败异常。"""


def _check(gate_name: str, condition: bool, detail: str) -> None:
    if not condition:
        raise GateFailure(f"[{gate_name}] FAILED: {detail}")


def run_all_gates(
    recipe_count: int,
    user_count: int,
    ingredient_count: int,
    time_profile_count: int,
    nutrition_count: int,
    rag_count: int,
    cross_domain_errors: int,
) -> dict:
    """运行全部质量门禁。失败抛出 GateFailure。"""
    PIPELINE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    def gate(name: str, condition: bool, detail: str = ""):
        results.append({"name": name, "passed": condition, "detail": detail})
        if not condition:
            raise GateFailure(f"[{name}] FAILED: {detail}")

    # G1: 菜品数量正确
    gate("G1_recipe_count", recipe_count == 2000,
        f"Expected 2000, got {recipe_count}")

    # G2: 用户数量正确
    gate("G2_user_count", user_count == 50,
        f"Expected 50, got {user_count}")

    # G3: 食材注册表非空
    gate("G3_ingredient_registry", ingredient_count > 0,
        f"Ingredient registry is empty")

    # G4: 时间画像覆盖全部菜品
    gate("G4_time_profile_coverage", time_profile_count >= recipe_count * 0.95,
        f"Only {time_profile_count}/{recipe_count} recipes have time profiles")

    # G5: 营养特征覆盖全部菜品
    gate("G5_nutrition_coverage", nutrition_count >= recipe_count * 0.90,
        f"Only {nutrition_count}/{recipe_count} recipes have nutrition features")

    # G6: RAG 文档数量匹配
    gate("G6_rag_document_count", rag_count == recipe_count,
        f"RAG docs ({rag_count}) != recipes ({recipe_count})")

    # G7: 跨域校验无错误
    gate("G7_cross_domain_errors", cross_domain_errors == 0,
        f"{cross_domain_errors} cross-domain validation errors")

    # G8: 无 per_serving 泄漏
    nutrition_path = Path(PIPELINE_REPORTS_DIR).parent.parent / "data" / "cleaned" / "nutrition_profiles.jsonl"
    if nutrition_path.exists():
        has_per_serving = False
        with nutrition_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip() and "per_serving" in json.loads(line):
                    has_per_serving = True
                    break
        gate("G8_no_per_serving", not has_per_serving,
            "per_serving field found in nutrition profiles")

    # G9: RAG 文档无健康字段
    rag_path = Path(PIPELINE_REPORTS_DIR).parent.parent / "data" / "cleaned" / "rag_documents.jsonl"
    if rag_path.exists():
        has_health = False
        with rag_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    doc = json.loads(line)
                    fields = doc.get("searchable_fields", {})
                    if any(k in fields for k in ["allergen_types", "health_features", "risk_tags"]):
                        has_health = True
                        break
        gate("G9_rag_no_health_fields", not has_health,
            "Health-related fields found in RAG documents")

    # G10: 步骤未自动补写
    time_path = Path(PIPELINE_REPORTS_DIR).parent.parent / "data" / "cleaned" / "time_profiles.jsonl"
    if time_path.exists():
        supplemented = 0
        with time_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    if rec.get("_supplemented"):
                        supplemented += 1
        gate("G10_no_step_supplementation", supplemented == 0,
            f"{supplemented} recipes have supplemented steps")

    report = {
        "stage": "quality_gates",
        "status": "passed",
        "total_gates": len(results),
        "passed_gates": sum(1 for r in results if r["passed"]),
        "results": results,
    }

    report_path = PIPELINE_REPORTS_DIR / "quality_gates.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return report
