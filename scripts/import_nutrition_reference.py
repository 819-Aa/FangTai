"""把已固化的中国疾控与 USDA 官方记录机械转换为九维营养参考。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from food_agent_v2.b1.nutrition_reference import convert_china_composition_record
from food_agent_v2.b1.usda_nutrition import load_usda_nutrition_archive

_FOUNDATION_URL = (
    "https://fdc.nal.usda.gov/fdc-datasets/"
    "FoodData_Central_foundation_food_csv_2026-04-30.zip"
)
_SR_LEGACY_URL = (
    "https://fdc.nal.usda.gov/fdc-datasets/"
    "FoodData_Central_sr_legacy_food_csv_2018-04.zip"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--usda-foundation-archive", type=Path)
    parser.add_argument("--usda-sr-legacy-archive", type=Path)
    parser.add_argument("--usda-manifest-output", type=Path)
    args = parser.parse_args()

    references = [
        convert_china_composition_record(json.loads(line))
        for line in args.source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    usda_sources = []
    if args.usda_foundation_archive is not None:
        audit = []
        imported = load_usda_nutrition_archive(
            args.usda_foundation_archive,
            source_dataset="usda_foundation",
            release="2026-04",
            audit=audit,
        )
        references.extend(imported)
        usda_sources.append(
            _usda_manifest_row(
                args.usda_foundation_archive,
                source_dataset="usda_foundation",
                release="2026-04",
                download_url=_FOUNDATION_URL,
                record_count=len(imported),
                audit=audit,
            )
        )
    if args.usda_sr_legacy_archive is not None:
        audit = []
        imported = load_usda_nutrition_archive(
            args.usda_sr_legacy_archive,
            source_dataset="usda_sr_legacy",
            release="2018-04",
            audit=audit,
        )
        references.extend(imported)
        usda_sources.append(
            _usda_manifest_row(
                args.usda_sr_legacy_archive,
                source_dataset="usda_sr_legacy",
                release="2018-04",
                download_url=_SR_LEGACY_URL,
                record_count=len(imported),
                audit=audit,
            )
        )
    reference_ids = [item.reference_id for item in references]
    if len(reference_ids) != len(set(reference_ids)):
        raise ValueError("机械转换产生重复 reference_id")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for reference in references:
            handle.write(
                json.dumps(
                    reference.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    if usda_sources:
        if args.usda_manifest_output is None:
            raise ValueError("导入 USDA 时必须提供 --usda-manifest-output")
        args.usda_manifest_output.parent.mkdir(parents=True, exist_ok=True)
        args.usda_manifest_output.write_text(
            json.dumps(
                {
                    "source_name": "USDA FoodData Central",
                    "license": "CC0 1.0 Universal / public domain",
                    "sources": usda_sources,
                    "record_count": sum(item["record_count"] for item in usda_sources),
                    "output": str(args.output),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    print(json.dumps({"status": "generated", "count": len(references)}, ensure_ascii=False))
    return 0


def _usda_manifest_row(
    archive_path: Path,
    *,
    source_dataset: str,
    release: str,
    download_url: str,
    record_count: int,
    audit: list[dict[str, str]],
) -> dict:
    return {
        "source_dataset": source_dataset,
        "release": release,
        "download_url": download_url,
        "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "record_count": record_count,
        "import_audit": audit,
    }


if __name__ == "__main__":
    raise SystemExit(main())
