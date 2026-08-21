"""整菜模糊用量候选生成与审阅 CSV 输出。"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from food_agent_v2.b1.quantity_normalizer import normalize_quantity

_SYSTEM_PROMPT = """你是家庭烹饪食材用量估算器。请基于整道菜的名称、全部待估食材和完整步骤，
为每个 occurrence_id 估算一个大于 0 的原始食材克重。只估算输入中列出的 occurrence，
不补充新食材，不输出区间、置信度、来源或解释。输出严格 JSON：
{"grams":{"occurrence_id":克重数值}}。"""


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
    decision_grams: Decimal | None = None
    review_status: str = "pending"


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


def generate_quantity_candidates(contexts, estimator):
    candidates: list[QuantityReviewCandidate] = []
    for context in contexts:
        if not context.ingredients:
            continue
        estimated = estimator.estimate(context)
        expected_ids = {item.occurrence_id for item in context.ingredients}
        if set(estimated) != expected_ids:
            raise ValueError(f"整菜用量估算未完整覆盖 occurrence: recipe {context.recipe_id}")
        for ingredient in context.ingredients:
            grams = Decimal(estimated[ingredient.occurrence_id])
            if grams <= 0:
                raise ValueError(f"候选克重必须大于零: {ingredient.occurrence_id}")
            candidates.append(
                QuantityReviewCandidate(
                    occurrence_id=ingredient.occurrence_id,
                    recipe_id=context.recipe_id,
                    recipe_name=context.recipe_name,
                    ingredient_name=ingredient.ingredient_name,
                    raw_quantity=ingredient.quantity_raw or "",
                    step_context=context.step_context,
                    deterministic_calculation="not_available",
                    candidate_basis="whole_recipe_context_model",
                    candidate_grams=grams,
                )
            )
    return tuple(candidates)


def write_quantity_candidates(candidates, output_path: Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
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
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in candidates:
            writer.writerow(
                {
                    "occurrence_id": item.occurrence_id,
                    "recipe_id": item.recipe_id,
                    "recipe_name": item.recipe_name,
                    "ingredient_name": item.ingredient_name,
                    "raw_quantity": item.raw_quantity,
                    "step_context": item.step_context,
                    "deterministic_calculation": item.deterministic_calculation,
                    "candidate_basis": item.candidate_basis,
                    "candidate_grams": str(item.candidate_grams),
                    "decision_grams": (
                        str(item.decision_grams) if item.decision_grams is not None else ""
                    ),
                    "review_status": item.review_status,
                }
            )
