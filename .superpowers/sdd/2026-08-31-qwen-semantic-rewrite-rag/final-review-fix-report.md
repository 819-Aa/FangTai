# Final review fixes — 2026-08-31

- Fixed negative include parsing: `不想吃花生，想吃豆腐` now deterministically yields `exclude_ingredients=(花生,)`, `include_ingredients=(豆腐,)`, and retrieval query `豆腐`. Added timeout and invalid-model-output regression coverage.
- Fixed semantic health projection: `我不能吃花生` and `别吃花生` now project to `p1:禁忌:花生`, including through `QueryPlan.health_exclusions`; existing allergy/disease projection remains covered.

Verification:

- `pytest tests/c3/test_query_normalizer.py tests/c3/test_orchestrator.py -k "not_want_to_eat or semantic_rewrite" -q` — 7 passed.
- Ruff check on the four scoped files — passed.
- `git diff --check` — passed.
- The full two-file run was attempted; two pre-existing data-dependent deterministic-chain tests returned `no_feasible_menu` (52 passed, 2 failed), outside this focused fix.
- Added `不想吃` to model retrieval-query contamination detection; a valid model response containing `不想吃花生 豆腐` now falls back to the safe query `豆腐`.
- Follow-up verification: normalizer focused regression — 1 passed; full normalizer suite — 45 passed; Ruff and `git diff --check` — passed.
