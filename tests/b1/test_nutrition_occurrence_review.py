from __future__ import annotations

import csv
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

import food_agent_v2.b1.nutrition_occurrence_review as nutrition_occurrence_review
from food_agent_v2.b1.consumer_views import (
    BuildIdentity,
    IngredientIdentityFact,
    IngredientOccurrenceFact,
    NutritionOccurrenceInput,
    RecipeFact,
    RecipeNutritionInputView,
    RecipeStepBindingView,
    StructuredStep,
)
from food_agent_v2.b1.nutrition_occurrence_review import (
    NutritionOccurrenceReviewContext,
    generate_nutrition_occurrence_review,
    write_nutrition_occurrence_review,
)
from food_agent_v2.core.paths import PROJECT_ROOT


def _create_symlink_or_skip_windows_privilege(
    alias: Path, formal: Path, context: str
) -> None:
    try:
        alias.symlink_to(formal)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip(
                "Windows symlink privilege unavailable for "
                f"{context} formal-target alias test: {exc}"
            )
        raise


def _context(
    *,
    name: str = "鸡肉",
    category: str = "肉类",
    ingredient_id: int = 7,
    step: str | None = "加入鸡肉炒熟",
    usage_requires_review: bool = True,
    retention_requires_review: bool = True,
    usage_code: str | None = None,
    retained_in_dish: bool | None = None,
) -> NutritionOccurrenceReviewContext:
    build = BuildIdentity(UUID(int=1), "a" * 64)
    occurrence = IngredientOccurrenceFact(
        occurrence_id="1-1",
        recipe_id=1,
        source_fragment="鸡肉 200g",
        name_clean=name,
        ingredient_id=ingredient_id,
        consumption_role="edible",
        quantity_raw="200g",
        form="切块",
    )
    input_item = NutritionOccurrenceInput(
        occurrence_id="1-1",
        ingredient_id=ingredient_id,
        quantity_raw="200g",
        unit_raw="g",
        quantity_status="explicit",
        ingredient_name=name,
        normalized_form="切块",
        usage_code=usage_code,  # type: ignore[arg-type]
        retained_in_dish=retained_in_dish,
        usage_requires_review=usage_requires_review,
        retention_requires_review=retention_requires_review,
    )
    nutrition_view = RecipeNutritionInputView(
        build_id=build.build_id,
        source_manifest_hash=build.source_manifest_hash,
        recipe_id=1,
        ingredients=(input_item,),
    )
    steps = () if step is None else (
        StructuredStep(step_index=1, raw_text=step, bound_occurrence_ids=("1-1",)),
    )
    step_view = RecipeStepBindingView(
        build_id=build.build_id,
        source_manifest_hash=build.source_manifest_hash,
        recipe_id=1,
        ingredient_ids=(ingredient_id,),
        steps=steps,
    )
    return NutritionOccurrenceReviewContext(
        views=SimpleNamespace(nutrition_views=(nutrition_view,), step_views=(step_view,)),
        occurrences=(occurrence,),
        identities=(
            IngredientIdentityFact(
                ingredient_id=ingredient_id,
                name_canonical=name,
                family_id=ingredient_id,
                category=category,
            ),
        ),
        recipes=(RecipeFact(recipe_id=1, name="测试菜", record_type="dish"),),
    )


def test_bound_solid_without_discard_risk_is_pending_retention_candidate() -> None:
    bundle = generate_nutrition_occurrence_review(_context())

    assert [
        (row.occurrence_id, row.candidate_retained_in_dish, row.review_status)
        for row in bundle.retention_candidates
    ] == [("1-1", True, "pending")]
    assert bundle.retention_exceptions == ()


def test_discard_and_no_binding_are_separate_retention_exceptions() -> None:
    discard = generate_nutrition_occurrence_review(_context(name="香料", step="煮后捞出香料"))
    missing = generate_nutrition_occurrence_review(_context(name="香料", step=None))

    assert discard.retention_exceptions[0].exception_codes == (
        "NUTRITION_RETENTION_DISCARD_RISK",
    )
    assert missing.retention_exceptions[0].exception_codes == (
        "NUTRITION_RETENTION_NO_STEP_BINDING",
    )


def test_resolved_seasoning_is_not_repeated_as_usage_candidate() -> None:
    bundle = generate_nutrition_occurrence_review(
        _context(
            name="盐",
            category="调料",
            usage_requires_review=False,
            usage_code="seasoning",
        )
    )

    assert bundle.usage_candidates == ()
    assert bundle.usage_exceptions == ()


@pytest.mark.parametrize(
    ("name", "candidate_usage_code"),
    (("食用油", "cooking_fat"), ("清水", "cooking_liquid")),
)
def test_controlled_oil_and_liquid_are_usage_candidates_only(
    name: str, candidate_usage_code: str
) -> None:
    bundle = generate_nutrition_occurrence_review(_context(name=name))

    assert [row.candidate_usage_code for row in bundle.usage_candidates] == [
        candidate_usage_code
    ]
    assert bundle.usage_exceptions == ()


def test_uncontrolled_usage_is_ambiguous_exception() -> None:
    bundle = generate_nutrition_occurrence_review(_context(name="鸡肉"))

    assert bundle.usage_exceptions[0].exception_codes == ("NUTRITION_USAGE_AMBIGUOUS",)


@pytest.mark.parametrize("name", ("水果", "水牛芝士", "汤圆"))
def test_solid_names_with_liquid_characters_are_not_controlled_liquids(name: str) -> None:
    bundle = generate_nutrition_occurrence_review(_context(name=name))

    assert bundle.usage_candidates == ()
    assert bundle.usage_exceptions[0].exception_codes == ("NUTRITION_USAGE_AMBIGUOUS",)
    assert [row.candidate_retained_in_dish for row in bundle.retention_candidates] == [True]
    assert bundle.retention_exceptions == ()


@pytest.mark.parametrize("name", ("食用油", "清水"))
def test_oil_and_liquid_retention_never_auto_true(name: str) -> None:
    bundle = generate_nutrition_occurrence_review(_context(name=name))

    assert bundle.retention_candidates == ()
    assert bundle.retention_exceptions[0].exception_codes == (
        "NUTRITION_RETENTION_AMBIGUOUS",
    )


@pytest.mark.parametrize(
    ("name", "category"),
    (("芝麻油", "坚果"), ("牛奶", "蛋奶")),
)
def test_real_oil_and_milk_identities_never_enter_bound_solid_candidates(
    name: str, category: str
) -> None:
    bundle = generate_nutrition_occurrence_review(
        _context(name=name, category=category)
    )

    assert bundle.retention_candidates == ()
    assert bundle.retention_exceptions[0].exception_codes == (
        "NUTRITION_RETENTION_AMBIGUOUS",
    )


def test_approved_density_identities_from_repository_are_retention_ambiguous() -> None:
    categories = {
        "纯净水": "其他",
        "醋": "调料",
        "牛奶": "蛋奶",
        "柠檬汁": "水果",
        "色拉油": "调料",
        "橄榄油": "调料",
        "烧烤汁": "调料",
    }
    with (PROJECT_ROOT / "data" / "review" / "ingredient_measure_rules.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        approved_density_identities = {
            (int(row["ingredient_id"]), row["ingredient_name"])
            for row in csv.DictReader(handle)
            if row["rule_type"] == "density" and row["review_status"] == "approved"
        }

    assert approved_density_identities == {
        (111, "纯净水"),
        (72, "醋"),
        (122, "牛奶"),
        (154, "柠檬汁"),
        (155, "色拉油"),
        (20, "橄榄油"),
        (996, "烧烤汁"),
    }
    for ingredient_id, name in sorted(approved_density_identities):
        bundle = generate_nutrition_occurrence_review(
            _context(
                name=name,
                category=categories[name],
                ingredient_id=ingredient_id,
            )
        )
        assert bundle.retention_candidates == (), name
        assert bundle.retention_exceptions[0].exception_codes == (
            "NUTRITION_RETENTION_AMBIGUOUS",
        )


def test_unknown_physical_state_is_not_asserted_as_bound_solid() -> None:
    bundle = generate_nutrition_occurrence_review(
        _context(name="未分类食材", category="其他")
    )

    assert bundle.retention_candidates == ()
    assert bundle.retention_exceptions[0].exception_codes == (
        "NUTRITION_RETENTION_AMBIGUOUS",
    )


@pytest.mark.parametrize(
    "name",
    (
        "盐", "海盐", "粗盐", "精盐", "椒盐", "白糖", "细砂糖", "绵白糖",
        "糖", "冰糖", "黄冰糖", "红糖", "麦芽糖", "糖霜",
        "姜", "老姜", "嫩姜", "良姜", "沙姜", "葱", "小葱", "香葱", "大葱",
        "蒜", "蒜瓣", "蒜苗", "八角", "花椒", "香叶", "桂皮", "黑胡椒",
        "白胡椒", "孜然", "酵母", "枸杞", "豆豉", "紫薯", "莲子", "银耳",
        "蔓越莓干", "饺子皮", "小苏打", "白扁豆", "赤小豆", "黑豆", "红豆",
        "红小豆", "红腰豆", "黄豆", "绿豆", "脱皮绿豆", "芸豆",
    ),
)
def test_audited_exact_solid_identity_is_a_bound_solid_candidate(name: str) -> None:
    bundle = generate_nutrition_occurrence_review(
        _context(name=name, category="其他")
    )

    assert [row.evidence_codes for row in bundle.retention_candidates] == [
        ("NUTRITION_RETENTION_BOUND_SOLID",)
    ]
    assert bundle.retention_exceptions == ()


@pytest.mark.parametrize(
    "name",
    ("生抽", "有机生抽", "老抽", "李锦记老抽", "金兰油膏", "香草精", "糟卤"),
)
def test_audited_non_suffix_fluid_identity_remains_retention_ambiguous(
    name: str,
) -> None:
    bundle = generate_nutrition_occurrence_review(
        _context(name=name, category="其他")
    )

    assert bundle.retention_candidates == ()
    assert bundle.retention_exceptions[0].exception_codes == (
        "NUTRITION_RETENTION_AMBIGUOUS",
    )


def _bundle_fixture():
    fat = generate_nutrition_occurrence_review(_context(name="食用油"))
    solid = generate_nutrition_occurrence_review(_context(name="鸡肉"))
    return replace(
        fat,
        usage_exceptions=(replace(solid.usage_exceptions[0], occurrence_id="1-2"),),
        retention_candidates=(replace(solid.retention_candidates[0], occurrence_id="1-2"),),
    )


def test_writer_outputs_four_pending_only_atomic_csvs(tmp_path: Path) -> None:
    counts = write_nutrition_occurrence_review(_bundle_fixture(), tmp_path)

    assert counts == {
        "usage_candidates": 1,
        "usage_exceptions": 1,
        "retention_candidates": 1,
        "retention_exceptions": 1,
    }
    assert {path.name for path in tmp_path.iterdir()} == {
        "nutrition_usage_candidates.csv",
        "nutrition_usage_exceptions.csv",
        "nutrition_retention_candidates.csv",
        "nutrition_retention_exceptions.csv",
    }
    expected_headers = {
        "nutrition_usage_candidates.csv": (
            "occurrence_id", "recipe_id", "recipe_name", "ingredient_id",
            "ingredient_name", "source_fragment", "normalized_form", "category",
            "quantity_raw", "bound_step_indexes", "bound_step_text",
            "candidate_usage_code", "evidence_codes", "review_status",
        ),
        "nutrition_usage_exceptions.csv": (
            "occurrence_id", "recipe_id", "recipe_name", "ingredient_id",
            "ingredient_name", "source_fragment", "normalized_form", "category",
            "quantity_raw", "bound_step_indexes", "bound_step_text",
            "exception_codes", "review_status",
        ),
        "nutrition_retention_candidates.csv": (
            "occurrence_id", "recipe_id", "recipe_name", "ingredient_id",
            "ingredient_name", "source_fragment", "normalized_form", "category",
            "quantity_raw", "bound_step_indexes", "bound_step_text",
            "candidate_retained_in_dish", "evidence_codes", "review_status",
        ),
        "nutrition_retention_exceptions.csv": (
            "occurrence_id", "recipe_id", "recipe_name", "ingredient_id",
            "ingredient_name", "source_fragment", "normalized_form", "category",
            "quantity_raw", "bound_step_indexes", "bound_step_text",
            "exception_codes", "review_status",
        ),
    }
    for filename, expected_header in expected_headers.items():
        with (tmp_path / filename).open(encoding="utf-8", newline="") as handle:
            assert tuple(csv.DictReader(handle).fieldnames or ()) == expected_header


def test_writer_sorts_directly_supplied_rows_by_recipe_and_occurrence(tmp_path: Path) -> None:
    bundle = _bundle_fixture()
    later = replace(bundle.usage_candidates[0], occurrence_id="2-1", recipe_id=2)
    earlier = replace(bundle.usage_candidates[0], occurrence_id="1-3", recipe_id=1)

    write_nutrition_occurrence_review(
        replace(bundle, usage_candidates=(later, earlier)), tmp_path
    )

    with (tmp_path / "nutrition_usage_candidates.csv").open(encoding="utf-8", newline="") as handle:
        assert [row["occurrence_id"] for row in csv.DictReader(handle)] == ["1-3", "2-1"]


def test_writer_escapes_formulas_in_every_text_field(tmp_path: Path) -> None:
    bundle = generate_nutrition_occurrence_review(
        _context(name="=食用油", step="@加入油", category="+油类")
    )

    write_nutrition_occurrence_review(bundle, tmp_path)

    text = (tmp_path / "nutrition_usage_candidates.csv").read_text(encoding="utf-8")
    assert "'=食用油" in text
    assert "'+油类" in text
    assert "'@加入油" in text


@pytest.mark.parametrize("alias_kind", ("relative", "case_variant"))
def test_writer_refuses_relative_and_case_variant_formal_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias_kind: str
) -> None:
    formal = (
        tmp_path
        / "data"
        / "review"
        / "ingredient_nutrition_retention_decisions.csv"
    )
    formal.parent.mkdir(parents=True)
    formal.write_text("preserve formal", encoding="utf-8")
    monkeypatch.setattr(nutrition_occurrence_review, "PROJECT_ROOT", tmp_path)
    if alias_kind == "relative":
        monkeypatch.chdir(tmp_path)
        alias = Path("data/review/ingredient_nutrition_retention_decisions.csv")
    else:
        alias = (
            tmp_path
            / "DATA"
            / "REVIEW"
            / "INGREDIENT_NUTRITION_RETENTION_DECISIONS.CSV"
        )

    with pytest.raises(ValueError, match="候选不得写入正式规则或决定文件"):
        write_nutrition_occurrence_review(_bundle_fixture(), alias)

    assert formal.read_text(encoding="utf-8") == "preserve formal"


def test_writer_refuses_hard_link_to_formal_target_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    formal = (
        tmp_path
        / "data"
        / "review"
        / "ingredient_nutrition_retention_decisions.csv"
    )
    formal.parent.mkdir(parents=True)
    formal.write_text("preserve formal", encoding="utf-8")
    alias = tmp_path / "retention-review-hard-link.csv"
    os.link(formal, alias)
    monkeypatch.setattr(nutrition_occurrence_review, "PROJECT_ROOT", tmp_path)

    with pytest.raises(ValueError, match="候选不得写入正式规则或决定文件"):
        write_nutrition_occurrence_review(_bundle_fixture(), alias)

    assert formal.read_text(encoding="utf-8") == "preserve formal"
    assert alias.read_text(encoding="utf-8") == "preserve formal"


def test_writer_refuses_symlink_to_formal_target_when_windows_privilege_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    formal = (
        tmp_path
        / "data"
        / "review"
        / "ingredient_nutrition_retention_decisions.csv"
    )
    formal.parent.mkdir(parents=True)
    formal.write_text("preserve formal", encoding="utf-8")
    alias = tmp_path / "retention-review-symlink.csv"
    _create_symlink_or_skip_windows_privilege(alias, formal, "direct writer")
    monkeypatch.setattr(nutrition_occurrence_review, "PROJECT_ROOT", tmp_path)

    with pytest.raises(ValueError, match="候选不得写入正式规则或决定文件"):
        write_nutrition_occurrence_review(_bundle_fixture(), alias)

    assert formal.read_text(encoding="utf-8") == "preserve formal"


def test_writer_symlink_coverage_reraises_other_permission_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = PermissionError("unrelated symlink permission failure")

    def fail_symlink(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(Path, "symlink_to", fail_symlink)

    with pytest.raises(PermissionError, match="unrelated symlink permission failure"):
        _create_symlink_or_skip_windows_privilege(
            tmp_path / "alias.csv", tmp_path / "formal.csv", "direct writer"
        )


def test_writer_rejects_invalid_status_duplicate_and_cross_queue_before_replace(
    tmp_path: Path,
) -> None:
    bundle = _bundle_fixture()
    targets = tuple(tmp_path / name for name in (
        "nutrition_usage_candidates.csv", "nutrition_usage_exceptions.csv",
        "nutrition_retention_candidates.csv", "nutrition_retention_exceptions.csv",
    ))
    for target in targets:
        target.write_text(f"old:{target.name}", encoding="utf-8")

    invalid = replace(bundle.usage_candidates[0], review_status="approved")
    duplicate = replace(bundle, usage_candidates=(bundle.usage_candidates[0], bundle.usage_candidates[0]))
    cross_queue = replace(
        bundle,
        usage_exceptions=(
            replace(
                bundle.usage_exceptions[0],
                occurrence_id=bundle.usage_candidates[0].occurrence_id,
            ),
        ),
    )

    for broken in (
        replace(bundle, usage_candidates=(invalid,)),
        duplicate,
        cross_queue,
    ):
        with pytest.raises(ValueError):
            write_nutrition_occurrence_review(broken, tmp_path)
        assert [target.read_text(encoding="utf-8") for target in targets] == [
            f"old:{target.name}" for target in targets
        ]


def test_writer_rolls_back_every_queue_when_a_replace_fails(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle_fixture()
    targets = tuple(tmp_path / name for name in (
        "nutrition_usage_candidates.csv", "nutrition_usage_exceptions.csv",
        "nutrition_retention_candidates.csv", "nutrition_retention_exceptions.csv",
    ))
    for target in targets:
        target.write_text(f"old:{target.name}", encoding="utf-8")
    original_replace = Path.replace
    replacement_calls = 0

    def fail_third_queue_replace(path: Path, target: Path) -> Path:
        nonlocal replacement_calls
        if path.suffix == ".tmp":
            replacement_calls += 1
            if replacement_calls == 3:
                raise RuntimeError("injected replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_third_queue_replace)

    with pytest.raises(RuntimeError, match="injected replace failure"):
        write_nutrition_occurrence_review(bundle, tmp_path)

    assert [target.read_text(encoding="utf-8") for target in targets] == [
        f"old:{target.name}" for target in targets
    ]


def test_writer_removes_untracked_temp_file_when_csv_write_fails(tmp_path: Path, monkeypatch) -> None:
    original_writerow = csv.DictWriter.writerow
    calls = 0

    def fail_first_data_row(writer, row):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected CSV failure")
        return original_writerow(writer, row)

    monkeypatch.setattr(csv.DictWriter, "writerow", fail_first_data_row)

    with pytest.raises(RuntimeError, match="injected CSV failure"):
        write_nutrition_occurrence_review(_bundle_fixture(), tmp_path)

    assert not list(tmp_path.glob("*.tmp"))


def test_writer_removes_untracked_backup_when_copy_fails(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "nutrition_usage_candidates.csv"
    target.write_text("old", encoding="utf-8")

    def fail_copy(*_args, **_kwargs):
        raise RuntimeError("injected copy failure")

    monkeypatch.setattr("food_agent_v2.b1.nutrition_occurrence_review.shutil.copyfile", fail_copy)

    with pytest.raises(RuntimeError, match="injected copy failure"):
        write_nutrition_occurrence_review(_bundle_fixture(), tmp_path)

    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.bak"))
