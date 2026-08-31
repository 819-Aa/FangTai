# Task 4 implementer report

Implemented ready-build-supported soft-facet projection for C1 retrieval.

- `RecipeRetrievalService.load()` records facet vocabularies from eligible ready-build RAG documents.
- `project_filters()` removes unsupported `dish_type_tags`, `taste_tags`, `cuisine_tags`, and `scenario_tags` only.
- Meal/population/include/exclude filters are copied unchanged; QueryPlan is untouched.
- C3 projects filters before single-person, multi-person, fallback, and expansion retrieval paths.
- Injected retrieval ports without `project_filters()` retain the original filters; projection exceptions propagate.

Validation:

- RED observed first: missing `project_filters` raised `AttributeError` in the new C1 test.
- `uv run pytest tests/c1/test_full_hybrid_retrieval.py tests/c3/test_mc02_online_b2.py -q` — 20 passed.
- `uv run pytest tests/c1 tests/c3/test_mc02_online_b2.py -q` — 31 passed.
- `git diff --check` — passed.

Follow-up C3 path coverage:

- Added tests for projection reaching single-person, multi-person, fallback, and expansion retrieval.
- Added tests for legacy ports retaining filters and projection errors propagating.
- `uv run pytest tests/c1/test_full_hybrid_retrieval.py tests/c3/test_mc02_online_b2.py -q` — 25 passed.
