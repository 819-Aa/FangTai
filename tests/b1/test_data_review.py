import csv
import os
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

import food_agent_v2.b1.data_review as data_review
from food_agent_v2.b1.consumer_views import RecipeFact
from food_agent_v2.b1.nutrition_occurrence_review import (
    NutritionOccurrenceReviewBundle,
    NutritionRetentionCandidate,
    NutritionRetentionException,
    NutritionUsageCandidate,
    NutritionUsageException,
)
from food_agent_v2.b1.schemas import SourceRecipeRow


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


@pytest.mark.parametrize("kind", ("quantity-rules", "edible-fractions", "nutrition-occurrences"))
def test_offline_review_kinds_are_registered(monkeypatch, tmp_path: Path, kind: str) -> None:
    monkeypatch.setattr(data_review, "load_verified_recipe_source", lambda *_args: ())
    monkeypatch.setattr(data_review, "canonical_source_manifest", lambda: ())
    monkeypatch.setattr(data_review, "classify_all", lambda *_args: ())
    monkeypatch.setattr(data_review, "load_overrides", lambda *_args: {})
    monkeypatch.setattr(data_review, "load_recipe_profile_enrichments", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(data_review, "recipe_facts_from_source", lambda *_args: ())
    monkeypatch.setattr(
        data_review,
        {
            "quantity-rules": "_write_quantity_rule_review",
            "edible-fractions": "_write_edible_fraction_review",
            "nutrition-occurrences": "_write_nutrition_occurrence_review",
        }[kind],
        lambda *_args, **_kwargs: 0,
        raising=False,
    )

    assert data_review.main(
        ["--kind", kind, "--output", str(tmp_path / "candidates.csv")]
    ) == 0


def _install_nutrition_occurrence_offline_sentinels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_offline_dependency(*_args, **_kwargs):
        raise AssertionError(
            "offline nutrition-occurrences route called an LLM or loaded configuration"
        )

    for adapter_name in (
        "LLMFoodOriginEstimator",
        "LLMProfileEstimator",
        "LLMQuantityEstimator",
        "LLMUsdaCandidateSelector",
        "LLMUsdaSearchTermEstimator",
    ):
        monkeypatch.setattr(data_review, adapter_name, fail_offline_dependency)
    monkeypatch.setattr(
        "food_agent_v2.c3.llm_client.get_llm_client", fail_offline_dependency
    )
    monkeypatch.setattr(
        "food_agent_v2.core.config.load_config", fail_offline_dependency
    )
    monkeypatch.setattr(
        data_review, "get_llm_client", fail_offline_dependency, raising=False
    )
    monkeypatch.setattr(
        data_review, "load_config", fail_offline_dependency, raising=False
    )


def test_nutrition_occurrences_public_route_is_fully_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_nutrition_occurrence_offline_sentinels(monkeypatch)
    review_dir = tmp_path / "review"
    _write_nutrition_review_fixture(review_dir)
    rows = (
        _source_row(1, "样本菜一", "姜丝5克"),
        _source_row(2, "全局菜二", "姜10克"),
        _source_row(3, "全局菜三", "蒜10克；蒜末5克"),
    )
    monkeypatch.setattr(data_review, "_REVIEW_DIR", review_dir)
    monkeypatch.setattr(
        data_review, "load_verified_recipe_source", lambda *_args: rows
    )
    monkeypatch.setattr(
        data_review, "canonical_source_manifest", lambda: {"fixture": "offline"}
    )
    monkeypatch.setattr(data_review, "load_overrides", lambda *_args: {})
    monkeypatch.setattr(
        data_review,
        "load_recipe_profile_enrichments",
        lambda *_args, **_kwargs: {},
    )
    output = tmp_path / "nutrition-occurrences"

    assert data_review.main(
        ["--kind", "nutrition-occurrences", "--output", str(output)]
    ) == 0

    assert {path.name for path in output.iterdir()} == {
        "nutrition_usage_candidates.csv",
        "nutrition_usage_exceptions.csv",
        "nutrition_retention_candidates.csv",
        "nutrition_retention_exceptions.csv",
    }
    queue_rows = _read_queue_rows(output)
    assert queue_rows
    assert {row["review_status"] for row in queue_rows} == {"pending"}


def _prepare_nutrition_occurrence_cli(
    monkeypatch: pytest.MonkeyPatch, project_root: Path
) -> None:
    monkeypatch.setattr(data_review, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(data_review, "load_verified_recipe_source", lambda *_args: ())
    monkeypatch.setattr(data_review, "canonical_source_manifest", lambda: ())
    monkeypatch.setattr(data_review, "classify_all", lambda *_args: ())
    monkeypatch.setattr(data_review, "load_overrides", lambda *_args: {})
    monkeypatch.setattr(
        data_review,
        "load_recipe_profile_enrichments",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(data_review, "recipe_facts_from_source", lambda *_args: ())
    monkeypatch.setattr(
        data_review,
        "_build_review_context",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("formal target reached nutrition occurrence context build")
        ),
    )


@pytest.mark.parametrize("alias_kind", ("relative", "case_variant"))
def test_nutrition_occurrence_cli_refuses_relative_and_case_formal_aliases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    alias_kind: str,
) -> None:
    formal = (
        tmp_path
        / "data"
        / "review"
        / "ingredient_nutrition_retention_decisions.csv"
    )
    formal.parent.mkdir(parents=True)
    formal.write_text("preserve formal", encoding="utf-8")
    _prepare_nutrition_occurrence_cli(monkeypatch, tmp_path)
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
        data_review.main(
            ["--kind", "nutrition-occurrences", "--output", str(alias)]
        )

    assert formal.read_text(encoding="utf-8") == "preserve formal"


def test_nutrition_occurrence_cli_refuses_hard_link_to_formal_target_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    formal = (
        tmp_path
        / "data"
        / "review"
        / "ingredient_nutrition_retention_decisions.csv"
    )
    formal.parent.mkdir(parents=True)
    formal.write_text("preserve formal", encoding="utf-8")
    alias = tmp_path / "retention-cli-hard-link.csv"
    os.link(formal, alias)
    _prepare_nutrition_occurrence_cli(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="候选不得写入正式规则或决定文件"):
        data_review.main(
            ["--kind", "nutrition-occurrences", "--output", str(alias)]
        )

    assert formal.read_text(encoding="utf-8") == "preserve formal"
    assert alias.read_text(encoding="utf-8") == "preserve formal"


def test_nutrition_occurrence_cli_refuses_symlink_to_formal_target_when_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    formal = (
        tmp_path
        / "data"
        / "review"
        / "ingredient_nutrition_retention_decisions.csv"
    )
    formal.parent.mkdir(parents=True)
    formal.write_text("preserve formal", encoding="utf-8")
    alias = tmp_path / "retention-cli-symlink.csv"
    _create_symlink_or_skip_windows_privilege(alias, formal, "CLI")
    _prepare_nutrition_occurrence_cli(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="候选不得写入正式规则或决定文件"):
        data_review.main(
            ["--kind", "nutrition-occurrences", "--output", str(alias)]
        )

    assert formal.read_text(encoding="utf-8") == "preserve formal"


def test_cli_symlink_coverage_reraises_other_permission_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    error = PermissionError("unrelated symlink permission failure")

    def fail_symlink(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(Path, "symlink_to", fail_symlink)

    with pytest.raises(PermissionError, match="unrelated symlink permission failure"):
        _create_symlink_or_skip_windows_privilege(
            tmp_path / "alias.csv", tmp_path / "formal.csv", "CLI"
        )


def _write_nutrition_review_fixture(review_dir: Path) -> None:
    review_dir.mkdir(parents=True)
    (review_dir / "ingredient_identity_overrides.csv").write_text(
        "source_key,operation,target_ingredient_ids,reason_code,form,review_status,reviewer,reviewed_at\n"
        "姜丝,merge,1,processing_variant,丝,approved,owner,2026-08-24\n"
        "蒜末,merge,3,processing_variant,末,approved,owner,2026-08-24\n",
        encoding="utf-8",
    )
    (review_dir / "ingredient_condition_defaults.csv").write_text(
        "recipe_name,choice_group,selected_ingredient,retained_alternatives,review_status\n"
        "全局菜二,备选,姜,姜|姜丝,approved\n",
        encoding="utf-8",
    )
    (review_dir / "ingredient_nutrition_usage_decisions.csv").write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,usage_code,review_status\n"
        "2-1,2,1,姜,,supporting,approved\n",
        encoding="utf-8",
    )
    (review_dir / "ingredient_nutrition_retention_decisions.csv").write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,retained_in_dish,review_status\n"
        "2-1,2,1,姜,,true,approved\n",
        encoding="utf-8",
    )


def _source_row(recipe_id: int, name: str, ingredients: str) -> SourceRecipeRow:
    return SourceRecipeRow(
        recipe_id=recipe_id,
        source_row_number=recipe_id,
        name=name,
        ingredients_raw=ingredients,
        steps_raw=f"加入{ingredients}炒熟",
        labels_raw="",
        row_sha256=str(recipe_id) * 64,
    )


def _read_queue_rows(output: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for filename in (
        "nutrition_usage_candidates.csv",
        "nutrition_usage_exceptions.csv",
        "nutrition_retention_candidates.csv",
        "nutrition_retention_exceptions.csv",
    ):
        with (output / filename).open(encoding="utf-8", newline="") as handle:
            rows.extend(csv.DictReader(handle))
    return rows


def test_nutrition_occurrence_sample_builds_full_context_before_selecting_queue_recipes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    review_dir = tmp_path / "review"
    _write_nutrition_review_fixture(review_dir)
    monkeypatch.setattr(data_review, "_REVIEW_DIR", review_dir)
    rows = (
        _source_row(1, "样本菜一", "姜丝5克"),
        _source_row(2, "全局菜二", "姜10克"),
        _source_row(3, "全局菜三", "蒜10克；蒜末5克"),
    )
    facts = tuple(
        RecipeFact(row.recipe_id, row.name, "dish", (row.steps_raw,))
        for row in rows
    )
    output = tmp_path / "queues"

    data_review._write_nutrition_occurrence_review(
        rows, facts, output, sample=1
    )

    queue_rows = _read_queue_rows(output)
    assert {(row["recipe_id"], row["ingredient_id"]) for row in queue_rows} == {
        ("1", "1")
    }


def _review_row_kwargs(recipe_id: int, occurrence_index: int) -> dict:
    return {
        "occurrence_id": f"{recipe_id}-{occurrence_index}",
        "recipe_id": recipe_id,
        "recipe_name": f"菜{recipe_id}",
        "ingredient_id": recipe_id,
        "ingredient_name": f"食材{recipe_id}",
        "source_fragment": f"食材{recipe_id}10克",
        "normalized_form": "",
        "category": "其他",
        "quantity_raw": "10克",
        "bound_step_indexes": (1,),
        "bound_step_text": (f"加入食材{recipe_id}",),
        "evidence_codes": ("EVIDENCE",),
    }


def test_nutrition_occurrence_sample_filters_same_first_reviewable_recipes_in_all_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = NutritionOccurrenceReviewBundle(
        usage_candidates=tuple(
            NutritionUsageCandidate(
                **_review_row_kwargs(recipe_id, 1),
                candidate_usage_code="main",
            )
            for recipe_id in (3, 1, 2)
        ),
        usage_exceptions=tuple(
            NutritionUsageException(
                **_review_row_kwargs(recipe_id, 2),
                exception_codes=("USAGE_AMBIGUOUS",),
            )
            for recipe_id in (2, 3, 1)
        ),
        retention_candidates=tuple(
            NutritionRetentionCandidate(**_review_row_kwargs(recipe_id, 3))
            for recipe_id in (1, 3, 2)
        ),
        retention_exceptions=tuple(
            NutritionRetentionException(
                **_review_row_kwargs(recipe_id, 4),
                exception_codes=("RETENTION_AMBIGUOUS",),
            )
            for recipe_id in (3, 2, 1)
        ),
    )
    rows = tuple(
        _source_row(recipe_id, f"菜{recipe_id}", f"食材{recipe_id}10克")
        for recipe_id in (1, 2, 3)
    )
    facts = tuple(
        RecipeFact(recipe_id, f"菜{recipe_id}", "dish")
        for recipe_id in (1, 2, 3)
    )
    monkeypatch.setattr(data_review, "_build_review_context", lambda *args: args)
    monkeypatch.setattr(
        data_review, "generate_nutrition_occurrence_review", lambda _context: bundle
    )
    output = tmp_path / "queues"

    data_review._write_nutrition_occurrence_review(
        rows, facts, output, sample=2
    )

    expected_rows = {
        "nutrition_usage_candidates.csv": [("1", "1-1"), ("2", "2-1")],
        "nutrition_usage_exceptions.csv": [("1", "1-2"), ("2", "2-2")],
        "nutrition_retention_candidates.csv": [("1", "1-3"), ("2", "2-3")],
        "nutrition_retention_exceptions.csv": [("1", "1-4"), ("2", "2-4")],
    }
    for filename, expected in expected_rows.items():
        with (output / filename).open(encoding="utf-8", newline="") as handle:
            assert [
                (row["recipe_id"], row["occurrence_id"])
                for row in csv.DictReader(handle)
            ] == expected


@pytest.mark.parametrize(
    "path_builder",
    (
        lambda root: root / "data" / "review" / "ingredient_nutrition_retention_decisions.csv",
        lambda root: root / "DATA" / "REVIEW" / "INGREDIENT_NUTRITION_RETENTION_DECISIONS.CSV",
        lambda root: root / "data" / "review",
    ),
)
def test_nutrition_occurrences_refuses_retention_formal_path_before_context(
    monkeypatch, tmp_path: Path, path_builder
) -> None:
    monkeypatch.setattr(data_review, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        data_review, "_build_review_context", lambda *_args: (_ for _ in ()).throw(
            AssertionError("formal target reached context build")
        ), raising=False,
    )
    target = path_builder(tmp_path)
    if target.suffix:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("preserve", encoding="utf-8")

    with pytest.raises(ValueError, match="候选不得写入正式规则或决定文件"):
        data_review._write_nutrition_occurrence_review((), (), target, sample=None)


def test_quantity_rules_route_is_offline_and_uses_pure_writer(monkeypatch, tmp_path: Path) -> None:
    class _LLMSentinel:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("offline quantity-rules route called an LLM")

    monkeypatch.setattr(data_review, "LLMQuantityEstimator", _LLMSentinel)
    monkeypatch.setattr(data_review, "_build_review_views", lambda *_args: object())
    monkeypatch.setattr(data_review, "load_quantity_review_candidates", lambda *_args: ())

    assert data_review._write_quantity_rule_review(
        (), (), tmp_path / "rules.csv", input_path=tmp_path / "input.csv", sample=None
    ) == 0


def test_edible_fractions_route_is_offline_and_uses_real_writer(monkeypatch, tmp_path: Path) -> None:
    class _Views:
        nutrition_views = ()

    monkeypatch.setattr(data_review, "_build_review_views", lambda *_args: _Views())
    monkeypatch.setattr(data_review, "_REVIEW_DIR", tmp_path)

    assert data_review._write_edible_fraction_review(
        (), (), tmp_path / "fractions.csv", sample=None
    ) == 0
    assert "review_status" in (tmp_path / "fractions.csv").read_text(encoding="utf-8")


def test_repository_retention_decisions_reach_inputs_and_only_unresolved_are_queued() -> None:
    rows = tuple(
        data_review.load_verified_recipe_source(
            data_review.RECIPES_RAW, data_review.canonical_source_manifest()
        )
    )
    classifications = tuple(
        data_review.classify_all(
            list(rows),
            data_review.load_overrides(
                data_review._REVIEW_DIR / "recipe_classification_overrides.csv"
            ),
        )
    )
    profile_enrichments = data_review.load_recipe_profile_enrichments(
        data_review._REVIEW_DIR / "recipe_profile_enrichment.jsonl",
        known_recipe_ids={row.recipe_id for row in rows},
    )
    facts = data_review.recipe_facts_from_source(
        rows, classifications, profile_enrichments
    )
    context = data_review._build_review_context(rows, facts)
    bundle = data_review.generate_nutrition_occurrence_review(context)

    retention_path = (
        data_review._REVIEW_DIR / "ingredient_nutrition_retention_decisions.csv"
    )
    with retention_path.open(encoding="utf-8", newline="") as handle:
        formal_rows = list(csv.DictReader(handle))

    current_occurrences = data_review.nutrition_occurrence_metadata_from_views(
        context.views.nutrition_views
    )
    retention_index = data_review.load_nutrition_retention_decisions(
        retention_path, current_occurrences=current_occurrences
    )
    final_inputs = {
        ingredient.occurrence_id: ingredient
        for view in context.views.nutrition_views
        for ingredient in view.ingredients
    }
    formal_retention_by_id = {
        row["occurrence_id"]: row["retained_in_dish"] == "true"
        for row in formal_rows
    }
    retention_status_by_id = {
        row["occurrence_id"]: row["review_status"] for row in formal_rows
    }
    edible_retention_corrections = {
        "23-2",
        "538-2",
        "870-3",
        "982-4",
        "1175-4",
        "1224-2",
        "1770-8",
        "1886-11",
    }
    false_occurrence_ids = {
        occurrence_id
        for occurrence_id, retained in formal_retention_by_id.items()
        if not retained
    }
    assert len(formal_rows) == len(formal_retention_by_id) == 16_820
    assert sum(formal_retention_by_id.values()) == 16_636
    assert len(false_occurrence_ids) == 184
    assert {
        "285-5",
        "414-8",
        "414-9",
        "414-10",
        "637-2",
        "693-11",
        "935-5",
        "1947-8",
        "1963-2",
    } <= false_occurrence_ids
    assert edible_retention_corrections <= false_occurrence_ids
    assert {
        occurrence_id: retention_status_by_id[occurrence_id]
        for occurrence_id in edible_retention_corrections
    } == dict.fromkeys(edible_retention_corrections, "modified")
    assert formal_retention_by_id["1426-27"] is True
    assert formal_retention_by_id["1611-5"] is True
    assert retention_status_by_id["1426-27"] == "approved"
    assert retention_status_by_id["1611-5"] == "approved"
    assert Counter(retention_status_by_id.values()) == {
        "approved": 16_812,
        "modified": 8,
    }
    assert all(
        retention_index.get_effective(occurrence_id).retained_in_dish is retained
        for occurrence_id, retained in formal_retention_by_id.items()
    )
    assert all(
        final_inputs[occurrence_id].retained_in_dish is retained
        and final_inputs[occurrence_id].retention_requires_review is False
        for occurrence_id, retained in formal_retention_by_id.items()
    )
    assert len(bundle.retention_candidates) == 0
    assert len(bundle.retention_exceptions) == 0
    assert len(bundle.usage_candidates) == 1_733
    assert len(bundle.usage_exceptions) == 8_684

    edible_path = data_review._REVIEW_DIR / "ingredient_edible_fraction_decisions.csv"
    with edible_path.open(encoding="utf-8", newline="") as handle:
        edible_rows = list(csv.DictReader(handle))
    edible_by_id = {row["occurrence_id"]: row for row in edible_rows}
    fractions = [Decimal(row["edible_fraction"]) for row in edible_rows]
    edible_decisions = data_review.load_edible_fraction_decisions(
        edible_path, current_occurrence_ids=set(current_occurrences)
    )
    edible_rules = data_review.load_edible_fraction_rules(
        data_review._REVIEW_DIR / "ingredient_edible_fraction_rules.csv"
    )

    assert len(edible_rows) == len(edible_by_id) == 16_636
    assert sum(fraction == Decimal("1") for fraction in fractions) == 16_323
    assert sum(fraction < Decimal("1") for fraction in fractions) == 313
    assert all(Decimal("0") < fraction <= Decimal("1") for fraction in fractions)
    assert edible_by_id["15-1"]["edible_fraction"] == "0.72"
    assert edible_by_id["1611-5"]["edible_fraction"] == "0.67"
    assert all("待判" not in row["decision_form"] for row in edible_rows)
    assert data_review.generate_edible_fraction_candidates(
        context.views.nutrition_views, edible_rules, edible_decisions
    ) == ()


@pytest.mark.parametrize(
    "filename",
    (
        "ingredient_measure_rules.csv",
        "ingredient_quantity_decisions.csv",
        "ingredient_nutrition_usage_decisions.csv",
        "ingredient_nutrition_retention_decisions.csv",
        "ingredient_edible_fraction_rules.csv",
        "ingredient_edible_fraction_decisions.csv",
    ),
)
@pytest.mark.parametrize("kind", ("quantity-rules", "edible-fractions"))
def test_offline_routes_refuse_formal_targets_before_build(
    monkeypatch, tmp_path: Path, filename: str, kind: str
) -> None:
    monkeypatch.setattr(data_review, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        data_review, "_build_review_views", lambda *_args: (_ for _ in ()).throw(
            AssertionError("formal target reached build")
        )
    )
    formal = tmp_path / "data" / "review" / filename
    formal.parent.mkdir(parents=True, exist_ok=True)
    formal.write_text("preserve", encoding="utf-8")

    writer = data_review._write_quantity_rule_review if kind == "quantity-rules" else data_review._write_edible_fraction_review
    with pytest.raises(ValueError, match="正式"):
        writer((), (), formal, **({"input_path": formal, "sample": None} if kind == "quantity-rules" else {"sample": None}))
    assert formal.read_text(encoding="utf-8") == "preserve"
