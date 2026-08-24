"""整菜模糊用量候选生成与审阅 CSV 输出。"""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile

from food_agent_v2.b1.quantity_normalizer import normalize_quantity
from food_agent_v2.core.paths import PROJECT_ROOT

_USAGE_CODES = frozenset(
    {"main", "supporting", "seasoning", "cooking_fat", "cooking_liquid"}
)
_FUZZY_TOKEN_CLASSES = frozenset(
    {"as_needed", "small_amount", "several_count", "few_drops"}
)

_SYSTEM_PROMPT = """你是家庭烹饪食材用量估算器。请基于整道菜的名称、全部待估食材和完整步骤，
为每个 occurrence_id 估算一个大于 0 的原始食材克重。只估算输入中列出的 occurrence，
不补充新食材，不输出区间、置信度、来源或解释。输出严格 JSON：
{"grams":{"occurrence_id":克重数值}}。"""
QUANTITY_PROMPT_VERSION = "quantity-review-v1"

_LEGACY_CANDIDATE_HEADERS = (
    "occurrence_id",
    "recipe_id",
    "recipe_name",
    "ingredient_name",
    "raw_quantity",
    "step_context",
    "deterministic_calculation",
    "candidate_basis",
    "candidate_grams",
    "decision_grams",
    "review_status",
)
_V2_CANDIDATE_HEADERS = (
    "occurrence_id",
    "recipe_id",
    "recipe_name",
    "ingredient_id",
    "ingredient_name",
    "normalized_form",
    "normalized_unit",
    "usage_code",
    "fuzzy_token_class",
    "raw_quantity",
    "step_context",
    "deterministic_calculation",
    "candidate_basis",
    "candidate_grams",
    "decision_grams",
    "review_status",
)


@dataclass(frozen=True)
class RecipeQuantityReviewContext:
    recipe_id: int
    recipe_name: str
    step_context: str
    ingredients: tuple


@dataclass(frozen=True)
class QuantityReviewCandidate:
    occurrence_id: str
    recipe_id: int
    recipe_name: str
    ingredient_name: str
    raw_quantity: str
    step_context: str
    deterministic_calculation: str
    candidate_basis: str
    candidate_grams: Decimal
    ingredient_id: int | None = None
    normalized_form: str = ""
    normalized_unit: str | None = None
    usage_code: str | None = None
    fuzzy_token_class: str | None = None
    decision_grams: Decimal | None = None
    review_status: str = "pending"


class QuantityEstimateCache:
    """Regenerable exact-content cache for pending model estimates."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._entries: dict[str, dict[str, Decimal]] = {}
        if self.path is not None and self.path.exists():
            for line_number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    self._entries[str(payload["cache_key"])] = {
                        str(key): Decimal(str(value))
                        for key, value in payload["grams"].items()
                    }
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"克重缓存第 {line_number} 行无效") from exc

    def get(self, cache_key: str) -> dict[str, Decimal] | None:
        value = self._entries.get(cache_key)
        return dict(value) if value is not None else None

    def put_many(self, entries: Mapping[str, dict[str, Decimal]]) -> None:
        self._entries.update({key: dict(value) for key, value in entries.items()})
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for cache_key in sorted(self._entries):
                handle.write(json.dumps({
                    "cache_key": cache_key,
                    "grams": {
                        key: str(value)
                        for key, value in sorted(self._entries[cache_key].items())
                    },
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        temporary.replace(self.path)


def quantity_context_cache_key(
    context: RecipeQuantityReviewContext,
    model_id: str,
) -> str:
    material = {
        "prompt_version": QUANTITY_PROMPT_VERSION,
        "model_id": model_id,
        "recipe_id": context.recipe_id,
        "recipe_name": context.recipe_name,
        "step_context": context.step_context,
        "ingredients": [
            {
                "occurrence_id": item.occurrence_id,
                "ingredient_name": item.ingredient_name,
                "quantity_raw": item.quantity_raw,
                "unit_raw": item.unit_raw,
                "form": item.form,
            }
            for item in context.ingredients
        ],
    }
    encoded = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class LLMQuantityEstimator:
    """一次整菜调用生成待审候选；模型结果永远不直接成为批准决定。"""

    def __init__(self, llm_client) -> None:
        self._llm = llm_client

    def estimate(self, context: RecipeQuantityReviewContext) -> dict[str, Decimal]:
        ingredients = [
            {
                "occurrence_id": item.occurrence_id,
                "ingredient_name": item.ingredient_name,
                "raw_quantity": item.quantity_raw,
                "unit": item.unit_raw,
                "form": item.form,
            }
            for item in context.ingredients
        ]
        user_message = json.dumps(
            {
                "recipe_id": context.recipe_id,
                "recipe_name": context.recipe_name,
                "ingredients_to_estimate": ingredients,
                "steps": context.step_context,
            },
            ensure_ascii=False,
        )
        response = self._llm.invoke(
            "quantity_estimation",
            _SYSTEM_PROMPT,
            user_message,
            response_format={"type": "json_object"},
        )
        try:
            payload = json.loads(response.get("content", ""))
            grams = payload["grams"]
            if not isinstance(grams, dict):
                raise TypeError("grams must be an object")
            return {str(key): Decimal(str(value)) for key, value in grams.items()}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"整菜用量模型返回非法 JSON: recipe {context.recipe_id}") from exc


def build_quantity_review_contexts(
    nutrition_views,
    recipe_facts,
    measure_rules,
    decisions,
) -> tuple[RecipeQuantityReviewContext, ...]:
    facts_by_id = {item.recipe_id: item for item in recipe_facts}
    contexts: list[RecipeQuantityReviewContext] = []
    for view in nutrition_views:
        pending = tuple(
            item
            for item in view.ingredients
            if normalize_quantity(item, measure_rules, decisions).requires_review
        )
        if not pending:
            continue
        fact = facts_by_id.get(view.recipe_id)
        if fact is None:
            raise ValueError(f"营养视图缺少菜品事实: {view.recipe_id}")
        contexts.append(
            RecipeQuantityReviewContext(
                recipe_id=view.recipe_id,
                recipe_name=fact.name,
                step_context="\n".join(fact.step_segments),
                ingredients=pending,
            )
        )
    return tuple(contexts)


def generate_quantity_candidates(
    contexts,
    estimator,
    *,
    cache: QuantityEstimateCache | None = None,
    model_id: str = "unspecified",
    max_workers: int = 1,
    checkpoint_size: int = 25,
):
    contexts = tuple(contexts)
    estimate_cache = cache or QuantityEstimateCache()
    estimates: dict[int, dict[str, Decimal]] = {}
    pending: list[tuple[int, RecipeQuantityReviewContext, str]] = []
    for index, context in enumerate(contexts):
        cache_key = quantity_context_cache_key(context, model_id)
        cached = estimate_cache.get(cache_key)
        if cached is None:
            pending.append((index, context, cache_key))
        else:
            estimates[index] = _validate_estimate(context, cached)

    checkpoint: dict[str, dict[str, Decimal]] = {}
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        future_rows = {
            executor.submit(estimator.estimate, context): (index, context, cache_key)
            for index, context, cache_key in pending
        }
        for future in as_completed(future_rows):
            index, context, cache_key = future_rows[future]
            estimated = _validate_estimate(context, future.result())
            estimates[index] = estimated
            checkpoint[cache_key] = estimated
            if len(checkpoint) >= max(1, checkpoint_size):
                estimate_cache.put_many(checkpoint)
                checkpoint.clear()
    if checkpoint:
        estimate_cache.put_many(checkpoint)

    candidates: list[QuantityReviewCandidate] = []
    for index, context in enumerate(contexts):
        if not context.ingredients:
            continue
        estimated = estimates[index]
        for ingredient in context.ingredients:
            grams = Decimal(estimated[ingredient.occurrence_id])
            if not _is_finite_positive_decimal(grams):
                raise ValueError(f"候选克重必须为有限正数: {ingredient.occurrence_id}")
            candidates.append(
                QuantityReviewCandidate(
                    occurrence_id=ingredient.occurrence_id,
                    recipe_id=context.recipe_id,
                    recipe_name=context.recipe_name,
                    ingredient_id=ingredient.ingredient_id,
                    ingredient_name=ingredient.ingredient_name,
                    normalized_form=ingredient.normalized_form,
                    normalized_unit=ingredient.unit_raw,
                    usage_code=ingredient.usage_code,
                    fuzzy_token_class=ingredient.fuzzy_token_class,
                    raw_quantity=ingredient.quantity_raw or "",
                    step_context=context.step_context,
                    deterministic_calculation="not_available",
                    candidate_basis="whole_recipe_context_model",
                    candidate_grams=grams,
                )
            )
    return tuple(candidates)


def _validate_estimate(context, estimated) -> dict[str, Decimal]:
    normalized = {str(key): Decimal(value) for key, value in estimated.items()}
    expected_ids = {item.occurrence_id for item in context.ingredients}
    if set(normalized) != expected_ids:
        raise ValueError(f"整菜用量估算未完整覆盖 occurrence: recipe {context.recipe_id}")
    if any(not _is_finite_positive_decimal(value) for value in normalized.values()):
        raise ValueError(f"候选克重必须为有限正数: recipe {context.recipe_id}")
    return normalized


def write_quantity_candidates(candidates, output_path: Path) -> None:
    path = Path(output_path)
    if _is_formal_measure_rule_path(path):
        raise ValueError("数量候选不得写入正式计量规则文件")
    candidate_rows = tuple(candidates)
    for item in candidate_rows:
        _validate_output_candidate(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
        "occurrence_id",
        "recipe_id",
        "recipe_name",
        "ingredient_id",
        "ingredient_name",
        "normalized_form",
        "normalized_unit",
        "usage_code",
        "fuzzy_token_class",
        "raw_quantity",
        "step_context",
        "deterministic_calculation",
        "candidate_basis",
        "candidate_grams",
        "decision_grams",
        "review_status",
    )
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for item in candidate_rows:
                writer.writerow(
                    {
                        "occurrence_id": _safe_csv_text(item.occurrence_id),
                        "recipe_id": item.recipe_id,
                        "recipe_name": _safe_csv_text(item.recipe_name),
                        "ingredient_id": (
                            item.ingredient_id if item.ingredient_id is not None else ""
                        ),
                        "ingredient_name": _safe_csv_text(item.ingredient_name),
                        "normalized_form": _safe_csv_text(item.normalized_form),
                        "normalized_unit": _safe_csv_text(item.normalized_unit),
                        "usage_code": _safe_csv_text(item.usage_code),
                        "fuzzy_token_class": _safe_csv_text(item.fuzzy_token_class),
                        "raw_quantity": _safe_csv_text(item.raw_quantity),
                        "step_context": _safe_csv_text(item.step_context),
                        "deterministic_calculation": _safe_csv_text(
                            item.deterministic_calculation
                        ),
                        "candidate_basis": _safe_csv_text(item.candidate_basis),
                        "candidate_grams": str(item.candidate_grams),
                        "decision_grams": (
                            str(item.decision_grams)
                            if item.decision_grams is not None
                            else ""
                        ),
                        "review_status": _safe_csv_text(item.review_status),
                    }
                )
        if _is_formal_measure_rule_path(path):
            raise ValueError("数量候选不得写入正式计量规则文件")
        temporary_path.replace(path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def load_quantity_review_candidates(path: Path, views, facts) -> tuple[QuantityReviewCandidate, ...]:
    """Load legacy or V2 occurrence candidates and bind them to current views."""
    candidate_path = Path(path)
    if not candidate_path.exists():
        raise ValueError(f"数量候选文件不存在: {candidate_path}")
    facts_by_id = {fact.recipe_id: fact for fact in facts}
    occurrences = {
        item.occurrence_id: (view.recipe_id, item)
        for view in views.nutrition_views
        for item in view.ingredients
    }
    with candidate_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = tuple(reader.fieldnames or ())
        if headers not in {_LEGACY_CANDIDATE_HEADERS, _V2_CANDIDATE_HEADERS}:
            raise ValueError("数量候选 CSV 表头非法")
        output = []
        seen_occurrence_ids: set[str] = set()
        for line_number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"数量候选 CSV 第 {line_number} 行列数非法")
            occurrence_id = (row.get("occurrence_id") or "").strip()
            if occurrence_id in seen_occurrence_ids:
                raise ValueError(f"重复 occurrence_id: {occurrence_id}")
            seen_occurrence_ids.add(occurrence_id)
            output.append(
                _parse_review_candidate_row(
                    row, line_number, occurrences=occurrences, facts_by_id=facts_by_id,
                    is_v2=headers == _V2_CANDIDATE_HEADERS,
                )
            )
    return tuple(output)


def _parse_review_candidate_row(
    row: dict[str, str],
    line_number: int,
    *,
    occurrences: dict,
    facts_by_id: dict,
    is_v2: bool,
) -> QuantityReviewCandidate:
    try:
        occurrence_id = _required_candidate_text(row, "occurrence_id")
        current = occurrences.get(occurrence_id)
        if current is None:
            raise ValueError(f"未知 occurrence_id: {occurrence_id}")
        recipe_id, occurrence = current
        recipe_id_value = int(_required_candidate_text(row, "recipe_id"))
        if recipe_id_value != recipe_id:
            raise ValueError(f"recipe_id 与 occurrence 不一致: {occurrence_id}")
        fact = facts_by_id.get(recipe_id)
        if fact is None:
            raise ValueError(f"occurrence 缺少菜品事实: {occurrence_id}")
        recipe_name = _required_candidate_text(row, "recipe_name")
        if recipe_name != fact.name:
            raise ValueError(f"recipe_name 与当前事实不一致: {occurrence_id}")
        ingredient_name = _required_candidate_text(row, "ingredient_name")
        if ingredient_name != occurrence.ingredient_name:
            raise ValueError(f"ingredient_name 与当前事实不一致: {occurrence_id}")
        raw_quantity = row.get("raw_quantity") or ""
        if raw_quantity != (occurrence.quantity_raw or ""):
            raise ValueError(f"raw_quantity 与当前 occurrence 不一致: {occurrence_id}")
        normalized_form = occurrence.normalized_form
        normalized_unit = occurrence.unit_raw
        usage_code = occurrence.usage_code
        fuzzy_token_class = occurrence.fuzzy_token_class
        if is_v2:
            if int(_required_candidate_text(row, "ingredient_id")) != occurrence.ingredient_id:
                raise ValueError(f"ingredient_id 与当前 occurrence 不一致: {occurrence_id}")
            if (row.get("normalized_form") or "") != normalized_form:
                raise ValueError(f"normalized_form 与当前 occurrence 不一致: {occurrence_id}")
            if (row.get("normalized_unit") or "") != (normalized_unit or ""):
                raise ValueError(f"normalized_unit 与当前 occurrence 不一致: {occurrence_id}")
            if (row.get("usage_code") or "") != (usage_code or ""):
                raise ValueError(f"usage_code 与当前 occurrence 不一致: {occurrence_id}")
            if (row.get("fuzzy_token_class") or "") != (fuzzy_token_class or ""):
                raise ValueError(f"fuzzy_token_class 与当前 occurrence 不一致: {occurrence_id}")
        candidate_grams = _candidate_decimal(row, "candidate_grams")
        decision_text = (row.get("decision_grams") or "").strip()
        decision_grams = _candidate_decimal(row, "decision_grams") if decision_text else None
        if (row.get("review_status") or "").strip() != "pending":
            raise ValueError("数量候选必须保持 pending")
        candidate = QuantityReviewCandidate(
            occurrence_id=occurrence_id,
            recipe_id=recipe_id,
            recipe_name=recipe_name,
            ingredient_id=occurrence.ingredient_id,
            ingredient_name=ingredient_name,
            normalized_form=normalized_form,
            normalized_unit=normalized_unit,
            usage_code=usage_code,
            fuzzy_token_class=fuzzy_token_class,
            raw_quantity=raw_quantity,
            step_context=row.get("step_context") or "",
            deterministic_calculation=row.get("deterministic_calculation") or "",
            candidate_basis=row.get("candidate_basis") or "",
            candidate_grams=candidate_grams,
            decision_grams=decision_grams,
        )
        _validate_output_candidate(candidate)
        return candidate
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError(f"数量候选 CSV 第 {line_number} 行非法: {exc}") from exc


def _required_candidate_text(row: dict[str, str], field: str) -> str:
    value = row[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 不能为空")
    return value.strip()


def _candidate_decimal(row: dict[str, str], field: str) -> Decimal:
    try:
        value = Decimal(_required_candidate_text(row, field))
    except InvalidOperation as exc:
        raise ValueError(f"{field} 必须为十进制数") from exc
    if not _is_finite_positive_decimal(value):
        raise ValueError(f"{field} 必须为有限正数")
    return value


def _is_finite_positive_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _validate_candidate_text_fields(candidate: QuantityReviewCandidate) -> None:
    required = (
        candidate.occurrence_id,
        candidate.recipe_name,
        candidate.ingredient_name,
        candidate.normalized_form,
        candidate.raw_quantity,
        candidate.step_context,
        candidate.deterministic_calculation,
        candidate.candidate_basis,
        candidate.review_status,
    )
    optional = (
        candidate.normalized_unit,
        candidate.usage_code,
        candidate.fuzzy_token_class,
    )
    if any(not isinstance(value, str) for value in required) or any(
        value is not None and not isinstance(value, str) for value in optional
    ):
        raise ValueError("数量候选文本字段非法")


def _validate_output_candidate(candidate: QuantityReviewCandidate) -> None:
    _validate_candidate_text_fields(candidate)
    required_nonempty = (
        candidate.occurrence_id,
        candidate.recipe_name,
        candidate.ingredient_name,
        candidate.deterministic_calculation,
        candidate.candidate_basis,
    )
    if any(not value.strip() for value in required_nonempty):
        raise ValueError("数量候选必填文本不能为空")
    if type(candidate.recipe_id) is not int or candidate.recipe_id < 1:
        raise ValueError("数量候选 recipe_id 必须为正整数")
    if candidate.ingredient_id is not None and (
        type(candidate.ingredient_id) is not int or candidate.ingredient_id < 1
    ):
        raise ValueError("数量候选 ingredient_id 必须为正整数或为空")
    if candidate.usage_code is not None and candidate.usage_code not in _USAGE_CODES:
        raise ValueError("数量候选 usage_code 非法")
    if (
        candidate.fuzzy_token_class is not None
        and candidate.fuzzy_token_class not in _FUZZY_TOKEN_CLASSES
    ):
        raise ValueError("数量候选 fuzzy_token_class 非法")
    if candidate.review_status != "pending":
        raise ValueError("数量候选必须保持 pending")
    if not _is_finite_positive_decimal(candidate.candidate_grams):
        raise ValueError("候选克重必须为有限正数")
    if candidate.decision_grams is not None and not _is_finite_positive_decimal(
        candidate.decision_grams
    ):
        raise ValueError("决定克重必须为有限正数")


def _safe_csv_text(value: str | None) -> str:
    text = value or ""
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _is_formal_measure_rule_path(path: Path) -> bool:
    review_root = PROJECT_ROOT / "data" / "review"
    formal_paths = (
        review_root / "ingredient_measure_rules.csv",
        review_root / "ingredient_quantity_decisions.csv",
        review_root / "ingredient_nutrition_usage_decisions.csv",
        review_root / "ingredient_edible_fraction_rules.csv",
        review_root / "ingredient_edible_fraction_decisions.csv",
    )
    if any(path.name.casefold() == formal.name.casefold() for formal in formal_paths):
        return True
    try:
        resolved_path = path.resolve(strict=False)
        resolved = str(resolved_path).casefold()
        resolved_root = review_root.resolve(strict=False)
        if resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root):
            return True
        for formal in formal_paths:
            if resolved == str(formal.resolve(strict=False)).casefold():
                return True
            if path.exists() and formal.exists() and path.samefile(formal):
                return True
        return False
    except OSError:
        return True
