"""离线候选审阅包生成命令。任何候选都不得自动批准。"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from food_agent_v2.b1.consumer_views import (
    BuildIdentity,
    build_consumer_views,
    identity_facts_from_records,
    occurrence_facts_from_records,
    recipe_facts_from_source,
)
from food_agent_v2.b1.edible_fraction_review import (
    generate_edible_fraction_candidates,
    load_edible_fraction_decisions,
    load_edible_fraction_rules,
    write_edible_fraction_candidates,
)
from food_agent_v2.b1.food_origin_review import (
    FoodOriginCandidateCache,
    FoodOriginReviewInput,
    LLMFoodOriginEstimator,
    generate_food_origin_candidates,
    write_food_origin_candidates,
)
from food_agent_v2.b1.ingredient_identity import rebuild_ingredient_identities
from food_agent_v2.b1.nutrition_occurrence_rules import load_nutrition_usage_decisions
from food_agent_v2.b1.nutrition_reference import (
    load_nutrition_references,
    nutrition_form_from_occurrence,
    write_nutrition_crosswalk_candidates,
)
from food_agent_v2.b1.profile_review import (
    LLMProfileEstimator,
    ProfileCandidateCache,
    ProfileReviewInput,
    generate_profile_candidates,
    write_profile_candidate_records,
)
from food_agent_v2.b1.quantity_normalizer import (
    load_measure_rules,
    load_quantity_decisions,
)
from food_agent_v2.b1.quantity_review import (
    LLMQuantityEstimator,
    QuantityEstimateCache,
    build_quantity_review_contexts,
    generate_quantity_candidates,
    load_quantity_review_candidates,
    write_quantity_candidates,
)
from food_agent_v2.b1.quantity_rule_review import (
    generate_quantity_rule_candidates,
    write_quantity_rule_candidates,
)
from food_agent_v2.b1.recipe_classifier import classify_all, load_overrides
from food_agent_v2.b1.review_inputs import (
    apply_condition_defaults,
    load_ingredient_condition_defaults,
    load_recipe_profile_enrichments,
)
from food_agent_v2.b1.source_manifest import canonical_source_manifest, load_verified_recipe_source
from food_agent_v2.b1.step_atomizer import atomize_recipe_steps
from food_agent_v2.b1.time_review_decisions import (
    apply_time_review_decisions,
    load_time_review_decisions,
)
from food_agent_v2.b1.usda_crosswalk_review import (
    LLMUsdaCandidateSelector,
    LLMUsdaSearchTermEstimator,
    UsdaCandidateSelectionCache,
    UsdaCrosswalkReviewInput,
    UsdaSearchTermCandidateCache,
    generate_usda_crosswalk_candidates,
    generate_usda_search_terms,
    select_usda_crosswalk_candidates,
    write_usda_crosswalk_candidates,
)
from food_agent_v2.contracts.build import source_manifest_hash
from food_agent_v2.core.paths import PROJECT_ROOT, RECIPES_RAW

_REVIEW_DIR = PROJECT_ROOT / "data" / "review"


def _read_jsonl(path: Path) -> tuple[dict, ...]:
    return tuple(
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="food-agent-v2 data-review")
    parser.add_argument(
        "--kind",
        choices=(
            "profiles",
            "quantities",
            "quantity-rules",
            "nutrition",
            "usda-nutrition",
            "food-origins",
            "time-graphs",
            "edible-fractions",
        ),
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--input",
        type=Path,
        help="离线阶段的 occurrence 候选输入；默认使用 reports/data_review/quantity_candidates.csv",
    )
    parser.add_argument(
        "--sample",
        type=int,
        help="仅生成前 N 道待审菜品，便于先做小样本检查",
    )
    args = parser.parse_args(argv)
    if args.sample is not None and args.sample <= 0:
        parser.error("--sample 必须大于 0")
    if args.workers <= 0:
        parser.error("--workers 必须大于 0")

    rows = tuple(load_verified_recipe_source(RECIPES_RAW, canonical_source_manifest()))
    classifications = tuple(
        classify_all(
            list(rows),
            load_overrides(
                _REVIEW_DIR / "recipe_classification_overrides.csv"
            ),
        )
    )
    profile_enrichments = load_recipe_profile_enrichments(
        _REVIEW_DIR / "recipe_profile_enrichment.jsonl",
        known_recipe_ids={row.recipe_id for row in rows},
    )
    facts = recipe_facts_from_source(rows, classifications, profile_enrichments)
    if args.kind == "profiles":
        count = _write_profile_review(
            rows,
            facts,
            args.output,
            sample=args.sample,
            workers=args.workers,
        )
    elif args.kind == "quantities":
        count = _write_quantity_review(
            rows, facts, args.output, sample=args.sample, workers=args.workers
        )
    elif args.kind == "quantity-rules":
        count = _write_quantity_rule_review(
            rows, facts, args.output, input_path=args.input, sample=args.sample
        )
    elif args.kind == "nutrition":
        count = _write_nutrition_review(rows, facts, args.output, sample=args.sample)
    elif args.kind == "usda-nutrition":
        count = _write_usda_nutrition_review(
            rows,
            facts,
            args.output,
            sample=args.sample,
            workers=args.workers,
        )
    elif args.kind == "food-origins":
        count = _write_food_origin_review(
            rows,
            facts,
            args.output,
            sample=args.sample,
            workers=args.workers,
        )
    elif args.kind == "edible-fractions":
        count = _write_edible_fraction_review(
            rows, facts, args.output, sample=args.sample
        )
    else:
        count = _write_time_graph_review(
            rows, facts, args.output, sample=args.sample, workers=args.workers
        )
    print(
        json.dumps(
            {"status": "generated", "kind": args.kind, "count": count,
             "output": str(args.output)},
            ensure_ascii=False,
        )
    )
    return 0


def _write_profile_review(
    rows, facts, output: Path, *, sample: int | None, workers: int
) -> int:
    from food_agent_v2.c3.llm_client import get_llm_client
    from food_agent_v2.core.config import load_config

    selected_rows = rows[:sample] if sample is not None else rows
    selected_ids = {row.recipe_id for row in selected_rows}
    selected_facts = tuple(fact for fact in facts if fact.recipe_id in selected_ids)
    fact_by_id = {fact.recipe_id: fact for fact in selected_facts}
    inputs = tuple(
        ProfileReviewInput(
            recipe_id=row.recipe_id,
            name=row.name,
            record_type=str(fact_by_id[row.recipe_id].record_type),
            ingredients_raw=row.ingredients_raw,
            steps_raw=row.steps_raw,
            label_tags=fact_by_id[row.recipe_id].label_tags,
        )
        for row in selected_rows
    )
    model_id = load_config().llm.model_for_role("profile_enrichment")
    candidates = generate_profile_candidates(
        inputs,
        selected_facts,
        LLMProfileEstimator(get_llm_client(), model_id=model_id),
        cache=ProfileCandidateCache(
            PROJECT_ROOT / "data" / "cache" / "recipe_profile_candidates.jsonl"
        ),
        max_workers=workers,
    )
    write_profile_candidate_records(candidates, output)
    return len(candidates)


def _write_quantity_review(
    rows, facts, output: Path, *, sample: int | None, workers: int
) -> int:
    views = _build_review_views(rows, facts)
    contexts = build_quantity_review_contexts(
        views.nutrition_views,
        facts,
        load_measure_rules(_REVIEW_DIR / "ingredient_measure_rules.csv"),
        load_quantity_decisions(_REVIEW_DIR / "ingredient_quantity_decisions.csv"),
    )
    if sample is not None:
        contexts = contexts[:sample]
    from food_agent_v2.c3.llm_client import get_llm_client
    from food_agent_v2.core.config import load_config

    candidates = generate_quantity_candidates(
        contexts,
        LLMQuantityEstimator(get_llm_client()),
        cache=QuantityEstimateCache(
            PROJECT_ROOT / "data" / "cache" / "recipe_quantity_estimates.jsonl"
        ),
        model_id=load_config().llm.model_for_role("quantity_estimation"),
        max_workers=workers,
    )
    write_quantity_candidates(candidates, output)
    return len(candidates)


def _write_quantity_rule_review(
    rows, facts, output: Path, *, input_path: Path | None, sample: int | None
) -> int:
    _refuse_formal_review_target(output)
    views = _build_review_views(rows, facts)
    source = input_path or PROJECT_ROOT / "reports" / "data_review" / "quantity_candidates.csv"
    candidates = load_quantity_review_candidates(source, views, facts)
    if sample is not None:
        candidates = candidates[:sample]
    rule_candidates = generate_quantity_rule_candidates(
        candidates,
        load_measure_rules(_REVIEW_DIR / "ingredient_measure_rules.csv"),
    )
    write_quantity_rule_candidates(rule_candidates, output)
    return len(rule_candidates)


def _write_edible_fraction_review(
    rows, facts, output: Path, *, sample: int | None
) -> int:
    _refuse_formal_review_target(output)
    views = _build_review_views(rows, facts)
    rules = load_edible_fraction_rules(
        _REVIEW_DIR / "ingredient_edible_fraction_rules.csv"
    )
    occurrence_ids = {
        item.occurrence_id
        for view in views.nutrition_views
        for item in view.ingredients
    }
    decisions = load_edible_fraction_decisions(
        _REVIEW_DIR / "ingredient_edible_fraction_decisions.csv",
        current_occurrence_ids=occurrence_ids,
    )
    candidates = generate_edible_fraction_candidates(
        views.nutrition_views, rules, decisions
    )
    if sample is not None:
        candidates = candidates[:sample]
    write_edible_fraction_candidates(candidates, output)
    return len(candidates)


def _refuse_formal_review_target(path: Path) -> None:
    formal_root = PROJECT_ROOT / "data" / "review"
    formal_paths = (
        formal_root / "ingredient_measure_rules.csv",
        formal_root / "ingredient_quantity_decisions.csv",
        formal_root / "ingredient_nutrition_usage_decisions.csv",
        formal_root / "ingredient_edible_fraction_rules.csv",
        formal_root / "ingredient_edible_fraction_decisions.csv",
    )
    if any(path.name.casefold() == formal.name.casefold() for formal in formal_paths):
        raise ValueError("候选不得写入正式规则或决定文件")
    try:
        resolved_path = path.resolve(strict=False)
        review_root = formal_root.resolve(strict=False)
        if resolved_path == review_root or resolved_path.is_relative_to(review_root):
            raise ValueError("候选不得写入正式规则或决定文件")
        resolved = str(resolved_path).casefold()
        for formal in formal_paths:
            if resolved == str(formal.resolve(strict=False)).casefold():
                raise ValueError("候选不得写入正式规则或决定文件")
            if path.exists() and formal.exists() and path.samefile(formal):
                raise ValueError("候选不得写入正式规则或决定文件")
    except OSError as exc:
        raise ValueError("候选目标路径无法安全校验") from exc


def _write_nutrition_review(rows, facts, output: Path, *, sample: int | None) -> int:
    views = _build_review_views(rows, facts)
    ingredient_rows = tuple(
        (
            ingredient.ingredient_id,
            ingredient.ingredient_name,
            nutrition_form_from_occurrence(ingredient.form),
        )
        for view in views.nutrition_views
        for ingredient in view.ingredients
    )
    unique_rows = tuple(
        {
            (ingredient_id, form): (ingredient_id, name, form)
            for ingredient_id, name, form in ingredient_rows
        }.values()
    )
    if sample is not None:
        unique_rows = unique_rows[:sample]
    references = load_nutrition_references(
        PROJECT_ROOT / "data" / "reference" / "ingredient_nutrition.jsonl"
    )
    write_nutrition_crosswalk_candidates(unique_rows, references, output)
    return len(unique_rows)


def _write_food_origin_review(
    rows,
    facts,
    output: Path,
    *,
    sample: int | None,
    workers: int,
) -> int:
    views = _build_review_views(rows, facts)
    ingredient_rows = tuple(
        (
            ingredient.ingredient_id,
            ingredient.ingredient_name,
            nutrition_form_from_occurrence(ingredient.form),
        )
        for view in views.nutrition_views
        for ingredient in view.ingredients
    )
    unique_rows = tuple(
        {
            (ingredient_id, form): (ingredient_id, name, form)
            for ingredient_id, name, form in ingredient_rows
        }.values()
    )
    references = load_nutrition_references(
        PROJECT_ROOT / "data" / "reference" / "ingredient_nutrition.jsonl"
    )
    candidate_references = {}
    for _, ingredient_name, _ in unique_rows:
        candidates = references.find_by_name(str(ingredient_name))
        if not candidates:
            candidates = references.find_by_review_alias(str(ingredient_name))
        for reference in candidates:
            candidate_references[reference.reference_id] = reference
    usda_candidates_path = (
        PROJECT_ROOT / "reports" / "data_review" / "usda_nutrition_candidates.csv"
    )
    if usda_candidates_path.exists():
        with usda_candidates_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                reference_id = (row.get("candidate_reference_id") or "").strip()
                if not reference_id:
                    continue
                reference = references.get(reference_id)
                if reference is None:
                    raise ValueError(
                        f"USDA 营养候选引用未知 reference_id: {reference_id}"
                    )
                candidate_references[reference.reference_id] = reference
    selected = tuple(candidate_references.values())
    if sample is not None:
        selected = selected[:sample]

    from food_agent_v2.c3.llm_client import get_llm_client
    from food_agent_v2.core.config import load_config

    model_id = load_config().llm.model_for_role("nutrition_review")
    candidates = generate_food_origin_candidates(
        (
            FoodOriginReviewInput(
                reference_id=reference.reference_id,
                canonical_name=reference.canonical_name,
                form=reference.form,
            )
            for reference in selected
        ),
        LLMFoodOriginEstimator(get_llm_client(), model_id=model_id),
        cache=FoodOriginCandidateCache(
            PROJECT_ROOT / "data" / "cache" / "food_origin_candidates.jsonl"
        ),
        max_workers=workers,
    )
    write_food_origin_candidates(candidates, output)
    return len(candidates)


def _write_usda_nutrition_review(
    rows,
    facts,
    output: Path,
    *,
    sample: int | None,
    workers: int,
) -> int:
    views = _build_review_views(rows, facts)
    ingredient_rows = tuple(
        (
            ingredient.ingredient_id,
            ingredient.ingredient_name,
            nutrition_form_from_occurrence(ingredient.form),
        )
        for view in views.nutrition_views
        for ingredient in view.ingredients
    )
    unique_rows = tuple(
        {
            (ingredient_id, form): (ingredient_id, name, form)
            for ingredient_id, name, form in ingredient_rows
        }.values()
    )
    references = load_nutrition_references(
        PROJECT_ROOT / "data" / "reference" / "ingredient_nutrition.jsonl"
    )
    unresolved_rows = tuple(
        row
        for row in unique_rows
        if not references.find_by_name(str(row[1]))
        and not references.find_by_review_alias(str(row[1]))
    )
    if sample is not None:
        unresolved_rows = unresolved_rows[:sample]

    from food_agent_v2.c1.siliconflow import SiliconFlowEmbedder
    from food_agent_v2.c3.llm_client import get_llm_client
    from food_agent_v2.core.config import load_config

    review_inputs = tuple(
        UsdaCrosswalkReviewInput(
            ingredient_id=int(ingredient_id),
            ingredient_name=str(ingredient_name),
            form=str(form),
        )
        for ingredient_id, ingredient_name, form in unresolved_rows
    )
    llm_model_id = load_config().llm.model_for_role("nutrition_review")
    search_terms = generate_usda_search_terms(
        review_inputs,
        LLMUsdaSearchTermEstimator(get_llm_client(), model_id=llm_model_id),
        cache=UsdaSearchTermCandidateCache(
            PROJECT_ROOT / "data" / "cache" / "usda_search_terms.jsonl"
        ),
        max_workers=workers,
    )

    ranked_candidates = generate_usda_crosswalk_candidates(
        review_inputs,
        references.values,
        SiliconFlowEmbedder(),
        embedding_model_id=os.getenv(
            "SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3"
        ),
        search_terms=search_terms,
        top_k=20,
        max_workers=workers,
        embedding_cache_dir=(
            PROJECT_ROOT / "data" / "cache" / "usda_crosswalk_embeddings"
        ),
    )
    candidates = select_usda_crosswalk_candidates(
        review_inputs,
        ranked_candidates,
        search_terms,
        LLMUsdaCandidateSelector(get_llm_client(), model_id=llm_model_id),
        cache=UsdaCandidateSelectionCache(
            PROJECT_ROOT / "data" / "cache" / "usda_candidate_selections.jsonl"
        ),
        max_workers=workers,
    )
    write_usda_crosswalk_candidates(candidates, output)
    return len(unresolved_rows)


def _write_time_graph_review(
    rows, facts, output: Path, *, sample: int | None, workers: int
) -> int:
    from food_agent_v2.b1.llm_time_profiler import generate_time_graph_review

    views = _build_review_views(rows, facts)
    name_by_id = {fact.recipe_id: fact.name for fact in facts}
    time_decisions = load_time_review_decisions(
        _REVIEW_DIR / "recipe_time_graph_decisions.csv"
    )
    recipes = tuple(
        (
            view.recipe_id,
            name_by_id[view.recipe_id],
            apply_time_review_decisions(
                recipe_id=view.recipe_id,
                atoms=atomize_recipe_steps(
                    recipe_id=view.recipe_id,
                    steps=((step.step_index, step.raw_text) for step in view.steps),
                ),
                decisions=time_decisions,
            ),
        )
        for view in views.step_views
    )
    if sample is not None:
        recipes = recipes[:sample]
    result = generate_time_graph_review(recipes, output=output, max_workers=workers)
    return int(result["conflicts"])


def _build_review_views(rows, facts):
    with TemporaryDirectory(prefix="food-agent-quantity-review-") as temp_dir:
        staging = Path(temp_dir)
        report = rebuild_ingredient_identities(
            list(rows),
            _REVIEW_DIR / "ingredient_identity_overrides.csv",
            staging,
        )
        if report.get("status") != "passed":
            raise RuntimeError(f"食材身份尚未通过，不能生成克重候选: {report}")
        identities = identity_facts_from_records(
            _read_jsonl(staging / "ingredient_registry.jsonl"),
            _read_jsonl(staging / "ingredient_aliases.jsonl"),
        )
        occurrences = occurrence_facts_from_records(
            _read_jsonl(staging / "ingredient_occurrences.jsonl")
        )
        condition_defaults = load_ingredient_condition_defaults(
            _REVIEW_DIR / "ingredient_condition_defaults.csv",
            known_recipe_names={fact.name for fact in facts},
        )
        occurrences = apply_condition_defaults(
            occurrences,
            recipe_names={fact.recipe_id: fact.name for fact in facts},
            decisions=condition_defaults,
        )
        usage_decisions = load_nutrition_usage_decisions(
            _REVIEW_DIR / "ingredient_nutrition_usage_decisions.csv",
            current_occurrence_ids={item.occurrence_id for item in occurrences},
        )
        views = build_consumer_views(
            build=BuildIdentity(
                UUID(int=0),
                source_manifest_hash(canonical_source_manifest()),
            ),
            recipes=facts,
            occurrences=occurrences,
            identities=identities,
            nutrition_usage_decisions=usage_decisions,
        )
    return views
