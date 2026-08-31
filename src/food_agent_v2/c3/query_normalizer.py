"""每次检索前把口语请求重写为封闭、可验证的语义计划。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from food_agent_v2.c3.llm_client import get_llm_client

_SYSTEM_PROMPT = """你只负责把用户的膳食请求重写成封闭、可验证的检索计划，不推荐菜名或
recipe_id，不判断健康安全。只输出一个 JSON 对象，字段严格限于：retrieval_query,
meal_types, population_tags, dish_types, taste_tags, cuisine_tags, scenario_tags,
include_ingredients, exclude_ingredients, health_constraints, nutrition_goal_codes,
max_time_minutes, dish_count。

规则：
1. 每个非空字段必须能在用户原话中找到证据；不得丢失、反转否定或增加用户没说的硬约束。
2. “三菜一汤”表示 dish_count=4 且 dish_types 包含“汤”；“四菜一汤”表示 5 且包含“汤”。
3. “家常”保留在 taste_tags；餐次、口味、食材和菜型用标准短词。
4. 明确“N分钟内”保留 max_time_minutes=N；“尽量快”不生成时间上限。
5. 疾病、过敏及“不能吃”原话放 health_constraints；普通食材排除同时放
   exclude_ingredients。人数不得写入 population_tags，泛词“健康”不得伪造成疾病。
6. retrieval_query 只保留有助于找菜的正向语义；禁止写入被否定的食材、疾病、过敏、
   健康指标及“不要/不能吃/过敏”等负向词。这些信息只进入结构化约束和 B4。
7. 数组字段没有证据时返回空数组；标量没有证据时返回 null。
多轮时只参考 previous_query_plan，不推测自由对话历史。"""

_MEALS = ("早餐", "早午餐", "午餐", "下午茶", "晚餐", "夜宵", "加餐")
_POPULATIONS = ("婴儿", "幼儿", "儿童", "青少年", "学生", "孕妇", "产妇", "老人")
_DISH_TYPES = (
    "主食", "主菜", "配菜", "汤", "汤羹", "粥", "面食", "点心", "小吃", "饮品", "甜品", "酱料",
)
_TASTES = (
    "家常", "清淡", "清爽", "暖胃", "补气血", "热乎", "有仪式感", "下饭",
    "酸", "甜", "辣", "麻辣", "香辣", "酸甜", "咸鲜", "鲜香", "奶香",
)
_CUISINES = (
    "家常", "中式", "西式", "川味", "粤式", "鲁式", "苏式", "浙式", "闽式",
    "湘式", "徽式", "东北", "清真", "日韩", "东南亚",
)
_SCENARIOS = ("日常", "快手", "宴客", "节日", "便当", "聚餐", "一人食", "家庭", "加餐")
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
_HEALTH_QUERY_TERMS = (
    "过敏", "不耐受", "高血压", "糖尿病", "痛风", "肾病", "心脏病", "脂肪肝",
    "血压", "血糖", "血脂", "尿酸", "胆固醇",
)
_NEGATIVE_QUERY_RE = re.compile(
    r"(?:不要|不能吃|不吃|别放|别吃|排除|忌口|过敏|不耐受|"
    r"不(?:含|放|辣|甜|咸|油|盐|糖)|无(?:糖|盐|麸质)|少(?:油|盐|糖))"
)
_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_CONTROLLED_FIELDS = {
    "meal_types": frozenset(_MEALS),
    "population_tags": frozenset(_POPULATIONS),
    "dish_types": frozenset(_DISH_TYPES),
    "taste_tags": frozenset(_TASTES),
    "cuisine_tags": frozenset(_CUISINES),
    "scenario_tags": frozenset(_SCENARIOS),
    "nutrition_goal_codes": frozenset(_NUTRITION_GOALS.values()),
}
_NON_INGREDIENT_TERMS = frozenset(
    (*_MEALS, *_DISH_TYPES, *_TASTES, *_CUISINES, *_SCENARIOS)
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
    """调用模型至多一次；任何非法输出都使用确定性保真 fallback。"""

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
        try:
            response = self._llm.invoke(
                "query_understanding",
                _SYSTEM_PROMPT,
                user_message,
                response_format={"type": "json_object"},
                timeout_seconds=timeout_seconds,
            )
            parsed = SemanticRewrite.model_validate_json(response.get("content", ""))
            if not _has_valid_semantic_filters(parsed, message):
                return fallback
            if not _is_positive_retrieval_query(parsed, fallback):
                return fallback
            return _merge_with_explicit_fallback(parsed, fallback)
        except Exception:
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
    dish_types = ("汤",) if "汤" in text else ()
    cuisines = tuple(tag for tag in _CUISINES if tag != "家常" and tag in text)
    scenarios = tuple(tag for tag in _SCENARIOS if tag in text)
    include = _extract_includes(text)
    exclude = _extract_excludes(text)
    seconds, policy = _time_constraint(text)
    max_time = seconds // 60 if seconds is not None and policy == "hard" else None
    goals = tuple(code for phrase, code in _NUTRITION_GOALS.items() if phrase in text)
    health = (text,) if _is_grounded_health_constraint(text) else ()
    positive_parts = (
        *meal_types,
        *populations,
        *dish_types,
        *tastes,
        *cuisines,
        *scenarios,
        *include,
    )
    retrieval_query = " ".join(dict.fromkeys(positive_parts))
    if not retrieval_query:
        retrieval_query = _sanitized_free_text_query(text, exclude)
    return SemanticRewrite(
        retrieval_query=retrieval_query,
        meal_types=meal_types,
        population_tags=populations,
        dish_types=dish_types,
        taste_tags=tastes,
        cuisine_tags=cuisines,
        scenario_tags=scenarios,
        include_ingredients=include,
        exclude_ingredients=exclude,
        health_constraints=health,
        nutrition_goal_codes=tuple(dict.fromkeys(goals)),
        max_time_minutes=max_time,
        dish_count=_dish_count(text),
    )


def _parse_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if value in _CN_DIGITS:
        return _CN_DIGITS[value]
    if "十" not in value:
        return None
    tens, _, ones = value.partition("十")
    tens_value = _CN_DIGITS.get(tens, 1) if tens else 1
    ones_value = _CN_DIGITS.get(ones, 0) if ones else 0
    return tens_value * 10 + ones_value


def _dish_count(message: str) -> int | None:
    match = re.search(r"([一二两三四五六七八九十\d]+)\s*菜一汤", message)
    if match:
        count = _parse_number(match.group(1))
        return count + 1 if count is not None else None
    match = re.search(r"([一二两三四五六七八九十\d]+)\s*(?:道菜|个菜|菜)", message)
    return _parse_number(match.group(1)) if match else None


def _time_constraint(message: str) -> tuple[int | None, str]:
    if "半小时" in message:
        return 1800, "hard"
    if "一刻钟" in message:
        return 900, "hard"
    match = re.search(r"(\d+)\s*分钟", message)
    if match:
        return int(match.group(1)) * 60, "hard"
    match = re.search(r"([一二两三四五六七八九十]+)\s*分钟", message)
    if match:
        minutes = _parse_number(match.group(1))
        if minutes is not None:
            return minutes * 60, "hard"
    return None, "flexible"


def _extract_includes(text: str) -> tuple[str, ...]:
    found = []
    for match in re.finditer(r"(?:想吃|想要|来点|包含|要有)([\u4e00-\u9fff]{1,8})", text):
        value = re.split(r"(?:不要|不吃|别放|并且|而且|和|，|。)", match.group(1))[0]
        for suffix in ("面条", "汤", "粥", "面"):
            if value.endswith(suffix) and len(value) > len(suffix):
                value = ""
                break
        if value in _NON_INGREDIENT_TERMS:
            continue
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


def _unique(*groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            value.strip()
            for group in groups
            for item in group
            if (value := str(item).strip())
        )
    )


def _merge_with_explicit_fallback(
    parsed: SemanticRewrite,
    fallback: SemanticRewrite,
) -> SemanticRewrite:
    return SemanticRewrite(
        retrieval_query=parsed.retrieval_query.strip(),
        meal_types=_unique(parsed.meal_types, fallback.meal_types),
        population_tags=_unique(parsed.population_tags, fallback.population_tags),
        dish_types=_unique(parsed.dish_types, fallback.dish_types),
        taste_tags=_unique(parsed.taste_tags, fallback.taste_tags),
        cuisine_tags=_unique(parsed.cuisine_tags, fallback.cuisine_tags),
        scenario_tags=_unique(parsed.scenario_tags, fallback.scenario_tags),
        include_ingredients=_unique(parsed.include_ingredients, fallback.include_ingredients),
        exclude_ingredients=_unique(parsed.exclude_ingredients, fallback.exclude_ingredients),
        health_constraints=_unique(parsed.health_constraints, fallback.health_constraints),
        nutrition_goal_codes=_unique(
            parsed.nutrition_goal_codes,
            fallback.nutrition_goal_codes,
        ),
        max_time_minutes=(
            fallback.max_time_minutes
            if fallback.max_time_minutes is not None
            else parsed.max_time_minutes
        ),
        dish_count=fallback.dish_count if fallback.dish_count is not None else parsed.dish_count,
    )


def _has_valid_semantic_filters(parsed: SemanticRewrite, message: str) -> bool:
    for field, allowed in _CONTROLLED_FIELDS.items():
        if any(value not in allowed for value in getattr(parsed, field)):
            return False
    return all(
        _is_grounded_health_constraint(value)
        and _health_constraint_matches_message(value, message)
        for value in parsed.health_constraints
    )


def _health_constraint_matches_message(value: str, message: str) -> bool:
    normalized_value = re.sub(r"\s+", "", value)
    normalized_message = re.sub(r"\s+", "", message)
    return normalized_value in normalized_message or normalized_message in normalized_value


def _is_positive_retrieval_query(
    parsed: SemanticRewrite,
    fallback: SemanticRewrite,
) -> bool:
    query = parsed.retrieval_query.strip()
    if not query or _NEGATIVE_QUERY_RE.search(query):
        return False
    excluded = _unique(parsed.exclude_ingredients, fallback.exclude_ingredients)
    if any(value in query for value in excluded):
        return False
    if any(term in query for term in _HEALTH_QUERY_TERMS):
        return False
    return re.search(r"[\u4e00-\u9fff]{1,12}病", query) is None


def _sanitized_free_text_query(text: str, exclude: Sequence[str]) -> str:
    clauses = re.split(r"[，。；;！？!?]", text)
    clean_clauses = []
    for clause in clauses:
        candidate = clause.strip()
        if not candidate or _NEGATIVE_QUERY_RE.search(candidate):
            continue
        if any(value in candidate for value in exclude):
            continue
        if any(term in candidate for term in _HEALTH_QUERY_TERMS):
            continue
        if re.search(r"[\u4e00-\u9fff]{1,12}病", candidate):
            continue
        clean_clauses.append(candidate)
    return " ".join(clean_clauses) or "家常菜"


def _is_grounded_health_constraint(value: str) -> bool:
    if "过敏" in value or "不耐受" in value:
        return True
    if re.search(r"[\u4e00-\u9fff]{1,12}病", value):
        return True
    return any(phrase in value for phrase in _EXPLICIT_DISEASE_PHRASES)
