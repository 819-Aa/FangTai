"""Model-assisted, review-only food-origin candidates."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

FOOD_ORIGIN_PROMPT_VERSION = "food-origin-review-v1"
FOOD_ORIGINS = frozenset({"plant", "animal", "mixed", "unknown"})

_SYSTEM_PROMPT = """你是食物来源分类审阅助手。只根据参考食物的名称与形态，把每项归入封闭枚举：
plant=纯植物来源；animal=纯动物来源；mixed=明确同时含植物与动物来源或复合配方；
unknown=名称不足以可靠判断。调味、烹饪方式不改变食物来源；不确定时必须选 unknown。
顶层 JSON 必须且只能是 {"origins":[对象,...]}。每个对象必须且只能包含
reference_id 和 food_origin，不得输出置信度、来源、解释、营养值或额外字段。"""


@dataclass(frozen=True)
class FoodOriginReviewInput:
    reference_id: str
    canonical_name: str
    form: str


class LLMFoodOriginEstimator:
    def __init__(self, llm_client, *, model_id: str) -> None:
        self._llm = llm_client
        self.model_id = model_id

    def estimate_batch(
        self, inputs: tuple[FoodOriginReviewInput, ...]
    ) -> dict[str, str]:
        response = self._llm.invoke(
            "nutrition_review",
            _SYSTEM_PROMPT,
            json.dumps(
                {
                    "references": [
                        {
                            "reference_id": item.reference_id,
                            "canonical_name": item.canonical_name,
                            "form": item.form,
                        }
                        for item in inputs
                    ]
                },
                ensure_ascii=False,
            ),
            response_format={"type": "json_object"},
        )
        try:
            payload = json.loads(response.get("content", ""))
            if set(payload) != {"origins"} or not isinstance(payload["origins"], list):
                raise ValueError("顶层字段非法")
            parsed: dict[str, str] = {}
            for row in payload["origins"]:
                if not isinstance(row, dict) or set(row) != {
                    "reference_id",
                    "food_origin",
                }:
                    raise ValueError("food_origin 行字段非法")
                reference_id = str(row["reference_id"])
                if reference_id in parsed:
                    raise ValueError("food_origin reference_id 重复")
                parsed[reference_id] = _validate_origin(row["food_origin"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("food_origin 模型返回非法 JSON") from exc
        expected_ids = {item.reference_id for item in inputs}
        if set(parsed) != expected_ids:
            raise ValueError("food_origin 模型未完整且唯一覆盖 reference_id")
        return parsed


class FoodOriginCandidateCache:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._entries: dict[str, str] = {}
        if self.path is not None and self.path.exists():
            for line_number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    self._entries[str(payload["cache_key"])] = _validate_origin(
                        payload["food_origin"]
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"food_origin 候选缓存第 {line_number} 行无效"
                    ) from exc

    def get(self, cache_key: str) -> str | None:
        return self._entries.get(cache_key)

    def put_many(self, entries: Mapping[str, str]) -> None:
        self._entries.update(
            {cache_key: _validate_origin(value) for cache_key, value in entries.items()}
        )
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for cache_key in sorted(self._entries):
                handle.write(
                    json.dumps(
                        {
                            "cache_key": cache_key,
                            "food_origin": self._entries[cache_key],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        temporary.replace(self.path)


def generate_food_origin_candidates(
    inputs,
    estimator,
    *,
    cache: FoodOriginCandidateCache | None = None,
    max_workers: int = 1,
    batch_size: int = 16,
) -> tuple[dict[str, str], ...]:
    review_inputs = tuple(inputs)
    if len({item.reference_id for item in review_inputs}) != len(review_inputs):
        raise ValueError("food_origin 输入 reference_id 重复")
    candidate_cache = cache or FoodOriginCandidateCache()
    origins: dict[str, str] = {}
    pending: list[tuple[FoodOriginReviewInput, str]] = []
    for item in review_inputs:
        cache_key = _cache_key(item, estimator.model_id)
        cached = candidate_cache.get(cache_key)
        if cached is None:
            pending.append((item, cache_key))
        else:
            origins[item.reference_id] = cached

    batches = [
        pending[index : index + max(1, batch_size)]
        for index in range(0, len(pending), max(1, batch_size))
    ]
    checkpoint: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = {
            executor.submit(
                estimator.estimate_batch,
                tuple(item for item, _ in batch),
            ): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            estimated = future.result()
            for item, cache_key in batch:
                origin = _validate_origin(estimated[item.reference_id])
                origins[item.reference_id] = origin
                checkpoint[cache_key] = origin
            if len(checkpoint) >= 64:
                candidate_cache.put_many(checkpoint)
                checkpoint.clear()
    if checkpoint:
        candidate_cache.put_many(checkpoint)

    return tuple(
        {
            "reference_id": item.reference_id,
            "canonical_name": item.canonical_name,
            "form": item.form,
            "candidate_food_origin": origins[item.reference_id],
            "review_status": "pending",
        }
        for item in review_inputs
    )


def write_food_origin_candidates(records, output_path: Path) -> None:
    fieldnames = (
        "reference_id",
        "canonical_name",
        "form",
        "candidate_food_origin",
        "review_status",
    )
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            if set(record) != set(fieldnames):
                raise ValueError("food_origin 候选字段不完整或含额外字段")
            if record["review_status"] != "pending":
                raise ValueError("food_origin 候选只能保持 pending")
            _validate_origin(record["candidate_food_origin"])
            writer.writerow(record)


def _validate_origin(value) -> str:
    if not isinstance(value, str) or value not in FOOD_ORIGINS:
        raise ValueError(f"food_origin 越出封闭词表: {value!r}")
    return value


def _cache_key(item: FoodOriginReviewInput, model_id: str) -> str:
    encoded = json.dumps(
        {
            "prompt_version": FOOD_ORIGIN_PROMPT_VERSION,
            "model_id": model_id,
            **item.__dict__,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
