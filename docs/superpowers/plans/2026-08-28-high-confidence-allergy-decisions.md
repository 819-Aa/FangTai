# High-Confidence Allergy Decisions Implementation Plan

> For agentic workers: execute this plan with test-driven development and independent review.

**Goal:** Add the eight owner-approved, high-confidence allergen relations as exact-name hard exclusions while preserving every other reviewed decision.

**Architecture:** Extend the existing guarded exact-name matcher with a separate closed set for this approval batch, regenerate only the eight corresponding decision rows, and lock the scope with regression tests. This is a source-data change only; runtime artifacts and services remain untouched.

**Tech Stack:** Python 3.11+, pytest, Ruff, CSV review inputs.

## Global Constraints

- Work only in `C:\Users\zhiyo\Desktop\竞赛\program_v2\.worktrees\recipe-rag-nutrition-time-v2` on `feature/recipe-rag-nutrition-time-v2`.
- The repository is already dirty with owner and prior-task work. Preserve every unrelated change and do not use destructive Git commands.
- Use strict test-driven development: add a failing focused test before implementation, then implement the minimum change and run the focused and full B1 suites.
- Do not commit. Produce task-scoped snapshots/report/review artifacts so this batch can be reviewed independently from the dirty worktree.
- Do not rebuild or publish data, and do not modify Docker, databases, Qdrant, API services, readiness state, staging data, built data, raw data, or fixed data.
- Add exactly these eight `(constraint_code, ingredient_id, canonical_name)` hard relations and no others:
  - `allergy_tree_nut`, `961`, `榛果糖浆`
  - `allergy_dairy`, `2032`, `婴儿奶粉`
  - `allergy_shrimp`, `423`, `小青龙`
  - `allergy_fish`, `1578`, `海参斑`
  - `allergy_soy`, `1216`, `蒸鱼豆豉油`
  - `allergy_soy`, `1275`, `厚百叶`
  - `allergy_soy`, `1519`, `黑豆`
  - `allergy_soy`, `1608`, `薄百叶`
- Preserve the existing hard relations for `allergy_soy` ingredient `196` (`日本豆腐`) and `1638` (`鸡蛋豆腐`) byte-for-byte at the row-field level.
- Keep all cooking-alcohol decisions and all still-ambiguous cases unchanged. Do not introduce broad substring rules for `奶粉`, `百叶`, `豆豉`, `黑豆`, `酒`, or related terms.
- Matcher evidence for the eight additions is exactly `exact_name:<canonical_name>` and must remain behind the existing relation-name guard.
- The eight review rows must be `hard_exclude`, `approved`, reviewer `project_owner`, date `2026-08-28`, and use the existing FDA authority plus policy `project_owner-batch-2026-08-28`.
- The decision CSV remains 65,588 unique `(constraint_code, ingredient_id)` rows; final totals are exactly 1,072 `hard_exclude` and 64,516 `no_hard_relation`.

## Task 1: Implement and verify the approved exact-name batch

**Files:**

- Modify: `src/food_agent_v2/b1/health_relation_builder.py`
- Modify: `data/review/health_relation_decisions.csv`
- Modify: `tests/b1/test_confirmed_allergy_review_inputs.py`
- Create: `tests/b1/test_high_confidence_allergy_review_inputs.py`

1. Snapshot the task's starting state for the relevant source and CSV files in this plan's `.superpowers/sdd` workspace.
2. Add focused tests containing the literal eight approved triples. Verify the real matcher returns only `exact_name:<canonical_name>` for them, and verify the current CSV has one exact row per triple with the required decision/evidence/review fields.
3. Add regression tests that the CSV retains 65,588 unique keys and reaches exactly 1,072 hard and 64,516 no-hard rows. Include row-field equality guards for the two existing soy hard relations (`196`, `1638`) against their pre-task signatures.
4. Update the prior confirmed-allergy regression list by removing only the seven cases now approved from `NON_TARGET_NO_HARD_CASES`; all cooking alcohol and remaining ambiguous cases stay asserted as no-hard.
5. Run the new focused test and show that it fails for the expected missing eight relations before changing production/source data.
6. Add a separate closed exact-name mapping for this batch and have `_matching_patterns()` recognize either approved exact-name mapping only after the existing guard.
7. Change exactly the eight CSV rows. Use evidence format `authority=https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know;matched_patterns=exact_name:<canonical_name>;review_policy=project_owner-batch-2026-08-28`.
8. Re-run the focused tests, full `tests/b1` suite, and Ruff on the touched Python files.
9. Compare the task snapshot with the final state and prove that the CSV delta is exactly eight rows and the matcher batch is exactly the eight approved names. Record commands, outcomes, changed files, and concerns in the task report.

