"""每次检索前把口语请求重写为封闭、可验证的语义计划。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from food_agent_v2.c3.fast_intent import _dish_count, _time_constraint
from food_agent_v2.c3.llm_client import get_llm_client

_SYSTEM_PROMPT = """你只负责解释用户的膳食检索语义，不推荐菜品、不返回菜名或菜品 ID，
不判断健康 PASS/FAIL，也不能丢失或反转否定。输出严格 JSON，且只能包含：
retrieval_query, meal_types, population_tags, dish_types, taste_tags, cuisine_tags,
scenario_tags, include_ingredients, exclude_ingredients, health_constraints,
nutrition_goal_codes, max_time_minutes, dish_count。
同字段数组内是 OR；不同字段之间是 AND。普通食材排除写 exclude_ingredients；
健康限制原文写 health_constraints。多轮时只参考 previous_query_plan，不推测自由对话历史。"""

_MEALS = ("早餐", "早午餐", "午餐", "晚餐", "夜宵", "加餐")
_POPULATIONS = ("婴儿", "幼儿", "儿童", "青少年", "学生", "孕妇", "产妇", "老人")
_TASTES = ("清淡", "酸甜", "麻辣", "香辣", "鲜香", "奶香", "甜", "辣")
_NUTRITION_GOALS = {
    "高蛋白": "high_protein",
    "低钠": "low_sodium",
    "低盐": "low_sodium",
    "高纤维": "high_fiber",
    "膳食纤维": "high_fiber",
    "低脂": "low_fat",
    "高钙": "high_calcium",
    "补铁": "high_iron",
    "高铁": "high_iron",
}
_EXPLICIT_DISEASE_PHRASES = (
    "高血压",
    "糖尿病",
    "痛风",
)


class SemanticRewrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retrieval_query: str = Field(min_length=1)
    meal_types: tuple[str, ...] = ()
    population_tags: tuple[str, ...] = ()
    dish_types: tuple[str, ...] = ()
    taste_tags: tuple[str, ...] = ()
    cuisine_tags: tuple[str, ...] = ()
    scenario_tags: tuple[str, ...] = ()
    include_ingredients: tuple[str, ...] = ()
    exclude_ingredients: tuple[str, ...] = ()
    health_constraints: tuple[str, ...] = ()
    nutrition_goal_codes: tuple[str, ...] = ()
    max_time_minutes: int | None = Field(default=None, gt=0)
    dish_count: int | None = Field(default=None, gt=0)

    @field_validator(
        "meal_types",
        "population_tags",
        "dish_types",
        "taste_tags",
        "cuisine_tags",
        "scenario_tags",
        "include_ingredients",
        "exclude_ingredients",
        "health_constraints",
        "nutrition_goal_codes",
    )
    @classmethod
    def _clean_unique(cls, values):
        return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))

    @property
    def time_constraint_seconds(self) -> int | None:
        return self.max_time_minutes * 60 if self.max_time_minutes is not None else None


class QueryNormalizer:
    """最多初始调用加两次重试；仍失败时使用确定性保真 fallback。"""

    def __init__(self, llm=None) -> None:
        self._llm = llm or get_llm_client()

    def normalize(
        self,
        message: str,
        participant_refs: Sequence[str],
        previous_query_plan: Mapping | BaseModel | None = None,
        timeout_seconds: float = 3.0,
    ) -> SemanticRewrite:
        fallback = deterministic_semantic_fallback(message)
        user_message = _build_user_message(
            message,
            participant_refs,
            previous_query_plan,
        )
        for _attempt in range(3):
            try:
                response = self._llm.invoke(
                    "query_understanding",
                    _SYSTEM_PROMPT,
                    user_message,
                    response_format={"type": "json_object"},
                    timeout_seconds=timeout_seconds,
                )
                parsed = SemanticRewrite.model_validate_json(response.get("content", ""))
                if _has_valid_semantic_filters(parsed) and _preserves_explicit_semantics(parsed, fallback):
                    return parsed
            except Exception:
                continue
        return fallback


def _build_user_message(
    message: str,
    participant_refs: Sequence[str],
    previous_query_plan: Mapping | BaseModel | None,
) -> str:
    if isinstance(previous_query_plan, BaseModel):
        previous = previous_query_plan.model_dump(mode="json")
    elif previous_query_plan is None:
        previous = None
    else:
        previous = dict(previous_query_plan)
    return json.dumps(
        {
            "message": message,
            "participant_refs": list(participant_refs),
            "previous_query_plan": previous,
        },
        ensure_ascii=False,
    )


def deterministic_semantic_fallback(message: str) -> SemanticRewrite:
    text = (message or "").strip()
    meal_types = tuple(
        meal
        for meal in _MEALS
        if meal in text or (meal == "晚餐" and "晚饭" in text)
    )
    populations = tuple(tag for tag in _POPULATIONS if tag in text)
    tastes = tuple(
        taste
        for taste in _TASTES
        if taste in text and not re.search(rf"(?:不|不要|别|忌).{{0,2}}{re.escape(taste)}", text)
    )
    include = _extract_includes(text)
    exclude = _extract_excludes(text)
    seconds, policy = _time_constraint(text)
    max_time = seconds // 60 if seconds is not None and policy == "hard" else None
    goals = tuple(code for phrase, code in _NUTRITION_GOALS.items() if phrase in text)
    health = (text,) if _is_grounded_health_constraint(text) else ()
    positive_parts = (*meal_types, *populations, *tastes, *include)
    retrieval_query = " ".join(dict.fromkeys(positive_parts)) or text or "家常菜"
    return SemanticRewrite(
        retrieval_query=retrieval_query,
        meal_types=meal_types,
        population_tags=populations,
        taste_tags=tastes,
        include_ingredients=include,
        exclude_ingredients=exclude,
        health_constraints=health,
        nutrition_goal_codes=tuple(dict.fromkeys(goals)),
        max_time_minutes=max_time,
        dish_count=_dish_count(text),
    )


def _extract_includes(text: str) -> tuple[str, ...]:
    found = []
    for match in re.finditer(r"(?:想吃|想要|来点|包含|要有)([\u4e00-\u9fff]{1,8})", text):
        value = re.split(r"(?:不要|不吃|别放|并且|而且|和|，|。)", match.group(1))[0]
        if value:
            found.append(value)
    return tuple(dict.fromkeys(found))


def _extract_excludes(text: str) -> tuple[str, ...]:
    found: list[str] = []
    if any(marker in text for marker in ("不要辣", "不吃辣", "别放辣", "忌辣")):
        found.append("辣椒")
    for match in re.finditer(r"(?:不要|不吃|别放|排除)([\u4e00-\u9fff]{1,8})", text):
        value = re.split(r"(?:想吃|想要|并且|而且|和|，|。)", match.group(1))[0]
        if value == "辣":
            value = "辣椒"
        if value:
            found.append(value)
    return tuple(dict.fromkeys(found))


def _preserves_explicit_semantics(
    parsed: SemanticRewrite,
    fallback: SemanticRewrite,
) -> bool:
    required_collections = (
        "meal_types",
        "population_tags",
        "include_ingredients",
        "exclude_ingredients",
        "health_constraints",
        "nutrition_goal_codes",
    )
    if any(
        not set(getattr(fallback, field)).issubset(getattr(parsed, field))
        for field in required_collections
    ):
        return False
    if fallback.max_time_minutes is not None:
        return parsed.max_time_minutes == fallback.max_time_minutes
    if fallback.dish_count is not None:
        return parsed.dish_count == fallback.dish_count
    return True


def _has_valid_semantic_filters(parsed: SemanticRewrite) -> bool:
    if any(tag not in _POPULATIONS for tag in parsed.population_tags):
        return False
    if any(not _is_grounded_health_constraint(value) for value in parsed.health_constraints):
        return False
    return True


def _is_grounded_health_constraint(value: str) -> bool:
    if "过敏" in value or "不耐受" in value:
        return True
    if re.search(r"[\u4e00-\u9fff]{1,12}病", value):
        return True
    return any(phrase in value for phrase in _EXPLICIT_DISEASE_PHRASES)
