from pathlib import Path

import pytest

import food_agent_v2.b1.data_review as data_review


@pytest.mark.parametrize("kind", ("quantity-rules", "edible-fractions"))
def test_offline_review_kinds_are_registered(monkeypatch, tmp_path: Path, kind: str) -> None:
    monkeypatch.setattr(data_review, "load_verified_recipe_source", lambda *_args: ())
    monkeypatch.setattr(data_review, "canonical_source_manifest", lambda: ())
    monkeypatch.setattr(data_review, "classify_all", lambda *_args: ())
    monkeypatch.setattr(data_review, "load_overrides", lambda *_args: {})
    monkeypatch.setattr(data_review, "load_recipe_profile_enrichments", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(data_review, "recipe_facts_from_source", lambda *_args: ())
    monkeypatch.setattr(
        data_review,
        "_write_quantity_rule_review" if kind == "quantity-rules" else "_write_edible_fraction_review",
        lambda *_args, **_kwargs: 0,
        raising=False,
    )

    assert data_review.main(
        ["--kind", kind, "--output", str(tmp_path / "candidates.csv")]
    ) == 0


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
