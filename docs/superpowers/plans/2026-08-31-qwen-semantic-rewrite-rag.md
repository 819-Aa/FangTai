# Qwen Semantic Rewrite and RAG Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task with TDD, per-task commits, reports, and review gates.

**Goal:** Make Qwen query understanding a single structured call with a deterministic, field-preserving fallback, then prevent unsupported soft facets from zeroing H07 retrieval.

**Architecture:** Add a query-understanding-only model request body, normalize and merge semantic fields before QueryPlan creation, and project only ready-build-supported soft facets into `RetrievalFilters`. Keep hard constraints and all downstream safety/time behavior unchanged.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, existing OpenAI-compatible `LLMClient`, existing BM25/Qdrant retrieval service.

## Global Constraints

- Work only in `<repo-root>/.worktrees/recipe-rag-nutrition-time-v2-reviewed-build-20260830`.
- Design source of truth: `docs/superpowers/specs/2026-08-31-qwen-semantic-rewrite-rag-design.md`.
- Do not modify H07 data, MySQL, Qdrant collections/aliases, Redis, B4, C2/B5, or answer-generation behavior.
- Keep `45分钟内` as a hard 2700-second constraint.
- Exactly one `query_understanding` model attempt per normalization request; failures use the deterministic fallback.
- Negative ingredients, disease/allergy/indicator terms, and negation markers must not remain in the positive `retrieval_query`.
- Explicit rule-detected dish count, hard time, meal/population, include/exclude ingredients, health signals, and nutrition goals cannot be deleted by model output.
- Meal, population, include, and exclude filters are never relaxed. Only dish/taste/cuisine/scenario facets may be omitted from RAG hard filters when absent from the loaded ready-build vocabulary.
- Use `apply_patch` for source and test edits. Never write or print API keys. Commit only task-scoped files.
- Follow TDD for every behavior change and record RED/GREEN commands and output in the task report.

## Task 1: Query-understanding-only Qwen request parameters

**Files:**

- Modify: `src/food_agent_v2/core/config.py`
- Modify: `.env.example`
- Modify: `tests/test_config_defaults.py`

**Acceptance criteria:**

- [ ] `LLMConfig` has optional `query_extra_body` without changing existing constructor behavior.
- [ ] `extra_body_for_role("query_understanding")` returns `query_extra_body` when configured; other reasoning roles still return `reasoning_extra_body`; answer generation still returns `answer_extra_body`.
- [ ] `load_config()` parses `LLM_MODEL_QUERY_EXTRA_BODY` as JSON.
- [ ] `.env.example` documents `LLM_MODEL_QUERY_EXTRA_BODY={"enable_thinking":false}` and uses provider-neutral/Qwen-oriented comments without secrets.
- [ ] Focused config tests pass with pristine output.

**TDD steps:**

1. Add tests for role selection and environment parsing.
2. Run `uv run pytest tests/test_config_defaults.py -q` and capture the expected RED failure.
3. Implement the minimal config and example changes.
4. Re-run `uv run pytest tests/test_config_defaults.py -q` and capture GREEN.
5. Run `uv run pytest tests/c3/test_llm_client.py tests/test_config_defaults.py -q` if `tests/c3/test_llm_client.py` exists; otherwise run the config test alone and note that fact.
6. Commit with subject `feat: add query-specific llm request body`.

## Task 2: Single-attempt, field-preserving semantic normalization

**Files:**

- Modify: `src/food_agent_v2/c3/query_normalizer.py`
- Modify: `tests/c3/test_query_normalizer.py`

**Acceptance criteria:**

- [ ] Prompt matches the approved design: closed JSON only, grounded fields, explicit N菜一汤 semantics, hard minute handling, no negative/health content in positive query.
- [ ] `normalize()` makes at most one model call and falls back on transport, timeout, JSON, schema, invalid controlled facet, or positive-query contamination.
- [ ] Controlled validation rejects values such as `meal_types=["dinner"]` and `dish_types=["三菜一汤"]`.
- [ ] Deterministic fallback for `推荐三菜一汤，家常口味，45分钟内` yields dish count 4, dish type `汤`, taste `家常`, and max time 45.
- [ ] Model arrays merge with deterministic explicit fields instead of replacing them; explicit max time and dish count win.
- [ ] `晚餐不要花生，我花生过敏` produces a positive query without `花生`, `过敏`, `不要`, while preserving exclusions/health fields.
- [ ] Existing health grounding behavior remains fail-closed.

**TDD steps:**

1. Update/add tests for one-call fallback, E02, invalid controlled values, field merge, and negative-query sanitation.
2. Run `uv run pytest tests/c3/test_query_normalizer.py -q` and capture RED.
3. Implement the optimized prompt, validation, single attempt, deterministic extraction, and merge helpers.
4. Re-run `uv run pytest tests/c3/test_query_normalizer.py -q` and capture GREEN.
5. Run `uv run pytest tests/c3/test_fast_intent.py tests/c3/test_query_normalizer.py -q`.
6. Commit with subject `feat: make semantic rewrite single-pass and field-safe`.

## Task 3: Preserve router fields when applying the semantic rewrite

**Files:**

- Modify: `src/food_agent_v2/c3/orchestrator.py`
- Modify: `tests/c3/test_orchestrator.py`
- Modify only if needed for focused isolated coverage: `tests/c3/test_query_normalizer.py`

**Acceptance criteria:**

- [ ] `_apply_semantic_rewrite` deduplicates and merges collection fields rather than unconditionally replacing router output.
- [ ] Router-detected dish count and hard time take precedence over missing or conflicting model values.
- [ ] Router health exclusions are preserved; semantic health exclusions fill only when the router has none.
- [ ] Rewritten positive query remains the retrieval query.
- [ ] Existing action conversion and multi-turn behavior remain unchanged.

**TDD steps:**

1. Add focused tests using an explicit routed `IntentDelta` plus a sparse/conflicting `SemanticRewrite`.
2. Run `uv run pytest tests/c3/test_orchestrator.py -q -k semantic_rewrite` and capture RED.
3. Implement a small stable-order merge helper and scalar precedence rules.
4. Re-run the focused test and capture GREEN.
5. Run `uv run pytest tests/c3/test_orchestrator.py tests/c3/test_query_normalizer.py -q`.
6. Commit with subject `fix: preserve deterministic intent fields during rewrite`.

## Task 4: Project only ready-build-supported soft facets into RAG filters

**Files:**

- Modify: `src/food_agent_v2/c1/__init__.py`
- Modify: `src/food_agent_v2/c3/tool_handler.py`
- Modify: `tests/c1/test_full_hybrid_retrieval.py`
- Modify: `tests/c3/test_mc02_online_b2.py`

**Acceptance criteria:**

- [ ] Loaded `RecipeRetrievalService` can project a `RetrievalFilters` instance against facet values present in its current in-memory ready build.
- [ ] Projection may remove only unsupported `dish_type_tags`, `taste_tags`, `cuisine_tags`, and `scenario_tags`.
- [ ] `meal_tags`, `population_tags`, `include_ingredients`, and `exclude_ingredients` remain byte-for-byte/tuple-for-tuple unchanged even when absent from the corpus.
- [ ] `_retrieve_recipes` and `_expand_retrieval` use projected filters before every service retrieval path, including multi-person retrieval.
- [ ] Compatibility with injected test retrieval ports is explicit: if a port does not expose projection, filters remain unchanged.
- [ ] QueryPlan itself is not mutated.

**TDD steps:**

1. Add C1 tests with fixture documents showing supported soft facets retained and unsupported soft facets removed while hard fields remain.
2. Add C3 tests proving projected filters reach single-person, multi-person, and expansion retrieval calls.
3. Run `uv run pytest tests/c1/test_full_hybrid_retrieval.py tests/c3/test_mc02_online_b2.py -q` and capture RED.
4. Implement the minimal projection API and C3 call-site helper.
5. Re-run the focused tests and capture GREEN.
6. Run `uv run pytest tests/c1 tests/c3/test_mc02_online_b2.py -q`.
7. Commit with subject `fix: avoid unsupported soft facet hard filters`.

## Task 5: Integration regression and H07 evidence

**Files:**

- Modify only when a real regression requires a test correction: files already listed in Tasks 1-4.
- Write implementation evidence only to the SDD report/ledger workspace; do not commit live credentials or generated payloads.

**Acceptance criteria:**

- [ ] All targeted core/C1/C3 tests pass.
- [ ] A live H07 diagnostic with process-local `LLM_MODEL_QUERY_EXTRA_BODY={"enable_thinking":false}` confirms `qwen3.8-max`, one query-understanding call, E02 field fidelity, and nonzero RAG candidates.
- [ ] The diagnostic confirms E02 may still have zero feasible menu under the unchanged 2700-second hard limit; this is recorded as expected, not fixed in this task.
- [ ] H07 build ID and MySQL/Qdrant counts remain unchanged.
- [ ] Secret scan and `git status --short` are clean except intended commits/ignored SDD evidence.

**Verification steps:**

1. Run `uv run pytest tests/test_config_defaults.py tests/c1 tests/c3/test_fast_intent.py tests/c3/test_query_normalizer.py tests/c3/test_orchestrator.py tests/c3/test_mc02_online_b2.py -q`.
2. Run the existing evaluation harness or a minimal read-only equivalent against H07 with only the query-specific nonthinking environment override; record commands and summarized outputs without secrets.
3. Verify service readiness, build identity, recipe/vector counts, one-call trace, and E02 retrieval counts.
4. Run a tracked-file secret-pattern scan and `git status --short`.
5. If code/test changes were required, commit them with subject `test: verify qwen semantic rewrite on h07`; otherwise create no empty commit and record verification in the report.
