"""审计画像候选，生成待人工复核队列，不修改任何正式审批数据。"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from food_agent_v2.b1.consumer_views import recipe_facts_from_source
from food_agent_v2.b1.profile_review import PROFILE_VOCABULARIES
from food_agent_v2.b1.recipe_classifier import classify_all, load_overrides
from food_agent_v2.b1.source_manifest import (
    canonical_source_manifest,
    load_verified_recipe_source,
)
from food_agent_v2.core.paths import PROJECT_ROOT, RECIPES_RAW

EXPECTED_FIELDS = {
    "recipe_id",
    "meal_tags",
    "dish_type_tags",
    "taste_tags",
    "cuisine_tags",
    "cooking_method_tags",
    "texture_tags",
    "scenario_tags",
    "population_tags",
    "review_status",
}

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

ISSUES = {
    "NON_DISH_PROFILE": (
        "P0",
        "该记录不是单道菜，不应进入菜品画像审批或菜品向量库。",
        "从画像审批清单排除，不写入正式 recipe_profile_enrichment。",
    ),
    "MEAL_TAGS_4_PLUS": (
        "P0",
        "候选含 4 个及以上餐次，硬过滤时区分度过低。",
        "逐项确认并删除不构成真实推荐场景的餐次。",
    ),
    "NIGHT_SNACK_MODEL_ADDED": (
        "P0",
        "夜宵标签不在原始 label 中，由模型新增，需单独确认。",
        "仅在菜品明显适合作为夜宵时保留。",
    ),
    "REGULAR_MEAL_ON_DESSERT_DRINK": (
        "P0",
        "仅被标成点心/甜品/饮品，却带午餐或晚餐，可能被当成整餐推荐。",
        "核对原 label；必要时删除正餐标签，或由查询侧增加菜品类型约束。",
    ),
    "AFTERNOON_TEA_ON_MEAL_COMPONENT": (
        "P0",
        "仅被标成主菜/配菜/汤羹，却带下午茶，语义可能冲突。",
        "按实际食用场景确认下午茶标签。",
    ),
    "MEAL_MODEL_ONLY": (
        "P1",
        "原始 label 没有餐次，候选餐次完全由模型生成。",
        "按菜名、食材和步骤确认全部候选餐次。",
    ),
    "MEAL_ADDITION_ON_LABELED": (
        "P1",
        "原始 label 已有餐次，模型又新增餐次；当前合并逻辑只增不减。",
        "只审核新增餐次，不要把合并结果直接批量批准。",
    ),
    "TASTE_TAGS_5_PLUS": (
        "P1",
        "口味标签达到 5 个及以上，检索区分度可能过低。",
        "保留成品的主要口味，删除只描述可选调料的标签。",
    ),
    "TASTE_LIGHT_SPICY_CONFLICT": (
        "P1",
        "同时含清淡与辣/麻辣/香辣，可能来自原标签与模型合并冲突。",
        "区分成品主口味与可选蘸料后再决定。",
    ),
    "CUISINE_TAGS_3_PLUS": (
        "P2",
        "菜系标签达到 3 个及以上，可能过宽。",
        "确认是否确属融合菜，否则保留最贴切的菜系。",
    ),
    "DISH_TYPE_TAGS_3_PLUS": (
        "P2",
        "菜品类型标签达到 3 个及以上，可能过宽。",
        "保留实际参与检索筛选的主要类型。",
    ),
}

ISSUE_LABELS = {
    "NON_DISH_PROFILE": "非单道菜仍生成画像",
    "MEAL_TAGS_4_PLUS": "4个及以上餐次",
    "NIGHT_SNACK_MODEL_ADDED": "模型新增夜宵",
    "REGULAR_MEAL_ON_DESSERT_DRINK": "点心/甜品/饮品带正餐",
    "AFTERNOON_TEA_ON_MEAL_COMPONENT": "正餐组件带下午茶",
    "MEAL_MODEL_ONLY": "餐次完全由模型生成",
    "MEAL_ADDITION_ON_LABELED": "模型扩展已有餐次",
    "TASTE_TAGS_5_PLUS": "5个及以上口味",
    "TASTE_LIGHT_SPICY_CONFLICT": "清淡与辣味并存",
    "CUISINE_TAGS_3_PLUS": "3个及以上菜系",
    "DISH_TYPE_TAGS_3_PLUS": "3个及以上菜品类型",
}


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _structural_errors(candidates: list[dict], facts_by_id: dict[int, object]) -> list[str]:
    errors: list[str] = []
    ids: list[int] = []
    for line_number, candidate in enumerate(candidates, 1):
        if set(candidate) != EXPECTED_FIELDS:
            errors.append(f"line={line_number}:字段集合不合法")
            continue
        recipe_id = candidate.get("recipe_id")
        if not isinstance(recipe_id, int):
            errors.append(f"line={line_number}:recipe_id 不是整数")
            continue
        ids.append(recipe_id)
        fact = facts_by_id.get(recipe_id)
        if fact is None:
            errors.append(f"line={line_number}:未知 recipe_id={recipe_id}")
            continue
        if candidate.get("review_status") != "pending":
            errors.append(f"recipe_id={recipe_id}:候选状态不是 pending")
        for field, vocabulary in PROFILE_VOCABULARIES.items():
            values = candidate.get(field)
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                errors.append(f"recipe_id={recipe_id}:{field} 不是字符串数组")
                continue
            if len(values) != len(set(values)):
                errors.append(f"recipe_id={recipe_id}:{field} 含重复标签")
            invalid = set(values) - set(vocabulary)
            if invalid:
                errors.append(
                    f"recipe_id={recipe_id}:{field} 越出闭集 {sorted(invalid)}"
                )
        population_tags = candidate.get("population_tags")
        if not isinstance(population_tags, list) or any(
            not isinstance(value, str) for value in population_tags
        ):
            errors.append(f"recipe_id={recipe_id}:population_tags 不是字符串数组")
        elif set(population_tags) != set(fact.population_tags):
            errors.append(f"recipe_id={recipe_id}:人群标签与原始 label 不一致")
        if not candidate.get("meal_tags"):
            errors.append(f"recipe_id={recipe_id}:meal_tags 为空")
        if not candidate.get("dish_type_tags"):
            errors.append(f"recipe_id={recipe_id}:dish_type_tags 为空")
        if not set(fact.meal_tags).issubset(set(candidate.get("meal_tags") or ())):
            errors.append(f"recipe_id={recipe_id}:丢失原始餐次标签")
        if not set(fact.taste_tags).issubset(set(candidate.get("taste_tags") or ())):
            errors.append(f"recipe_id={recipe_id}:丢失原始口味标签")
    if len(ids) != len(set(ids)):
        errors.append("recipe_id 不唯一")
    expected_ids = set(facts_by_id)
    if set(ids) != expected_ids:
        errors.append("候选 recipe_id 未完整覆盖固定源")
    return errors


def _issue_codes(candidate: dict, fact, record_type: str) -> list[str]:
    if record_type != "dish":
        return ["NON_DISH_PROFILE"]

    raw_meal = set(fact.meal_tags)
    candidate_meal = set(candidate["meal_tags"])
    added_meal = candidate_meal - raw_meal
    dish_types = set(candidate["dish_type_tags"])
    tastes = set(candidate["taste_tags"])
    codes: list[str] = []

    if not raw_meal:
        codes.append("MEAL_MODEL_ONLY")
    elif added_meal:
        codes.append("MEAL_ADDITION_ON_LABELED")
    if len(candidate["meal_tags"]) >= 4:
        codes.append("MEAL_TAGS_4_PLUS")
    if "夜宵" in added_meal:
        codes.append("NIGHT_SNACK_MODEL_ADDED")
    if (
        dish_types <= {"点心", "甜品", "饮品"}
        and candidate_meal & {"午餐", "晚餐"}
    ):
        codes.append("REGULAR_MEAL_ON_DESSERT_DRINK")
    if (
        dish_types <= {"主菜", "配菜", "汤羹"}
        and "下午茶" in candidate_meal
    ):
        codes.append("AFTERNOON_TEA_ON_MEAL_COMPONENT")
    if len(candidate["taste_tags"]) >= 5:
        codes.append("TASTE_TAGS_5_PLUS")
    if "清淡" in tastes and tastes & {"辣", "麻辣", "香辣"}:
        codes.append("TASTE_LIGHT_SPICY_CONFLICT")
    if len(candidate["cuisine_tags"]) >= 3:
        codes.append("CUISINE_TAGS_3_PLUS")
    if len(candidate["dish_type_tags"]) >= 3:
        codes.append("DISH_TYPE_TAGS_3_PLUS")
    return codes


def _pipe(values) -> str:
    return "|".join(str(value) for value in values)


def _unique(values) -> list[str]:
    return list(dict.fromkeys(values))


def audit(candidate_path: Path) -> tuple[dict, list[dict]]:
    rows = tuple(
        load_verified_recipe_source(RECIPES_RAW, canonical_source_manifest())
    )
    classifications = tuple(
        classify_all(
            list(rows),
            load_overrides(
                PROJECT_ROOT / "data" / "review" / "recipe_classification_overrides.csv"
            ),
        )
    )
    facts = recipe_facts_from_source(rows, classifications)
    facts_by_id = {fact.recipe_id: fact for fact in facts}
    rows_by_id = {row.recipe_id: row for row in rows}
    type_by_id = {
        item.recipe_id: item.record_type.value for item in classifications
    }
    candidates = _read_jsonl(candidate_path)

    errors = _structural_errors(candidates, facts_by_id)
    if errors:
        preview = "；".join(errors[:10])
        raise ValueError(f"画像候选结构门禁失败，共 {len(errors)} 项：{preview}")

    queue: list[dict] = []
    issue_counts: Counter[str] = Counter()
    eligible_count = sum(value == "dish" for value in type_by_id.values())
    added_meal_assignments = 0
    eligible_without_raw_meal = 0
    eligible_with_meal_addition = 0
    raw_regular_meal_on_dessert = 0

    for candidate in candidates:
        recipe_id = candidate["recipe_id"]
        fact = facts_by_id[recipe_id]
        record_type = type_by_id[recipe_id]
        raw_meal = set(fact.meal_tags)
        added_meal = [tag for tag in candidate["meal_tags"] if tag not in raw_meal]
        if record_type == "dish":
            if not raw_meal:
                eligible_without_raw_meal += 1
            elif added_meal:
                eligible_with_meal_addition += 1
            added_meal_assignments += len(added_meal)
            if (
                set(candidate["dish_type_tags"]) <= {"点心", "甜品", "饮品"}
                and raw_meal & {"午餐", "晚餐"}
            ):
                raw_regular_meal_on_dessert += 1

        codes = _issue_codes(candidate, fact, record_type)
        if not codes:
            continue
        issue_counts.update(codes)
        priorities = [ISSUES[code][0] for code in codes]
        highest_priority = min(priorities, key=PRIORITY_ORDER.__getitem__)
        reasons = _unique(ISSUES[code][1] for code in codes)
        actions = _unique(ISSUES[code][2] for code in codes)
        queue.append(
            {
                "highest_priority": highest_priority,
                "issue_codes": _pipe(codes),
                "recipe_id": recipe_id,
                "recipe_name": rows_by_id[recipe_id].name,
                "record_type": record_type,
                "raw_meal_tags": _pipe(fact.meal_tags),
                "candidate_meal_tags": _pipe(candidate["meal_tags"]),
                "added_meal_tags": _pipe(added_meal),
                "dish_type_tags": _pipe(candidate["dish_type_tags"]),
                "taste_tags": _pipe(candidate["taste_tags"]),
                "population_tags": _pipe(candidate["population_tags"]),
                "issue_reasons": "；".join(reasons),
                "suggested_review_action": "；".join(actions),
                "candidate_status": candidate["review_status"],
                "review_decision": "",
                "review_note": "",
            }
        )

    queue.sort(
        key=lambda row: (
            PRIORITY_ORDER[row["highest_priority"]],
            int(row["recipe_id"]),
        )
    )
    priority_counts = Counter(row["highest_priority"] for row in queue)
    eligible_flagged = sum(row["record_type"] == "dish" for row in queue)
    non_dish_count = len(candidates) - eligible_count
    eligible_p0 = sum(
        row["record_type"] == "dish" and row["highest_priority"] == "P0"
        for row in queue
    )

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "candidate_file": candidate_path.as_posix(),
        "status": "review_required",
        "structural_gates": {
            "candidate_rows": len(candidates),
            "unique_recipe_ids": len({row["recipe_id"] for row in candidates}),
            "fixed_source_coverage": len(candidates),
            "closed_vocabulary_violations": 0,
            "missing_required_meal_tags": 0,
            "missing_required_dish_type_tags": 0,
            "raw_meal_tags_dropped": 0,
            "raw_taste_tags_dropped": 0,
            "population_tag_mismatches": 0,
            "non_pending_candidates": 0,
        },
        "scope": {
            "dish_records": eligible_count,
            "non_dish_records": non_dish_count,
            "flagged_unique_records": len(queue),
            "flagged_dish_records": eligible_flagged,
            "unflagged_dish_records": eligible_count - eligible_flagged,
            "p0_dish_records": eligible_p0,
        },
        "meal_provenance": {
            "dish_records_without_raw_meal_tags": eligible_without_raw_meal,
            "dish_records_with_model_additions_on_existing_meal_tags": (
                eligible_with_meal_addition
            ),
            "model_added_meal_assignments_on_dish_records": added_meal_assignments,
            "dessert_drink_records_with_raw_lunch_or_dinner": (
                raw_regular_meal_on_dessert
            ),
        },
        "priority_counts": dict(sorted(priority_counts.items())),
        "issue_counts": {
            code: issue_counts.get(code, 0) for code in ISSUES
        },
        "automatic_approvals": 0,
        "formal_review_files_modified": False,
    }
    return summary, queue


def write_queue(path: Path, queue: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(queue[0]) if queue else [
        "highest_priority",
        "issue_codes",
        "recipe_id",
        "recipe_name",
        "review_decision",
        "review_note",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(queue)


def write_issue_counts(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "issue_code": code,
            "category": ISSUE_LABELS[code],
            "priority": ISSUES[code][0],
            "scope": "非单道菜" if code == "NON_DISH_PROFILE" else "单道菜",
            "count": count,
            "overlap_allowed": "是",
        }
        for code, count in summary["issue_counts"].items()
    ]
    rows.sort(key=lambda row: (-int(row["count"]), str(row["issue_code"])))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_report(path: Path, summary: dict, queue_path: Path) -> None:
    scope = summary["scope"]
    meal = summary["meal_provenance"]
    issue = summary["issue_counts"]
    priority = summary["priority_counts"]
    text = f"""# 菜品画像候选全量审计

## Executive Summary

- **结构门禁通过，但不适合整批批准。** 2,000 条候选完整、唯一、均处于 `pending`，闭集、必填项、原始餐次/口味继承和人群标签约束均未发现结构错误。
- **当前餐次误推荐的主要原因是单向合并。** 画像生成把原始 `label` 餐次与模型结果做并集，能补标签但不能删除错误标签；共有 {meal['dessert_drink_records_with_raw_lunch_or_dinner']} 条仅为点心/甜品/饮品的菜品，其午餐或晚餐直接来自原始 `label`。
- **先处理 {scope['p0_dish_records']} 条 P0 菜品，再审模型补全。** P0 是餐次过宽、夜宵新增、甜品/饮品正餐冲突或下午茶与正餐组件冲突的去重菜品数；此外 {scope['non_dish_records']} 条非单道菜记录应从画像审批范围排除。
- **本次没有自动批准或改写正式数据。** 待审队列共 {scope['flagged_unique_records']} 条，其中单道菜 {scope['flagged_dish_records']} 条；`review_decision` 与 `review_note` 均留空。

## 数据是否完整可信

| 检查项 | 结果 | 判断 |
|---|---:|---|
| 候选行数 / 唯一 recipe_id | 2,000 / 2,000 | 通过 |
| 单道菜 / 非单道菜 | {scope['dish_records']} / {scope['non_dish_records']} | 非单道菜不应进入菜品画像审批 |
| 闭集越界、餐次空、菜品类型空 | 0 / 0 / 0 | 通过 |
| 原始餐次丢失 / 原始口味丢失 | 0 / 0 | 通过 |
| 人群标签与原始 label 不一致 | 0 | 通过 |
| 自动批准 / 正式审批文件修改 | 0 / 否 | 通过 |

结构正确只说明文件可读取、标签没有越界，并不等于餐次语义已经正确。

## 餐次问题集中在原标签和只增不减的合并规则

| 待审类别（可重叠） | 条数 | 处理含义 |
|---|---:|---|
| 非单道菜仍生成画像 | {issue['NON_DISH_PROFILE']} | 从画像审批范围排除 |
| 原 label 无餐次、完全由模型生成 | {issue['MEAL_MODEL_ONLY']} | 全量核对候选餐次 |
| 原 label 有餐次、模型继续新增 | {issue['MEAL_ADDITION_ON_LABELED']} | 只审新增值，不能直接并集批准 |
| 4 个及以上餐次 | {issue['MEAL_TAGS_4_PLUS']} | 区分度过低，优先收窄 |
| 模型新增夜宵 | {issue['NIGHT_SNACK_MODEL_ADDED']} | 单独逐条确认 |
| 点心/甜品/饮品带午餐或晚餐 | {issue['REGULAR_MEAL_ON_DESSERT_DRINK']} | 防止被当作整餐推荐 |
| 主菜/配菜/汤羹带下午茶 | {issue['AFTERNOON_TEA_ON_MEAL_COMPONENT']} | 核对真实场景 |

在 {scope['dish_records']} 条单道菜中，{meal['dish_records_without_raw_meal_tags']} 条没有原始餐次，{meal['dish_records_with_model_additions_on_existing_meal_tags']} 条在已有原始餐次上又被模型扩展；模型共新增 {meal['model_added_meal_assignments_on_dish_records']} 个餐次值。原始标签确实进入了候选，但当前数据结构无法表达“保留原始 label 文本，同时删除某个错误的结构化餐次”。

典型例子包括：`秋梨膏`、`焦糖奶茶冰淇淋`、`水果乳酪派` 的原始标签均含晚餐；`银耳蛋汤` 在午餐/晚餐基础上又被模型加入早餐和夜宵；`乳化` 属于烹饪程序，却仍生成了四个餐次画像。这些都说明不能按当前并集结果整批落库。

## 其他画像字段可后置复核

| 待审类别（可重叠） | 条数 | 优先级 |
|---|---:|---|
| 口味达到 5 个及以上 | {issue['TASTE_TAGS_5_PLUS']} | P1 |
| 同时含清淡与辣/麻辣/香辣 | {issue['TASTE_LIGHT_SPICY_CONFLICT']} | P1 |
| 菜系达到 3 个及以上 | {issue['CUISINE_TAGS_3_PLUS']} | P2 |
| 菜品类型达到 3 个及以上 | {issue['DISH_TYPE_TAGS_3_PLUS']} | P2 |

这些项会影响语义检索的区分度，但不会像餐次错误那样直接造成“早餐/晚餐推荐错菜”，因此可以在餐次 P0 之后处理。

## 建议的审批顺序

1. 先确认排除 {scope['non_dish_records']} 条非单道菜画像，不写入正式画像审批文件。
2. 审 {scope['p0_dish_records']} 条 P0 单道菜；决定餐次时采用替换后的最终集合，而不是在原始标签上继续做并集。
3. 再审原 label 无餐次的 {meal['dish_records_without_raw_meal_tags']} 条，以及在原餐次上新增标签的 {meal['dish_records_with_model_additions_on_existing_meal_tags']} 条。
4. 对其余未命中规则的 {scope['unflagged_dish_records']} 条按餐次、菜品类型分层抽样；抽样通过后再讨论分批批准，仍不自动批准。
5. 批准前修改正式画像加载语义：原始 `label_tags` 继续完整进入向量 payload；经审定的 `meal_tags` 等结构化画像字段作为最终值覆盖对应原始结构化切面。

## 仍需确认的问题

- “晚餐”是指可作为晚餐中的任一组成，还是可被直接推荐为一项晚餐选择？当前单一 `meal_tags` 无法区分这两种语义。
- 通用“推荐晚餐”是否默认只接受主食、主菜、配菜和汤羹，而甜品、饮品需要用户明确提出后才召回？建议答案为“是”，并在查询重写后的 RAG 硬过滤中体现。
- 非单道菜中的套餐是否未来需要独立检索入口？如果需要，应使用独立 `record_type` 检索策略，不复用单道菜画像审批。

## Caveats and Assumptions

- 结构门禁结论是确定性的；语义问题清单是保守的规则筛查，用于缩小人工复核范围，不代表每条都已判错。
- 没有可作为绝对真值的餐次标注集，因此本次不对 {scope['unflagged_dish_records']} 条未命中规则的单道菜做自动正确性背书。
- 各问题类别存在重叠，优先级计数按每个 recipe_id 的最高优先级去重：P0={priority.get('P0', 0)}、P1={priority.get('P1', 0)}、P2={priority.get('P2', 0)}。

待审明细：`{queue_path.as_posix()}`
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--candidates",
        type=Path,
        default=PROJECT_ROOT / "reports" / "data_review" / "profile_candidates.jsonl",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=(
            PROJECT_ROOT
            / "reports"
            / "data_review"
            / "profile_candidate_audit_summary.json"
        ),
    )
    parser.add_argument(
        "--queue",
        type=Path,
        default=(
            PROJECT_ROOT
            / "reports"
            / "data_review"
            / "profile_candidate_review_queue.csv"
        ),
    )
    parser.add_argument(
        "--issue-counts",
        type=Path,
        default=(
            PROJECT_ROOT
            / "reports"
            / "data_review"
            / "profile_candidate_audit_issue_counts.csv"
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=(
            PROJECT_ROOT
            / "reports"
            / "data_review"
            / "profile_candidate_audit.md"
        ),
    )
    args = parser.parse_args()

    summary, queue = audit(args.candidates)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_queue(args.queue, queue)
    write_issue_counts(args.issue_counts, summary)
    write_report(args.report, summary, args.queue)
    print(
        json.dumps(
            {
                "status": summary["status"],
                "flagged_unique_records": len(queue),
                "summary": str(args.summary),
                "queue": str(args.queue),
                "issue_counts": str(args.issue_counts),
                "report": str(args.report),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
