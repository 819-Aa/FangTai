import csv

import numpy as np

from food_agent_v2.b1.nutrition_reference import (
    IngredientNutritionReference,
    NutritionVector,
)
from food_agent_v2.b1.usda_crosswalk_review import (
    UsdaCandidateSelectionCache,
    UsdaCrosswalkReviewInput,
    UsdaSearchTermCandidateCache,
    generate_usda_crosswalk_candidates,
    generate_usda_search_terms,
    select_usda_crosswalk_candidates,
    write_usda_crosswalk_candidates,
)


class _Embedder:
    def encode(self, texts, normalize_embeddings=True):
        vectors = []
        for text in texts:
            if "鸡腿" in text or "chicken leg" in text.casefold():
                vectors.append([1.0, 0.0, 0.0])
            elif "chicken soup" in text.casefold():
                vectors.append([0.8, 0.2, 0.0])
            else:
                vectors.append([0.0, 1.0, 0.0])
        return np.asarray(vectors, dtype=np.float32)


class _Translator:
    model_id = "translation-model"

    def __init__(self):
        self.calls = []

    def estimate_batch(self, inputs):
        self.calls.append(tuple(item.ingredient_id for item in inputs))
        return {
            (item.ingredient_id, item.form): (
                "chicken leg" if item.ingredient_name == "鸡腿" else "table salt"
            )
            for item in inputs
        }


class _Selector:
    model_id = "selection-model"

    def __init__(self, selections):
        self.selections = selections
        self.calls = []

    def select_batch(self, items):
        self.calls.append(tuple(item["ingredient_id"] for item in items))
        return {
            (item["ingredient_id"], item["form"]): self.selections[
                (item["ingredient_id"], item["form"])
            ]
            for item in items
        }


def _reference(reference_id: str, name: str, form: str):
    return IngredientNutritionReference(
        reference_id=reference_id,
        canonical_name=name,
        form=form,
        per_100g=NutritionVector(),
        source_name="USDA FoodData Central",
        source_url=f"https://example.invalid/{reference_id}",
        source_dataset="usda_sr_legacy",
        food_origin="unknown",
    )


def test_usda_multilingual_candidates_are_ranked_form_compatible_and_pending(
    tmp_path,
) -> None:
    inputs = (UsdaCrosswalkReviewInput(10, "鸡腿", "raw"),)
    references = (
        _reference("usda-fdc-1", "Chicken, leg, raw", "raw"),
        _reference("usda-fdc-2", "Chicken soup, cooked", "cooked"),
        _reference("usda-fdc-3", "Rice, raw", "raw"),
    )

    candidates = generate_usda_crosswalk_candidates(
        inputs,
        references,
        _Embedder(),
        embedding_model_id="BAAI/bge-m3",
        search_terms={(10, "raw"): "chicken leg"},
        top_k=2,
    )
    output = tmp_path / "usda_candidates.csv"
    write_usda_crosswalk_candidates(candidates, output)
    rows = list(csv.DictReader(output.open(encoding="utf-8")))

    assert rows[0]["candidate_reference_id"] == "usda-fdc-1"
    assert {row["candidate_form"] for row in rows} == {"raw"}
    assert {row["review_status"] for row in rows} == {"pending"}
    assert {row["match_method"] for row in rows} == {"multilingual_embedding"}
    assert "similarity" not in rows[0]


def test_usda_search_terms_are_closed_to_identity_text_and_cacheable(tmp_path) -> None:
    inputs = (
        UsdaCrosswalkReviewInput(10, "鸡腿", "raw"),
        UsdaCrosswalkReviewInput(20, "盐", "unspecified"),
    )
    cache_path = tmp_path / "translations.jsonl"
    translator = _Translator()

    terms = generate_usda_search_terms(
        inputs,
        translator,
        cache=UsdaSearchTermCandidateCache(cache_path),
        max_workers=2,
        batch_size=1,
    )

    assert terms == {
        (10, "raw"): "chicken leg",
        (20, "unspecified"): "table salt",
    }
    cached_translator = _Translator()
    assert generate_usda_search_terms(
        inputs,
        cached_translator,
        cache=UsdaSearchTermCandidateCache(cache_path),
    ) == terms
    assert cached_translator.calls == []


def test_usda_model_selection_can_choose_only_a_retrieved_id_or_no_match(
    tmp_path,
) -> None:
    inputs = (
        UsdaCrosswalkReviewInput(10, "鸡腿", "raw"),
        UsdaCrosswalkReviewInput(20, "罗汉果", "unspecified"),
    )
    ranked = (
        {
            "ingredient_id": 10,
            "ingredient_name": "鸡腿",
            "form": "raw",
            "candidate_reference_id": "usda-fdc-1",
            "candidate_name": "Chicken, leg, raw",
            "candidate_form": "raw",
            "source_dataset": "usda_sr_legacy",
            "match_method": "multilingual_embedding",
            "candidate_rank": 1,
            "reason": "usda_identity_and_form_requires_review",
            "review_status": "pending",
        },
        {
            "ingredient_id": 20,
            "ingredient_name": "罗汉果",
            "form": "unspecified",
            "candidate_reference_id": "usda-fdc-2",
            "candidate_name": "Fish, monkfish, raw",
            "candidate_form": "raw",
            "source_dataset": "usda_sr_legacy",
            "match_method": "multilingual_embedding",
            "candidate_rank": 1,
            "reason": "usda_identity_and_form_requires_review",
            "review_status": "pending",
        },
    )
    selector = _Selector({(10, "raw"): "usda-fdc-1", (20, "unspecified"): None})

    selected = select_usda_crosswalk_candidates(
        inputs,
        ranked,
        {(10, "raw"): "chicken leg", (20, "unspecified"): "monk fruit"},
        selector,
        cache=UsdaCandidateSelectionCache(tmp_path / "selections.jsonl"),
        max_workers=2,
        batch_size=1,
    )

    assert selected[0]["candidate_reference_id"] == "usda-fdc-1"
    assert selected[0]["review_status"] == "pending"
    assert selected[1]["candidate_reference_id"] == ""
    assert selected[1]["reason"] == "no_exact_usda_identity_match"


def test_usda_candidate_writer_never_marks_a_candidate_approved(tmp_path) -> None:
    output = tmp_path / "usda_candidates.csv"
    candidate = {
        "ingredient_id": 10,
        "ingredient_name": "鸡腿",
        "form": "raw",
        "candidate_reference_id": "usda-fdc-1",
        "candidate_name": "Chicken, leg, raw",
        "candidate_form": "raw",
        "source_dataset": "usda_sr_legacy",
        "match_method": "multilingual_embedding",
        "candidate_rank": 1,
        "reason": "usda_identity_and_form_requires_review",
        "review_status": "approved",
    }

    try:
        write_usda_crosswalk_candidates((candidate,), output)
    except ValueError as exc:
        assert "pending" in str(exc)
    else:
        raise AssertionError("approved USDA candidate must be rejected")
