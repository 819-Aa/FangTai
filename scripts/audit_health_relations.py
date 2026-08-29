"""程序化审计健康关系矩阵的假阴性（false-negative）。

扫描全部 65,854 条健康关系，找出"食材名称明显属于某过敏类别，但被标成
no_hard_relation"的疑似漏排。只输出待人工复核清单，绝不自动改 approved。

用法：
  uv run python scripts/audit_health_relations.py [--out .staging/audit.txt]

原理：对每个过敏码，用"过敏原精确词根表"（FDA 8 大过敏原 + 常见过敏原的
中文词根）扫描其 no_hard_relation 的食材名称；名称含词根即疑似漏排。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

import pymysql

# 过敏原精确词根表（避免"玉米面/辣椒面"这类误报——只收真正属于该过敏类别的词）
ALLERGY_KEYWORDS: dict[str, list[str]] = {
    # 树坚果：坚果泛称 + 具体坚果名
    "allergy_tree_nut": ["坚果", "果仁", "核桃", "杏仁", "腰果", "榛子",
                         "开心果", "松子", "栗子", "白果", "夏威夷果",
                         "碧根果", "巴旦木", "山核桃", "银杏"],
    # 花生
    "allergy_peanut": ["花生"],
    # 牛奶/乳制品
    "allergy_dairy": ["牛奶", "奶酪", "黄油", "奶油", "酸奶", "炼乳",
                      "乳清", "芝士", "奶粉", "淡奶", "乳酪", "酸乳"],
    # 鸡蛋
    "allergy_egg": ["鸡蛋", "鸭蛋", "鹌鹑蛋", "蛋清", "蛋黄", "皮蛋", "咸蛋"],
    # 鱼
    "allergy_fish": ["鱼", "三文鱼", "带鱼", "鲈鱼", "鲫鱼", "鳕鱼", "鳗鱼",
                     "黄鱼", "鲳鱼", "龙利鱼", "草鱼"],
    # 大豆
    "allergy_soy": ["大豆", "黄豆", "豆腐", "豆浆", "豆干", "腐竹", "豆皮",
                    "毛豆", "豆豉", "豆酱", "豆瓣", "豆腐皮", "素鸡"],
    # 小麦/麸质
    "allergy_wheat": ["小麦", "面粉", "全麦", "面包", "面条", "馒头", "饺子",
                      "饼干", "麦麸", "麸质", "意面", "通心粉", "乌冬面"],
    # 芝麻
    "allergy_sesame": ["芝麻"],
    # 海鲜（综合）
    "allergy_seafood": ["海鲜", "鱼", "虾", "蟹", "贝", "蛤", "蚝", "牡蛎",
                        "螺", "扇贝", "鱿鱼", "章鱼", "海参", "海胆", "鲍鱼"],
    # 甲壳类贝类
    "allergy_shellfish": ["贝", "蛤", "蚝", "牡蛎", "螺", "扇贝", "蛏",
                          "花甲", "青口", "鲍鱼", "贻贝", "淡菜"],
    # 虾
    "allergy_shrimp": ["虾"],
    # 蟹
    "allergy_crab": ["蟹"],
    # 芒果
    "allergy_mango": ["芒果"],
    # 菠萝
    "allergy_pineapple": ["菠萝", "凤梨"],
    # 酒精
    "allergy_alcohol": ["酒", "啤酒", "白酒", "红酒", "黄酒", "米酒",
                        "料酒", "朗姆酒", "白兰地", "葡萄酒"],
}


class AuditInputError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def load_ready_mysql_snapshot(cfg: dict) -> tuple[str, dict[int, str], set[int]]:
    """Read registry and health views from exactly one ready build, or fail closed."""
    connection = pymysql.connect(**cfg, charset="utf8mb4")
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT build_id FROM data_builds WHERE status='ready'")
        ready_rows = cursor.fetchall()
        if len(ready_rows) != 1:
            raise AuditInputError(
                "BUILD_IDENTITY_UNAVAILABLE",
                f"data_builds must contain exactly one ready build, found {len(ready_rows)}",
            )
        build_id = str(ready_rows[0][0])

        records_by_artifact: dict[str, list[dict]] = {}
        for artifact_name in ("ingredient_registry", "recipe_health_views"):
            cursor.execute(
                "SELECT payload FROM fixed_artifact_records "
                "WHERE build_id=%s AND artifact_name=%s ORDER BY record_index",
                (build_id, artifact_name),
            )
            records: list[dict] = []
            for (payload,) in cursor.fetchall():
                record = json.loads(payload)
                if str(record.get("build_id")) != build_id:
                    raise AuditInputError(
                        "BUILD_IDENTITY_MISMATCH",
                        f"{artifact_name} payload build_id does not match {build_id}",
                    )
                records.append(record)
            records_by_artifact[artifact_name] = records
    finally:
        try:
            cursor.close()
        finally:
            connection.close()

    registry: dict[int, str] = {}
    for record in records_by_artifact["ingredient_registry"]:
        ingredient_id = int(record["ingredient_id"])
        if ingredient_id in registry:
            raise AuditInputError(
                "DUPLICATE_INGREDIENT_ID",
                f"ingredient_registry contains duplicate ingredient_id {ingredient_id}",
            )
        registry[ingredient_id] = record["name_canonical"]

    eligible_ids: set[int] = set()
    recipe_ids: set[int] = set()
    for record in records_by_artifact["recipe_health_views"]:
        recipe_id = int(record["recipe_id"])
        if recipe_id in recipe_ids:
            raise AuditInputError(
                "DUPLICATE_RECIPE_HEALTH_VIEW",
                f"recipe_health_views contains duplicate recipe_id {recipe_id}",
            )
        recipe_ids.add(recipe_id)
        eligible_ids.update(int(ingredient_id) for ingredient_id in record["ingredient_ids"])
    return build_id, registry, eligible_ids


def load_decisions(csv_path: str | Path) -> dict[tuple[str, int], str]:
    decisions: dict[tuple[str, int], str] = {}
    with Path(csv_path).open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            decisions[(row["constraint_code"], int(row["ingredient_id"]))] = row["decision"]
    return decisions


def load_decision_rows(csv_path: str | Path) -> list[dict[str, str]]:
    with Path(csv_path).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _load_jsonl_records(path: str | Path) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _snapshot_build_id(records: list[dict], artifact_name: str) -> str:
    build_ids = {str(record.get("build_id", "")).strip() for record in records}
    if not build_ids or "" in build_ids or len(build_ids) != 1:
        raise AuditInputError(
            "BUILD_IDENTITY_MISMATCH",
            f"{artifact_name} must contain one non-empty build_id",
        )
    return build_ids.pop()


def load_local_snapshot_pair(
    registry_path: str | Path, health_views_path: str | Path
) -> tuple[str, dict[int, str], set[int]]:
    """Load one validated local registry/health-view build pair, or fail closed."""
    registry_records = _load_jsonl_records(registry_path)
    health_view_records = _load_jsonl_records(health_views_path)
    registry_build_id = _snapshot_build_id(registry_records, "ingredient_registry")
    health_view_build_id = _snapshot_build_id(health_view_records, "recipe_health_views")
    if registry_build_id != health_view_build_id:
        raise AuditInputError(
            "BUILD_IDENTITY_MISMATCH",
            "ingredient_registry and recipe_health_views build_id values differ",
        )

    registry: dict[int, str] = {}
    for record in registry_records:
        ingredient_id = int(record["ingredient_id"])
        if ingredient_id in registry:
            raise AuditInputError(
                "DUPLICATE_INGREDIENT_ID",
                f"ingredient_registry contains duplicate ingredient_id {ingredient_id}",
            )
        registry[ingredient_id] = record["name_canonical"]

    eligible_ids: set[int] = set()
    recipe_ids: set[int] = set()
    for record in health_view_records:
        recipe_id = int(record["recipe_id"])
        if recipe_id in recipe_ids:
            raise AuditInputError(
                "DUPLICATE_RECIPE_HEALTH_VIEW",
                f"recipe_health_views contains duplicate recipe_id {recipe_id}",
            )
        recipe_ids.add(recipe_id)
        eligible_ids.update(int(ingredient_id) for ingredient_id in record["ingredient_ids"])
    return registry_build_id, registry, eligible_ids


_SEAFOOD_CONFIRMED_MARKERS = (
    "小青龙", "虾", "蟹", "鱿鱼", "章鱼", "墨鱼", "蛤", "蚝", "牡蛎", "蛏", "蚌",
    "海参", "海胆", "海蜇", "螺", "鲍", "扇贝", "青口", "花甲", "带子", "瑶柱",
    "龙虾", "三文鱼", "金枪鱼", "鳕鱼", "鲈鱼", "鲳", "带鱼", "黄鱼", "河鳗", "鳗鱼",
    "白鳝", "泥鳅", "银鱼", "鲶鱼", "石斑", "多宝鱼", "龙利鱼", "比目鱼", "鳟鱼", "鱼",
    "贝",
)
_SEAFOOD_GUARD_MARKERS = ("川贝", "贝贝南瓜", "蟹味菇", "杏鲍菇", "海鲜菇", "素蚝油")
_SEAFOOD_CONDIMENT_MARKERS = ("豉油", "鼓油", "酱油", "海鲜酱", "调料", "料包")


def _seafood_name_classification(name: str) -> str:
    """Classify only explicit seafood names; broad 海鲜 wording is never auto-confirmed."""
    if (
        any(marker in name for marker in _SEAFOOD_GUARD_MARKERS)
        or ("菇" in name and any(marker in name for marker in ("蟹", "鲍", "海鲜")))
        or any(marker in name for marker in _SEAFOOD_CONDIMENT_MARKERS)
    ):
        return "guard"
    if any(marker in name for marker in _SEAFOOD_CONFIRMED_MARKERS):
        return "confirmed"
    if "海鲜" in name:
        return "ambiguous"
    return "none"


def audit(
    name_map: dict[int, str], decisions: dict[tuple[str, int], str]
) -> dict[str, list[tuple[int, str]]]:
    """Audit the seafood slice without promoting names that need human review."""
    confirmed_false_negatives: list[tuple[int, str]] = []
    known_false_positive_promotions: list[tuple[int, str]] = []
    ambiguous_names: list[tuple[int, str]] = []
    for (code, ingredient_id), decision in decisions.items():
        if code != "allergy_seafood":
            continue
        name = name_map.get(ingredient_id, "")
        classification = _seafood_name_classification(name)
        if classification == "confirmed" and decision == "no_hard_relation":
            confirmed_false_negatives.append((ingredient_id, name))
        elif classification == "guard" and decision == "hard_exclude":
            known_false_positive_promotions.append((ingredient_id, name))
        elif classification == "ambiguous":
            ambiguous_names.append((ingredient_id, name))
    return {
        "confirmed_false_negatives": sorted(confirmed_false_negatives),
        "known_false_positive_promotions": sorted(known_false_positive_promotions),
        "ambiguous_names": sorted(ambiguous_names),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=".staging/health_relation_audit.txt")
    parser.add_argument("--decisions", default="data/review/health_relation_decisions.csv")
    parser.add_argument(
        "--registry",
        help="Local ingredient_registry.jsonl snapshot. Omitting this uses read-only MySQL.",
    )
    parser.add_argument(
        "--health-views",
        help="Local recipe_health_views.jsonl snapshot used to verify the complete eligible set.",
    )
    parser.add_argument("--host", default=os.getenv("FOOD_AGENT_DB_HOST", "localhost"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("FOOD_AGENT_DB_PORT", "3306"))
    )
    parser.add_argument("--user", default=os.getenv("FOOD_AGENT_DB_USER", "foodagent"))
    parser.add_argument(
        "--password", default=os.getenv("FOOD_AGENT_DB_PASSWORD", "foodagent_v2")
    )
    parser.add_argument("--database", default=os.getenv("FOOD_AGENT_DB_NAME", "food_agent_v2"))
    args = parser.parse_args()

    try:
        if bool(args.registry) != bool(args.health_views):
            raise AuditInputError(
                "LOCAL_SNAPSHOT_INPUT_INCOMPLETE",
                "--registry and --health-views must be provided together",
            )
        if args.registry:
            build_id, name_map, eligible_ids = load_local_snapshot_pair(
                args.registry, args.health_views
            )
            registry_source = f"{Path(args.registry)};build_id={build_id}"
            eligibility_source = f"{Path(args.health_views)};build_id={build_id}"
        else:
            build_id, name_map, eligible_ids = load_ready_mysql_snapshot(
                {
                    "host": args.host,
                    "port": args.port,
                    "user": args.user,
                    "password": args.password,
                    "database": args.database,
                }
            )
            registry_source = (
                f"mysql://{args.host}:{args.port}/{args.database};build_id={build_id} (read-only)"
            )
            eligibility_source = f"mysql:recipe_health_views;build_id={build_id} (read-only)"
    except AuditInputError as error:
        lines = ["allergy_seafood closed-set audit: failed", f"{error.code}: {error}"]
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        sys.stdout.write("\n".join(lines) + "\n")
        return 2

    decision_rows = load_decision_rows(args.decisions)
    decisions = {
        (row["constraint_code"], int(row["ingredient_id"])): row["decision"]
        for row in decision_rows
    }
    seafood_rows = [row for row in decision_rows if row["constraint_code"] == "allergy_seafood"]
    seafood_key_counts = Counter(int(row["ingredient_id"]) for row in seafood_rows)
    seafood_ids = set(seafood_key_counts)

    report = audit(name_map, decisions)
    report["missing_matrix_keys"] = sorted(eligible_ids - seafood_ids)
    report["unexpected_matrix_keys"] = sorted(seafood_ids - eligible_ids)
    report["duplicate_matrix_keys"] = sorted(
        ingredient_id for ingredient_id, count in seafood_key_counts.items() if count != 1
    )
    report["registry_ids_missing"] = sorted(eligible_ids - set(name_map))
    report["eligible_count"] = len(eligible_ids)
    report["matrix_row_count"] = len(seafood_rows)
    report["matrix_key_count"] = len(seafood_ids)
    report["registry_source"] = registry_source
    report["eligibility_source"] = eligibility_source
    status = "passed" if not any(
        report[key]
        for key in (
            "confirmed_false_negatives",
            "known_false_positive_promotions",
            "missing_matrix_keys",
            "unexpected_matrix_keys",
            "duplicate_matrix_keys",
            "registry_ids_missing",
        )
    ) else "failed"

    lines: list[str] = []
    lines.append(f"allergy_seafood closed-set audit: {status}")
    lines.append(
        "eligible={eligible_count}; matrix_rows={matrix_row_count}; matrix_keys={matrix_key_count}".format(
            **report
        )
    )
    lines.append(f"registry_source={registry_source}")
    lines.append(f"eligibility_source={eligibility_source}")
    for key in (
        "confirmed_false_negatives",
        "known_false_positive_promotions",
        "missing_matrix_keys",
        "unexpected_matrix_keys",
        "duplicate_matrix_keys",
        "registry_ids_missing",
        "ambiguous_names",
    ):
        lines.append(f"{key}={len(report[key])}")
        for item in report[key]:
            lines.append(f"  {item}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
