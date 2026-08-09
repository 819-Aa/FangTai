"""T05 确定性分类器测试。

封闭枚举；确定性规则产生候选与 evidence；冲突/低置信度项 pending，只从
人工 override 读取；发布门禁要求 pending=0。
"""

import pytest

from food_agent_v2.b1.recipe_classifier import (
    ClassificationGateError,
    classify_all,
    classify_recipe,
    enforce_classification_gate,
    pending_recipe_ids,
    write_classification_output,
)
from food_agent_v2.b1.schemas import (
    ClassificationOverride,
    ClassificationReview,
    RecordType,
    SourceRecipeRow,
)


def make_row(recipe_id: int, name: str, ingredients: str = "食材", steps: str = "步骤", labels: str = "label") -> SourceRecipeRow:
    return SourceRecipeRow(
        recipe_id=recipe_id,
        source_row_number=recipe_id,
        name=name,
        ingredients_raw=ingredients,
        steps_raw=steps,
        labels_raw=labels,
        row_sha256="a" * 64,
    )


class TestClosedTypes:
    def test_dish_default(self) -> None:
        c = classify_recipe(make_row(1, "红烧肉"))
        assert c.record_type == RecordType.DISH
        assert c.review_status == ClassificationReview.APPROVED
        assert c.rule_evidence

    def test_test_record(self) -> None:
        c = classify_recipe(make_row(2, "气电灶测试菜谱"))
        assert c.record_type == RecordType.TEST_RECORD

    def test_meal_bundle(self) -> None:
        c = classify_recipe(make_row(3, "小龙虾套餐"))
        assert c.record_type == RecordType.MEAL_BUNDLE

    def test_preparation(self) -> None:
        c = classify_recipe(make_row(4, "万能凉拌汁"))
        assert c.record_type == RecordType.PREPARATION

    def test_cooking_program(self) -> None:
        c = classify_recipe(make_row(5, "蛋白打发"))
        assert c.record_type == RecordType.COOKING_PROGRAM

    def test_braised_dish_not_preparation(self) -> None:
        # 卤鸭翅是菜品，不是备料。
        c = classify_recipe(make_row(6, "卤鸭翅"))
        assert c.record_type == RecordType.DISH

    def test_all_classifications_closed_enum(self) -> None:
        rows = [make_row(i, f"测试菜{i}" if i == 1 else f"菜{i}") for i in range(1, 21)]
        for c in classify_all(rows):
            # 已批准分类必须属于封闭 5 类（不含 UNKNOWN 哨兵）。
            assert c.record_type in {
                RecordType.DISH,
                RecordType.PREPARATION,
                RecordType.MEAL_BUNDLE,
                RecordType.COOKING_PROGRAM,
                RecordType.TEST_RECORD,
            }


class TestConflictsPending:
    def test_weak_signal_pending(self) -> None:
        # "秋梨膏"以"膏"结尾，可能是糖浆备料也可能是甜品 → 弱信号必须 pending。
        c = classify_recipe(make_row(1, "秋梨膏"))
        assert c.review_status == ClassificationReview.PENDING
        assert c.record_type == RecordType.UNKNOWN

    def test_override_applied(self) -> None:
        overrides = {
            1: ClassificationOverride(
                recipe_id=1,
                record_type=RecordType.DISH,
                reason="人工确认",
                reviewer="reviewer",
                reviewed_at="2026-08-09",
            )
        }
        c = classify_recipe(make_row(1, "秋梨膏"), overrides)
        assert c.record_type == RecordType.DISH
        assert c.review_status == ClassificationReview.APPROVED

    def test_pending_blocks_gate(self) -> None:
        rows = [make_row(i, f"菜{i}") for i in range(1, 11)]
        rows.append(make_row(11, "秋梨膏"))
        classifications = classify_all(rows)
        assert pending_recipe_ids(classifications) == [11]

    def test_pending_zero_required_for_publish(self) -> None:
        rows = [make_row(i, f"菜{i}") for i in range(1, 5)]
        classifications = classify_all(rows)
        assert pending_recipe_ids(classifications) == []


class TestPublishGate:
    def test_gate_rejects_pending(self) -> None:
        rows = [make_row(i, f"菜{i}") for i in range(1, 4)]
        rows.append(make_row(4, "秋梨膏"))
        with pytest.raises(ClassificationGateError) as excinfo:
            enforce_classification_gate(classify_all(rows))
        assert excinfo.value.code == "CLASSIFICATION_PENDING"

    def test_gate_rejects_duplicate_ids(self) -> None:
        classifications = [
            classify_recipe(make_row(1, "红烧肉")),
            classify_recipe(make_row(1, "清蒸鱼")),
        ]
        with pytest.raises(ClassificationGateError) as excinfo:
            enforce_classification_gate(classifications)
        assert excinfo.value.code == "CLASSIFICATION_NOT_UNIQUE"

    def test_gate_passes_all_approved(self) -> None:
        rows = [make_row(i, f"菜{i}") for i in range(1, 4)]
        enforce_classification_gate(classify_all(rows))

    def test_writer_produces_artifact_and_blocked_report(self, tmp_path) -> None:
        rows = [make_row(i, f"菜{i}") for i in range(1, 4)]
        rows.append(make_row(4, "秋梨膏"))
        report = write_classification_output(rows, classify_all(rows), tmp_path)
        assert (tmp_path / "recipe_classifications.jsonl").exists()
        assert (tmp_path / "classification_report.json").exists()
        assert report["status"] == "blocked"
        assert report["pending_ids"] == [4]
