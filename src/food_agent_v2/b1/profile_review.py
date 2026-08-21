"""Closed-vocabulary, model-assisted recipe profile review candidates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

PROFILE_PROMPT_VERSION = "recipe-profile-review-v2"

PROFILE_VOCABULARIES: dict[str, frozenset[str]] = {
    "meal_tags": frozenset({"早餐", "早午餐", "午餐", "下午茶", "晚餐", "夜宵"}),
    "dish_type_tags": frozenset({
        "主食", "主菜", "配菜", "汤羹", "粥", "面食", "点心", "小吃", "饮品", "甜品", "酱料"
    }),
    "taste_tags": frozenset({
        "清淡", "酸", "甜", "辣", "麻辣", "香辣", "酸甜", "咸鲜", "鲜香", "奶香"
    }),
    "cuisine_tags": frozenset({
        "家常", "中式", "西式", "川味", "粤式", "鲁式", "苏式", "浙式", "闽式",
        "湘式", "徽式", "东北", "清真", "日韩", "东南亚"
    }),
    "cooking_method_tags": frozenset({
        "炒", "煎", "炸", "蒸", "煮", "炖", "焖", "烤", "烧", "拌", "腌", "卤",
        "熬", "焗", "烩", "汆", "涮", "焯", "烫", "煲", "烙", "炝", "煸", "酿",
        "烘焙", "微波", "免烹饪"
    }),
    "texture_tags": frozenset({
        "软嫩", "酥脆", "爽脆", "软糯", "绵密", "筋道", "滑嫩", "浓稠", "清爽", "蓬松"
    }),
    "scenario_tags": frozenset({
        "日常", "快手", "宴客", "节日", "便当", "聚餐", "一人食", "家庭", "加餐"
    }),
}
_PROFILE_FIELDS = tuple(PROFILE_VOCABULARIES)

_SYSTEM_PROMPT = f"""你是菜谱检索画像标注员。根据菜名、原始食材、完整步骤、原 label，
为每道菜生成非医疗、非功效、非人群的检索标签。只能从以下封闭词表选择：
{json.dumps({key: sorted(value) for key, value in PROFILE_VOCABULARIES.items()}, ensure_ascii=False)}
meal_tags 至少选一个真实适合的餐次；普通正餐菜通常可选午餐和晚餐，但不要把甜点误标正餐，
也不要仅因“营养/清淡”而标早餐。dish_type_tags 至少一个；其他字段不确定可为空。
不得生成老人、儿童、孕妇、疾病、低钠、控糖、减肥、养生、功效等敏感标签。
顶层 JSON 必须且只能是 {{"profiles":[对象,...]}}；每个对象严格包含 recipe_id 和七个标签数组，
不得输出解释、来源、置信度或额外字段。"""


@dataclass(frozen=True)
class ProfileReviewInput:
    recipe_id: int
    name: str
    record_type: str
    ingredients_raw: str
    steps_raw: str
    label_tags: tuple[str, ...]


class LLMProfileEstimator:
    def __init__(self, llm_client, *, model_id: str) -> None:
        self._llm = llm_client
        self.model_id = model_id

    def estimate_batch(
        self, inputs: tuple[ProfileReviewInput, ...]
    ) -> dict[int, dict[str, list[str]]]:
        response = self._llm.invoke(
            "profile_enrichment",
            _SYSTEM_PROMPT,
            json.dumps({
                "recipes": [
                    {
                        "recipe_id": item.recipe_id,
                        "name": item.name,
                        "record_type": item.record_type,
                        "ingredients": item.ingredients_raw,
                        "steps": item.steps_raw,
                        "raw_label_tags": list(item.label_tags),
                    }
                    for item in inputs
                ]
            }, ensure_ascii=False),
            response_format={"type": "json_object"},
        )
        try:
            payload = json.loads(response.get("content", ""))
            rows = payload["profiles"]
            if not isinstance(rows, list):
                raise TypeError("profiles must be a list")
            parsed = {
                int(row["recipe_id"]): _validate_profile_payload(row)
                for row in rows
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("画像模型返回非法 JSON 或越出封闭词表") from exc
        expected_ids = {item.recipe_id for item in inputs}
        if set(parsed) != expected_ids or len(parsed) != len(rows):
            raise ValueError("画像模型未完整且唯一覆盖 recipe_id")
        return parsed


class ProfileCandidateCache:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._entries: dict[str, dict[str, list[str]]] = {}
        if self.path is not None and self.path.exists():
            for line_number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    self._entries[str(payload["cache_key"])] = _validate_profile_payload(
                        {"recipe_id": 0, **payload["profile"]}
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"画像候选缓存第 {line_number} 行无效") from exc

    def get(self, cache_key: str) -> dict[str, list[str]] | None:
        value = self._entries.get(cache_key)
        return {key: list(tags) for key, tags in value.items()} if value else None

    def put_many(self, entries: Mapping[str, dict[str, list[str]]]) -> None:
        self._entries.update({key: dict(value) for key, value in entries.items()})
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for cache_key in sorted(self._entries):
                handle.write(json.dumps({
                    "cache_key": cache_key,
                    "profile": self._entries[cache_key],
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        temporary.replace(self.path)


def generate_profile_candidates(
    inputs,
    facts,
    estimator,
    *,
    cache: ProfileCandidateCache | None = None,
    max_workers: int = 1,
    batch_size: int = 8,
) -> tuple[dict, ...]:
    inputs = tuple(inputs)
    fact_by_id = {int(fact.recipe_id): fact for fact in facts}
    if set(fact_by_id) != {item.recipe_id for item in inputs}:
        raise ValueError("画像输入与菜品事实 recipe_id 不一致")
    candidate_cache = cache or ProfileCandidateCache()
    profiles: dict[int, dict[str, list[str]]] = {}
    pending: list[tuple[ProfileReviewInput, str]] = []
    for item in inputs:
        cache_key = _profile_cache_key(item, estimator.model_id)
        cached = candidate_cache.get(cache_key)
        if cached is None:
            pending.append((item, cache_key))
        else:
            profiles[item.recipe_id] = cached

    batches = [pending[index:index + max(1, batch_size)] for index in range(0, len(pending), max(1, batch_size))]
    checkpoint: dict[str, dict[str, list[str]]] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        future_batches = {
            executor.submit(estimator.estimate_batch, tuple(item for item, _ in batch)): batch
            for batch in batches
        }
        for future in as_completed(future_batches):
            batch = future_batches[future]
            try:
                estimated = future.result()
                for item, cache_key in batch:
                    profile = estimated[item.recipe_id]
                    profiles[item.recipe_id] = profile
                    checkpoint[cache_key] = profile
                if len(checkpoint) >= 32:
                    candidate_cache.put_many(checkpoint)
                    checkpoint.clear()
            except Exception as exc:
                errors.append(f"recipe_ids={[item.recipe_id for item, _ in batch]}:{type(exc).__name__}")
    if checkpoint:
        candidate_cache.put_many(checkpoint)
    if errors:
        raise RuntimeError(f"画像候选生成存在 {len(errors)} 个失败批次: {errors[:5]}")

    records = []
    for item in inputs:
        fact = fact_by_id[item.recipe_id]
        inferred = profiles[item.recipe_id]
        records.append({
            "recipe_id": item.recipe_id,
            "meal_tags": _merge(fact.meal_tags, inferred["meal_tags"]),
            "dish_type_tags": inferred["dish_type_tags"],
            "taste_tags": _merge(fact.taste_tags, inferred["taste_tags"]),
            "cuisine_tags": inferred["cuisine_tags"],
            "cooking_method_tags": inferred["cooking_method_tags"],
            "texture_tags": inferred["texture_tags"],
            "scenario_tags": inferred["scenario_tags"],
            "population_tags": list(fact.population_tags),
            "review_status": "pending",
        })
    return tuple(records)


def write_profile_candidate_records(records, output_path: Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _validate_profile_payload(payload: dict) -> dict[str, list[str]]:
    if set(payload) != {"recipe_id", *_PROFILE_FIELDS}:
        raise ValueError("画像字段不完整或含额外字段")
    normalized: dict[str, list[str]] = {}
    for field, vocabulary in PROFILE_VOCABULARIES.items():
        values = payload[field]
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError(f"画像字段 {field} 必须为字符串数组")
        normalized[field] = list(
            dict.fromkeys(value for value in values if value in vocabulary)
        )
    if not normalized["meal_tags"]:
        raise ValueError("meal_tags 不得为空")
    return normalized


def _profile_cache_key(item: ProfileReviewInput, model_id: str) -> str:
    material = {
        "prompt_version": PROFILE_PROMPT_VERSION,
        "model_id": model_id,
        **item.__dict__,
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=list).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _merge(primary, secondary) -> list[str]:
    return list(dict.fromkeys((*tuple(primary), *tuple(secondary))))
