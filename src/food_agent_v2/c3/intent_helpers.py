"""Shared intent constraint helpers for the LangGraph agent."""

import re


def _semantic_health_exclusions(
    constraints: tuple[str, ...],
    participant_refs: tuple[str, ...],
    exclude_ingredients: tuple[str, ...],
) -> tuple[str, ...]:
    if not constraints:
        return ()
    output: list[str] = []
    for constraint in constraints:
        if constraint.count(":") == 2:
            output.append(constraint)
            continue
        if len(participant_refs) != 1:
            output.append(constraint)
            continue
        participant = participant_refs[0]
        if "过敏" in constraint or "不耐受" in constraint:
            allergy = next(
                (
                    ingredient
                    for ingredient in exclude_ingredients
                    if re.search(
                        rf"{re.escape(ingredient)}\s*(?:过敏|不耐受)"
                        rf"|(?:过敏|不耐受)\s*{re.escape(ingredient)}",
                        constraint,
                    )
                ),
                None,
            )
            if allergy:
                output.append(f"{participant}:过敏:{allergy}")
                continue
            output.append(constraint)
            continue
        taboo = re.search(r"(?:不能吃|别吃)([\u4e00-\u9fff]{1,8})", constraint)
        if taboo:
            output.append(f"{participant}:禁忌:{taboo.group(1)}")
            continue
        disease = next(
            (
                item
                for item in ("糖尿病", "高血压", "痛风", "肾病", "脂肪肝")
                if item in constraint
            ),
            None,
        )
        if disease:
            output.append(f"{participant}:疾病:{disease}")
    return tuple(output)

def _stable_merge(*collections: tuple[str, ...]) -> tuple[str, ...]:
    """按输入顺序合并集合，保留每个值首次出现的位置。"""
    return tuple(dict.fromkeys(
        value for collection in collections for value in collection
    ))
