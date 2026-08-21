"""离线候选审阅包生成命令。任何候选都不得自动批准。"""

from __future__ import annotations

import argparse
import json
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
from food_agent_v2.b1.ingredient_identity import rebuild_ingredient_identities
from food_agent_v2.b1.nutrition_reference import (
    load_nutrition_references,
    nutrition_form_from_occurrence,
    write_nutrition_crosswalk_candidates,
)
from food_agent_v2.b1.quantity_normalizer import (
    load_measure_rules,
    load_quantity_decisions,
)
from food_agent_v2.b1.quantity_review import (
    LLMQuantityEstimator,
    build_quantity_review_contexts,
    generate_quantity_candidates,
    write_quantity_candidates,
)
from food_agent_v2.b1.recipe_classifier import classify_all, load_overrides
from food_agent_v2.b1.review_inputs import (
    apply_condition_defaults,
    load_ingredient_condition_defaults,
    load_recipe_profile_enrichments,
    write_profile_candidates,
)
from food_agent_v2.b1.source_manifest import canonical_source_manifest, load_verified_recipe_source
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
        choices=("profiles", "quantities", "nutrition"),
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--sample",
        type=int,
        help="仅生成前 N 道待审菜品，便于先做小样本检查",
    )
    args = parser.parse_args(argv)
    if args.sample is not None and args.sample <= 0:
        parser.error("--sample 必须大于 0")

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
        write_profile_candidates(facts, args.output)
        count = len(facts)
    elif args.kind == "quantities":
        count = _write_quantity_review(rows, facts, args.output, sample=args.sample)
    else:
        count = _write_nutrition_review(rows, facts, args.output, sample=args.sample)
    print(
        json.dumps(
            {"status": "generated", "kind": args.kind, "count": count,
             "output": str(args.output)},
            ensure_ascii=False,
        )
    )
    return 0


def _write_quantity_review(rows, facts, output: Path, *, sample: int | None) -> int:
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

    candidates = generate_quantity_candidates(
        contexts,
        LLMQuantityEstimator(get_llm_client()),
    )
    write_quantity_candidates(candidates, output)
    return len(candidates)


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
        views = build_consumer_views(
            build=BuildIdentity(
                UUID(int=0),
                source_manifest_hash(canonical_source_manifest()),
            ),
            recipes=facts,
            occurrences=occurrences,
            identities=identities,
        )
    return views
