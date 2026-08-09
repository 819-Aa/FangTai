"""T06 食材阶段 CLI/编排入口测试。"""

import json
from pathlib import Path

from food_agent_v2.b1.rebuild import run_ingredient_stage


def test_run_ingredient_stage_uses_signed_fixed_source(tmp_path: Path) -> None:
    report = run_ingredient_stage(tmp_path / "T06")

    assert report["status"] == "passed"
    assert report["row_count"] == 2000
    assert report["registry_count"] == 1781
    assert report["pending_decision_count"] == 0
    for artifact in (
        "ingredient_registry.jsonl",
        "ingredient_occurrences.jsonl",
        "ingredient_aliases.jsonl",
        "ingredient_forms.jsonl",
        "ingredient_crosswalk.jsonl",
        "recipe_ingredient_relations.jsonl",
        "ingredient_identity_report.json",
    ):
        assert (tmp_path / "T06" / artifact).exists()

    persisted = json.loads(
        (tmp_path / "T06" / "ingredient_identity_report.json").read_text(encoding="utf-8")
    )
    assert persisted == report
