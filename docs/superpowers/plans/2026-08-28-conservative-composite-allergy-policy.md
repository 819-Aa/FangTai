# Conservative Composite-Allergy Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the owner-approved safety-first allergy policy to exactly twelve composite/product-name relations while preserving the ten explicitly resolved non-target relations.

**Architecture:** Add a separate closed exact-name matcher layer whose meaning is “do not recommend under this allergy because reasonable composition risk exists,” distinct from confirmed and high-confidence identity layers. Update exactly twelve approved CSV cells and protect both positive and negative policy boundaries with real-generator regression tests.

**Tech Stack:** Python 3.11+, pytest, Ruff, CSV review inputs.

## Global Constraints

- Work only in `<repo-root>/.worktrees/recipe-rag-nutrition-time-v2` on `feature/recipe-rag-nutrition-time-v2`.
- Preserve the dirty worktree and all unrelated owner/prior-task changes. Do not commit or use destructive Git operations.
- Use strict TDD: add a focused behavior test, run and record the expected RED result, then implement the minimum source/data change.
- `hard_exclude` in this batch means the recipe must not be recommended to a user with the matching allergy because a reasonable composition risk exists; it does not assert that every commercial formulation contains the allergen.
- Add exactly twelve closed exact-name hard relations:
  - `allergy_dairy`: `1599 液态酥油`, `1697 片状酥油`
  - `allergy_soy`: `181 豆瓣酱`, `753 红油豆瓣酱`, `903 辣豆瓣酱`, `1854 六月鲜豆瓣酱`, `1865 大豆油`
  - `allergy_wheat`: `204 甜面酱`, `351 蛋挞皮`, `1569 蛋挞胚`, `553 郫县豆瓣`, `710 郫县豆瓣酱`
- Preserve exactly these ten policy boundaries:
  - no hard: `allergy_tree_nut/1068 白果仁`, `allergy_tree_nut/1253 白果`
  - no hard: `allergy_egg/351 蛋挞皮`, `allergy_egg/1569 蛋挞胚`
  - existing hard unchanged: `allergy_crab/459 蟹肉棒`, `allergy_crab/1905 蟹柳`
  - no hard: `allergy_soy/553 郫县豆瓣`, `allergy_soy/710 郫县豆瓣酱`, `allergy_soy/1768 青豆瓣`, `allergy_soy/1853 蚕豆瓣`
- Do not add broad substring rules for `酥油`, `豆瓣`, `豆瓣酱`, `大豆`, `面酱`, `蛋挞`, `面`, or related terms. The existing guard must remain first.
- Matcher evidence for the twelve additions is exactly `exact_name:<canonical_name>`.
- The twelve CSV rows use `decision=hard_exclude`, `review_status=approved`, `reviewer=project_owner`, `reviewed_at=2026-08-28`, and policy `project_owner-conservative-2026-08-28` with the existing FDA allergy authority.
- The CSV remains exactly 65,588 unique keys and finishes with 1,084 `hard_exclude` plus 64,504 `no_hard_relation` rows.
- Change no CSV row outside the twelve approved keys. Do not change cooking-alcohol decisions.
- Do not rebuild or publish data and do not touch Docker, DB, MySQL, Qdrant, API, readiness, staging, built, cleaned, raw, fixed, or runtime state.

---

### Task 1: Implement the closed conservative-allergy batch

**Files:**

- Modify: `src/food_agent_v2/b1/health_relation_builder.py`
- Modify: `data/review/health_relation_decisions.csv`
- Modify: `tests/b1/test_confirmed_allergy_review_inputs.py`
- Modify: `tests/b1/test_high_confidence_allergy_review_inputs.py`
- Create: `tests/b1/test_conservative_allergy_policy.py`

**Interfaces:**

- Consumes: existing `_matching_patterns(code, name, patterns)` guard-first exact/pattern matching behavior.
- Produces: a separate `_CONSERVATIVE_ALLERGY_EXACT_NAMES` closed mapping and twelve exact-name hard candidates.

- [ ] **Step 1: Capture the attributable before state**

Copy every file that may be edited into this plan's SDD workspace and record SHA-256 hashes, CSV row/key/decision counts, the twelve target rows, the ten preservation rows, the non-target-row canonical digest, the alcohol-row digest, and a task-start timestamp/path inventory for prohibited surfaces.

- [ ] **Step 2: Write focused literal behavior tests**

Create `tests/b1/test_conservative_allergy_policy.py` with independent literal tables for all twelve approved triples and all ten preserved triples. Exercise `generate_health_relation_candidates()` rather than grepping source. Assert exact candidate decisions/evidence, exact active CSV row fields, controlled stale-row rejection, preserved full row fields, unique keys, and final decision totals.

The test must catch these realistic mutations:

```text
missing any approved exact relation
adding a broad 豆瓣/蛋挞/酥油 substring relation
incorrectly promoting 白果/白果仁, egg-allergy 蛋挞皮/蛋挞胚,
or soy-allergy 郫县豆瓣/郫县豆瓣酱/青豆瓣/蚕豆瓣
removing or rewriting the existing crab rows
wrong reviewer/date/policy/evidence on any of the twelve rows
```

- [ ] **Step 3: Verify RED before production/data edits**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/b1/test_conservative_allergy_policy.py tests/b1/test_confirmed_allergy_review_inputs.py tests/b1/test_high_confidence_allergy_review_inputs.py -q
```

Expected: the focused module fails because the twelve candidate/CSV relations and final counts do not yet exist; preservation assertions remain green.

- [ ] **Step 4: Add the minimal closed matcher layer**

Add exactly this logical mapping, formatted to project style:

```python
_CONSERVATIVE_ALLERGY_EXACT_NAMES = {
    "allergy_dairy": frozenset({"液态酥油", "片状酥油"}),
    "allergy_soy": frozenset({"豆瓣酱", "红油豆瓣酱", "辣豆瓣酱", "六月鲜豆瓣酱", "大豆油"}),
    "allergy_wheat": frozenset({"甜面酱", "蛋挞皮", "蛋挞胚", "郫县豆瓣", "郫县豆瓣酱"}),
}
```

Make `_matching_patterns()` recognize it after `_relation_name_guarded()` and return `(f"exact_name:{name}",)`. Do not modify broad pattern tables.

- [ ] **Step 5: Update exactly twelve review rows**

Use this evidence template:

```text
authority=https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know;matched_patterns=exact_name:<canonical_name>;review_policy=project_owner-conservative-2026-08-28
```

Set the twelve rows to approved/project_owner/2026-08-28 and change no other CSV row.

- [ ] **Step 6: Reconcile existing regression fixtures**

In `NON_TARGET_NO_HARD_CASES`, remove only `allergy_dairy/液态酥油`, `allergy_dairy/片状酥油`, and `allergy_wheat/甜面酱`; retain both ginkgo names and all alcohol cases. Update both existing global count assertions from `1,072 / 64,516` to `1,084 / 64,504` without changing their earlier batch membership/evidence expectations.

- [ ] **Step 7: Verify GREEN and full B1 health-data compatibility**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/b1/test_conservative_allergy_policy.py tests/b1/test_confirmed_allergy_review_inputs.py tests/b1/test_high_confidence_allergy_review_inputs.py -q
& '.\.venv\Scripts\python.exe' -m pytest tests/b1 -q
& '.\.venv\Scripts\python.exe' -m ruff check src/food_agent_v2/b1/health_relation_builder.py tests/b1/test_conservative_allergy_policy.py tests/b1/test_confirmed_allergy_review_inputs.py tests/b1/test_high_confidence_allergy_review_inputs.py
```

- [ ] **Step 8: Prove exact scope and report**

Compare task-start copies with final files. Prove exactly twelve CSV records changed, all transitions are `no_hard_relation -> hard_exclude`, the twelve keys equal the literal approved set, all other row fields outside those keys are unchanged, alcohol digest is unchanged, the ten policy boundaries retain their specified decisions, and no prohibited surface was written during the task window. Write the complete evidence to the task report and review package. Do not commit, rebuild, or publish.

