"""离线候选审阅包生成命令。任何候选都不得自动批准。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from food_agent_v2.b1.consumer_views import recipe_facts_from_source
from food_agent_v2.b1.recipe_classifier import classify_all, load_overrides
from food_agent_v2.b1.review_inputs import write_profile_candidates
from food_agent_v2.b1.source_manifest import canonical_source_manifest, load_verified_recipe_source
from food_agent_v2.core.paths import PROJECT_ROOT, RECIPES_RAW


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="food-agent-v2 data-review")
    parser.add_argument("--kind", choices=("profiles",), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    rows = tuple(load_verified_recipe_source(RECIPES_RAW, canonical_source_manifest()))
    classifications = tuple(
        classify_all(
            list(rows),
            load_overrides(
                PROJECT_ROOT / "data" / "review" / "recipe_classification_overrides.csv"
            ),
        )
    )
    facts = recipe_facts_from_source(rows, classifications)
    write_profile_candidates(facts, args.output)
    print(
        json.dumps(
            {"status": "generated", "kind": args.kind, "count": len(facts),
             "output": str(args.output)},
            ensure_ascii=False,
        )
    )
    return 0
