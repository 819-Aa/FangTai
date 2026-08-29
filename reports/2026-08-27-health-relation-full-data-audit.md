# Health-relation full data-quality audit — 2026-08-27

## Executive verdict

**NOT SAFE TO REBUILD.** The closed matrix is structurally loadable, but it is not semantically safe to freeze into a new publication. The current approved CSV contains **81 confirmed wrong allergy cells** (79 false negatives and 2 false positives) plus **12 high-confidence likely wrong cells** (10 false negatives and 2 false positives). These 93 cells involve **92 unique ingredients** and **281 of 1,932 eligible/RAG dishes** (306 ingredient–recipe links). Rebuilding now would faithfully republish those errors because the current generator and approved CSV agree on every cell.

The deployed/staged H06 snapshot is also older than the approved source: the current CSV has the 12 approved `allergy_seafood` corrections, but T08 still has the former 12 `no_hard_relation` values. Those stale deployed values affect **13 eligible/RAG dishes**. This audit treats the current CSV values as approved, as instructed; it does not apply or republish them.

A separate structural defect reinforces the stop decision: `ingredient_registry.jsonl` under-reports recipe provenance for **230/1,771 ingredients (12.99%)**, omitting **2,784 ingredient–recipe links** and under-counting **2,980 occurrences** relative to `recipe_ingredient_relations.jsonl`. The 230 affected ingredients occur in **1,879/1,932 eligible recipes**. This does not break the current key lookup, but it makes registry-based traceability and impact analysis unsafe.

No source, staging, database, Docker, test, or code file was changed. This report is the only created artifact.

## Scope and interpretation

- Authoritative decision input: `data/review/health_relation_decisions.csv`.
- Snapshot identity used for recipe impact: build `fefd8bd7-dafa-4cc5-be0a-40ca22939392`, source manifest `0d0e133fa86b063d1f254edaacba71ae4575413b0a1baa0bd81fd7827825f596`.
- Snapshot inputs: T06 ingredient registry; T07 health and retrieval views; T08 candidates, decisions, relations, and coverage.
- Semantic scope: all 15 allergy codes requested. Disease, indicator, and population advice was not reinterpreted as allergy logic.
- `CONFIRMED` means the ingredient identity or the recipe step evidence directly contradicts the cell. `HIGH` means the culinary identity is strong but a product label or ontology definition could still change the result. `AMBIGUOUS` is manual review only. `NON-issue` is an explicitly checked guard or expected projection difference.
- “Affected recipe” below means the ingredient is present in the current T07 health view, hence also maps to an eligible retrieval view/RAG dish. Registry `appears_in_recipes` was not trusted for impact because of the structural drift described below.

## Compact data profile

| Dataset | Grain / key | Rows | Quality result |
|---|---:|---:|---|
| Approved CSV | `(constraint_code, ingredient_id)` | 65,626 | Exactly `38 × 1,727`; 65,626 unique keys; 0 exact duplicates; 0 missing/extra eligible keys |
| Ingredient registry | `ingredient_id` | 1,771 | 1,771 unique IDs; 0 blank canonical names; 0 normalized-name duplicates; 1 build/hash; all `approved` |
| Recipe health views | `recipe_id` | 1,932 | 1,932 unique; all `eligible`, `atomic`, unresolved count 0; 1,727 distinct ingredient IDs; 0 unknown IDs |
| Retrieval build views | `recipe_id` | 1,932 | Same recipe key set/build/hash as health view |
| T08 candidates | `(constraint_code, ingredient_id)` | 65,626 | Exact current key/name coverage, but rows carry no build/hash fields by schema |
| T08 staged decisions | `(constraint_code, ingredient_id)` | 65,626 | Internally consistent with staged relations/coverage, but 12 cells stale versus current CSV |
| T08 staged hard relations | relation ID | 985 | Exact projection of the older T08 decision snapshot |
| T08 coverage | `constraint_code` | 38 | 38 unique rows; each reports 1,727 reviewed/universe ingredients; counts match old relations |

CSV completeness and loader-contract checks:

- Header is exactly the seven required columns: `constraint_code, ingredient_id, decision, evidence, review_status, reviewer, reviewed_at`.
- Blank values: 0 in every required column.
- Status: 65,626/65,626 `approved`; pending/rejected/modified/non-approved rows: 0.
- Reviewer: 65,626/65,626 `project_owner`; self-signature check passes against builder identity `food-agent-v2:T08`.
- Review dates: 65,398 rows dated `2026-08-10`; 228 rows dated `2026-08-23`; all ISO dates.
- Decision counts: 64,629 `no_hard_relation`; 997 `hard_exclude`.
- The production loader accepted all 65,626 rows and returned 997 hard exclusions.
- SHA-256 of the audited CSV: `294106de0da218ede133d501640a7f2e2fb233072ddf62939dd939489a464159`.

Build and projection consistency:

- Registry, health view, retrieval view, staged decisions, relations, and coverage each contain exactly one identical build ID and source manifest hash.
- The CSV has no build columns, but its ingredient key set equals the 1,727-ingredient health-view universe for every one of the 38 codes.
- Current source matcher suggestions and current approved decisions agree on **65,626/65,626 cells**, including **25,905/25,905 allergy cells** and all 997 hard suggestions. This is agreement, not independent validation: the semantic errors below are shared by both.
- Health and retrieval ingredient lists differ in 8 links across 7 recipes. All 8 retrieval-only links are `is_alternative=true` members of a choice group, so the health view's selected-default projection is working as designed. This is a **NON-issue**, not matrix drift.

## Allergy-code screening summary

| Constraint code | Current hard | CONFIRMED wrong | HIGH likely wrong | AMBIGUOUS/manual | Verdict |
|---|---:|---:|---:|---:|---|
| `allergy_peanut` | 13 | 0 | 0 | 0 | No issue found |
| `allergy_tree_nut` | 13 | 6 FN | 1 FN | 2 | Unsafe |
| `allergy_dairy` | 40 | 5 FN | 1 FN | 2 | Unsafe |
| `allergy_egg` | 21 | 7 FN | 0 | 2 | Unsafe |
| `allergy_seafood` | 164 | 1 FP | 0 | 0 | Current CSV also has 12 approved corrections absent from deployed T08 |
| `allergy_shrimp` | 39 | 0 | 1 FN | 0 | Likely unsafe |
| `allergy_crab` | 10 | 0 | 0 | 2 possible FP | Manual review |
| `allergy_fish` | 60 | 4 FN + 1 FP | 1 FN | 0 | Unsafe |
| `allergy_shellfish` | 27 | 6 FN | 0 | 0 | Unsafe |
| `allergy_soy` | 30 | 15 FN | 4 FN + 2 FP | 9 | Unsafe |
| `allergy_wheat` | 30 | 27 FN | 0 | 3 | Unsafe |
| `allergy_sesame` | 12 | 0 | 0 | 0 | No issue found; ID 612 remains sesame-positive after identity repair |
| `allergy_mango` | 5 | 0 | 0 | 0 | No issue found |
| `allergy_pineapple` | 5 | 0 | 0 | 0 | No issue found |
| `allergy_alcohol` | 17 | 9 FN | 2 FN | 0 | Unsafe |

`FN` = current `no_hard_relation` should be reviewed toward `hard_exclude`; `FP` = current `hard_exclude` should be reviewed toward `no_hard_relation` or, where stated, repaired at ingredient identity level first.

## CONFIRMED semantic issues

### Tree nut, dairy, egg, fish, and shellfish

| constraint_code | ingredient_id | name | current decision | suggested correction | affected recipe IDs + names | reason |
|---|---:|---|---|---|---|---|
| allergy_tree_nut | 171 | 坚果 | no_hard_relation | hard_exclude | 33 红枣核桃糯糕; 200 酸奶坚果番薯泥; 1578 香蕉燕麦能量棒 | Explicit generic nuts |
| allergy_tree_nut | 524 | 板栗 | no_hard_relation | hard_exclude | 177 糖桂花烤栗子; 565 红烧栗子鸡; 711 糖烤栗子; 940 糖烤栗子; 1935 栗子焖羊肉 | Same identity as already-hard 栗子 |
| allergy_tree_nut | 526 | 板栗仁 | no_hard_relation | hard_exclude | 1570 香菇板栗烧鸡; 1974 板栗老鸭汤 | Chestnut kernel |
| allergy_tree_nut | 921 | 混合坚果 | no_hard_relation | hard_exclude | 417 能量棒面包 | Explicit mixed nuts |
| allergy_tree_nut | 1382 | 板栗肉 | no_hard_relation | hard_exclude | 849 栗子烧鸡 | Chestnut flesh |
| allergy_tree_nut | 1696 | 综合坚果 | no_hard_relation | hard_exclude | 1202 酸奶红薯泥 | Explicit assorted nuts |
| allergy_dairy | 669 | 奶粉 | no_hard_relation | hard_exclude | 245 网红牛奶烤吐司; 249 金钱小面包; 316 手指小面包; 346 阿拉棒; 419 芋泥鲜奶; 421 奶黄小餐包; 472 南瓜吐司; 504 天使白面包; 635 蛋白糖; 662 南瓜吐司; 665 肉松面包卷; 768 蒜香面包; 971 小餐包; 999 小餐包; 1010 芒果冰激凌; 1050 蒸小米发糕; 1069 葱花花卷; 1075 花环椰蓉面包; 1088 牛奶布丁; 1230 曲奇饼干; 1255 葡萄干奶酥球; 1263 牛奶杏仁排包; 1328 维也纳蛋糕; 1356 奶香饼干; 1364 芝士面包圈; 1440 豆沙一口酥; 1494 奶香糯米窝窝头; 1521 日式馒头; 1608 巧克力蔓越莓饼干; 1610 曲奇饼干（多层）; 1642 蔓越莓三色吐司; 1660 抹茶蜜豆吐司; 1775 椰蓉开口酥; 1797 乳酪黄金月饼; 1817 奶酪包; 1818 可可黄油饼干; 1858 奶香黄油曲奇; 1889 菠萝包; 1894 芝士焗南瓜泥; 1922 黑豆浆蒸黑米蛋糕; 1944 酒酿馒头; 1985 辫子面包 | Explicit milk powder |
| allergy_dairy | 682 | 三花淡奶 | no_hard_relation | hard_exclude | 251 马拉糕; 863 奶香苏打饼干 | Evaporated milk product |
| allergy_dairy | 1154 | 脱脂奶粉 | no_hard_relation | hard_exclude | 610 芝士泡菜面包 | Explicit skim milk powder |
| allergy_dairy | 1234 | 淡奶 | no_hard_relation | hard_exclude | 698 奶茶 | Evaporated milk |
| allergy_dairy | 1997 | 全脂奶粉 | no_hard_relation | hard_exclude | 1672 双色刀切馒头 | Explicit whole milk powder |
| allergy_egg | 42 | 蛋清 | no_hard_relation | hard_exclude | 6 杏仁瓦片小饼干; 61 蒸马蹄肉丸; 131 香菇酿肉丸; 149 嫩滑蒸鱼片; 162 蒸藕夹; 215 观音坐莲; 264 虎皮蛋糕卷; 295 鱼香春卷; 335 肉丸; 389 巧克力马卡龙; 397 全麦戚风（6寸）; 426 手指饼干; 428 温州鱼饼; 504 天使白面包; 664 虾滑; 671 比司吉蛋糕; 672 泰式马蹄虾饼; 801 抹茶达克瓦兹; 911 年年有余—清蒸东星斑; 953 鱼饼; 1114 水煮肉片; 1168 水晶萝卜狮子头; 1226 翡翠鳕鱼; 1238 水果盒子蛋糕; 1307 红丝绒卷蛋糕; 1338 福州鱼丸; 1340 番茄虾滑; 1342 蒸鱼丸; 1635 彩椒鸡胸肉; 1636 时蔬鸡肉串; 1680 猪肉包菜卷; 1732 无糖大米饼; 1947 藤椒笋壳鱼; 1988 黄鱼河虾宴球 | Explicit egg white; alias missing from matcher |
| allergy_egg | 841 | 全蛋 | no_hard_relation | hard_exclude | 357 水果蛋挞; 686 巴斯克蛋糕; 1614 香橙玛德琳; 1713 红茶戚风; 1906 火焰布丁 | Whole egg |
| allergy_egg | 1035 | 水煮蛋 | no_hard_relation | hard_exclude | 495 卤一锅 | Boiled egg |
| allergy_egg | 1391 | 无菌蛋 | no_hard_relation | hard_exclude | 860 鸡蛋牛奶炖燕窝 | Pasteurized/sterile egg |
| allergy_egg | 1607 | 鹅蛋 | no_hard_relation | hard_exclude | 1100 蒸鹅蛋 | Goose egg |
| allergy_egg | 1930 | 松花蛋 | no_hard_relation | hard_exclude | 1550 三色蒸蛋 | Preserved egg |
| allergy_egg | 2009 | 鸽蛋 | no_hard_relation | hard_exclude | 1700 银耳鸽蛋羹 | Pigeon egg |
| allergy_fish | 291 | 河鳗 | no_hard_relation | hard_exclude | 74 烤鳗鱼 | Eel is explicitly a fish ingredient |
| allergy_fish | 441 | 泥鳅 | no_hard_relation | hard_exclude | 129 豆豉剁椒蒸泥鳅 | Loach is explicitly a fish ingredient |
| allergy_fish | 1531 | 银鲳 | no_hard_relation | hard_exclude | 1004 菇香银鲳 | Pomfret identity; omitted because name lacks 鱼 |
| allergy_fish | 1707 | 白鳝 | no_hard_relation | hard_exclude | 1215 酒酿蒸白鳝 | Eel identity; omitted because name lacks 鱼 |
| allergy_shellfish | 856 | 瑶柱 | no_hard_relation | hard_exclude | 367 广式荷香糯米鸡 | Dried scallop |
| allergy_shellfish | 1053 | 六头鲍 | no_hard_relation | hard_exclude | 510 低温慢煮卤鲍鱼 | Abalone |
| allergy_shellfish | 1236 | 澳洲带子 | no_hard_relation | hard_exclude | 700 豉汁带子蒸豆腐 | Scallop |
| allergy_shellfish | 1414 | 南日鲍 | no_hard_relation | hard_exclude | 887 泡菜牛奶炖大连鲍 | Abalone |
| allergy_shellfish | 1634 | 带子肉 | no_hard_relation | hard_exclude | 1139 芝士焗带子 | Scallop meat |
| allergy_shellfish | 2131 | 80头干瑶柱 | no_hard_relation | hard_exclude | 1899 石斛洋参炖响螺汤 | Dried scallop |

### Soy

| constraint_code | ingredient_id | name | current decision | suggested correction | affected recipe IDs + names | reason |
|---|---:|---|---|---|---|---|
| allergy_soy | 86 | 香干 | no_hard_relation | hard_exclude | 14 石锅拌饭; 62 韭菜香干; 858 蒸香干; 1172 青蒜慈菇炒香干; 1329 轻松一锅蒸; 1337 五味青团; 1576 素三丁 | Tofu product |
| allergy_soy | 92 | 豆豉 | no_hard_relation | hard_exclude | 15 豉汁蒜香蒸排骨; 30 湘味炒肉; 129 豆豉剁椒蒸泥鳅; 167 豉汁烤排骨; 187 无糖豉汁蒸排骨; 191 豉汁凤爪蒸排骨; 257 干蒸排骨; 268 腊味合蒸; 381 蒜蓉土豆蒸排骨; 424 清蒸葱白豆腐汤; 460 豉汁蒸排骨; 463 豆豉蒸五花肉; 501 蒜香豆豉蒸秋葵; 557 豆豉蒸黄骨鱼; 579 豆豉蒸青椒; 585 豉汁蒸牛柳; 620 豉汁蒸鲍鱼; 760 豉汁蒸鲈鱼; 802 豉椒花蛤; 835 豉汁蒸鲈鱼; 858 蒸香干; 947 双椒鳙鱼头; 962 香蒜豆豉蒸苦瓜; 992 时蔬烤鱼; 1082 梅酱蒸梭子蟹; 1209 豆豉蒸腐竹; 1242 豆豉蒸排骨; 1286 豆豉蒸腐竹（台式）; 1289 双椒鳙鱼头（台式）; 1291 豉汁凤爪; 1329 轻松一锅蒸; 1360 港式豉汁蒸鱼; 1542 豉汁蒸蛤蜊; 1569 南瓜蒸排骨; 1627 豆豉蒸排骨; 1654 豆豉蒸鱼; 1663 农家小炒鸡; 1729 豆豉鳕鱼; 1756 醉麸豆豉蒸黄鱼; 1774 青椒牛肚丝; 1932 豆豉蒸草鱼; 1963 葱香豆豉蒸扇贝 | Fermented soybean ingredient |
| allergy_soy | 411 | 豆豉酱 | no_hard_relation | hard_exclude | 115 清蒸排骨; 1201 豆豉辣酱蒸鱼片; 1204 豆豉酱蒸鸡腿 | Contains fermented soybean ingredient |
| allergy_soy | 476 | 豆豉辣椒油 | no_hard_relation | hard_exclude | 143 老干妈蒸茄子 | Contains fermented soybean ingredient |
| allergy_soy | 686 | 千张结 | no_hard_relation | hard_exclude | 254 千张结烧肉 | Tofu-sheet product |
| allergy_soy | 707 | 千张 | no_hard_relation | hard_exclude | 263 嫩烤鱼; 1426 嫩烤鱼; 1546 淮南牛肉汤; 1764 松茸双档海参盅 | Tofu-sheet product |
| allergy_soy | 1180 | 豆豉油辣椒 | no_hard_relation | hard_exclude | 628 香辣孜然烤鱿鱼须 | Contains fermented soybean ingredient |
| allergy_soy | 1215 | 素鸡 | no_hard_relation | hard_exclude | 668 蒜蓉蘸汁素鸡; 1526 红烧素鸡 | Soy/tofu product |
| allergy_soy | 1238 | 老干妈豆豉 | no_hard_relation | hard_exclude | 700 豉汁带子蒸豆腐; 1093 浏阳蒸鸡 | Contains fermented soybean ingredient |
| allergy_soy | 1396 | 豆豉鲮鱼罐头 | no_hard_relation | hard_exclude | 867 豆豉鲮鱼油麦菜 | Contains fermented soybean ingredient |
| allergy_soy | 1537 | 风味豆豉酱 | no_hard_relation | hard_exclude | 1008 冬菇滑鸡煲 | Contains fermented soybean ingredient |
| allergy_soy | 1554 | 香辣豆豉酱 | no_hard_relation | hard_exclude | 1029 豉汁蒸凤爪 | Contains fermented soybean ingredient |
| allergy_soy | 1593 | 黑豆豉 | no_hard_relation | hard_exclude | 1071 剁椒蒸鱿鱼 | Fermented black soybean |
| allergy_soy | 1828 | 虾米豆豉酱 | no_hard_relation | hard_exclude | 1394 虾米豆豉蒸排骨 | Contains fermented soybean ingredient |
| allergy_soy | 2178 | 老干妈风味豆豉 | no_hard_relation | hard_exclude | 1956 干锅包菜 | Contains fermented soybean ingredient |

### Wheat

| constraint_code | ingredient_id | name | current decision | suggested correction | affected recipe IDs + names | reason |
|---|---:|---|---|---|---|---|
| allergy_wheat | 8 | 蝴蝶面 | no_hard_relation | hard_exclude | 2 蔬菜焗蝴蝶面 | Wheat pasta |
| allergy_wheat | 47 | 低筋粉 | no_hard_relation | hard_exclude | 6 杏仁瓦片小饼干; 176 蔓越莓饼干; 192 椰丝球; 212 蔓越莓饼干（多层）; 216 老婆饼; 462 鲜肉月饼; 574 无水蜂蜜蛋糕; 770 蔓越莓饼干; 805 英式司康; 832 清爽百香果挞; 1055 小米红薯三角包; 1307 红丝绒卷蛋糕; 1440 豆沙一口酥; 1572 网红古早蛋糕; 1593 蛋黄酥（多层）; 1697 蛋黄酥; 1857 巧克力树莓蛋糕; 1875 红豆沙蒸蛋糕; 1984 清新小森林饼干 | Low-gluten wheat flour abbreviation |
| allergy_wheat | 136 | 烧麦皮 | no_hard_relation | hard_exclude | 24 玻璃烧麦; 523 草原羊肉烧麦 | Wheat wrapper |
| allergy_wheat | 360 | 金像高筋粉 | no_hard_relation | hard_exclude | 95 鲜虾火腿披萨2 | Bread flour |
| allergy_wheat | 367 | 挂面 | no_hard_relation | hard_exclude | 96 葱爆面; 783 红焖羊肉面 | Wheat noodles |
| allergy_wheat | 421 | 澄面 | no_hard_relation | hard_exclude | 120 榴莲冰皮月饼; 145 椰丝糯米糍; 184 榴莲班戟; 242 萝卜丝蒸紫菜滑肉; 259 虾饺; 280 水晶韭菜蒸饺; 670 冰皮月饼（米博）; 807 韭菜饺; 893 水晶白菜蒸饺; 1121 冰皮月饼; 1171 荠菜鲜肉青团; 1652 鲜虾粉果; 1751 萝卜糕; 1837 蜂蜜枣糕 | Wheat starch |
| allergy_wheat | 613 | 中筋粉 | no_hard_relation | hard_exclude | 216 老婆饼; 292 翡翠白菜饺; 332 意式薄底披萨; 462 鲜肉月饼; 928 广式月饼（台式）; 1593 蛋黄酥（多层）; 1697 蛋黄酥; 1730 豆苗树叶饺; 1792 广式月饼 | Medium-gluten wheat flour abbreviation |
| allergy_wheat | 677 | 高筋粉 | no_hard_relation | hard_exclude | 249 金钱小面包; 434 全麦吐司; 971 小餐包; 999 小餐包; 1544 葱香曲奇; 1619 咖喱鸡肉面包; 1831 日式长崎蛋糕 | High-gluten wheat flour abbreviation |
| allergy_wheat | 723 | 面团 | no_hard_relation | hard_exclude | 271 麻酱花卷 | Recipe dough is wheat-based in source context |
| allergy_wheat | 786 | 低粉 | no_hard_relation | hard_exclude | 317 法式草莓蛋糕 | Low-gluten wheat flour abbreviation |
| allergy_wheat | 919 | 全麦粉 | no_hard_relation | hard_exclude | 417 能量棒面包; 434 全麦吐司; 452 免揉基础小欧包 | Whole-wheat flour |
| allergy_wheat | 975 | 高粉 | no_hard_relation | hard_exclude | 452 免揉基础小欧包 | High-gluten wheat flour abbreviation |
| allergy_wheat | 1058 | 低筋小麦粉 | no_hard_relation | hard_exclude | 515 柠檬鳕鱼 | Explicit wheat flour |
| allergy_wheat | 1072 | 意大利细面 | no_hard_relation | hard_exclude | 531 意式肉酱面 | Wheat pasta |
| allergy_wheat | 1116 | 手抓饼 | no_hard_relation | hard_exclude | 564 手抓饼热狗; 909 创意手抓饼面包 | Wheat pastry |
| allergy_wheat | 1224 | 澄粉 | no_hard_relation | hard_exclude | 688 抹茶紫薯冰皮月饼; 1127 绵软广式馒头; 1400 钵仔糕; 1429 水晶虾饺; 1533 南瓜豆沙冰皮月饼; 1573 黄金猪排 | Wheat starch |
| allergy_wheat | 1266 | 长意面 | no_hard_relation | hard_exclude | 738 红酱肉丸意面 | Wheat pasta |
| allergy_wheat | 1460 | 老油条碎 | no_hard_relation | hard_exclude | 922 温州糯米饭 | Wheat fried dough |
| allergy_wheat | 1490 | 意面 | no_hard_relation | hard_exclude | 950 蘑菇培根意面; 1207 青酱意面; 1776 奶油培根意面 | Wheat pasta |
| allergy_wheat | 1502 | 意大利面 | no_hard_relation | hard_exclude | 963 白酒鲜虾意大利面; 1668 培根茄汁意面 | Wheat pasta |
| allergy_wheat | 1648 | 小麦粉 | no_hard_relation | hard_exclude | 1153 经典扬州烧麦 | Explicit wheat flour |
| allergy_wheat | 1681 | 面饼 | no_hard_relation | hard_exclude | 1188 韩式部队火锅; 1566 韩式部队火锅 | Wheat noodle cake in source context |
| allergy_wheat | 1817 | 印度飞饼皮 | no_hard_relation | hard_exclude | 1390 快手香蕉派 | Wheat pastry sheet |
| allergy_wheat | 1848 | 油条 | no_hard_relation | hard_exclude | 1419 黑米粢饭团 | Wheat fried dough |
| allergy_wheat | 2023 | 方便面 | no_hard_relation | hard_exclude | 1726 蒸泡面 | Wheat instant noodles |
| allergy_wheat | 2046 | 面筋 | no_hard_relation | hard_exclude | 1764 松茸双档海参盅 | Wheat gluten |
| allergy_wheat | 2167 | 中粉 | no_hard_relation | hard_exclude | 1944 酒酿馒头 | Medium-gluten wheat flour abbreviation |

### Alcohol and the confirmed ingredient-identity false positive

| constraint_code | ingredient_id | name | current decision | suggested correction | affected recipe IDs + names | reason |
|---|---:|---|---|---|---|---|
| allergy_alcohol | 664 | 玫瑰露酒 | no_hard_relation | hard_exclude | 241 太白拉糕 | Explicit alcoholic cooking wine |
| allergy_alcohol | 745 | 花雕酒 | no_hard_relation | hard_exclude | 290 花雕红烧肉; 432 麻辣捞汁小海鲜; 457 鲜沙姜生焗文昌鸡; 512 蟹酿橙; 719 腊味芥蓝炒饭; 757 花雕蒸蟹; 1486 广式啫啫鸡 | Explicit cooking wine |
| allergy_alcohol | 804 | 绍酒 | no_hard_relation | hard_exclude | 328 松香基围虾 | Shaoxing wine alias |
| allergy_alcohol | 952 | 樱桃酒 | no_hard_relation | hard_exclude | 440 黑森林蛋糕 | Explicit fruit wine |
| allergy_alcohol | 1421 | 波特酒 | no_hard_relation | hard_exclude | 890 红酒烩牛肉 | Port wine |
| allergy_alcohol | 1741 | 老酒 | no_hard_relation | hard_exclude | 1285 葱油小黄鱼 | Explicit cooking wine in source context |
| allergy_alcohol | 1867 | 清酒 | no_hard_relation | hard_exclude | 1456 酒蒸蛤蜊 | Sake |
| allergy_alcohol | 1947 | 苹果酒 | no_hard_relation | hard_exclude | 1580 烤火鸡 | Explicit cider/fruit wine |
| allergy_alcohol | 2109 | 烧酒 | no_hard_relation | hard_exclude | 1866 烤猪排 | Distilled spirit |
| allergy_seafood | 612 | 芝麻鱼 | hard_exclude | repair identity, then no_hard_relation | 215 观音坐莲 | Recipe step says “淋入芝麻油”; registry/source display says 芝麻鱼 and family=鱼族. This is sesame oil misidentified as fish, not seafood. |
| allergy_fish | 612 | 芝麻鱼 | hard_exclude | repair identity, then no_hard_relation | 215 观音坐莲 | Same confirmed identity error. Keep the sesame relation after remapping to 芝麻油. |

## HIGH-confidence likely issues

| constraint_code | ingredient_id | name | current decision | suggested correction | affected recipe IDs + names | reason / residual uncertainty |
|---|---:|---|---|---|---|---|
| allergy_tree_nut | 961 | 榛果糖浆 | no_hard_relation | likely hard_exclude | 446 榛果拿铁 | Explicit hazelnut-flavoured syrup; verify product label for real nut content |
| allergy_dairy | 2032 | 婴儿奶粉 | no_hard_relation | likely hard_exclude | 1742 蛋黄溶豆 | Generic infant milk powder is likely milk-based; verify formula label because soy/non-dairy formula exists |
| allergy_shrimp | 423 | 小青龙 | no_hard_relation | likely hard_exclude | 121 蒜蓉粉丝蒸龙虾; 596 芝士焗龙虾 | Culinary identity is spiny lobster and current shrimp matcher already groups lobster aliases; formal `allergy_shrimp` ontology is not documented |
| allergy_fish | 1578 | 海参斑 | no_hard_relation | likely hard_exclude | 1054 蒜蓉蒸海参斑 | Commercial fish name; current broad seafood relation exists, but fish alias is missing |
| allergy_soy | 1216 | 蒸鱼豆豉油 | no_hard_relation | likely hard_exclude | 668 蒜蓉蘸汁素鸡; 1353 葱香腊肠蒸鲈鱼 | Explicit 豆豉 condiment; verify formulation |
| allergy_soy | 1275 | 厚百叶 | no_hard_relation | likely hard_exclude | 740 韭香双笋蛏肉百叶卷 | In this culinary context 厚百叶 is tofu sheet; regional term can also be confused with tripe |
| allergy_soy | 1519 | 黑豆 | no_hard_relation | likely hard_exclude | 984 黑芝麻丸; 1606 黑豆酿梨; 1922 黑豆浆蒸黑米蛋糕 | Chinese recipe usage is likely black soybean; confirm cultivar/product identity |
| allergy_soy | 1608 | 薄百叶 | no_hard_relation | likely hard_exclude | 1101 鸡汁百叶包 | In this culinary context 薄百叶 is tofu sheet; regional ambiguity remains |
| allergy_soy | 196 | 日本豆腐 | hard_exclude | likely no_hard_relation after label check | 43 番茄肉末蒸日本豆腐; 136 蒸三鲜; 405 虾仁玉子豆腐; 542 海鲜日本豆腐烧 | Common “Japanese/egg tofu” can be egg-based rather than soy; registry family assignment alone is unsafe |
| allergy_soy | 1638 | 鸡蛋豆腐 | hard_exclude | likely no_hard_relation after label check | 1142 花开牡丹虾 | Registry category/family is egg, and recipe calls it egg tofu; soy content needs label confirmation |
| allergy_alcohol | 1516 | 糟卤 | no_hard_relation | likely hard_exclude | 980 醉糟鲜鲍鱼配鱼子酱; 1215 酒酿蒸白鳝; 1557 糟卤冰镇小龙虾 | Fermented wine-lees marinade; residual alcohol/product formulation should be confirmed |
| allergy_alcohol | 2041 | 醉麸 | no_hard_relation | likely hard_exclude | 1756 醉麸豆豉蒸黄鱼 | “Drunken” prepared gluten strongly implies wine; product recipe should be confirmed |

## The 12 approved seafood corrections and stale deployed impact

Current CSV decision is `hard_exclude` for all 12. Current matcher suggestion is also `hard_exclude`. The older staged T08 decision is `no_hard_relation` for all 12, so a new publication must carry the approved CSV delta—but only after the other blockers in this report are resolved.

| ingredient_id | name | current CSV | staged T08 | affected eligible/RAG recipes | re-check result |
|---:|---|---|---|---|---|
| 69 | 牡蛎 | hard_exclude | no_hard_relation | 12 清蒸海蛎子 | Correct explicit seafood |
| 291 | 河鳗 | hard_exclude | no_hard_relation | 74 烤鳗鱼 | Correct explicit aquatic fish |
| 423 | 小青龙 | hard_exclude | no_hard_relation | 121 蒜蓉粉丝蒸龙虾; 596 芝士焗龙虾 | Correct lobster identity |
| 441 | 泥鳅 | hard_exclude | no_hard_relation | 129 豆豉剁椒蒸泥鳅 | Correct explicit aquatic fish |
| 856 | 瑶柱 | hard_exclude | no_hard_relation | 367 广式荷香糯米鸡 | Correct dried scallop |
| 1053 | 六头鲍 | hard_exclude | no_hard_relation | 510 低温慢煮卤鲍鱼 | Correct abalone |
| 1236 | 澳洲带子 | hard_exclude | no_hard_relation | 700 豉汁带子蒸豆腐 | Correct scallop |
| 1414 | 南日鲍 | hard_exclude | no_hard_relation | 887 泡菜牛奶炖大连鲍 | Correct abalone |
| 1531 | 银鲳 | hard_exclude | no_hard_relation | 1004 菇香银鲳 | Correct pomfret |
| 1634 | 带子肉 | hard_exclude | no_hard_relation | 1139 芝士焗带子 | Correct scallop meat |
| 1707 | 白鳝 | hard_exclude | no_hard_relation | 1215 酒酿蒸白鳝 | Correct eel identity |
| 2131 | 80头干瑶柱 | hard_exclude | no_hard_relation | 1899 石斛洋参炖响螺汤 | Correct dried scallop |

The in-memory closed-set seafood audit on the **current CSV** reports: confirmed false negatives 0; known false-positive promotions 0; ambiguous names 0; missing matrix keys 0; unexpected keys 0; duplicate keys 0; unknown registry IDs 0. This result is specific to `allergy_seafood`; it does not validate fish/shellfish/shrimp subcodes.

## AMBIGUOUS / manual-review queue

These rows are not counted in the 81 confirmed or 12 high issues and should not be auto-corrected.

| constraint_code | ingredient IDs + names | current decision | question |
|---|---|---|---|
| allergy_tree_nut | 1068 白果仁; 1253 白果 | no_hard_relation | Decide whether ginkgo nut belongs in the project's tree-nut ontology; the source audit keyword list includes 白果/银杏, but the formal code definition is absent |
| allergy_dairy | 1599 液态酥油; 1697 片状酥油 | no_hard_relation | “酥油” may mean dairy ghee/butter or vegetable shortening in bakery data; product label required |
| allergy_egg | 351 蛋挞皮; 1569 蛋挞胚 | no_hard_relation | Product may or may not contain egg despite the dish name |
| allergy_crab | 459 蟹肉棒; 1905 蟹柳 | hard_exclude | Often imitation crab/surimi; may contain fish, crab flavour, or crab. Product label required before removing crab relation |
| allergy_soy | 181 豆瓣酱; 553 郫县豆瓣; 710 郫县豆瓣酱; 753 红油豆瓣酱; 903 辣豆瓣酱; 1768 青豆瓣; 1853 蚕豆瓣; 1854 六月鲜豆瓣酱; 1865 大豆油 | no_hard_relation | Broad-bean versus soybean formulation varies; highly refined soybean oil policy also needs an explicit project rule |
| allergy_wheat | 204 甜面酱; 351 蛋挞皮; 1569 蛋挞胚 | no_hard_relation | Common formulations contain wheat, but name-only evidence is insufficient for every product |

No external authority was needed to turn these into asserted findings; they remain explicitly unresolved rather than being forced into a medical or regulatory conclusion.

## False-positive guard results

The current source matcher returns `no_hard_relation` for all tested guard names:

- Seafood: 川贝、川贝粉、杏鲍菇、蟹味菇、鲜蟹味菇、贝贝南瓜、海鲜菇、海鲜酱、海鲜酱油、蒸鱼豉油、蒸鱼鼓油、素蚝油、小龙虾调料.
- Fish: 小鲍鱼、鱿鱼、墨鱼、章鱼干、蒸鱼豉油.
- Tree nut: 杜松子.
- Alcohol: 红酒醋、无酒精汽酒.

For current eligible IDs, the approved matrix also preserves the expected negatives for 川贝粉 (5), 杏鲍菇 (608), 蟹味菇 (799), 素蚝油 (945), 贝贝南瓜 (1533), 川贝 (1653), 杜松子 (1686), 红酒醋 (1623), and 无酒精汽酒 (1713). No known guard was promoted to a hard relation.

However, registry classification is unsafe for several of those same guards:

| ingredient_id | name | registry category/family | risk |
|---:|---|---|---|
| 5 | 川贝粉 | 谷物 / 贝族 | Family implies shellfish despite herbal identity |
| 608 | 杏鲍菇 | 水果 / 贝族 | Both category and family are wrong for a mushroom |
| 799 | 蟹味菇 | 菌菇 / 蟹族 | Family implies crab despite mushroom identity |
| 1533 | 贝贝南瓜 | 蔬菜 / 贝族 | Family implies shellfish despite squash identity |
| 1653 | 川贝 | 水产 / 贝族 | Category and family imply shellfish despite herbal identity |
| 612 | 芝麻鱼 | 坚果 / 鱼族 | Confirmed source identity corruption; recipe step uses 芝麻油 |

The current relation matrix's negative guards prevent most immediate false positives, but any future family-based rule would reintroduce them.

## Structural findings

### CONFIRMED — registry provenance drift

Against `T06/recipe_ingredient_relations.jsonl`:

- 230 registry rows have a wrong `appears_in_recipes`, wrong `occurrence_count`, or both.
- 227 rows fail both fields; 3 fail only the occurrence count.
- Registry totals: 14,179 reported unique ingredient–recipe links and 14,513 reported occurrences.
- Relation truth: 16,963 unique ingredient–recipe links and 17,493 relation rows.
- Net under-report: 2,784 unique links and 2,980 occurrences; no extra registry links were found.
- All 230 affected ingredient IDs are in the health matrix; they touch 1,879 eligible health-view recipes.

Examples: 姜 reports 121 recipes/122 occurrences but relations contain 646 recipes/702 rows; 蒜 reports 66/66 versus 419/425; 奶酪 reports 2/2 versus 35/35; 核桃 reports 2/2 versus 25/25. Impact tracing in this report therefore uses T07 health views, not the registry metadata.

### NON-issues and passed integrity checks

- Registry primary keys, recipe keys, matrix composite keys, relation IDs, and coverage-code keys are unique.
- No health-view ingredient points outside the registry.
- The 44 registry ingredients not present in the health-view universe are correctly absent from the closed decision matrix.
- Staged hard relations exactly equal the `hard_exclude` cells in the older staged decisions; staged coverage counts also agree.
- Retrieval/health projection differences are the eight non-default alternatives described above, not unexplained dropped required ingredients.

### Lineage limitations

- Candidate JSONL rows intentionally omit `build_id` and `source_manifest_hash`; exact keys/names match this snapshot, but the artifact cannot self-identify its build outside the staging-directory context.
- The approved CSV also has no build identity or row version. The 12 seafood corrections preserve the older signature date as instructed, so the current file alone cannot prove when those cells changed. This audit accepts their approval status because the task explicitly says they are already approved.
- Name-only screening cannot resolve proprietary condiment, formula, surimi, shortening, or refined-oil composition. Those items remain manual rather than being counted as confirmed.

## Minimal recommended actions

1. **Do not rebuild or publish yet.** Independently review the 81 confirmed cells first, then the 12 high-confidence cells. Preserve reviewer/date/evidence under the existing loader contract.
2. Repair ingredient identity 612 (`芝麻鱼`) at the source/crosswalk level, not only in two allergy cells; retain the sesame relationship after remapping to 芝麻油.
3. Add explicit positive aliases for the confirmed misses (especially egg forms, wheat flour abbreviations/pasta, soy products, alcohol aliases, fish/shellfish aliases) and keep the verified negative guards.
4. Define the project ontology for `allergy_tree_nut`, `allergy_shrimp`, refined soybean oil, imitation crab, and product-formulation rows before resolving ambiguous cells.
5. Fix and test registry provenance aggregation so `appears_in_recipes` and `occurrence_count` equal relation-derived values.
6. After approval, perform one clean build and verify: 65,626 exact keys; current CSV-to-build zero delta; all 12 seafood corrections present; no confirmed/high issue remains; registry provenance reconciles; T07/T08 build/hash identity is single-valued.

## Reproduction commands and scripts used

All probes were read-only and run with bytecode writes disabled. The existing `scripts/audit_health_relations.py` was imported in memory; its CLI was not invoked because its CLI writes an output file.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONUTF8='1'
git status --short
Get-FileHash data/review/health_relation_decisions.csv -Algorithm SHA256
```

Core profile and loader check:

```powershell
@'
import csv, json, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, 'src')
from food_agent_v2.b1.health_relation_builder import (
    ALLOWED_CONSTRAINT_CODES, generate_health_relation_candidates,
)
from food_agent_v2.b1.health_relation_review import (
    load_approved_health_relation_decisions,
)

def load_jsonl(path):
    return [json.loads(x) for x in Path(path).read_text(encoding='utf-8').splitlines() if x.strip()]

registry = load_jsonl('.staging/h06-nutrition-complete-v3/T06/ingredient_registry.jsonl')
health = load_jsonl('.staging/h06-nutrition-complete-v3/T07/recipe_health_views.jsonl')
eligible_ids = tuple(sorted({int(i) for row in health for i in row['ingredient_ids']}))
eligible = tuple(row for row in registry if int(row['ingredient_id']) in set(eligible_ids))
with Path('data/review/health_relation_decisions.csv').open(encoding='utf-8-sig', newline='') as f:
    rows = list(csv.DictReader(f))
decisions = {(r['constraint_code'], int(r['ingredient_id'])): r['decision'] for r in rows}
candidates = generate_health_relation_candidates(ALLOWED_CONSTRAINT_CODES, eligible)
print(len(rows), len(set((r['constraint_code'], int(r['ingredient_id'])) for r in rows)))
print(Counter(r['decision'] for r in rows), Counter(r['review_status'] for r in rows))
print('candidate mismatches', sum(decisions[(c.constraint_code, c.ingredient_id)] != c.suggested_decision for c in candidates))
approved = load_approved_health_relation_decisions(
    Path('data/review/health_relation_decisions.csv'),
    allowed_constraint_codes=ALLOWED_CONSTRAINT_CODES,
    health_ingredient_ids=eligible_ids,
    builder_identity='food-agent-v2:T08',
)
print('loader pass', len(approved))
'@ | .\.venv\Scripts\python.exe -
```

Registry provenance check:

```powershell
@'
import json
from collections import Counter, defaultdict
from pathlib import Path
def load_jsonl(path):
    return [json.loads(x) for x in Path(path).read_text(encoding='utf-8').splitlines() if x.strip()]
registry = load_jsonl('.staging/h06-nutrition-complete-v3/T06/ingredient_registry.jsonl')
relations = load_jsonl('.staging/h06-nutrition-complete-v3/T06/recipe_ingredient_relations.jsonl')
recipes, counts = defaultdict(set), Counter()
for row in relations:
    recipes[int(row['ingredient_id'])].add(int(row['recipe_id']))
    counts[int(row['ingredient_id'])] += 1
bad = []
for row in registry:
    iid = int(row['ingredient_id'])
    if set(map(int, row['appears_in_recipes'])) != recipes[iid] or int(row['occurrence_count']) != counts[iid]:
        bad.append(iid)
print('bad registry rows', len(bad))
print('missing links', sum(len(recipes[i]) - len(set(map(int, next(r for r in registry if int(r['ingredient_id']) == i)['appears_in_recipes']))) for i in bad))
print('occurrence undercount', len(relations) - sum(int(r['occurrence_count']) for r in registry))
'@ | .\.venv\Scripts\python.exe -
```

Seafood correction/guard re-check was performed with the in-memory `audit()` function from `scripts/audit_health_relations.py`, plus direct current-generator probes for the named guard cases. Semantic issue discovery used expanded positive alias lists, conservative negative guards, family/category only as a lead (never as sole proof), and T07 recipe steps/names for confirmation. The curated confirmed/high ID sets and every recipe mapping are fully enumerated in the issue tables above, making the final counts directly auditable from this report.

