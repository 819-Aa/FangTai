"""food-agent-v2 CLI 入口。"""

from __future__ import annotations

import sys

COMMANDS = {
    "data-rebuild": "Run B1 offline data pipeline",
    "data-verify": "Verify a fixed-data BuildManifest and all quality gates",
    "data-initialize": "Initialize empty V2 MySQL/Qdrant from a verified manifest",
    "data-review": "Generate offline profile/quantity/nutrition/time-graph review candidates",
    "seed-generate": "Generate MySQL seed SQL from B1 outputs",
    "qdrant-build": "Show migration path for removed direct Qdrant build",
    "database-load": "Show migration path for removed mutable database load",
    "models-prepare": "Download and cache BGE models",
    "time-profiler": "Show migration to data-review --kind time-graphs",
    "api-start": "Start FastAPI HTTP server",
    "config-show": "Show current configuration",
    "validate-data": "Run cross-domain validation",
}


def main() -> None:
    if len(sys.argv) < 2:
        print("food-agent-v2 <command>")
        print()
        for cmd, desc in COMMANDS.items():
            print(f"  {cmd:18s} {desc}")
        sys.exit(1)

    command = sys.argv[1]

    if command == "data-rebuild":
        from food_agent_v2.b1.rebuild import main as fn

        sys.exit(fn(sys.argv[2:]))

    elif command == "data-review":
        from food_agent_v2.b1.data_review import main as fn

        sys.exit(fn(sys.argv[2:]))

    elif command == "data-verify":
        import argparse
        import json as _json
        from pathlib import Path

        from food_agent_v2.b1.quality_gates import verify_build_manifest

        parser = argparse.ArgumentParser(prog="food-agent-v2 data-verify")
        parser.add_argument("--manifest", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        manifest = verify_build_manifest(args.manifest)
        print(
            _json.dumps(
                {
                    "status": "passed",
                    "build_id": str(manifest.build_id),
                    "source_manifest_hash": manifest.source_manifest_hash,
                    "artifact_count": len(manifest.artifacts),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    elif command == "data-initialize":
        import argparse
        import json as _json
        from pathlib import Path

        from food_agent_v2.b1.database_loader import initialize_verified_fixed_data

        parser = argparse.ArgumentParser(prog="food-agent-v2 data-initialize")
        parser.add_argument("--manifest", type=Path, required=True)
        parser.add_argument("--confirm-empty-v2", action="store_true")
        args = parser.parse_args(sys.argv[2:])
        result = initialize_verified_fixed_data(
            args.manifest,
            confirm_empty_v2=args.confirm_empty_v2,
        )
        print(_json.dumps(result, ensure_ascii=False, indent=2))

    elif command == "seed-generate":
        from food_agent_v2.b1.seed_writer import generate_mysql_seed, generate_qdrant_payloads

        sql_path = generate_mysql_seed()
        print(f"MySQL seed: {sql_path}")
        qdrant_path = generate_qdrant_payloads()
        print(f"Qdrant payloads: {qdrant_path}")

    elif command == "qdrant-build":
        from food_agent_v2.c1.index_builder import build_index

        result = build_index()
        import json as _j

        print(_j.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") == "blocked":
            sys.exit(2)

    elif command == "database-load":
        import json as _j

        result = {
            "status": "blocked",
            "reason": "MUTABLE_DATABASE_LOAD_REMOVED",
            "required_command": (
                "food-agent-v2 data-initialize --manifest <BuildManifest> --confirm-empty-v2"
            ),
        }
        print(_j.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(2)

    elif command == "models-prepare":
        print("Model preparation: placeholder")
        print("Required models: BGE-M3 (embedding), BGE-Reranker-v2-M3 (reranker)")
        print("Place model files in .model-cache/ or configure BGE_MODEL_PATH")
        print("V1 models can be symlinked if already downloaded.")

    elif command == "time-profiler":
        from food_agent_v2.b1.llm_time_profiler import run

        sample_arg = None
        if "--sample" in sys.argv:
            sample_arg = int(sys.argv[sys.argv.index("--sample") + 1])
        import json as _j

        print(_j.dumps(run(sample=sample_arg), ensure_ascii=False, indent=2))

    elif command == "api-start":
        from food_agent_v2.api_app import main as api_main

        api_main()

    elif command == "config-show":
        import json

        from food_agent_v2.core.config import load_config

        cfg = load_config()
        cfg_dict = {
            "mysql": {
                "host": cfg.mysql.host,
                "port": cfg.mysql.port,
                "database": cfg.mysql.database,
            },
            "qdrant": {
                "host": cfg.qdrant.host,
                "port": cfg.qdrant.rest_port,
                "collection": cfg.qdrant.collection,
            },
            "redis": {
                "host": cfg.redis.host,
                "port": cfg.redis.port,
                "prefix": cfg.redis.key_prefix,
            },
            "api": {"host": cfg.api.host, "port": cfg.api.port},
            "models": {"rag_warmup": cfg.models.rag_warmup_on_startup},
            "llm": {
                "reasoning": cfg.llm.model_reasoning or "(not set)",
                "answer": cfg.llm.model_answer or "(not set)",
                "base_url": cfg.llm.base_url or "(not set)",
            },
        }
        print(json.dumps(cfg_dict, ensure_ascii=False, indent=2))

    elif command == "validate-data":
        import json as _json

        from food_agent_v2.b1.cross_domain_validator import validate
        from food_agent_v2.core.paths import CLEANED_RECIPES, CLEANED_USERS

        with CLEANED_RECIPES.open("r", encoding="utf-8") as f:
            recipes = [_json.loads(line) for line in f if line.strip()]
        with CLEANED_USERS.open("r", encoding="utf-8") as f:
            users = [_json.loads(line) for line in f if line.strip()]
        report = validate(recipes, users)
        print(_json.dumps(report, ensure_ascii=False, indent=2))

    else:
        print(f"Unknown command: {command}")
        print("Available:", ", ".join(COMMANDS.keys()))
        sys.exit(1)


if __name__ == "__main__":
    main()
