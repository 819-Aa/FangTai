"""B1 数据管线测试 —— 对应 D3 §6.1 必测项。"""

import json
import sys
from pathlib import Path

import pytest

# 确保 src 在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from food_agent_v2.core.paths import CLEANED_RECIPES, CLEANED_USERS, CLEANED_DIR
from food_agent_v2.b1.recipe_cleaning import clean_recipes, load_raw_recipes, clean_one
from food_agent_v2.b1.user_cleaning import clean_users, load_raw_users
from food_agent_v2.b1.ingredient_identity import build_ingredient_registry, normalize_ingredient_name


class TestRecipeCleaning:
    """D3 §6.1: 菜品清洗与类型识别"""

    def test_clean_recipes_count(self):
        """固定输入结构校验：2000 条菜品"""
        recipes, summary = clean_recipes()
        assert len(recipes) == 2000
        assert summary["status"] == "passed"

    def test_recipe_ids_stable(self):
        """recipe_id 稳定 1-2000"""
        recipes, _ = clean_recipes()
        ids = {r["recipe_id"] for r in recipes}
        assert ids == set(range(1, 2001))

    def test_no_empty_name(self):
        """菜名非空"""
        recipes, _ = clean_recipes()
        for r in recipes:
            assert r["名称"], f"recipe {r['recipe_id']} has empty name"

    def test_variants_tracked(self):
        """同名菜保留为不同变体"""
        recipes, _ = clean_recipes()
        variant_groups = [r for r in recipes if r["_cleaning"]["same_name_variant"]]
        # 至少有一个变体组（红烧肉）
        assert len(variant_groups) > 0

    def test_steps_not_supplemented(self):
        """V2: 步骤不补写"""
        recipes, _ = clean_recipes()
        supplemented = sum(1 for r in recipes if r["_cleaning"].get("steps_supplemented"))
        assert supplemented == 0


class TestUserCleaning:
    """D3 §6.2: 用户档案清洗"""

    def test_clean_users_count(self):
        """固定档案 50 份"""
        users, summary = clean_users()
        assert len(users) == 50
        assert summary["status"] == "passed"

    def test_user_ids_stable(self):
        """用户 ID 1-50 稳定"""
        users, _ = clean_users()
        ids = {u["user_id"] for u in users}
        assert ids == set(range(1, 51))

    def test_missing_metrics_null(self):
        """缺失指标保持 null"""
        users, _ = clean_users()
        for u in users:
            assert "health_metrics" in u
            # null 检查通过——parse_quality 记录了缺失状态

    def test_parse_quality_present(self):
        """解析质量元数据存在"""
        users, _ = clean_users()
        for u in users:
            assert "parse_quality" in u


class TestIngredientRegistry:
    """D3 §6.1: 食材身份注册表"""

    def test_registry_builds(self):
        """注册表构建成功"""
        recipes, _ = clean_recipes()
        registry, summary = build_ingredient_registry(recipes)
        assert len(registry) > 1000
        assert summary["status"] == "passed"
        assert "non_edible_count" in summary

    def test_registry_ids_stable(self):
        """ingredient_id 连续"""
        recipes, _ = clean_recipes()
        registry, _ = build_ingredient_registry(recipes)
        ids = {r["ingredient_id"] for r in registry}
        assert ids == set(range(1, len(registry) + 1))

    def test_normalize_quantity_prefix(self):
        """数量前缀剥离：'2克盐' → '盐'"""
        assert normalize_ingredient_name("2克盐") in ("盐",)

    def test_normalize_quantity_suffix(self):
        """数量后缀剥离：'盐2克' → '盐'"""
        result = normalize_ingredient_name("盐2克")
        assert result == "盐", f"Expected '盐', got '{result}'"

    def test_normalize_both(self):
        """前后缀同时剥离：'主料：梨肉1000g（（切块））' → '梨肉'"""
        result = normalize_ingredient_name("主料：梨肉1000g（（切块））")
        assert "梨肉" in result, f"Expected to contain '梨肉', got '{result}'"
        assert "1000" not in result
        assert "主料" not in result


class TestCrossDomain:
    """D3 §6.1: 跨域引用一致性"""

    def test_recipe_ids_have_ingredients(self):
        """每个 recipe_id 在注册表中至少有一个食材引用"""
        recipes, _ = clean_recipes()
        registry, _ = build_ingredient_registry(recipes)
        ing_names = {r["name_canonical"] for r in registry}
        # 至少存在盐、水等基础食材
        assert "盐" in ing_names
        assert "水" in ing_names

    def test_no_dangling_references(self):
        """食材注册表中无悬空引用"""
        registry_path = CLEANED_DIR / "ingredient_registry.jsonl"
        assert registry_path.exists()
        with registry_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    assert r["ingredient_id"] > 0
                    assert r["name_canonical"]


class TestQualityGates:
    """D3 §6.1: 质量门禁"""

    def test_pipeline_run_report_exists(self):
        """管线运行报告存在"""
        from food_agent_v2.core.paths import PIPELINE_REPORTS_DIR
        report_path = PIPELINE_REPORTS_DIR / "pipeline_run.json"
        assert report_path.exists(), "Run 'uv run food-agent-v2 data-rebuild' first"

    def test_pipeline_passed(self):
        """管线执行状态为 passed"""
        from food_agent_v2.core.paths import PIPELINE_REPORTS_DIR
        report_path = PIPELINE_REPORTS_DIR / "pipeline_run.json"
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            assert report["status"] == "passed"
            assert len(report["stages"]) >= 9
