"""人工审阅输入的严格数据结构与加载边界。"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class RecipeProfileEnrichment:
    recipe_id: int
    meal_tags: tuple[str, ...] = ()
    dish_type_tags: tuple[str, ...] = ()
    taste_tags: tuple[str, ...] = ()
    cuisine_tags: tuple[str, ...] = ()
    cooking_method_tags: tuple[str, ...] = ()
    texture_tags: tuple[str, ...] = ()
    scenario_tags: tuple[str, ...] = ()
    population_tags: tuple[str, ...] = ()
    review_status: Literal["pending", "approved", "modified", "rejected"] = "pending"


@dataclass(frozen=True)
class RecipeDependency:
    parent_recipe_id: int
    dependency_recipe_id: int
    relation_type: Literal["requires_component", "uses_program", "bundle_contains"]
    review_status: Literal["pending", "approved", "modified", "rejected"] = "pending"


@dataclass(frozen=True)
class IngredientConditionDefault:
    recipe_name: str
    choice_group: str
    selected_ingredient: str
    retained_alternatives: tuple[str, ...]
    review_status: Literal["pending", "approved", "modified", "rejected"]


def load_recipe_dependencies(
    path: Path,
    *,
    known_recipe_ids: set[int],
    record_types: Mapping[int, str] | None = None,
) -> tuple[RecipeDependency, ...]:
    dependencies: list[RecipeDependency] = []
    seen: set[tuple[int, int, str]] = set()
    for line_number, raw_line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"菜品依赖文件第 {line_number} 行不是合法 JSON") from exc
        parent_recipe_id = int(payload.get("parent_recipe_id", 0))
        dependency_recipe_id = int(payload.get("dependency_recipe_id", 0))
        if parent_recipe_id not in known_recipe_ids:
            raise ValueError(f"菜品依赖引用未知 parent_recipe_id={parent_recipe_id}")
        if dependency_recipe_id not in known_recipe_ids:
            raise ValueError(f"菜品依赖引用未知 dependency_recipe_id={dependency_recipe_id}")
        dependency = RecipeDependency(
            parent_recipe_id=parent_recipe_id,
            dependency_recipe_id=dependency_recipe_id,
            relation_type=payload.get("relation_type", ""),
            review_status=payload.get("review_status", "pending"),
        )
        if dependency.relation_type not in {
            "requires_component",
            "uses_program",
            "bundle_contains",
        }:
            raise ValueError(
                f"菜品依赖 relation_type 非法: {dependency.relation_type}"
            )
        if dependency.review_status not in {
            "pending",
            "approved",
            "modified",
            "rejected",
        }:
            raise ValueError(
                f"菜品依赖 review_status 非法: {dependency.review_status}"
            )
        if record_types is not None:
            parent_type = str(record_types[parent_recipe_id])
            dependency_type = str(record_types[dependency_recipe_id])
            if (
                dependency.relation_type == "uses_program"
                and dependency_type != "cooking_program"
            ):
                raise ValueError(
                    "uses_program 只能引用 cooking_program: "
                    f"dependency_recipe_id={dependency_recipe_id}"
                )
            if dependency.relation_type == "bundle_contains" and (
                parent_type != "meal_bundle" or dependency_type != "dish"
            ):
                raise ValueError(
                    "bundle_contains 必须从 meal_bundle 指向 dish: "
                    f"parent_recipe_id={parent_recipe_id}, "
                    f"dependency_recipe_id={dependency_recipe_id}"
                )
            if (
                dependency.relation_type == "requires_component"
                and dependency_type not in {"dish", "preparation"}
            ):
                raise ValueError(
                    "requires_component 只能引用 dish 或 preparation: "
                    f"dependency_recipe_id={dependency_recipe_id}"
                )
        key = (
            dependency.parent_recipe_id,
            dependency.dependency_recipe_id,
            dependency.relation_type,
        )
        if key in seen:
            raise ValueError(
                "菜品依赖存在重复关系: "
                f"parent_recipe_id={parent_recipe_id}, "
                f"dependency_recipe_id={dependency_recipe_id}, "
                f"relation_type={dependency.relation_type}"
            )
        seen.add(key)
        if dependency.review_status in ("approved", "modified"):
            dependencies.append(dependency)
    graph: dict[int, list[int]] = {}
    for dependency in dependencies:
        graph.setdefault(dependency.parent_recipe_id, []).append(
            dependency.dependency_recipe_id
        )
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(recipe_id: int) -> None:
        if recipe_id in visiting:
            raise ValueError(f"菜品依赖存在环: recipe_id={recipe_id}")
        if recipe_id in visited:
            return
        visiting.add(recipe_id)
        for child_id in graph.get(recipe_id, ()):
            visit(child_id)
        visiting.remove(recipe_id)
        visited.add(recipe_id)

    for parent_recipe_id in graph:
        visit(parent_recipe_id)
    return tuple(dependencies)


def load_recipe_profile_enrichments(
    path: Path,
    *,
    known_recipe_ids: set[int],
) -> dict[int, RecipeProfileEnrichment]:
    records: dict[int, RecipeProfileEnrichment] = {}
    allowed_fields = set(RecipeProfileEnrichment.__dataclass_fields__)
    for line_number, raw_line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"画像审阅文件第 {line_number} 行不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"画像审阅文件第 {line_number} 行必须是对象")
        unknown = set(payload) - allowed_fields
        if unknown:
            raise ValueError(f"画像审阅文件含未知字段: {sorted(unknown)}")
        recipe_id = int(payload.get("recipe_id", 0))
        if recipe_id not in known_recipe_ids:
            raise ValueError(f"画像审阅引用未知 recipe_id={recipe_id}")
        if recipe_id in records:
            raise ValueError(f"画像审阅存在重复 recipe_id={recipe_id}")
        for field_name in allowed_fields - {"recipe_id", "review_status"}:
            payload[field_name] = tuple(payload.get(field_name) or ())
        enrichment = RecipeProfileEnrichment(**payload)
        if enrichment.review_status in ("approved", "modified"):
            records[recipe_id] = enrichment
    return records


def load_ingredient_condition_defaults(
    path: Path,
    *,
    known_recipe_names: set[str],
) -> dict[str, tuple[IngredientConditionDefault, ...]]:
    expected_header = (
        "recipe_name",
        "choice_group",
        "selected_ingredient",
        "retained_alternatives",
        "review_status",
    )
    grouped: dict[str, list[IngredientConditionDefault]] = {}
    seen: set[tuple[str, str]] = set()
    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != expected_header:
            raise ValueError("条件默认项文件表头不合法")
        for row_number, row in enumerate(reader, 2):
            recipe_name = (row.get("recipe_name") or "").strip()
            if recipe_name not in known_recipe_names:
                raise ValueError(f"条件默认项第 {row_number} 行引用未知菜品: {recipe_name}")
            choice_group = (row.get("choice_group") or "").strip()
            key = (recipe_name, choice_group)
            if key in seen:
                raise ValueError(f"条件默认项存在重复决定: {key}")
            seen.add(key)
            alternatives = tuple(
                item.strip()
                for item in (row.get("retained_alternatives") or "").split("|")
                if item.strip()
            )
            selected = (row.get("selected_ingredient") or "").strip()
            if not alternatives or selected not in alternatives:
                raise ValueError(f"条件默认项选择不在备选集合: {key}")
            status = (row.get("review_status") or "").strip()
            if status not in {"pending", "approved", "modified", "rejected"}:
                raise ValueError(f"条件默认项状态不合法: {status}")
            decision = IngredientConditionDefault(
                recipe_name=recipe_name,
                choice_group=choice_group,
                selected_ingredient=selected,
                retained_alternatives=alternatives,
                review_status=status,
            )
            if status in {"approved", "modified"}:
                grouped.setdefault(recipe_name, []).append(decision)
    return {name: tuple(items) for name, items in grouped.items()}


def apply_condition_defaults(occurrences, *, recipe_names, decisions):
    by_group: dict[tuple[int, str], list] = {}
    for occurrence in occurrences:
        if occurrence.condition_type == "one_of" and occurrence.choice_group_id is not None:
            by_group.setdefault(
                (occurrence.recipe_id, str(occurrence.choice_group_id)), []
            ).append(occurrence)

    selected_occurrence_ids: set[str] = set()
    for (recipe_id, _group_id), group in by_group.items():
        recipe_name = recipe_names.get(recipe_id)
        candidate_names = {item.name_clean for item in group}
        matching = [
            decision
            for decision in decisions.get(recipe_name, ())
            if set(decision.retained_alternatives) == candidate_names
        ]
        if len(matching) > 1:
            raise ValueError(f"菜品 {recipe_name} 的备选组匹配到多个默认决定")
        if matching:
            selected_name = matching[0].selected_ingredient
            selected_items = [item for item in group if item.name_clean == selected_name]
            if len(selected_items) != 1:
                raise ValueError(f"菜品 {recipe_name} 的默认食材无法唯一定位: {selected_name}")
            selected_occurrence_ids.add(selected_items[0].occurrence_id)

    return tuple(
        replace(
            occurrence,
            selected_for_base=occurrence.occurrence_id in selected_occurrence_ids,
        )
        if occurrence.condition_type == "one_of"
        else occurrence
        for occurrence in occurrences
    )


def write_profile_candidates(recipe_facts, output_path: Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for fact in sorted(recipe_facts, key=lambda item: item.recipe_id):
            payload = {
                "recipe_id": fact.recipe_id,
                "meal_tags": list(fact.meal_tags),
                "dish_type_tags": list(fact.dish_type_tags),
                "taste_tags": list(fact.taste_tags),
                "cuisine_tags": list(fact.cuisine_tags),
                "cooking_method_tags": list(fact.cooking_method_tags),
                "texture_tags": list(fact.texture_tags),
                "scenario_tags": list(fact.scenario_tags),
                "population_tags": list(fact.population_tags),
                "review_status": "pending",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
