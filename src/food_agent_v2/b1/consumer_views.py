"""从同一批 B1 结构化事实投影下游消费者视图。

本模块不读取原始文件，也不解析食材字符串。调用方必须先完成菜品分类、
步骤分段、食材 occurrence 解析和身份冻结，再把同一 build 的事实传入。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID

from food_agent_v2.b1.nutrition_occurrence_rules import (
    FuzzyTokenClass,
    NutritionRetentionDecisionIndex,
    NutritionUsageDecisionIndex,
    UsageCode,
    classify_fuzzy_token,
    derive_nutrition_retention,
    derive_nutrition_usage,
)
from food_agent_v2.b1.review_inputs import RecipeProfileEnrichment
from food_agent_v2.b1.schemas import RecipeClassification, RecordType, SourceRecipeRow

CatalogEligibility = Literal["eligible", "ineligible"]
ConsumptionRole = Literal["edible", "non_edible"]
QuantityStatus = Literal["explicit", "range", "unknown"]
ConditionType = Literal["required", "optional", "one_of"]

_ALLOWED_SEARCH_FIELDS = {
    "meal",
    "taste",
    "cuisine",
    "cooking_method",
    "dish_type",
    "temperature",
    "texture",
    "occasion",
}

_MEAL_TAGS = {"早餐", "早午餐", "午餐", "下午茶", "晚餐", "夜宵"}
_POPULATION_TAGS = {
    "婴儿", "幼儿", "儿童", "青少年", "学生", "孕妇", "产妇", "老人", "老年人"
}
_TASTE_TAGS = {
    "清淡", "酸", "甜", "辣", "麻辣", "香辣", "酸甜", "咸鲜", "鲜香", "奶香"
}
_LABEL_SPLIT_RE = re.compile(r"[、,，;；|/]+")
_NON_INGREDIENT_OCCURRENCE_RE = re.compile(r"^(?:改|划(?:十字)?)花刀$")


@dataclass(frozen=True)
class BuildIdentity:
    build_id: UUID
    source_manifest_hash: str


@dataclass(frozen=True)
class RecipeFact:
    recipe_id: int
    name: str
    record_type: RecordType | str
    step_segments: tuple[str, ...] = ()
    searchable_fields: Mapping[str, str] = field(default_factory=dict)
    source_row_sha256: str = ""
    label_tags: tuple[str, ...] = ()
    meal_tags: tuple[str, ...] = ()
    population_tags: tuple[str, ...] = ()
    dish_type_tags: tuple[str, ...] = ()
    taste_tags: tuple[str, ...] = ()
    cuisine_tags: tuple[str, ...] = ()
    cooking_method_tags: tuple[str, ...] = ()
    texture_tags: tuple[str, ...] = ()
    scenario_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class IngredientOccurrenceFact:
    occurrence_id: str
    recipe_id: int
    source_fragment: str
    name_clean: str
    ingredient_id: int | None
    consumption_role: ConsumptionRole | str
    quantity_raw: str | None = None
    unit_raw: str | None = None
    form: str | None = None
    is_optional: bool = False
    choice_group_id: str | None = None
    condition_type: ConditionType = "required"
    selected_for_base: bool = True
    is_process_material: bool = False
    added_from_step: bool = False


@dataclass(frozen=True)
class IngredientIdentityFact:
    ingredient_id: int
    name_canonical: str
    family_id: int
    aliases: tuple[str, ...] = ()
    category: str = ""


@dataclass(frozen=True)
class RecipeHealthIngredientRelation:
    occurrence_id: str
    ingredient_id: int
    condition_type: ConditionType
    choice_group_id: str | None
    is_default_choice: bool
    is_process_material: bool


@dataclass(frozen=True)
class RecipeHealthIngredientView:
    build_id: UUID
    source_manifest_hash: str
    recipe_id: int
    catalog_eligibility: CatalogEligibility
    ingredient_ids: tuple[int, ...]
    ingredient_relations: tuple[RecipeHealthIngredientRelation, ...]
    ingredient_evidence_paths: tuple[str, ...]
    unresolved_occurrence_count: int
    composition_expansion_status: Literal["atomic", "complete", "invalid"]


@dataclass(frozen=True)
class StructuredStep:
    step_index: int
    raw_text: str
    bound_occurrence_ids: tuple[str, ...] = ()
    bound_ingredient_ids: tuple[int, ...] = ()
    binding_methods: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecipeStepBindingView:
    build_id: UUID
    source_manifest_hash: str
    recipe_id: int
    ingredient_ids: tuple[int, ...]
    steps: tuple[StructuredStep, ...]


@dataclass(frozen=True)
class NutritionOccurrenceInput:
    occurrence_id: str
    ingredient_id: int
    quantity_raw: str | None
    unit_raw: str | None
    quantity_status: QuantityStatus
    ingredient_name: str = ""
    form: str | None = None
    normalized_form: str = ""
    usage_code: UsageCode | None = None
    fuzzy_token_class: FuzzyTokenClass | None = None
    retained_in_dish: bool | None = None
    usage_requires_review: bool = False
    retention_requires_review: bool = False
    requires_review: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "requires_review",
            self.usage_requires_review or self.retention_requires_review,
        )


@dataclass(frozen=True)
class RecipeNutritionInputView:
    build_id: UUID
    source_manifest_hash: str
    recipe_id: int
    ingredients: tuple[NutritionOccurrenceInput, ...]

    @property
    def ingredient_ids(self) -> tuple[int, ...]:
        return _unique(item.ingredient_id for item in self.ingredients)


@dataclass(frozen=True)
class RecipeRetrievalBuildView:
    build_id: UUID
    source_manifest_hash: str
    recipe_id: int
    catalog_eligibility: Literal["eligible"]
    name: str
    ingredient_ids: tuple[int, ...]
    ingredient_display_names: tuple[str, ...]
    ingredient_family_ids: tuple[int, ...]
    searchable_fields: Mapping[str, str]
    step_summary_input: tuple[str, ...]
    source_row_sha256: str = ""
    label_tags: tuple[str, ...] = ()
    meal_tags: tuple[str, ...] = ()
    population_tags: tuple[str, ...] = ()
    dish_type_tags: tuple[str, ...] = ()
    taste_tags: tuple[str, ...] = ()
    cuisine_tags: tuple[str, ...] = ()
    cooking_method_tags: tuple[str, ...] = ()
    texture_tags: tuple[str, ...] = ()
    scenario_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConsumerViewSet:
    build: BuildIdentity
    health_views: tuple[RecipeHealthIngredientView, ...]
    step_views: tuple[RecipeStepBindingView, ...]
    nutrition_views: tuple[RecipeNutritionInputView, ...]
    retrieval_views: tuple[RecipeRetrievalBuildView, ...]

    @property
    def recipe_ids(self) -> tuple[int, ...]:
        return tuple(view.recipe_id for view in self.health_views)


def recipe_facts_from_source(
    rows: tuple[SourceRecipeRow, ...],
    classifications: tuple[RecipeClassification, ...],
    profile_enrichments: Mapping[int, RecipeProfileEnrichment] | None = None,
) -> tuple[RecipeFact, ...]:
    """B1 原始步骤的唯一适配边界：分类后分段一次，再发布结构化事实。"""
    from food_agent_v2.b1.step_time_builder import split_steps

    classification_by_id = _unique_by(classifications, "recipe_id")
    enrichments = dict(profile_enrichments or {})
    if {row.recipe_id for row in rows} != set(classification_by_id):
        raise ValueError("菜品源与分类 recipe_id 集合不一致")
    if set(enrichments) - {row.recipe_id for row in rows}:
        raise ValueError("画像补全引用未知 recipe_id")

    facts: list[RecipeFact] = []
    for row in rows:
        label_tags = _split_label_tags(row.labels_raw)
        enrichment = enrichments.get(row.recipe_id)
        raw_population_tags = tuple(tag for tag in label_tags if tag in _POPULATION_TAGS)
        if enrichment is not None:
            invented_sensitive = set(enrichment.population_tags) - set(raw_population_tags)
            if invented_sensitive:
                raise ValueError(
                    f"recipe_id={row.recipe_id} 画像补全含未经原始 label 支持的敏感标签: "
                    f"{sorted(invented_sensitive)}"
                )
        meal_tags = _merge_tags(
            (tag for tag in label_tags if tag in _MEAL_TAGS),
            enrichment.meal_tags if enrichment else (),
        )
        population_tags = _merge_tags(raw_population_tags, ())
        taste_tags = _merge_tags(
            (tag for tag in label_tags if tag in _TASTE_TAGS),
            enrichment.taste_tags if enrichment else (),
        )
        dish_type_tags = tuple(enrichment.dish_type_tags) if enrichment else ()
        cuisine_tags = tuple(enrichment.cuisine_tags) if enrichment else ()
        cooking_method_tags = tuple(enrichment.cooking_method_tags) if enrichment else ()
        texture_tags = tuple(enrichment.texture_tags) if enrichment else ()
        scenario_tags = tuple(enrichment.scenario_tags) if enrichment else ()
        searchable_fields = {
            key: " ".join(values)
            for key, values in {
                "meal": meal_tags,
                "taste": taste_tags,
                "cuisine": cuisine_tags,
                "cooking_method": cooking_method_tags,
                "dish_type": dish_type_tags,
                "texture": texture_tags,
                "occasion": scenario_tags,
            }.items()
            if values
        }
        facts.append(
            RecipeFact(
            recipe_id=row.recipe_id,
            name=row.name,
            record_type=classification_by_id[row.recipe_id].record_type,
            step_segments=tuple(split_steps(row.steps_raw)),
            searchable_fields=searchable_fields,
            source_row_sha256=row.row_sha256,
            label_tags=label_tags,
            meal_tags=meal_tags,
            population_tags=population_tags,
            dish_type_tags=dish_type_tags,
            taste_tags=taste_tags,
            cuisine_tags=cuisine_tags,
            cooking_method_tags=cooking_method_tags,
            texture_tags=texture_tags,
            scenario_tags=scenario_tags,
        )
        )
    return tuple(facts)


def identity_facts_from_records(
    registry_records: tuple[dict, ...],
    alias_records: tuple[dict, ...] = (),
) -> tuple[IngredientIdentityFact, ...]:
    aliases_by_id: dict[int, list[str]] = {}
    for record in alias_records:
        aliases_by_id.setdefault(int(record["ingredient_id"]), []).append(record["alias"])
    return tuple(
        IngredientIdentityFact(
            ingredient_id=int(record["ingredient_id"]),
            name_canonical=record["name_canonical"],
            family_id=int(record["family_id"]),
            aliases=tuple(sorted(set(aliases_by_id.get(int(record["ingredient_id"]), [])))),
            category=record.get("category", ""),
        )
        for record in registry_records
    )


def occurrence_facts_from_records(
    occurrence_records: tuple[dict, ...],
) -> tuple[IngredientOccurrenceFact, ...]:
    facts: list[IngredientOccurrenceFact] = []
    for record in occurrence_records:
        choice_group_id = record.get("choice_group_id")
        is_optional = bool(record.get("is_optional", False))
        condition_type: ConditionType = (
            "one_of" if choice_group_id is not None else ("optional" if is_optional else "required")
        )
        is_processing_instruction = bool(
            _NON_INGREDIENT_OCCURRENCE_RE.fullmatch(record["name_clean"])
        )
        facts.append(
            IngredientOccurrenceFact(
            occurrence_id=record["occurrence_id"],
            recipe_id=int(record["recipe_id"]),
            source_fragment=record["source_fragment"],
            name_clean=record["name_clean"],
            ingredient_id=(
                int(record["resolved_ingredient_id"])
                if record.get("resolved_ingredient_id") is not None
                else None
            ),
            consumption_role=(
                "non_edible" if is_processing_instruction else record["consumption_role"]
            ),
            quantity_raw=record.get("quantity_raw"),
            unit_raw=record.get("unit_raw"),
            form=record.get("form"),
            is_optional=is_optional,
            choice_group_id=str(choice_group_id) if choice_group_id is not None else None,
            condition_type=condition_type,
            selected_for_base=bool(record.get("is_default_choice", True)),
            is_process_material=(
                is_processing_instruction or bool(record.get("is_process_material", False))
            ),
            added_from_step=bool(record.get("added_from_step", False)),
        )
        )
    return tuple(facts)


def publish_consumer_views(views: ConsumerViewSet, staging_dir: Path) -> dict:
    """把四类同构建视图写入隔离 staging，并输出交叉一致性报告。"""
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "recipe_health_views.jsonl": views.health_views,
        "recipe_step_binding_views.jsonl": views.step_views,
        "recipe_nutrition_input_views.jsonl": views.nutrition_views,
        "recipe_retrieval_build_views.jsonl": views.retrieval_views,
    }
    build_identity_errors = [
        {"artifact": name, "recipe_id": record.recipe_id}
        for name, records in artifacts.items()
        for record in records
        if record.build_id != views.build.build_id
        or record.source_manifest_hash != views.build.source_manifest_hash
    ]
    if build_identity_errors:
        raise ValueError(f"消费者视图构建身份不一致: {build_identity_errors[:5]}")

    consistency_errors = []
    recipe_sets = {tuple(view.recipe_id for view in records) for records in artifacts.values()}
    if len(recipe_sets) != 1:
        raise ValueError("消费者视图 recipe_id 集合不一致")
    health_by_id = {view.recipe_id: view for view in views.health_views}
    step_by_id = {view.recipe_id: view for view in views.step_views}
    nutrition_by_id = {view.recipe_id: view for view in views.nutrition_views}
    retrieval_by_id = {view.recipe_id: view for view in views.retrieval_views}
    for recipe_id in views.recipe_ids:
        health = health_by_id[recipe_id]
        health_ids = health.ingredient_ids
        expected_nutrition = _unique(
            relation.ingredient_id
            for relation in health.ingredient_relations
            if relation.condition_type != "optional"
            and relation.is_default_choice
            and not relation.is_process_material
        )
        observed = {
            "health": health_ids,
            "step": step_by_id[recipe_id].ingredient_ids,
            "nutrition": nutrition_by_id[recipe_id].ingredient_ids,
            "retrieval": retrieval_by_id[recipe_id].ingredient_ids,
        }
        retrieval_ids = observed["retrieval"]
        valid = (
            observed["step"] == retrieval_ids
            and set(health_ids).issubset(retrieval_ids)
            and observed["nutrition"] == expected_nutrition
        )
        if not valid:
            consistency_errors.append({"recipe_id": recipe_id, "ingredient_ids": observed})
    if consistency_errors:
        raise ValueError(f"消费者视图食材身份不一致: {consistency_errors[:5]}")

    for name, records in artifacts.items():
        with (staging / name).open("w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(
                    json.dumps(
                        asdict(record),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    + "\n"
                )

    report = {
        "status": "passed" if not consistency_errors else "failed",
        "build_id": str(views.build.build_id),
        "source_manifest_hash": views.build.source_manifest_hash,
        "eligible_recipe_count": len(views.recipe_ids),
        "view_counts": {name: len(records) for name, records in artifacts.items()},
        "ingredient_consistency_error_count": len(consistency_errors),
        "ingredient_consistency_errors": consistency_errors,
    }
    (staging / "consumer_projection_quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report


def publish_downstream_build_views(
    views: ConsumerViewSet,
    identities: tuple[IngredientIdentityFact, ...],
    food_composition_records: tuple[dict, ...],
    staging_dir: Path,
    *,
    time_graph_cache=None,
    time_graph_generator_model_id: str | None = None,
    time_graph_verifier_model_id: str | None = None,
    edible_fraction_decisions=None,
) -> dict:
    """运行只接收结构化视图的 B5/B6/C1 构建器并发布派生产物。"""
    from food_agent_v2.b1.nutrition_reference import (
        load_nutrition_crosswalk,
        load_nutrition_references,
    )
    from food_agent_v2.b1.quantity_normalizer import (
        load_edible_fraction_rules,
        load_measure_rules,
        load_quantity_decisions,
    )
    from food_agent_v2.b1.rag_document_builder import build_rag_documents_from_views
    from food_agent_v2.b1.step_time_builder import build_step_profiles_from_views
    from food_agent_v2.b1.time_graph_profiler import (
        PublishedRecipeTimeProfile,
        TimeGraphCache,
        load_cached_recipe_time_graph,
    )
    from food_agent_v2.b1.time_review_decisions import load_time_review_decisions
    from food_agent_v2.b5.scheduler import schedule_task_graphs
    from food_agent_v2.core.config import load_config
    from food_agent_v2.core.paths import PROJECT_ROOT

    del identities, food_composition_records
    staging = Path(staging_dir)
    review_dir = PROJECT_ROOT / "data" / "review"
    time_decisions = load_time_review_decisions(
        review_dir / "recipe_time_graph_decisions.csv"
    )
    atom_profiles, atom_report = build_step_profiles_from_views(
        views.step_views,
        time_decisions=time_decisions,
    )
    cache = (
        time_graph_cache
        if time_graph_cache is not None
        else TimeGraphCache(PROJECT_ROOT / "data" / "cache" / "recipe_time_graphs.jsonl")
    )
    configured_model_id = (
        load_config().llm.model_for_role("unified_review") or "unconfigured"
    )
    generator_model_id = time_graph_generator_model_id or configured_model_id
    verifier_model_id = time_graph_verifier_model_id or configured_model_id
    names = {view.recipe_id: view.name for view in views.retrieval_views}
    step_profiles = []
    for atom_profile in atom_profiles:
        cached = load_cached_recipe_time_graph(
            recipe_id=atom_profile.recipe_id,
            recipe_name=names[atom_profile.recipe_id],
            atoms=atom_profile.atoms,
            generator_model_id=generator_model_id,
            verifier_model_id=verifier_model_id,
            cache=cache,
        )
        scheduled = schedule_task_graphs(
            {cached.recipe_id: cached.step_tasks}
        )
        step_profiles.append(
            PublishedRecipeTimeProfile(
                build_id=atom_profile.build_id,
                source_manifest_hash=atom_profile.source_manifest_hash,
                recipe_id=cached.recipe_id,
                recipe_name=cached.recipe_name,
                active_seconds=scheduled.active_seconds,
                estimated_elapsed_seconds=scheduled.estimated_makespan_seconds,
                step_tasks=cached.step_tasks,
            )
        )
    step_report = {
        **atom_report,
        "stage": "validated_recipe_time_graphs",
        "ready_count": len(step_profiles),
    }
    nutrition_references = load_nutrition_references(
        PROJECT_ROOT / "data" / "reference" / "ingredient_nutrition.jsonl"
    )
    measure_rules = load_measure_rules(review_dir / "ingredient_measure_rules.csv")
    quantity_decisions = load_quantity_decisions(
        review_dir / "ingredient_quantity_decisions.csv"
    )
    edible_fractions = load_edible_fraction_rules(
        review_dir / "ingredient_edible_fraction_rules.csv"
    )
    nutrition_crosswalk = load_nutrition_crosswalk(
        review_dir / "ingredient_nutrition_crosswalk.jsonl",
        nutrition_references,
    )
    nutrition_features, nutrition_report = _build_nutrition_features_with_decisions(
        views.nutrition_views,
        measure_rules=measure_rules,
        quantity_decisions=quantity_decisions,
        edible_fractions=edible_fractions,
        edible_fraction_decisions=edible_fraction_decisions,
        nutrition_crosswalk=nutrition_crosswalk,
    )
    rag_documents, rag_report = build_rag_documents_from_views(views.retrieval_views)

    base_report = publish_consumer_views(views, staging)
    _write_models(staging / "step_time_profiles.jsonl", step_profiles)
    _write_models(staging / "nutrition_reference_views.jsonl", nutrition_features)
    _write_models(staging / "rag_documents.jsonl", rag_documents)

    expected = len(views.recipe_ids)
    derived_counts = {
        "step_time_profiles.jsonl": len(step_profiles),
        "nutrition_reference_views.jsonl": len(nutrition_features),
        "rag_documents.jsonl": len(rag_documents),
    }
    status = (
        "passed"
        if base_report["status"] == "passed" and set(derived_counts.values()) == {expected}
        else "failed"
    )
    report = {
        "status": status,
        "build_id": str(views.build.build_id),
        "source_manifest_hash": views.build.source_manifest_hash,
        "eligible_recipe_count": expected,
        "base_projection": base_report,
        "derived_counts": derived_counts,
        "step_time": step_report,
        "nutrition": nutrition_report,
        "rag": rag_report,
    }
    (staging / "downstream_build_quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    if status != "passed":
        raise ValueError("下游构建产物数量或构建身份不一致")
    return report

def _build_nutrition_features_with_decisions(
    views,
    *,
    measure_rules,
    quantity_decisions,
    edible_fractions,
    edible_fraction_decisions=None,
    nutrition_crosswalk,
):
    from food_agent_v2.b1.edible_fraction_review import EdibleFractionDecisionIndex
    from food_agent_v2.b1.nutrition_calculator import calculate_raw_recipe_nutrition

    decisions = edible_fraction_decisions or EdibleFractionDecisionIndex(())

    features = [
        calculate_raw_recipe_nutrition(
            view,
            measure_rules=measure_rules,
            quantity_decisions=quantity_decisions,
            edible_fractions=edible_fractions,
            edible_fraction_decisions=decisions,
            nutrition_crosswalk=nutrition_crosswalk,
        )
        for view in views
    ]
    available_count = sum(item.available for item in features)
    reason_counts = Counter(
        item.reason for item in features if not item.available and item.reason is not None
    )
    return features, {
        "stage": "raw_recipe_nutrition",
        "status": "passed",
        "total_recipes": len(features),
        "available_count": available_count,
        "unavailable_count": len(features) - available_count,
        "reason_counts": dict(sorted(reason_counts.items())),
    }



def _write_models(path: Path, records: list) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    record.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )


def build_consumer_views(
    *,
    build: BuildIdentity,
    recipes: tuple[RecipeFact, ...],
    occurrences: tuple[IngredientOccurrenceFact, ...],
    identities: tuple[IngredientIdentityFact, ...],
    nutrition_usage_decisions: NutritionUsageDecisionIndex | None = None,
    nutrition_retention_decisions: NutritionRetentionDecisionIndex | None = None,
) -> ConsumerViewSet:
    """投影同构建的 B4/B5/B6/C1 视图；仅发布 eligible dish。"""
    _validate_build_identity(build)
    recipe_by_id = _unique_by(recipes, "recipe_id")
    identity_by_id = _unique_by(identities, "ingredient_id")
    usage_decisions = nutrition_usage_decisions or NutritionUsageDecisionIndex(())
    retention_decisions = (
        nutrition_retention_decisions or NutritionRetentionDecisionIndex(())
    )
    _unique_by(occurrences, "occurrence_id")

    occurrences_by_recipe: dict[int, list[IngredientOccurrenceFact]] = {
        recipe_id: [] for recipe_id in recipe_by_id
    }
    for occurrence in occurrences:
        if occurrence.recipe_id not in recipe_by_id:
            raise ValueError(f"occurrence 引用未知 recipe_id={occurrence.recipe_id}")
        if occurrence.ingredient_id is not None and occurrence.ingredient_id not in identity_by_id:
            raise ValueError(f"occurrence 引用未知 ingredient_id={occurrence.ingredient_id}")
        occurrences_by_recipe[occurrence.recipe_id].append(occurrence)

    health_views: list[RecipeHealthIngredientView] = []
    step_views: list[RecipeStepBindingView] = []
    nutrition_views: list[RecipeNutritionInputView] = []
    retrieval_views: list[RecipeRetrievalBuildView] = []

    for recipe_id in sorted(recipe_by_id):
        recipe = recipe_by_id[recipe_id]
        recipe_occurrences = occurrences_by_recipe[recipe_id]
        unresolved = sum(
            item.consumption_role == "edible" and item.ingredient_id is None
            for item in recipe_occurrences
        )
        edible = [
            item
            for item in recipe_occurrences
            if item.consumption_role == "edible" and item.ingredient_id is not None
        ]
        retrieval_ids = _unique(
            item.ingredient_id for item in edible if item.ingredient_id is not None
        )
        health_occurrences = [
            item
            for item in edible
            if item.condition_type != "one_of" or item.selected_for_base
        ]
        nutrition_occurrences = [
            item
            for item in health_occurrences
            if item.condition_type != "optional"
            and item.selected_for_base
            and not item.is_process_material
        ]
        health_ids = _unique(
            item.ingredient_id for item in health_occurrences if item.ingredient_id is not None
        )
        record_type = (
            recipe.record_type.value
            if isinstance(recipe.record_type, RecordType)
            else recipe.record_type
        )
        eligible = record_type == RecordType.DISH.value and unresolved == 0 and bool(retrieval_ids)
        if not eligible:
            continue

        evidence_paths = tuple(
            f"recipe:{recipe_id}/occurrence:{item.occurrence_id}/ingredient:{item.ingredient_id}"
            for item in health_occurrences
        )
        health_views.append(
            RecipeHealthIngredientView(
                build_id=build.build_id,
                source_manifest_hash=build.source_manifest_hash,
                recipe_id=recipe_id,
                catalog_eligibility="eligible",
                ingredient_ids=health_ids,
                ingredient_relations=tuple(
                    RecipeHealthIngredientRelation(
                        occurrence_id=item.occurrence_id,
                        ingredient_id=int(item.ingredient_id),
                        condition_type=item.condition_type,
                        choice_group_id=item.choice_group_id,
                        is_default_choice=item.selected_for_base,
                        is_process_material=item.is_process_material,
                    )
                    for item in health_occurrences
                    if item.ingredient_id is not None
                ),
                ingredient_evidence_paths=evidence_paths,
                unresolved_occurrence_count=0,
                composition_expansion_status="atomic",
            )
        )

        steps = tuple(
            _bind_step(index, text, recipe_occurrences, identity_by_id)
            for index, text in enumerate(recipe.step_segments, start=1)
        )
        step_views.append(
            RecipeStepBindingView(
                build_id=build.build_id,
                source_manifest_hash=build.source_manifest_hash,
                recipe_id=recipe_id,
                ingredient_ids=retrieval_ids,
                steps=steps,
            )
        )

        nutrition_views.append(
            RecipeNutritionInputView(
                build_id=build.build_id,
                source_manifest_hash=build.source_manifest_hash,
                recipe_id=recipe_id,
                ingredients=_nutrition_inputs(
                    nutrition_occurrences,
                    identity_by_id,
                    steps,
                    usage_decisions,
                    retention_decisions,
                ),
            )
        )

        identities_for_recipe = [identity_by_id[item] for item in retrieval_ids]
        retrieval_views.append(
            RecipeRetrievalBuildView(
                build_id=build.build_id,
                source_manifest_hash=build.source_manifest_hash,
                recipe_id=recipe_id,
                catalog_eligibility="eligible",
                name=recipe.name,
                ingredient_ids=retrieval_ids,
                ingredient_display_names=tuple(
                    identity.name_canonical for identity in identities_for_recipe
                ),
                ingredient_family_ids=_unique(
                    identity.family_id for identity in identities_for_recipe
                ),
                searchable_fields={
                    key: value
                    for key, value in recipe.searchable_fields.items()
                    if key in _ALLOWED_SEARCH_FIELDS and value
                },
                step_summary_input=recipe.step_segments,
                source_row_sha256=recipe.source_row_sha256,
                label_tags=recipe.label_tags,
                meal_tags=recipe.meal_tags,
                population_tags=recipe.population_tags,
                dish_type_tags=recipe.dish_type_tags,
                taste_tags=recipe.taste_tags,
                cuisine_tags=recipe.cuisine_tags,
                cooking_method_tags=recipe.cooking_method_tags,
                texture_tags=recipe.texture_tags,
                scenario_tags=recipe.scenario_tags,
            )
        )

    recipe_sets = {
        tuple(view.recipe_id for view in health_views),
        tuple(view.recipe_id for view in step_views),
        tuple(view.recipe_id for view in nutrition_views),
        tuple(view.recipe_id for view in retrieval_views),
    }
    if len(recipe_sets) != 1:
        raise AssertionError("消费者视图 recipe_id 集合不一致")

    return ConsumerViewSet(
        build=build,
        health_views=tuple(health_views),
        step_views=tuple(step_views),
        nutrition_views=tuple(nutrition_views),
        retrieval_views=tuple(retrieval_views),
    )


def _bind_step(
    step_index: int,
    text: str,
    occurrences: list[IngredientOccurrenceFact],
    identities: dict[int, IngredientIdentityFact],
) -> StructuredStep:
    candidates_by_span: dict[
        tuple[int, int, str], tuple[int, int, int, IngredientOccurrenceFact, str]
    ] = {}
    for occurrence in occurrences:
        if occurrence.ingredient_id is None:
            names = {occurrence.name_clean: "exact_occurrence"}
        else:
            identity = identities[occurrence.ingredient_id]
            names = {
                identity.name_canonical: "exact_id",
                occurrence.name_clean: "exact_id",
                **{alias: "approved_alias" for alias in identity.aliases},
            }
            if occurrence.form:
                names[f"{identity.name_canonical}{occurrence.form}"] = "exact_id"
        for name, method in names.items():
            if not name:
                continue
            for match in re.finditer(re.escape(name), text):
                key = (match.start(), match.end(), occurrence.occurrence_id)
                candidates_by_span[key] = (
                    len(name),
                    match.start(),
                    match.end(),
                    occurrence,
                    method,
                )

    occupied: list[tuple[int, int]] = []
    selected_occurrences: set[str] = set()
    matches = []
    for candidate in sorted(
        candidates_by_span.values(),
        key=lambda item: (-item[0], item[1], item[3].occurrence_id),
    ):
        _, start, end, occurrence, _ = candidate
        if occurrence.occurrence_id in selected_occurrences:
            continue
        if any(
            start < occupied_end and occupied_start < end
            for occupied_start, occupied_end in occupied
        ):
            continue
        occupied.append((start, end))
        selected_occurrences.add(occurrence.occurrence_id)
        matches.append(candidate)

    matches.sort(key=lambda item: (item[1], item[2], item[3].occurrence_id))
    return StructuredStep(
        step_index=step_index,
        raw_text=text,
        bound_occurrence_ids=tuple(item[3].occurrence_id for item in matches),
        bound_ingredient_ids=_unique(
            item[3].ingredient_id for item in matches if item[3].ingredient_id is not None
        ),
        binding_methods=tuple(item[4] for item in matches),
    )


def _quantity_status(quantity_raw: str | None) -> QuantityStatus:
    if not quantity_raw:
        return "unknown"
    if any(marker in quantity_raw for marker in ("-", "–", "—", "~", "～", "至", "到")):
        return "range"
    return "explicit"


def _nutrition_inputs(
    occurrences: list[IngredientOccurrenceFact],
    identities: dict[int, IngredientIdentityFact],
    steps: tuple[StructuredStep, ...],
    decisions: NutritionUsageDecisionIndex,
) -> tuple[NutritionOccurrenceInput, ...]:
    inputs: list[NutritionOccurrenceInput] = []
    for occurrence in occurrences:
        if occurrence.ingredient_id is None:
            continue
        identity = identities[occurrence.ingredient_id]
        usage_resolution = derive_nutrition_usage(
            occurrence=occurrence,
            identity=identity,
            steps=steps,
            decisions=usage_decisions,
        )
        retention_resolution = derive_nutrition_retention(
            occurrence=occurrence,
            identity=identity,
            steps=steps,
            decisions=retention_decisions,
        )
        inputs.append(
            NutritionOccurrenceInput(
                occurrence_id=occurrence.occurrence_id,
                ingredient_id=occurrence.ingredient_id,
                ingredient_name=identity.name_canonical,
                form=occurrence.form,
                normalized_form=(occurrence.form or "").strip(),
                quantity_raw=occurrence.quantity_raw,
                unit_raw=occurrence.unit_raw,
                quantity_status=_quantity_status(occurrence.quantity_raw),
                usage_code=usage_resolution.usage_code,
                fuzzy_token_class=classify_fuzzy_token(
                    occurrence.quantity_raw, occurrence.source_fragment
                ),
                retained_in_dish=retention_resolution.retained_in_dish,
                usage_requires_review=usage_resolution.requires_review,
                retention_requires_review=retention_resolution.requires_review,
            )
        )
    return tuple(inputs)


def _split_label_tags(labels_raw: str) -> tuple[str, ...]:
    return _unique(
        tag.strip()
        for tag in _LABEL_SPLIT_RE.split(labels_raw or "")
        if tag.strip()
    )


def _merge_tags(primary, secondary) -> tuple[str, ...]:
    return _unique((*tuple(primary), *tuple(secondary)))


def _validate_build_identity(build: BuildIdentity) -> None:
    if len(build.source_manifest_hash) != 64:
        raise ValueError("source_manifest_hash 必须是 64 位 SHA-256")


def _unique(values) -> tuple:
    return tuple(dict.fromkeys(values))


def _unique_by(values, attribute: str) -> dict:
    output = {}
    for value in values:
        key = getattr(value, attribute)
        if key in output:
            raise ValueError(f"重复 {attribute}={key}")
        output[key] = value
    return output
