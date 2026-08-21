import csv

import pytest

from food_agent_v2.b1.food_origin_review import (
    FoodOriginCandidateCache,
    FoodOriginReviewInput,
    generate_food_origin_candidates,
    write_food_origin_candidates,
)


class _Estimator:
    model_id = "review-model"

    def __init__(self, outputs) -> None:
        self.outputs = outputs
        self.calls = []

    def estimate_batch(self, inputs):
        self.calls.append(tuple(item.reference_id for item in inputs))
        return {
            item.reference_id: self.outputs[item.reference_id]
            for item in inputs
        }


def test_food_origin_candidates_are_closed_pending_and_cacheable(tmp_path) -> None:
    inputs = (
        FoodOriginReviewInput("cn-1", "黄豆", "raw"),
        FoodOriginReviewInput("cn-2", "牛肉", "raw"),
        FoodOriginReviewInput("cn-3", "猪肉白菜水饺", "cooked"),
    )
    cache_path = tmp_path / "origin-cache.jsonl"
    estimator = _Estimator({"cn-1": "plant", "cn-2": "animal", "cn-3": "mixed"})

    candidates = generate_food_origin_candidates(
        inputs,
        estimator,
        cache=FoodOriginCandidateCache(cache_path),
        max_workers=2,
        batch_size=2,
    )
    output = tmp_path / "food_origin_candidates.csv"
    write_food_origin_candidates(candidates, output)
    rows = list(csv.DictReader(output.open(encoding="utf-8")))

    assert [row["candidate_food_origin"] for row in rows] == [
        "plant",
        "animal",
        "mixed",
    ]
    assert {row["review_status"] for row in rows} == {"pending"}
    assert set(rows[0]) == {
        "reference_id",
        "canonical_name",
        "form",
        "candidate_food_origin",
        "review_status",
    }
    cached_estimator = _Estimator(estimator.outputs)
    assert generate_food_origin_candidates(
        inputs,
        cached_estimator,
        cache=FoodOriginCandidateCache(cache_path),
    ) == candidates
    assert cached_estimator.calls == []


def test_food_origin_candidate_rejects_model_value_outside_closed_vocabulary() -> None:
    inputs = (FoodOriginReviewInput("cn-1", "黄豆", "raw"),)
    estimator = _Estimator({"cn-1": "mostly_plant"})

    with pytest.raises(ValueError, match="food_origin"):
        generate_food_origin_candidates(inputs, estimator)
