"""把已固化的中国疾控记录机械转换为九维营养参考。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from food_agent_v2.b1.nutrition_reference import convert_china_composition_record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    references = [
        convert_china_composition_record(json.loads(line))
        for line in args.source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
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
    print(json.dumps({"status": "generated", "count": len(references)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
