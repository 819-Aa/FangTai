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
import sys

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


def load_registry(cfg: dict) -> dict[int, str]:
    conn = pymysql.connect(**cfg, charset="utf8mb4")
    cur = conn.cursor()
    cur.execute("SELECT payload FROM fixed_artifact_records "
                "WHERE artifact_name='ingredient_registry'")
    name_map: dict[int, str] = {}
    for (payload,) in cur.fetchall():
        d = json.loads(payload)
        name_map[d.get("ingredient_id")] = d.get("name_canonical", "")
    conn.close()
    return name_map


def load_decisions(csv_path: str) -> dict[tuple[str, int], str]:
    decisions: dict[tuple[str, int], str] = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            decisions[(row["constraint_code"], int(row["ingredient_id"]))] = row["decision"]
    return decisions


def audit(name_map: dict[int, str],
          decisions: dict[tuple[str, int], str]) -> dict[str, list[tuple[int, str]]]:
    """返回 {allergy_code: [(ingredient_id, name), ...]} 疑似漏排。"""
    suspects: dict[str, list[tuple[int, str]]] = {}
    for code, keywords in sorted(ALLERGY_KEYWORDS.items()):
        hits: list[tuple[int, str]] = []
        for (c, iid), decision in decisions.items():
            if c != code or decision != "no_hard_relation":
                continue
            name = name_map.get(iid, "")
            if name and any(kw in name for kw in keywords):
                hits.append((iid, name))
        if hits:
            suspects[code] = sorted(hits)
    return suspects


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=".staging/health_relation_audit.txt")
    args = parser.parse_args()

    cfg = {
        "host": "localhost", "port": 33307,
        "user": "foodagent", "password": "foodagent_v2",
        "database": "food_agent_v2",
    }
    name_map = load_registry(cfg)
    decisions = load_decisions("data/review/health_relation_decisions.csv")
    suspects = audit(name_map, decisions)

    total = sum(len(v) for v in suspects.values())
    lines: list[str] = []
    lines.append("健康关系矩阵假阴性审计（待人工复核，未自动修改任何决定）")
    lines.append(f"疑似漏排合计: {total} 条\n")
    for code, hits in suspects.items():
        lines.append(f"\n[{code}] {len(hits)} 条疑似漏排:")
        for iid, name in hits:
            lines.append(f"  id={iid} name={name}")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    sys.stdout.write(f"审计完成：{total} 条疑似漏排，已写入 {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
