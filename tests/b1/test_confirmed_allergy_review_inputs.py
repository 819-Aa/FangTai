import csv
from collections import Counter
from pathlib import Path

import pytest

from food_agent_v2.b1.health_relation_builder import (
    generate_health_relation_candidates,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DECISIONS_PATH = REPO_ROOT / "data" / "review" / "health_relation_decisions.csv"

# Literal review approval scope.  Removing any exact-name matcher entry or changing any
# approved matrix cell must fail these independently derived expectations.
APPROVED_ALLERGY_NAMES = {
    "allergy_tree_nut": {
        171: "坚果",
        524: "板栗",
        526: "板栗仁",
        921: "混合坚果",
        1382: "板栗肉",
        1696: "综合坚果",
    },
    "allergy_dairy": {
        669: "奶粉",
        682: "三花淡奶",
        1154: "脱脂奶粉",
        1234: "淡奶",
        1997: "全脂奶粉",
    },
    "allergy_egg": {
        42: "蛋清",
        841: "全蛋",
        1035: "水煮蛋",
        1391: "无菌蛋",
        1607: "鹅蛋",
        1930: "松花蛋",
        2009: "鸽蛋",
    },
    "allergy_fish": {291: "河鳗", 441: "泥鳅", 1531: "银鲳", 1707: "白鳝"},
    "allergy_shellfish": {
        856: "瑶柱",
        1053: "六头鲍",
        1236: "澳洲带子",
        1414: "南日鲍",
        1634: "带子肉",
        2131: "80头干瑶柱",
    },
    "allergy_soy": {
        86: "香干",
        92: "豆豉",
        411: "豆豉酱",
        476: "豆豉辣椒油",
        686: "千张结",
        707: "千张",
        1180: "豆豉油辣椒",
        1215: "素鸡",
        1238: "老干妈豆豉",
        1396: "豆豉鲮鱼罐头",
        1537: "风味豆豉酱",
        1554: "香辣豆豉酱",
        1593: "黑豆豉",
        1828: "虾米豆豉酱",
        2178: "老干妈风味豆豉",
    },
    "allergy_wheat": {
        8: "蝴蝶面",
        47: "低筋粉",
        136: "烧麦皮",
        360: "金像高筋粉",
        367: "挂面",
        421: "澄面",
        613: "中筋粉",
        677: "高筋粉",
        723: "面团",
        786: "低粉",
        919: "全麦粉",
        975: "高粉",
        1058: "低筋小麦粉",
        1072: "意大利细面",
        1116: "手抓饼",
        1224: "澄粉",
        1266: "长意面",
        1460: "老油条碎",
        1490: "意面",
        1502: "意大利面",
        1648: "小麦粉",
        1681: "面饼",
        1817: "印度飞饼皮",
        1848: "油条",
        2023: "方便面",
        2046: "面筋",
        2167: "中粉",
    },
}

NON_TARGET_NO_HARD_CASES = (
    ("allergy_alcohol", "玫瑰露酒"),
    ("allergy_alcohol", "花雕酒"),
    ("allergy_alcohol", "绍酒"),
    ("allergy_alcohol", "樱桃酒"),
    ("allergy_alcohol", "波特酒"),
    ("allergy_alcohol", "老酒"),
    ("allergy_alcohol", "清酒"),
    ("allergy_alcohol", "苹果酒"),
    ("allergy_alcohol", "烧酒"),
    ("allergy_alcohol", "糟卤"),
    ("allergy_alcohol", "醉麸"),
    ("allergy_tree_nut", "白果"),
    ("allergy_tree_nut", "白果仁"),
)


def _approved_keys() -> set[tuple[str, int]]:
    return {
        (constraint_code, ingredient_id)
        for constraint_code, names in APPROVED_ALLERGY_NAMES.items()
        for ingredient_id in names
    }


def _read_decisions() -> list[dict[str, str]]:
    with DECISIONS_PATH.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _assert_confirmed_cells_are_active(rows: list[dict[str, str]]) -> None:
    by_key = {
        (row["constraint_code"], int(row["ingredient_id"])): row for row in rows
    }
    for constraint_code, ingredient_id in _approved_keys():
        row = by_key[(constraint_code, ingredient_id)]
        assert row["decision"] == "hard_exclude"
        assert row["review_status"] == "approved"
        assert row["reviewer"] == "project_owner"
        assert row["reviewed_at"] == "2026-08-27"
        assert row["evidence"] == (
            "authority=https://www.fda.gov/food/buy-store-serve-safe-food/"
            "food-allergies-what-you-need-know;"
            f"matched_patterns=exact_name:{APPROVED_ALLERGY_NAMES[constraint_code][ingredient_id]};"
            "review_policy=project_owner-batch-2026-08-27"
        )


def test_confirmed_allergy_scope_has_exactly_70_literal_members() -> None:
    assert {code: len(names) for code, names in APPROVED_ALLERGY_NAMES.items()} == {
        "allergy_tree_nut": 6,
        "allergy_dairy": 5,
        "allergy_egg": 7,
        "allergy_fish": 4,
        "allergy_shellfish": 6,
        "allergy_soy": 15,
        "allergy_wheat": 27,
    }
    assert len(_approved_keys()) == 70


def test_real_matcher_hard_excludes_each_confirmed_exact_name_only() -> None:
    ingredients = tuple(
        {"ingredient_id": ingredient_id, "name_canonical": name}
        for names in APPROVED_ALLERGY_NAMES.values()
        for ingredient_id, name in names.items()
    ) + (
        {"ingredient_id": 90001, "name_canonical": "面罩"},
        {"ingredient_id": 90002, "name_canonical": "三花"},
        {"ingredient_id": 90003, "name_canonical": "豆豉鱼调料"},
        {"ingredient_id": 90004, "name_canonical": "糟卤"},
        {"ingredient_id": 90005, "name_canonical": "芝麻油"},
    )
    candidates = {
        (candidate.constraint_code, candidate.ingredient_id): candidate
        for candidate in generate_health_relation_candidates(
            tuple(APPROVED_ALLERGY_NAMES) + ("allergy_alcohol", "allergy_sesame"),
            ingredients,
        )
    }

    for constraint_code, ingredient_id in _approved_keys():
        candidate = candidates[(constraint_code, ingredient_id)]
        assert candidate.suggested_decision == "hard_exclude"
        assert candidate.suggested_evidence.endswith(
            f"matched_patterns=exact_name:{APPROVED_ALLERGY_NAMES[constraint_code][ingredient_id]}"
        )

    assert candidates[("allergy_wheat", 90001)].suggested_decision == "no_hard_relation"
    assert candidates[("allergy_dairy", 90002)].suggested_decision == "no_hard_relation"
    assert candidates[("allergy_soy", 90003)].suggested_decision == "no_hard_relation"
    assert candidates[("allergy_alcohol", 90004)].suggested_decision == "no_hard_relation"
    assert candidates[("allergy_sesame", 90005)].suggested_decision == "hard_exclude"
    assert candidates[("allergy_fish", 90005)].suggested_decision == "no_hard_relation"


@pytest.mark.parametrize(("constraint_code", "ingredient_name"), NON_TARGET_NO_HARD_CASES)
def test_real_matcher_does_not_promote_reviewed_non_targets(
    constraint_code: str,
    ingredient_name: str,
) -> None:
    candidate = generate_health_relation_candidates(
        (constraint_code,),
        ({"ingredient_id": 99_001, "name_canonical": ingredient_name},),
    )[0]

    assert candidate.suggested_decision == "no_hard_relation"
    assert candidate.suggested_evidence.endswith("matched_patterns=none")


def test_approved_csv_has_unique_active_decisions_and_expected_final_totals() -> None:
    rows = _read_decisions()
    keys = [(row["constraint_code"], int(row["ingredient_id"])) for row in rows]

    assert len(rows) == 65_588
    assert len(keys) == len(set(keys))
    assert Counter(row["decision"] for row in rows) == {
        "hard_exclude": 1_084,
        "no_hard_relation": 64_504,
    }
    _assert_confirmed_cells_are_active(rows)


def test_confirmed_review_check_rejects_a_controlled_stale_no_hard_row() -> None:
    rows = _read_decisions()
    stale_key = ("allergy_soy", 92)
    stale_rows = [dict(row) for row in rows]
    stale_row = next(
        row
        for row in stale_rows
        if (row["constraint_code"], int(row["ingredient_id"])) == stale_key
    )
    stale_row["decision"] = "no_hard_relation"
    stale_row["evidence"] = "authority=stale;matched_patterns=none"

    with pytest.raises(AssertionError):
        _assert_confirmed_cells_are_active(stale_rows)
