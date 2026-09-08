# Registry Provenance Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every T06 `ingredient_registry.jsonl` provenance field exactly equal the final resolved `recipe_ingredient_relations.jsonl` truth and prevent future drift with a hard quality gate.

**Architecture:** Keep `recipe_ingredient_relations` as the sole provenance truth after approved merge/form/synonym resolution. Build the registry identity shell first, construct final relations, then replace `occurrence_count` and `appears_in_recipes` from relation-derived aggregates before writing artifacts. Recompute the invariant for the quality report so any future divergence blocks the build.

**Tech Stack:** Python 3.11+, pytest, Ruff, JSONL T06 artifacts.

## Global Constraints

- Work only in `<repo-root>/.worktrees/recipe-rag-nutrition-time-v2` on branch `feature/recipe-rag-nutrition-time-v2`.
- Preserve the existing dirty worktree and unrelated owner/prior-task changes. Do not commit or use destructive Git operations.
- Use strict TDD: create a real failing regression test and record RED before editing production code.
- Scope is the current T06 entry point `rebuild_ingredient_identities()`; do not alter the legacy `build_ingredient_registry()` API or its legacy tests.
- `recipe_ingredient_relations.jsonl` is the source of truth after all approved identity resolution.
- For every registry `ingredient_id`, `occurrence_count` must equal the number of final relation rows with that ID.
- For every registry `ingredient_id`, `appears_in_recipes` must equal the sorted unique integer `recipe_id` values from final relation rows with that ID.
- Registry identities with no final relations must have `occurrence_count=0` and `appears_in_recipes=[]`.
- Add a hard quality gate named `registry_provenance_mismatch_count`; a nonzero value must raise `IngredientIdentityError` with code `REGISTRY_PROVENANCE_MISMATCH`.
- Preserve all existing registry IDs, identity resolution, relations, aliases, forms, categories, families, review state, row counts, and schemas except the two corrected provenance values and the added report/gate field.
- The fresh current 2,000-recipe build must have 1,770 registry rows, 17,493 relation rows, zero provenance mismatches, 17,493 summed registry occurrences, and 16,963 summed unique ingredient-recipe links.
- Do not modify `.staging`, built/cleaned/raw/fixed data, review CSV/JSONL files, health decisions, Docker, DB/MySQL, Qdrant, API, readiness, or runtime state.
- Do not rebuild or publish data in this task. All generated validation artifacts must live in pytest temporary directories or this plan's `.superpowers/sdd/` evidence workspace.

---

### Task 1: Reconcile registry provenance from final relations

**Files:**

- Modify: `src/food_agent_v2/b1/ingredient_identity.py`
- Modify: `tests/b1/test_ingredient_identity_rebuild.py`

**Interfaces:**

- Consumes: final in-memory `recipe_ingredient_relations: list[dict]` created by `rebuild_ingredient_identities()` after approved merge/form/synonym resolution.
- Produces: relation-derived registry `occurrence_count` and sorted `appears_in_recipes`, plus `gates["registry_provenance_mismatch_count"]` and `gates["registry_provenance_mismatch_ids"]`.

- [x] **Step 1: Capture the attributable before state**

Copy the two task files to `.superpowers/sdd/2026-08-28-registry-provenance-reconciliation/task-1-before/`, record SHA-256 hashes and the current HEAD, and save the read-only reproduction evidence:

```text
fresh_registry_rows=1770
fresh_relations=17493
fresh_bad_registry_rows=230
fresh_missing_links=2785
fresh_occurrence_undercount=2981
```

Also record the controlled approved-merge reproduction in which the final target has three relation rows across recipes `[1, 2]`, while the pre-fix registry reports one occurrence and `[1]`.

- [x] **Step 2: Write focused failing behavior tests**

In `tests/b1/test_ingredient_identity_rebuild.py`, add a focused approved-merge regression using two recipes:

```python
rows = make_rows(
    [
        (1, "菜1", "姜10克；姜丝5克"),
        (2, "菜2", "姜丝3克"),
    ]
)
```

Approve `姜丝 -> ingredient_id 1` as a processing variant. Exercise the real `rebuild_ingredient_identities()` and assert the surviving `姜` registry row has literal `occurrence_count == 3` and `appears_in_recipes == [1, 2]`. Independently derive the same values from the emitted final relations so the test catches both alias/form omission and accidental per-recipe occurrence deduplication.

Add a gate test that passes an otherwise-valid gates dictionary with `registry_provenance_mismatch_count=1` and asserts `enforce_quality_gates()` raises `IngredientIdentityError` with code `REGISTRY_PROVENANCE_MISMATCH`.

Extend `test_real_2000_build_quality` to assert:

```text
gates["registry_provenance_mismatch_count"] == 0
gates["registry_provenance_mismatch_ids"] == []
sum(registry occurrence_count) == len(relations) == 17493
sum(len(appears_in_recipes) for registry rows) == 16963
every registry row equals an independently derived relation count and sorted recipe-id set
```

- [x] **Step 3: Verify RED before editing production code**

Run only the new controlled regression and confirm it fails with the old registry value `1/[1]` rather than the required `3/[1, 2]`. Run the gate test and confirm it fails because the new gate is not enforced. Do not edit production code until both failures are attributable to the missing behavior.

- [x] **Step 4: Implement relation-derived provenance**

In `rebuild_ingredient_identities()` initialize registry provenance fields as neutral values (`0` and `[]`). After `recipe_ingredient_relations` is complete and before artifacts are written, aggregate final relation rows by integer `ingredient_id`:

```python
relation_occurrence_counts[ingredient_id] += 1
relation_recipe_ids.setdefault(ingredient_id, set()).add(recipe_id)
```

Assign every registry row from those aggregates, sorting recipe IDs numerically. Do not use `name_clean`, raw occurrence evidence, alias source names, or a maximum recipe-list length.

- [x] **Step 5: Add the hard provenance invariant**

Recompute expected counts and recipe sets from `recipe_ingredient_relations`, compare them with every final registry row, and add:

```python
"registry_provenance_mismatch_count": len(registry_provenance_mismatch_ids),
"registry_provenance_mismatch_ids": registry_provenance_mismatch_ids,
```

to `gates`. Extend `enforce_quality_gates()` so a nonzero mismatch count raises:

```python
IngredientIdentityError(
    "REGISTRY_PROVENANCE_MISMATCH",
    f"注册表来源聚合与最终关系不一致 {count} 项: {ids}",
)
```

Keep the existing gate order and behavior otherwise unchanged. Update the module-level quality-gate description to include registry provenance consistency.

- [x] **Step 6: Verify GREEN and compatibility**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/b1/test_ingredient_identity_rebuild.py -q
& '.\.venv\Scripts\python.exe' -m pytest tests/b1 -q
& '.\.venv\Scripts\python.exe' -m ruff check src/food_agent_v2/b1/ingredient_identity.py tests/b1/test_ingredient_identity_rebuild.py
```

The focused tests, complete B1 suite, and Ruff must exit 0 with no new warnings or failures.

- [x] **Step 7: Perform a fresh data-level audit without publishing**

Run `rebuild_ingredient_identities()` against the verified 2,000-recipe source inside a temporary directory. Independently aggregate the emitted relation JSONL and prove:

```text
registry_rows=1770
relation_rows=17493
bad_registry_rows=0
registry_occurrence_total=17493
relation_occurrence_total=17493
registry_unique_recipe_links=16963
relation_unique_recipe_links=16963
```

Confirm the task did not alter the existing `.staging` snapshot or any prohibited surface.

- [x] **Step 8: Report and prepare independent review**

Write `.superpowers/sdd/2026-08-28-registry-provenance-reconciliation/task-1-report.md` with the before hashes, RED/GREEN evidence, exact diff, focused/full test results, lint result, fresh data-level audit, prohibited-surface audit, and concerns. Produce a task-scoped review package from the before copies because HEAD remains unchanged in the dirty worktree. Do not commit, rebuild, or publish.
