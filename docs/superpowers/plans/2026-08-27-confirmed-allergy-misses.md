# Confirmed Allergy Misses Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct exactly 70 approved, unambiguous food-allergen false negatives while preserving every alcohol, high-confidence-only, and ambiguous decision.

**Architecture:** Add a closed exact-name relation layer to the existing health candidate matcher so confirmed aliases do not require unsafe substring expansion. Update exactly the corresponding 70 approved matrix cells, then lock both matcher behavior and active review-input state with literal, mutation-sensitive tests.

**Tech Stack:** Python 3.11+, pytest, CSV review inputs, Ruff.

## Global Constraints

- Scope is exactly the 70 `(constraint_code, ingredient_id, ingredient_name)` triples listed below; no other health decision may change.
- Cooking alcohol is not a hard exclusion. Do not change any `allergy_alcohol` rule or matrix row, including the 9 formerly flagged cooking-wine rows and the 2 high-confidence alcohol rows (`糟卤`, `醉麸`).
- Do not change the 10 remaining high-confidence-only rows or the 20 ambiguous/manual-review rows.
- Preserve all existing seafood false-positive guards and the prior `芝麻鱼 -> 芝麻油` repair.
- Change both the matcher and the approved CSV; a CSV-only symptom fix is forbidden.
- Use an exact-name mapping for these additions. Do not broaden to substrings such as generic `豆豉`, `奶粉`, `百叶`, `面`, or `酒` that would promote unapproved rows.
- Existing schema-required `evidence`, `review_status`, `reviewer`, and `reviewed_at` fields remain; do not add confidence or source columns.
- Approved changed rows use `review_status=approved`, `reviewer=project_owner`, and `reviewed_at=2026-08-27`; evidence must agree with the matcher result.
- Expected active matrix row count remains `65,588`; hard exclusions change `994 -> 1,064`; no-hard decisions change `64,594 -> 64,524`.
- Do not edit raw/fixed recipes, nutrition data, identity data, readiness constants, generated/staged/built artifacts, Docker, databases, Qdrant, API processes, or publication state.
- Do not rebuild or publish in this task.
- Preserve all unrelated user-owned dirty-worktree changes. Do not commit; capture task-scoped before/after evidence instead.
- Follow strict RED -> GREEN TDD. The first focused test run must fail because the 70 approved behaviors are absent.

## Approved exact triples

```python
APPROVED_CONFIRMED_ALLERGY_MISSES = {
    "allergy_tree_nut": {
        171: "坚果", 524: "板栗", 526: "板栗仁", 921: "混合坚果",
        1382: "板栗肉", 1696: "综合坚果",
    },
    "allergy_dairy": {
        669: "奶粉", 682: "三花淡奶", 1154: "脱脂奶粉",
        1234: "淡奶", 1997: "全脂奶粉",
    },
    "allergy_egg": {
        42: "蛋清", 841: "全蛋", 1035: "水煮蛋", 1391: "无菌蛋",
        1607: "鹅蛋", 1930: "松花蛋", 2009: "鸽蛋",
    },
    "allergy_fish": {
        291: "河鳗", 441: "泥鳅", 1531: "银鲳", 1707: "白鳝",
    },
    "allergy_shellfish": {
        856: "瑶柱", 1053: "六头鲍", 1236: "澳洲带子",
        1414: "南日鲍", 1634: "带子肉", 2131: "80头干瑶柱",
    },
    "allergy_soy": {
        86: "香干", 92: "豆豉", 411: "豆豉酱", 476: "豆豉辣椒油",
        686: "千张结", 707: "千张", 1180: "豆豉油辣椒", 1215: "素鸡",
        1238: "老干妈豆豉", 1396: "豆豉鲮鱼罐头", 1537: "风味豆豉酱",
        1554: "香辣豆豉酱", 1593: "黑豆豉", 1828: "虾米豆豉酱",
        2178: "老干妈风味豆豉",
    },
    "allergy_wheat": {
        8: "蝴蝶面", 47: "低筋粉", 136: "烧麦皮", 360: "金像高筋粉",
        367: "挂面", 421: "澄面", 613: "中筋粉", 677: "高筋粉",
        723: "面团", 786: "低粉", 919: "全麦粉", 975: "高粉",
        1058: "低筋小麦粉", 1072: "意大利细面", 1116: "手抓饼",
        1224: "澄粉", 1266: "长意面", 1460: "老油条碎", 1490: "意面",
        1502: "意大利面", 1648: "小麦粉", 1681: "面饼",
        1817: "印度飞饼皮", 1848: "油条", 2023: "方便面",
        2046: "面筋", 2167: "中粉",
    },
}
```

---

### Task 1: Correct and lock the 70 confirmed food-allergen misses

**Files:**

- Modify: `src/food_agent_v2/b1/health_relation_builder.py`
- Modify: `data/review/health_relation_decisions.csv`
- Create: `tests/b1/test_confirmed_allergy_review_inputs.py`
- Modify only if needed for focused matcher coverage: `tests/b1/test_health_relation_matrix.py`

**Interfaces:**

- Consumes: `generate_health_relation_candidates()` and the active approved review CSV.
- Produces: exact-name candidate matches for the 70 triples and exactly 70 approved `hard_exclude` cells.

- [ ] **Step 1: Capture the task-scoped before state**

Copy only the expected task files into the task SDD workspace. Record current matrix totals and a normalized snapshot of every non-target row so the after-state can prove that no unrelated decision changed.

- [ ] **Step 2: Write the failing real-behavior tests**

Create literal table-driven tests that:

1. assert the approved table contains exactly 70 unique triples with the expected per-code counts `6, 5, 7, 4, 6, 15, 27`;
2. call `generate_health_relation_candidates()` on the real names and require `hard_exclude` for all 70;
3. read the active CSV and require each target row to be uniquely approved as `hard_exclude`;
4. require active totals `65,588 / 1,064 / 64,524` after GREEN;
5. lock representative high-confidence and ambiguous names to their current non-target decisions so exact additions cannot broaden them;
6. prove with controlled stale copies that reverting any target to `no_hard_relation` is rejected.

The expectations must be literals independent of production constants.

- [ ] **Step 3: Run RED and record the expected failure**

Run:

```powershell
uv run pytest tests/b1/test_confirmed_allergy_review_inputs.py -q
```

Expected: failure because the real matcher and active CSV still contain the confirmed false negatives. A collection error, malformed fixture, or unrelated failure is not an acceptable RED.

- [ ] **Step 4: Add the minimal exact-name matcher layer**

Add a closed mapping keyed by constraint code whose values are exact canonical names from the approved table. `_matching_patterns()` must treat an exact-name hit as evidence for a hard suggestion while retaining existing pattern matching and guards. Do not add broad substrings that capture non-target names.

- [ ] **Step 5: Update exactly 70 approved CSV cells**

For each approved triple, change `decision` to `hard_exclude`, make `evidence` agree with the new exact matcher, retain the seven-column schema, set `review_status=approved`, `reviewer=project_owner`, and `reviewed_at=2026-08-27`. No non-target row may change.

- [ ] **Step 6: Run GREEN and focused safeguards**

Run:

```powershell
uv run pytest tests/b1/test_confirmed_allergy_review_inputs.py tests/b1/test_health_relation_matrix.py -q
```

Expected: all pass. Then run a scoped comparison against the before snapshot and prove exactly 70 CSV records changed, all from `no_hard_relation` to `hard_exclude`, with zero additions/deletions and zero non-target changes.

- [ ] **Step 7: Run the full B1 suite and lint**

Run:

```powershell
uv run pytest tests/b1 -q
uv run ruff check src/food_agent_v2/b1/health_relation_builder.py tests/b1/test_confirmed_allergy_review_inputs.py tests/b1/test_health_relation_matrix.py
```

Expected: zero failures and clean Ruff output.

- [ ] **Step 8: Produce task evidence without rebuilding**

Write the implementation report and review package with RED/GREEN output, exact changed-row proof, per-code counts, final totals, and explicit evidence that alcohol/high/ambiguous rows, raw data, staging, runtime, and publication state were untouched.
