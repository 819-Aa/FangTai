"""Review-only multilingual retrieval of USDA nutrition references."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np

USDA_SEARCH_TERM_PROMPT_VERSION = "usda-search-term-v1"

_SEARCH_TERM_SYSTEM_PROMPT = """你是食材身份检索词翻译器。把每个中文食材名称翻译成适合在
USDA FoodData Central 中搜索的简洁英文食物身份。保留动物部位、植物品种、原料形态
（raw/cooked/dried 等）和关键加工状态；去掉刀工形状、数量、营销词和菜谱语境。
可以用括号或逗号补一个常见英文同义词，但不得推荐替代食材，不得生成营养值、USDA ID、
置信度、来源或解释。顶层 JSON 必须且只能是 {"translations":[对象,...]}；每个对象必须且
只能包含 ingredient_id、form、search_term。"""

_SELECTION_SYSTEM_PROMPT = """你是 USDA 食材身份映射审阅助手。对每个中文食材，只能从给定
FoodData Central 候选中选择身份与形态都相符的一项；近似食材、替代品、仅名称相似、不同动物
部位、不同加工状态、品牌/复合配方不明确时必须选择 null。不得改写或创造 reference_id，不得
生成营养值、置信度、来源或解释。顶层 JSON 必须且只能是 {"selections":[对象,...]}；每个对象
必须且只能包含 ingredient_id、form、selected_reference_id，其中 selected_reference_id 是给定 ID
或 null。"""

_FIELDNAMES = (
    "ingredient_id",
    "ingredient_name",
    "form",
    "candidate_reference_id",
    "candidate_name",
    "candidate_form",
    "source_dataset",
    "match_method",
    "candidate_rank",
    "reason",
    "review_status",
)


@dataclass(frozen=True)
class UsdaCrosswalkReviewInput:
    ingredient_id: int
    ingredient_name: str
    form: str


class LLMUsdaSearchTermEstimator:
    def __init__(self, llm_client, *, model_id: str) -> None:
        self._llm = llm_client
        self.model_id = model_id

    def estimate_batch(
        self, inputs: tuple[UsdaCrosswalkReviewInput, ...]
    ) -> dict[tuple[int, str], str]:
        response = self._llm.invoke(
            "nutrition_review",
            _SEARCH_TERM_SYSTEM_PROMPT,
            json.dumps(
                {
                    "ingredients": [
                        {
                            "ingredient_id": item.ingredient_id,
                            "ingredient_name": item.ingredient_name,
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
            if set(payload) != {"translations"} or not isinstance(
                payload["translations"], list
            ):
                raise ValueError("USDA search term 顶层字段非法")
            parsed = {}
            for row in payload["translations"]:
                if not isinstance(row, dict) or set(row) != {
                    "ingredient_id",
                    "form",
                    "search_term",
                }:
                    raise ValueError("USDA search term 行字段非法")
                key = (int(row["ingredient_id"]), str(row["form"]).strip())
                if key in parsed:
                    raise ValueError("USDA search term ingredient/form 重复")
                parsed[key] = _validate_search_term(row["search_term"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("USDA search term 模型返回非法 JSON") from exc
        expected = {(item.ingredient_id, item.form.strip()) for item in inputs}
        if set(parsed) != expected:
            raise ValueError("USDA search term 未完整且唯一覆盖 ingredient/form")
        return parsed


class UsdaSearchTermCandidateCache:
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
                    self._entries[str(payload["cache_key"])] = _validate_search_term(
                        payload["search_term"]
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"USDA search term 缓存第 {line_number} 行无效"
                    ) from exc

    def get(self, cache_key: str) -> str | None:
        return self._entries.get(cache_key)

    def put_many(self, entries: Mapping[str, str]) -> None:
        self._entries.update(
            {
                cache_key: _validate_search_term(value)
                for cache_key, value in entries.items()
            }
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
                            "search_term": self._entries[cache_key],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        temporary.replace(self.path)


class LLMUsdaCandidateSelector:
    def __init__(self, llm_client, *, model_id: str) -> None:
        self._llm = llm_client
        self.model_id = model_id

    def select_batch(self, items: tuple[dict, ...]) -> dict[tuple[int, str], str | None]:
        response = self._llm.invoke(
            "nutrition_review",
            _SELECTION_SYSTEM_PROMPT,
            json.dumps({"items": items}, ensure_ascii=False),
            response_format={"type": "json_object"},
        )
        try:
            payload = json.loads(response.get("content", ""))
            if set(payload) != {"selections"} or not isinstance(
                payload["selections"], list
            ):
                raise ValueError("USDA selection 顶层字段非法")
            parsed = {}
            for row in payload["selections"]:
                if not isinstance(row, dict) or set(row) != {
                    "ingredient_id",
                    "form",
                    "selected_reference_id",
                }:
                    raise ValueError("USDA selection 行字段非法")
                key = (int(row["ingredient_id"]), str(row["form"]).strip())
                selected = row["selected_reference_id"]
                if selected is not None and not isinstance(selected, str):
                    raise ValueError("USDA selection ID 类型非法")
                if key in parsed:
                    raise ValueError("USDA selection ingredient/form 重复")
                parsed[key] = selected
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("USDA selection 模型返回非法 JSON") from exc
        expected = {(int(item["ingredient_id"]), str(item["form"])) for item in items}
        if set(parsed) != expected:
            raise ValueError("USDA selection 未完整且唯一覆盖 ingredient/form")
        return parsed


class UsdaCandidateSelectionCache:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._entries: dict[str, str | None] = {}
        if self.path is not None and self.path.exists():
            for line_number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    selected = payload["selected_reference_id"]
                    if selected is not None and not isinstance(selected, str):
                        raise ValueError("selected_reference_id 类型非法")
                    self._entries[str(payload["cache_key"])] = selected
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"USDA selection 缓存第 {line_number} 行无效"
                    ) from exc

    def get(self, cache_key: str) -> tuple[bool, str | None]:
        return cache_key in self._entries, self._entries.get(cache_key)

    def put_many(self, entries: Mapping[str, str | None]) -> None:
        self._entries.update(entries)
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
                            "selected_reference_id": self._entries[cache_key],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        temporary.replace(self.path)


def generate_usda_search_terms(
    inputs,
    estimator,
    *,
    cache: UsdaSearchTermCandidateCache | None = None,
    max_workers: int = 1,
    batch_size: int = 16,
) -> dict[tuple[int, str], str]:
    review_inputs = tuple(inputs)
    keys = {(item.ingredient_id, item.form.strip()) for item in review_inputs}
    if len(keys) != len(review_inputs):
        raise ValueError("USDA search term 输入 ingredient/form 重复")
    candidate_cache = cache or UsdaSearchTermCandidateCache()
    terms: dict[tuple[int, str], str] = {}
    pending = []
    for item in review_inputs:
        cache_key = _search_term_cache_key(item, estimator.model_id)
        cached = candidate_cache.get(cache_key)
        if cached is None:
            pending.append((item, cache_key))
        else:
            terms[(item.ingredient_id, item.form.strip())] = cached
    batches = [
        pending[index : index + max(1, batch_size)]
        for index in range(0, len(pending), max(1, batch_size))
    ]
    checkpoint = {}
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
                key = (item.ingredient_id, item.form.strip())
                term = _validate_search_term(estimated[key])
                terms[key] = term
                checkpoint[cache_key] = term
            if len(checkpoint) >= 64:
                candidate_cache.put_many(checkpoint)
                checkpoint.clear()
    if checkpoint:
        candidate_cache.put_many(checkpoint)
    return terms


def select_usda_crosswalk_candidates(
    inputs,
    ranked_candidates,
    search_terms: Mapping[tuple[int, str], str],
    selector,
    *,
    cache: UsdaCandidateSelectionCache | None = None,
    max_workers: int = 1,
    batch_size: int = 4,
) -> tuple[dict, ...]:
    review_inputs = tuple(inputs)
    expected_keys = {(item.ingredient_id, item.form.strip()) for item in review_inputs}
    if set(search_terms) != expected_keys:
        raise ValueError("USDA selection search term 与输入不完全一致")
    candidates_by_key: dict[tuple[int, str], list[dict]] = {
        key: [] for key in expected_keys
    }
    for candidate in ranked_candidates:
        key = (int(candidate["ingredient_id"]), str(candidate["form"]).strip())
        if key not in candidates_by_key:
            raise ValueError("USDA selection 候选包含未知 ingredient/form")
        if candidate["review_status"] != "pending":
            raise ValueError("USDA selection 输入候选必须是 pending")
        candidates_by_key[key].append(dict(candidate))

    items = []
    for item in review_inputs:
        key = (item.ingredient_id, item.form.strip())
        candidates = sorted(
            candidates_by_key[key], key=lambda row: int(row["candidate_rank"])
        )
        items.append(
            {
                "ingredient_id": item.ingredient_id,
                "ingredient_name": item.ingredient_name,
                "form": item.form.strip(),
                "search_term": _validate_search_term(search_terms[key]),
                "candidates": [
                    {
                        "reference_id": row["candidate_reference_id"],
                        "name": row["candidate_name"],
                        "form": row["candidate_form"],
                    }
                    for row in candidates
                ],
            }
        )

    selection_cache = cache or UsdaCandidateSelectionCache()
    selections: dict[tuple[int, str], str | None] = {}
    pending = []
    for item in items:
        cache_key = _selection_cache_key(item, selector.model_id)
        found, selected = selection_cache.get(cache_key)
        key = (int(item["ingredient_id"]), str(item["form"]))
        if found:
            selections[key] = selected
        else:
            pending.append((item, cache_key))
    batches = [
        pending[index : index + max(1, batch_size)]
        for index in range(0, len(pending), max(1, batch_size))
    ]
    checkpoint: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = {
            executor.submit(
                selector.select_batch,
                tuple(item for item, _ in batch),
            ): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            estimated = future.result()
            for item, cache_key in batch:
                key = (int(item["ingredient_id"]), str(item["form"]))
                selected = estimated[key]
                allowed_ids = {
                    candidate["reference_id"] for candidate in item["candidates"]
                }
                if selected is not None and selected not in allowed_ids:
                    raise ValueError("USDA selection 选择了候选集外 reference_id")
                selections[key] = selected
                checkpoint[cache_key] = selected
            if len(checkpoint) >= 32:
                selection_cache.put_many(checkpoint)
                checkpoint.clear()
    if checkpoint:
        selection_cache.put_many(checkpoint)

    output = []
    for item in review_inputs:
        key = (item.ingredient_id, item.form.strip())
        selected = selections[key]
        if selected is None:
            output.append(
                {
                    "ingredient_id": item.ingredient_id,
                    "ingredient_name": item.ingredient_name,
                    "form": item.form,
                    "candidate_reference_id": "",
                    "candidate_name": "",
                    "candidate_form": "",
                    "source_dataset": "",
                    "match_method": "multilingual_embedding_model_review",
                    "candidate_rank": "",
                    "reason": "no_exact_usda_identity_match",
                    "review_status": "pending",
                }
            )
            continue
        matched = next(
            row
            for row in candidates_by_key[key]
            if row["candidate_reference_id"] == selected
        )
        output.append(
            {
                **matched,
                "match_method": "multilingual_embedding_model_review",
                "reason": "usda_identity_and_form_requires_owner_review",
                "review_status": "pending",
            }
        )
    return tuple(output)


def generate_usda_crosswalk_candidates(
    inputs,
    references,
    embedder,
    *,
    embedding_model_id: str,
    search_terms: Mapping[tuple[int, str], str] | None = None,
    top_k: int = 3,
    batch_size: int = 32,
    max_workers: int = 1,
    embedding_cache_dir: Path | None = None,
) -> tuple[dict, ...]:
    """Retrieve a small pending shortlist; embeddings never become nutrition facts."""
    review_inputs = tuple(inputs)
    reference_rows = tuple(
        reference
        for reference in references
        if reference.source_dataset in {"usda_foundation", "usda_sr_legacy"}
    )
    if top_k <= 0 or batch_size <= 0 or max_workers <= 0:
        raise ValueError("USDA 候选生成参数必须大于 0")
    input_keys = {
        (item.ingredient_id, item.form.strip()) for item in review_inputs
    }
    if len(input_keys) != len(review_inputs):
        raise ValueError("USDA 候选输入 ingredient/form 重复")
    if len({item.reference_id for item in reference_rows}) != len(reference_rows):
        raise ValueError("USDA 候选参考 reference_id 重复")
    if not review_inputs or not reference_rows:
        return ()

    reference_texts = tuple(
        item.canonical_name
        for item in reference_rows
    )
    if search_terms is not None and set(search_terms) != input_keys:
        raise ValueError("USDA search term 与候选输入不完全一致")
    query_texts = tuple(
        (
            _validate_search_term(search_terms[(item.ingredient_id, item.form.strip())])
            if search_terms is not None
            else item.ingredient_name
        )
        for item in review_inputs
    )
    reference_vectors = _encode_texts(
        embedder,
        reference_texts,
        model_id=embedding_model_id,
        batch_size=batch_size,
        max_workers=max_workers,
        cache_dir=embedding_cache_dir,
    )
    query_vectors = _encode_texts(
        embedder,
        query_texts,
        model_id=embedding_model_id,
        batch_size=batch_size,
        max_workers=max_workers,
        cache_dir=embedding_cache_dir,
    )
    if reference_vectors.shape[1] != query_vectors.shape[1]:
        raise ValueError("USDA 候选查询与参考向量维度不一致")

    records = []
    for query_index, item in enumerate(review_inputs):
        compatible = np.asarray(
            [
                index
                for index, reference in enumerate(reference_rows)
                if _forms_compatible(item.form, reference.form)
            ],
            dtype=np.int64,
        )
        if compatible.size == 0:
            continue
        scores = reference_vectors[compatible] @ query_vectors[query_index]
        ranked_local = sorted(
            range(len(compatible)),
            key=lambda index: (
                -float(scores[index]),
                reference_rows[int(compatible[index])].reference_id,
            ),
        )[:top_k]
        for rank, local_index in enumerate(ranked_local, 1):
            reference = reference_rows[int(compatible[local_index])]
            records.append(
                {
                    "ingredient_id": item.ingredient_id,
                    "ingredient_name": item.ingredient_name,
                    "form": item.form,
                    "candidate_reference_id": reference.reference_id,
                    "candidate_name": reference.canonical_name,
                    "candidate_form": reference.form,
                    "source_dataset": reference.source_dataset,
                    "match_method": "multilingual_embedding",
                    "candidate_rank": rank,
                    "reason": "usda_identity_and_form_requires_review",
                    "review_status": "pending",
                }
            )
    return tuple(records)


def write_usda_crosswalk_candidates(records, output_path: Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDNAMES)
        writer.writeheader()
        for record in records:
            if set(record) != set(_FIELDNAMES):
                raise ValueError("USDA 候选字段不完整或含额外字段")
            if record["review_status"] != "pending":
                raise ValueError("USDA 候选只能保持 pending")
            writer.writerow(record)


def _encode_texts(
    embedder,
    texts: tuple[str, ...],
    *,
    model_id: str,
    batch_size: int,
    max_workers: int,
    cache_dir: Path | None,
) -> np.ndarray:
    batches = [
        (index, texts[index : index + batch_size])
        for index in range(0, len(texts), batch_size)
    ]
    results: dict[int, np.ndarray] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _encode_batch,
                embedder,
                batch,
                model_id=model_id,
                cache_dir=cache_dir,
            ): index
            for index, batch in batches
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    matrix = np.concatenate([results[index] for index, _ in batches], axis=0)
    if matrix.shape[0] != len(texts) or matrix.ndim != 2:
        raise ValueError("USDA 候选嵌入返回形状异常")
    return matrix


def _encode_batch(
    embedder,
    texts: tuple[str, ...],
    *,
    model_id: str,
    cache_dir: Path | None,
) -> np.ndarray:
    cache_path = None
    if cache_dir is not None:
        material = json.dumps(
            {"model_id": model_id, "texts": texts},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        cache_path = Path(cache_dir) / (hashlib.sha256(material).hexdigest() + ".npy")
        if cache_path.exists():
            return np.load(cache_path, allow_pickle=False)
    encoded = np.asarray(
        embedder.encode(list(texts), normalize_embeddings=True),
        dtype=np.float32,
    )
    if encoded.ndim != 2 or encoded.shape[0] != len(texts):
        raise ValueError("USDA 候选嵌入批次形状异常")
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            np.save(handle, encoded, allow_pickle=False)
        temporary.replace(cache_path)
    return encoded


def _forms_compatible(ingredient_form: str, reference_form: str) -> bool:
    requested = ingredient_form.strip()
    candidate = reference_form.strip()
    return (
        requested == "unspecified"
        or candidate == requested
        or candidate == "unspecified"
    )


def _validate_search_term(value) -> str:
    if not isinstance(value, str):
        raise ValueError("USDA search term 必须是字符串")
    normalized = " ".join(value.strip().split())
    if not normalized or len(normalized) > 160 or not re.search(r"[A-Za-z]", normalized):
        raise ValueError(f"USDA search term 非法: {value!r}")
    return normalized


def _search_term_cache_key(item: UsdaCrosswalkReviewInput, model_id: str) -> str:
    material = json.dumps(
        {
            "prompt_version": USDA_SEARCH_TERM_PROMPT_VERSION,
            "model_id": model_id,
            **item.__dict__,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _selection_cache_key(item: dict, model_id: str) -> str:
    material = json.dumps(
        {
            "prompt_version": "usda-candidate-selection-v1",
            "model_id": model_id,
            "item": item,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()
