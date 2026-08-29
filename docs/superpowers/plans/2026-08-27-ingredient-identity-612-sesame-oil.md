# Ingredient 612 Sesame-Oil Identity Repair Plan

## Global Constraints

- Scope is only source occurrence `215-11` in recipe `215` (`观音坐莲`): raw text says `芝麻鱼4毫升`, while the recipe step says `淋入芝麻油`; canonical `芝麻油` already exists as ingredient ID `66`.
- Fix the identity at the T06 approved crosswalk boundary. Do not edit the fixed raw recipe CSV and do not merely flip fish/seafood cells while retaining the false identity.
- The corrected occurrence must resolve to ID `66`; `芝麻鱼` must not remain a canonical registry identity. Fish/seafood relations disappear because ID `612` disappears from the health universe; sesame exclusion remains through existing ID `66`.
- The nutrition mapping must use ID `66`'s approved sesame-oil reference (`usda-fdc-171016`), never ID `612`'s current surimi reference (`usda-fdc-173702`).
- Update every approved occurrence-level review row whose identity/name is bound to `215-11`, and remove source decisions whose key is the retired ingredient ID. Preserve all unrelated rows, ordering, signatures, and prior dirty work.
- No fixed-data rebuild, staging publication, Docker/database mutation, API restart, or readiness-count update in this task. Final readiness counts are deferred until all approved data corrections are complete and one final build determines the authoritative counts.
- Use TDD: establish RED against real pipeline behavior, implement the smallest source correction, then run focused and B1 regression tests.
- Do not commit: this worktree contains overlapping user-owned uncommitted changes. Create a task-scoped before/after review package in the ignored SDD workspace instead.

## Task 1: Repair `芝麻鱼` to canonical `芝麻油`

**Expected source/data changes:**

- `src/food_agent_v2/b1/ingredient_identity.py`: recognize `芝麻鱼` as the source typo/synonym for `芝麻油` so an approved crosswalk decision can be applied.
- `data/review/ingredient_identity_overrides.csv`: add one approved merge from `芝麻鱼` to ingredient ID `66`, signed by `project_owner` with the current approved correction date.
- `data/review/health_relation_decisions.csv`: remove all 38 matrix rows keyed by retired ingredient ID `612`; do not edit unrelated cells.
- `data/review/ingredient_nutrition_crosswalk.jsonl`: remove the obsolete ID `612` surimi mapping; retain the existing ID `66` sesame-oil mapping.
- `data/review/ingredient_nutrition_retention_decisions.csv` and `data/review/ingredient_edible_fraction_decisions.csv`: rebind occurrence `215-11` to ID `66` and name `芝麻油`, preserving the approved decisions.
- `data/review/ingredient_quantity_decisions.csv`: correct the display name for occurrence `215-11` to `芝麻油`, preserving the quantity decision and evidence.
- Focused tests: prove the real fixed-source identity pipeline produces no canonical `芝麻鱼`, resolves `215-11` to `66`, emits the alias/crosswalk to `66`, and keeps the quantity; prove the current review inputs have no active ID `612` and use the sesame-oil nutrition mapping.

**Required validation:**

1. Run a focused test before implementation and record a failure caused by the false identity.
2. After implementation, run the same focused test to GREEN.
3. Run `uv run pytest tests/b1/test_ingredient_identity_rebuild.py -q` plus directly affected health/nutrition review tests.
4. Run `uv run pytest tests/b1 -q` and focused Ruff checks.
5. Report projected artifact deltas without building: registry `1771→1770`, aliases `21→22`, crosswalk `431→432`, health matrix `65626→65588`; compute the projected hard-relation delta from the edited source decisions and report it, but do not update readiness constants yet.
6. Write a task report listing exact changed rows/files, RED/GREEN evidence, regression results, and any residual concern. Do not rebuild or publish.
