from pathlib import Path

import pytest

import food_agent_v2.b1.data_review as data_review


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


def test_nutrition_occurrences_route_is_offline_and_returns_four_counts(monkeypatch, tmp_path: Path) -> None:
    class _LLMSentinel:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("offline nutrition-occurrences route called an LLM")

    monkeypatch.setattr(data_review, "LLMQuantityEstimator", _LLMSentinel)
    monkeypatch.setattr(data_review, "_build_review_context", lambda *_args: object(), raising=False)
    monkeypatch.setattr(
        data_review,
        "generate_nutrition_occurrence_review",
        lambda _context: object(),
        raising=False,
    )
    monkeypatch.setattr(
        data_review,
        "write_nutrition_occurrence_review",
        lambda _bundle, _output: {
            "usage_candidates": 1, "usage_exceptions": 1,
            "retention_candidates": 1, "retention_exceptions": 1,
        },
        raising=False,
    )

    assert data_review._write_nutrition_occurrence_review((), (), tmp_path, sample=None) == {
        "usage_candidates": 1, "usage_exceptions": 1,
        "retention_candidates": 1, "retention_exceptions": 1,
    }


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

    assert data_review._write_edible_fraction_review(
        (), (), tmp_path / "fractions.csv", sample=None
    ) == 0
    assert "review_status" in (tmp_path / "fractions.csv").read_text(encoding="utf-8")


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
