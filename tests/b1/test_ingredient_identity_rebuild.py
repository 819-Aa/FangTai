"""T06 食材身份重建测试。

五个产物齐全；身份 id 确定性（首次出现顺序）；crosswalk pending 决策
需人工 approve/reject 后才应用；rejected 保留独立身份；推荐出现零未解析；
全量 2000 行质量断言（qty 泄漏=0、unresolved=0、类别/食材族覆盖、家族案例）。
"""

import json
from pathlib import Path

import pytest

from food_agent_v2.b1.ingredient_identity import (
    build_old_to_new_diff,
    rebuild_ingredient_identities,
)
from food_agent_v2.b1.schemas import SourceRecipeRow
from food_agent_v2.b1.source_manifest import canonical_source_manifest, load_verified_recipe_source

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_CSV = REPO_ROOT / "data" / "raw" / "recipes_sample_2000.csv"
OLD_REGISTRY = REPO_ROOT / "data" / "cleaned" / "ingredient_registry.jsonl"
OVERRIDES = REPO_ROOT / "data" / "review" / "ingredient_identity_overrides.csv"


def load_old_registry() -> list[dict]:
    with OLD_REGISTRY.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def make_rows(recipes: list[tuple[int, str, str]]) -> list[SourceRecipeRow]:
    return [
        SourceRecipeRow(
            recipe_id=recipe_id,
            source_row_number=recipe_id,
            name=name,
            ingredients_raw=ingredients,
            steps_raw="",
            labels_raw="",
            row_sha256="a" * 64,
        )
        for recipe_id, name, ingredients in recipes
    ]


def read_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class TestIdentityRebuild:
    def test_builds_registry(self, tmp_path: Path) -> None:
        rows = make_rows([(1, "菜1", "盐2克；猪肉300克"), (2, "菜2", "盐适量；牛肉200克")])
        report = rebuild_ingredient_identities(
            rows, tmp_path / "overrides.csv", tmp_path / "out"
        )
        assert report["registry_count"] == 3  # 盐、猪肉、牛肉
        assert report["unresolved_count"] == 0

    def test_all_five_outputs_written(self, tmp_path: Path) -> None:
        rows = make_rows([(1, "菜1", "盐2克")])
        rebuild_ingredient_identities(rows, tmp_path / "overrides.csv", tmp_path / "out")
        for name in (
            "ingredient_occurrences",
            "ingredient_registry",
            "ingredient_aliases",
            "ingredient_crosswalk",
            "recipe_ingredient_relations",
        ):
            assert (tmp_path / "out" / f"{name}.jsonl").exists()

    def test_deterministic_ids_first_seen_order(self, tmp_path: Path) -> None:
        rows = make_rows([(1, "菜1", "牛肉200克；盐2克")])
        rebuild_ingredient_identities(rows, tmp_path / "overrides.csv", tmp_path / "out")
        registry = read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")
        assert [r["name_canonical"] for r in registry] == ["牛肉", "盐"]

    def test_processing_variant_merge_pending(self, tmp_path: Path) -> None:
        rows = make_rows([(1, "菜1", "姜10克；姜丝5克")])
        rebuild_ingredient_identities(rows, tmp_path / "overrides.csv", tmp_path / "out")
        crosswalk = read_jsonl(tmp_path / "out" / "ingredient_crosswalk.jsonl")
        merge = [d for d in crosswalk if d["operation"] == "merge" and d["source_key"] == "姜丝"]
        assert len(merge) == 1
        assert merge[0]["review_status"] == "pending"

    def test_non_edible_discard_pending(self, tmp_path: Path) -> None:
        rows = make_rows([(1, "菜1", "盐2克；竹签2根")])
        rebuild_ingredient_identities(rows, tmp_path / "overrides.csv", tmp_path / "out")
        crosswalk = read_jsonl(tmp_path / "out" / "ingredient_crosswalk.jsonl")
        discard = [d for d in crosswalk if d["operation"] == "discard" and d["source_key"] == "竹签"]
        assert len(discard) == 1
        assert discard[0]["review_status"] == "pending"

    def test_discard_applied_after_approval(self, tmp_path: Path) -> None:
        overrides = tmp_path / "overrides.csv"
        overrides.write_text(
            "source_key,operation,target_ingredient_ids,reason_code,reviewer,reviewed_at\n"
            "竹签,discard,,non_edible,project_owner,2026-08-09\n",
            encoding="utf-8",
        )
        rows = make_rows([(1, "菜1", "盐2克；竹签2根")])
        rebuild_ingredient_identities(rows, overrides, tmp_path / "out")
        registry = read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")
        assert [r["name_canonical"] for r in registry] == ["盐"]
        relations = read_jsonl(tmp_path / "out" / "recipe_ingredient_relations.jsonl")
        assert all(r["ingredient_id"] != 2 for r in relations)

    def test_merge_applied_after_approval(self, tmp_path: Path) -> None:
        overrides = tmp_path / "overrides.csv"
        overrides.write_text(
            "source_key,operation,target_ingredient_ids,reason_code,reviewer,reviewed_at\n"
            "姜丝,merge,1,processing_variant,project_owner,2026-08-09\n",
            encoding="utf-8",
        )
        rows = make_rows([(1, "菜1", "姜10克；姜丝5克")])
        rebuild_ingredient_identities(rows, overrides, tmp_path / "out")
        relations = read_jsonl(tmp_path / "out" / "recipe_ingredient_relations.jsonl")
        assert all(r["ingredient_id"] == 1 for r in relations)

    def test_merge_no_ghost_registry_and_unique_alias(self, tmp_path: Path) -> None:
        # 批准姜丝→姜 后：注册表不含"姜丝"幽灵身份；别名中"姜丝"仅一条映射(→1)。
        overrides = tmp_path / "overrides.csv"
        overrides.write_text(
            "source_key,operation,target_ingredient_ids,reason_code,reviewer,reviewed_at\n"
            "姜丝,merge,1,processing_variant,project_owner,2026-08-09\n",
            encoding="utf-8",
        )
        rows = make_rows([(1, "菜1", "姜10克；姜丝5克")])
        rebuild_ingredient_identities(rows, overrides, tmp_path / "out")
        registry = read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")
        assert [r["name_canonical"] for r in registry] == ["姜"]
        aliases = read_jsonl(tmp_path / "out" / "ingredient_aliases.jsonl")
        jiangsi = [a for a in aliases if a["alias"] == "姜丝"]
        assert jiangsi == [{"alias": "姜丝", "ingredient_id": 1}]

    def test_registry_pending_before_freeze(self, tmp_path: Path) -> None:
        # 有 pending 决策时注册表身份未冻结（review_status=pending）。
        rows = make_rows([(1, "菜1", "姜10克；姜丝5克")])
        rebuild_ingredient_identities(rows, tmp_path / "overrides.csv", tmp_path / "out")
        registry = read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")
        assert all(r["review_status"] == "pending" for r in registry)

    def test_registry_approved_after_freeze(self, tmp_path: Path) -> None:
        # 全部决策批准后注册表冻结为 approved。
        overrides = tmp_path / "overrides.csv"
        overrides.write_text(
            "source_key,operation,target_ingredient_ids,reason_code,reviewer,reviewed_at\n"
            "姜丝,merge,1,processing_variant,project_owner,2026-08-09\n",
            encoding="utf-8",
        )
        rows = make_rows([(1, "菜1", "姜10克；姜丝5克")])
        rebuild_ingredient_identities(rows, overrides, tmp_path / "out")
        registry = read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")
        assert all(r["review_status"] == "approved" for r in registry)

    def test_single_char_ingredient_preserved(self, tmp_path: Path) -> None:
        # 单字符食材（盐/葱/姜/蒜）不得被丢弃。
        rows = make_rows([(1, "菜1", "盐；葱")])
        rebuild_ingredient_identities(rows, tmp_path / "overrides.csv", tmp_path / "out")
        registry = read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")
        assert {r["name_canonical"] for r in registry} == {"盐", "葱"}


class TestRejectedAndForms:
    def test_rejected_merge_keeps_independent_identity(self, tmp_path: Path) -> None:
        overrides = tmp_path / "overrides.csv"
        overrides.write_text(
            "source_key,operation,target_ingredient_ids,reason_code,form,review_status,reviewer,reviewed_at\n"
            "高汤块,merge,,processing_variant,块,rejected,project_owner,2026-08-09\n",
            encoding="utf-8",
        )
        rows = make_rows([(1, "菜1", "高汤块100克；高汤50克")])
        rebuild_ingredient_identities(rows, overrides, tmp_path / "out")
        registry = read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")
        assert "高汤块" in {r["name_canonical"] for r in registry}

    def test_form_merge_produces_forms_and_alias(self, tmp_path: Path) -> None:
        overrides = tmp_path / "overrides.csv"
        overrides.write_text(
            "source_key,operation,target_ingredient_ids,reason_code,form,review_status,reviewer,reviewed_at\n"
            "姜丝,merge,1,processing_variant,丝,approved,project_owner,2026-08-09\n",
            encoding="utf-8",
        )
        rows = make_rows([(1, "菜1", "姜10克；姜丝5克")])
        rebuild_ingredient_identities(rows, overrides, tmp_path / "out")
        forms = read_jsonl(tmp_path / "out" / "ingredient_forms.jsonl")
        assert forms == [{"ingredient_id": 1, "name_canonical": "姜", "form": "丝"}]
        aliases = read_jsonl(tmp_path / "out" / "ingredient_aliases.jsonl")
        assert {"alias": "姜丝", "ingredient_id": 1} in aliases

    def test_synonym_merge_produces_alias(self, tmp_path: Path) -> None:
        overrides = tmp_path / "overrides.csv"
        overrides.write_text(
            "source_key,operation,target_ingredient_ids,reason_code,form,review_status,reviewer,reviewed_at\n"
            "生姜,merge,1,synonym,,approved,project_owner,2026-08-09\n",
            encoding="utf-8",
        )
        rows = make_rows([(1, "菜1", "姜10克；生姜5克")])
        rebuild_ingredient_identities(rows, overrides, tmp_path / "out")
        aliases = read_jsonl(tmp_path / "out" / "ingredient_aliases.jsonl")
        assert {"alias": "生姜", "ingredient_id": 1} in aliases

    def test_loader_rejects_missing_signature(self, tmp_path: Path) -> None:
        overrides = tmp_path / "overrides.csv"
        overrides.write_text(
            "source_key,operation,target_ingredient_ids,reason_code,form,review_status,reviewer,reviewed_at\n"
            "姜丝,merge,1,processing_variant,丝,approved,,\n",
            encoding="utf-8",
        )
        rows = make_rows([(1, "菜1", "姜10克；姜丝5克")])
        with pytest.raises(ValueError):
            rebuild_ingredient_identities(rows, overrides, tmp_path / "out")


class TestFullScale:
    def test_real_2000_build_quality(self, tmp_path: Path) -> None:
        rows = load_verified_recipe_source(SOURCE_CSV, canonical_source_manifest())
        report = rebuild_ingredient_identities(rows, OVERRIDES, tmp_path / "out")
        gates = report["gates"]
        assert report["registry_count"] == 2249
        assert report["row_count"] == 2000
        assert gates["qty_leakage_count"] == 0
        assert gates["unresolved_count"] == 0
        assert gates["family_coverage"] == 1.0
        assert gates["category_coverage"] >= gates["category_threshold"]
        assert report["status"] in ("blocked", "passed")
        assert Path(report["h02_candidates_path"]).exists()
        # 文档化家族案例
        family = {r["name_canonical"]: r["family_name"] for r in read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")}
        assert family["小龙虾"] == "虾族"
        assert family["基围虾"] == "虾族"
        assert family["对虾"] == "虾族"
        assert family["低筋面粉"] == "小麦粉族"
        assert family["中筋面粉"] == "小麦粉族"
        assert family["高筋面粉"] == "小麦粉族"
        assert family["梨肉"] == "水果族"

    def test_old_to_new_diff(self, tmp_path: Path) -> None:
        rows = load_verified_recipe_source(SOURCE_CSV, canonical_source_manifest())
        rebuild_ingredient_identities(rows, OVERRIDES, tmp_path / "out")
        new_registry = read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")
        id_by_name = {r["name_canonical"]: r["ingredient_id"] for r in new_registry}
        old = load_old_registry()
        diff, stats = build_old_to_new_diff(old, new_registry, id_by_name)
        assert stats["old_total"] == 3326
        assert stats["new_total"] == 2249
        assert stats["merge"] > 0
        assert stats["orphan"] < 20
        assert len(diff) == 3326
