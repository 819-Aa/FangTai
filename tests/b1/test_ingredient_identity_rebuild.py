"""T06 食材身份重建测试。

五个产物齐全；身份 id 确定性（首次出现顺序）；crosswalk pending 决策
需人工 approve/reject 后才应用；rejected 保留独立身份；推荐出现零未解析；
全量 2000 行质量断言（qty 泄漏=0、unresolved=0、类别/食材族覆盖、家族案例）。
"""

import csv
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
        occurrences = read_jsonl(tmp_path / "out" / "ingredient_occurrences.jsonl")
        bamboo = next(item for item in occurrences if item["name_clean"] == "竹签")
        assert bamboo["resolved_ingredient_id"] is None
        assert bamboo["consumption_role"] == "non_edible"
        relations = read_jsonl(tmp_path / "out" / "recipe_ingredient_relations.jsonl")
        assert relations == [
            {
                "recipe_id": 1,
                "occurrence_id": "1-1",
                "ingredient_id": 1,
                "role": "required",
                "choice_group_id": None,
                "is_alternative": False,
                "is_composition_ref": False,
                "form": None,
            }
        ]

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
        # 批准姜丝→姜 后：注册表不含"姜丝"幽灵身份；处理形态不得进入别名表。
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
        assert aliases == []

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

    def test_form_merge_produces_form_but_not_alias(self, tmp_path: Path) -> None:
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
        assert aliases == []

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

    def test_signed_crosswalk_retains_candidate_evidence(self, tmp_path: Path) -> None:
        overrides = tmp_path / "overrides.csv"
        overrides.write_text(
            "source_key,operation,target_ingredient_ids,reason_code,form,review_status,reviewer,reviewed_at\n"
            "姜丝,merge,1,processing_variant,丝,approved,project_owner,2026-08-09\n",
            encoding="utf-8",
        )
        rows = make_rows([(1, "菜1", "姜10克；姜丝5克")])

        rebuild_ingredient_identities(rows, overrides, tmp_path / "out")

        crosswalk = read_jsonl(tmp_path / "out" / "ingredient_crosswalk.jsonl")
        decision = next(item for item in crosswalk if item["source_key"] == "姜丝")
        assert decision["occurrence_count"] == 1
        assert decision["sample_recipe_ids"] == [1]
        assert decision["sample_fragments"] == ["姜丝5克"]

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

    def test_multiple_aliases_may_share_one_terminal_identity(self, tmp_path: Path) -> None:
        overrides = tmp_path / "overrides.csv"
        overrides.write_text(
            "source_key,operation,target_ingredient_ids,reason_code,form,review_status,reviewer,reviewed_at\n"
            "白砂糖,merge,1,synonym,,approved,project_owner,2026-08-09\n"
            "砂糖,merge,1,synonym,,approved,project_owner,2026-08-09\n",
            encoding="utf-8",
        )
        rows = make_rows([(1, "菜1", "白糖10克；白砂糖5克；砂糖3克")])

        report = rebuild_ingredient_identities(rows, overrides, tmp_path / "out")

        aliases = read_jsonl(tmp_path / "out" / "ingredient_aliases.jsonl")
        assert aliases == [
            {"alias": "白砂糖", "ingredient_id": 1},
            {"alias": "砂糖", "ingredient_id": 1},
        ]
        assert report["gates"]["alias_unique"] is True

    def test_merge_chain_resolves_every_reference_to_terminal_identity(self, tmp_path: Path) -> None:
        overrides = tmp_path / "overrides.csv"
        overrides.write_text(
            "source_key,operation,target_ingredient_ids,reason_code,form,review_status,reviewer,reviewed_at\n"
            "大蒜,merge,1,synonym,,approved,project_owner,2026-08-09\n"
            "大蒜末,merge,2,processing_variant,末,approved,project_owner,2026-08-09\n",
            encoding="utf-8",
        )
        rows = make_rows([(1, "菜1", "蒜10克；大蒜5克；大蒜末3克")])

        report = rebuild_ingredient_identities(rows, overrides, tmp_path / "out")

        registry = read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")
        relations = read_jsonl(tmp_path / "out" / "recipe_ingredient_relations.jsonl")
        aliases = read_jsonl(tmp_path / "out" / "ingredient_aliases.jsonl")
        forms = read_jsonl(tmp_path / "out" / "ingredient_forms.jsonl")
        assert [(r["ingredient_id"], r["name_canonical"]) for r in registry] == [(1, "蒜")]
        assert {r["ingredient_id"] for r in relations} == {1}
        assert {a["alias"]: a["ingredient_id"] for a in aliases} == {
            "大蒜": 1,
        }
        assert forms == [{"ingredient_id": 1, "name_canonical": "蒜", "form": "末"}]
        assert report["gates"]["dangling_reference_count"] == 0

    def test_h02_candidate_contains_prefilled_form(self, tmp_path: Path) -> None:
        rows = make_rows([(1, "菜1", "姜10克；姜丝5克")])
        report = rebuild_ingredient_identities(rows, tmp_path / "overrides.csv", tmp_path / "out")

        with Path(report["h02_candidates_path"]).open(encoding="utf-8", newline="") as handle:
            candidates = list(csv.DictReader(handle))

        assert candidates[0]["source_key"] == "姜丝"
        assert candidates[0]["suggested_target_ingredient_id"] == "1"
        assert candidates[0]["form"] == "丝"

    def test_processing_candidate_wins_over_overlapping_synonym(self, tmp_path: Path) -> None:
        rows = make_rows([(1, "菜1", "干辣椒10克；干辣椒段5克")])
        report = rebuild_ingredient_identities(rows, tmp_path / "overrides.csv", tmp_path / "out")

        with Path(report["h02_candidates_path"]).open(encoding="utf-8", newline="") as handle:
            candidates = [
                row for row in csv.DictReader(handle) if row["source_key"] == "干辣椒段"
            ]

        assert len(candidates) == 1
        assert candidates[0]["reason_code"] == "processing_variant"
        assert candidates[0]["form"] == "干+段"
        registry = read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")
        assert "辣椒段" not in {row["name_canonical"] for row in registry}

    def test_prefix_and_state_forms_generate_terminal_candidates(self, tmp_path: Path) -> None:
        rows = make_rows(
            [(1, "菜1", "南瓜10克；去皮新鲜南瓜20克；花生10克；熟花生20克；番茄10克；罐装去皮番茄20克")]
        )
        report = rebuild_ingredient_identities(rows, tmp_path / "overrides.csv", tmp_path / "out")

        with Path(report["h02_candidates_path"]).open(encoding="utf-8", newline="") as handle:
            candidates = {row["source_key"]: row for row in csv.DictReader(handle)}

        assert candidates["去皮新鲜南瓜"]["suggested_target_name"] == "南瓜"
        assert candidates["去皮新鲜南瓜"]["form"] == "去皮+新鲜"
        assert candidates["熟花生"]["suggested_target_name"] == "花生"
        assert candidates["熟花生"]["form"] == "熟"
        assert candidates["罐装去皮番茄"]["suggested_target_name"] == "番茄"
        assert candidates["罐装去皮番茄"]["form"] == "罐装+去皮"

    def test_modified_non_edible_is_a_discard_candidate(self, tmp_path: Path) -> None:
        rows = make_rows([(1, "菜1", "荷叶1张；干荷叶1张")])
        report = rebuild_ingredient_identities(rows, tmp_path / "overrides.csv", tmp_path / "out")

        with Path(report["h02_candidates_path"]).open(encoding="utf-8", newline="") as handle:
            candidates = {row["source_key"]: row for row in csv.DictReader(handle)}

        assert candidates["荷叶"]["operation"] == "discard"
        assert candidates["干荷叶"]["operation"] == "discard"
        assert candidates["干荷叶"]["suggested_target_name"] == ""

    def test_prefix_form_can_target_a_synthetic_first_occurrence_base(self, tmp_path: Path) -> None:
        rows = make_rows([(1, "菜1", "新鲜牡蛎300克")])
        report = rebuild_ingredient_identities(rows, tmp_path / "overrides.csv", tmp_path / "out")

        registry = read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")
        with Path(report["h02_candidates_path"]).open(encoding="utf-8", newline="") as handle:
            candidates = {row["source_key"]: row for row in csv.DictReader(handle)}

        assert [(row["ingredient_id"], row["name_canonical"]) for row in registry] == [
            (1, "牡蛎"),
            (2, "新鲜牡蛎"),
        ]
        assert candidates["新鲜牡蛎"]["suggested_target_ingredient_id"] == "1"
        assert candidates["新鲜牡蛎"]["form"] == "新鲜"

    def test_curated_temperature_form_is_not_a_synonym(self, tmp_path: Path) -> None:
        overrides = tmp_path / "overrides.csv"
        overrides.write_text(
            "source_key,operation,target_ingredient_ids,reason_code,form,review_status,reviewer,reviewed_at\n"
            "温水,merge,1,processing_variant,温,approved,project_owner,2026-08-09\n",
            encoding="utf-8",
        )
        rows = make_rows([(1, "菜1", "水100克；温水50克")])

        rebuild_ingredient_identities(rows, overrides, tmp_path / "out")

        forms = read_jsonl(tmp_path / "out" / "ingredient_forms.jsonl")
        aliases = read_jsonl(tmp_path / "out" / "ingredient_aliases.jsonl")
        assert forms == [{"ingredient_id": 1, "name_canonical": "水", "form": "温"}]
        assert aliases == []

    def test_oil_fritter_is_not_a_processing_merge_candidate(self, tmp_path: Path) -> None:
        rows = make_rows([(1, "菜1", "油10克；油条1根")])
        rebuild_ingredient_identities(rows, tmp_path / "overrides.csv", tmp_path / "out")

        registry = read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")
        crosswalk = read_jsonl(tmp_path / "out" / "ingredient_crosswalk.jsonl")
        oil_fritter = next(r for r in registry if r["name_canonical"] == "油条")
        assert oil_fritter["category"] == "谷物"
        assert all(d["source_key"] != "油条" for d in crosswalk)


class TestFullScale:
    def test_real_2000_build_quality(self, tmp_path: Path) -> None:
        rows = load_verified_recipe_source(SOURCE_CSV, canonical_source_manifest())
        report = rebuild_ingredient_identities(rows, OVERRIDES, tmp_path / "out")
        gates = report["gates"]
        assert report["registry_count"] == 1855
        assert report["occurrence_count"] == 17526
        assert report["form_count"] == 365
        assert report["alias_count"] == 21
        assert report["pending_decision_count"] == 0
        assert report["row_count"] == 2000
        assert gates["qty_leakage_count"] == 0
        assert gates["unresolved_count"] == 0
        assert gates["family_coverage"] == 1.0
        assert gates["category_coverage"] >= gates["category_threshold"]
        assert report["status"] == "passed"
        assert Path(report["h02_candidates_path"]).exists()
        occurrences = read_jsonl(tmp_path / "out" / "ingredient_occurrences.jsonl")
        relations = read_jsonl(tmp_path / "out" / "recipe_ingredient_relations.jsonl")
        forms = read_jsonl(tmp_path / "out" / "ingredient_forms.jsonl")
        aliases = read_jsonl(tmp_path / "out" / "ingredient_aliases.jsonl")
        assert len(occurrences) == 17526
        assert sum(item["consumption_role"] == "non_edible" for item in occurrences) == 15
        assert len(relations) == 17511
        assert len({(item["ingredient_id"], item["form"]) for item in forms}) == len(forms)
        assert len({item["alias"] for item in aliases}) == len(aliases)
        assert "姜丝" not in {item["alias"] for item in aliases}
        # 文档化家族案例
        family = {r["name_canonical"]: r["family_name"] for r in read_jsonl(tmp_path / "out" / "ingredient_registry.jsonl")}
        assert {
            "切",
            "切丝",
            "切块",
            "洗净",
            "去皮",
            "冷冻",
            "根和叶分开",
            "葱姜",
            "姜葱",
            "青红椒",
        }.isdisjoint(family)
        assert {"咖喱块", "高汤块", "燕麦片", "干葱", "小米椒", "小葱", "香葱", "陈醋", "白醋"} <= set(family)
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
        assert stats["new_total"] == len(new_registry)
        assert stats["merge"] > 0
        assert stats["orphan"] == 0
        assert {record["operation"] for record in diff} <= {"identity", "merge", "split", "discard"}
        assert len(diff) == 3326

    def test_old_alternative_identity_maps_to_multiple_new_targets(self) -> None:
        new_registry = [
            {"ingredient_id": 1, "name_canonical": "牛肩肉"},
            {"ingredient_id": 2, "name_canonical": "牛腩"},
        ]
        old = [{"ingredient_id": 99, "name_canonical": "牛肩肉或牛腩"}]

        diff, stats = build_old_to_new_diff(
            old,
            new_registry,
            {"牛肩肉": 1, "牛腩": 2},
        )

        assert diff == [
            {
                "old_ingredient_id": 99,
                "old_name": "牛肩肉或牛腩",
                "operation": "split",
                "new_ingredient_ids": [1, 2],
                "new_names": ["牛肩肉", "牛腩"],
            }
        ]
        assert stats["split"] == 1
        assert stats["orphan"] == 0
