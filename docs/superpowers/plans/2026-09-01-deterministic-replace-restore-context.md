# Deterministic Replace/Restore Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route replace and restore commands through the committed C4 menu/query-plan memory, deterministic delta planning, current B2/B4 safety checks, and atomic commit instead of the legacy model workflow.

**Architecture:** C4 exposes the last five committed menu versions with their optional QueryPlan snapshots. Replace resolves exactly one current menu name locally, calls Qwen once only for replacement semantics, and reuses the existing delta/RAG path. Restore selects the version immediately preceding the current version, skips Qwen and RAG, revalidates the exact historical recipe IDs, and commits them as a new version.

**Tech Stack:** Python 3.13, Pydantic artifacts, pytest, MySQL-backed C4 memory, existing C1/B2/B4/C2 tools.

## Global Constraints

- Do not save or send complete free-text chat history.
- Do not add a database table or column and do not modify H07, Qdrant, or Redis data.
- Replace may make at most one Qwen `query_understanding` call; restore makes zero model calls.
- Current menu names and committed versions are resolved deterministically; ambiguous targets must clarify.
- Restore must re-run current B2/B4 and final validation before commit.
- Failed, cancelled, unavailable, or clarification outcomes must not update current menu or QueryPlan.
- Legacy fixed-BUILD R-002 tests are outside this plan.

---

### Task 1: Connect replace and restore to committed memory

**Files:**
- Modify: `src/food_agent_v2/c4/__init__.py`
- Modify: `src/food_agent_v2/c3/orchestrator.py`
- Modify if a reusable pure helper is needed: `src/food_agent_v2/c3/delta_planner.py`
- Test: `tests/c4/test_context_manifest.py`
- Test: `tests/c3/test_orchestrator.py`
- Test: `tests/c3/test_delta_planner.py`

**Interfaces:**
- Consumes: `SessionMemorySource.load_menu_versions(session_id) -> list[dict]`, `SessionMemorySource.load_query_plan(session_id, plan_id) -> dict | None`, `ContextService.get_session_state(session_id) -> dict | None`, `DeltaPlanner.plan(...)`, `QueryNormalizer.normalize(...)`.
- Produces: `get_session_state()["menu_history"]` containing the last five committed versions; deterministic replace target binding; deterministic restore of the immediately previous version.

- [ ] **Step 1: Write failing C4 history projection tests**

Add tests proving that history is ordered, capped at five, carries the optional snapshot for each plan, and identifies the current version by `current_menu_plan_id` rather than list position:

```python
def test_get_session_state_projects_last_five_versioned_query_plans() -> None:
    state = service.get_session_state("sess-history")
    assert [item["plan_id"] for item in state["menu_history"]] == [
        "plan-2", "plan-3", "plan-4", "plan-5", "plan-6",
    ]
    assert state["current_menu"]["plan_id"] == "plan-5"
    assert state["menu_history"][3]["query_plan"]["meal_types"] == ["晚餐"]
    assert state["menu_history"][4]["query_plan"] is None
```

- [ ] **Step 2: Run the C4 test and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/c4/test_context_manifest.py -k "versioned_query_plans" -q
```

Expected: FAIL because `menu_history` is absent from the public session state.

- [ ] **Step 3: Implement the minimal C4 projection**

In `ContextService.get_session_state()`, keep MySQL menu order, cap to `menus[-5:]`, attach the plan-bound snapshot through the existing optional loader, and return both current and history:

```python
history = []
for menu in menus[-5:]:
    item = dict(menu)
    item["query_plan"] = (
        load_query_plan(session_id, item["plan_id"])
        if callable(load_query_plan) else None
    )
    history.append(item)

return {
    # existing fields remain unchanged
    "current_menu": current_menu,
    "query_plan": query_plan,
    "menu_history": history,
}
```

- [ ] **Step 4: Verify C4 GREEN**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/c4/test_context_manifest.py -q
```

Expected: all C4 context tests PASS.

- [ ] **Step 5: Write failing replace-path tests**

Add production-entry tests with a C4 fake returning named current items and a prior QueryPlan. Cover exact target binding, no/multiple target clarification, sparse/failed Qwen output, and direct RAG/DeltaPlanner inputs:

```python
def test_replace_exact_current_name_rejects_only_target_and_keeps_hard_context():
    runner.run(request_id, session_id, "把青椒肉丝换成清蒸鱼", participants)
    assert captured_delta.target_recipe_id == 2
    assert captured_delta.rejected_recipe_ids == (2,)
    assert captured_query_plan.exclude_ingredients == ("花生",)
    assert captured_query_plan.include_ingredients == ("鱼",)
    assert "豆腐" not in captured_query_plan.include_ingredients

def test_replace_without_unique_current_name_needs_clarification():
    runner.run(request_id, session_id, "不要这道", participants)
    assert request_status(request_id) == "needs_clarification"
```

The Qwen fake for the successful case must return only current replacement semantics. A separate unavailable-LLM case must prove the target ID is still rejected through deterministic fallback.

- [ ] **Step 6: Run replace tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/c3/test_orchestrator.py -k "replace and not legacy" -q
```

Expected: FAIL because replace still enters `super()._run_locked(...)`.

- [ ] **Step 7: Implement deterministic replace**

Add focused helpers to `DeterministicRecommendationOrchestrator`:

```python
@staticmethod
def _resolve_replace_target(message: str, current_menu: dict) -> int | None:
    matches = [
        int(item["recipe_id"])
        for item in current_menu.get("items", [])
        if str(item.get("name") or "").strip()
        and str(item["name"]).strip() in message
    ]
    return matches[0] if len(set(matches)) == 1 else None

@staticmethod
def _replace_previous_plan(previous: dict | None) -> dict | None:
    if not previous:
        return None
    keep = {
        "meal_types", "population_tags", "exclude_ingredients",
        "nutrition_goal_codes", "health_exclusions",
        "time_constraint_seconds", "time_constraint_policy",
        "dish_count_requested",
    }
    return {key: value for key, value in previous.items() if key in keep}
```

Remove replace from the legacy early return. When a current menu is absent or the target is not unique, call the existing clarification finalizer. Otherwise call `QueryNormalizer` once with the reduced previous plan, keep `intent="replace"` and `target_recipe_id`, and send it to `_run_delta`.

Update `_run_delta` to pass the actual replace intent to `DeltaPlanner`:

```python
delta = DeltaPlanner().plan(current_ids, intent, safe_ids)
```

Keep the existing special behavior for `reject_plan`; do not coerce replace to `add_constraint`.

- [ ] **Step 8: Verify replace GREEN**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/c3/test_delta_planner.py tests/c3/test_orchestrator.py -k "replace or reject_plan" -q
```

Expected: replace/reject tests PASS and no test expects legacy fallback.

- [ ] **Step 9: Write failing restore-path tests**

Cover exact previous-version selection, zero Qwen calls, zero RAG calls, current B4 revalidation, unavailable IDs, unsafe history, no history, and successful commit memory:

```python
def test_restore_previous_version_revalidates_exact_ids_without_qwen_or_rag():
    runner.run(request_id, session_id, "恢复上一版", participants)
    assert llm.calls == []
    assert retrieval.calls == []
    assert health_evaluation.recipe_ids == [4, 5, 6]
    assert committed_recipe_ids == [4, 5, 6]

def test_restore_without_previous_version_needs_clarification():
    runner.run(request_id, session_id, "回到之前那个方案", participants)
    assert request_status(request_id) == "needs_clarification"

def test_restore_unavailable_recipe_fails_without_commit():
    runner.run(request_id, session_id, "恢复上一版", participants)
    assert error_code(request_id) == "RESTORE_VERSION_UNAVAILABLE"
    assert commit_calls == []
```

- [ ] **Step 10: Run restore tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/c3/test_orchestrator.py -k "restore and not legacy" -q
```

Expected: FAIL because restore still enters the legacy path.

- [ ] **Step 11: Implement deterministic restore**

Add pure previous-version selection and a `_run_restore(...)` path. Select the version immediately before the current plan in committed order:

```python
@staticmethod
def _previous_menu_version(session_state: dict) -> dict | None:
    history = list(session_state.get("menu_history") or [])
    current_id = (session_state.get("current_menu") or {}).get("plan_id")
    current_indexes = [
        index for index, item in enumerate(history)
        if item.get("plan_id") == current_id
    ]
    if len(current_indexes) != 1 or current_indexes[0] == 0:
        return None
    return history[current_indexes[0] - 1]
```

`_run_restore` must:

1. Build current C4/B2 context with the current ready build.
2. Build a fresh QueryPlan artifact from the historical snapshot, or the current snapshot when the historical record is old and lacks one.
3. Verify every historical recipe ID has a current-build B3 view; otherwise fail with `RESTORE_VERSION_UNAVAILABLE`.
4. Call `evaluate_recipe_health` on exactly those IDs and require all of them in `safe_recipe_ids`.
5. Call `generate_feasible_menus` with those IDs as both `safe_recipe_ids` and `locked_recipe_ids`; no retrieval call is allowed.
6. Require the produced plan to contain the same recipe ID set before `_select_validate_answer` and atomic commit.

- [ ] **Step 12: Verify restore GREEN and memory continuity**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/c3/test_orchestrator.py tests/c4/test_context_manifest.py -k "restore or versioned_query_plans or committed_query_plan" -q
```

Expected: all selected tests PASS.

- [ ] **Step 13: Run related regression and static checks**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_config_defaults.py tests/c1 tests/c3/test_fast_intent.py tests/c3/test_query_normalizer.py tests/c3/test_delta_planner.py tests/c3/test_orchestrator.py tests/c3/test_mc02_online_b2.py tests/c3/test_mc03_committed_projection.py tests/c4 -q
& .\.venv\Scripts\python.exe -m ruff check src/food_agent_v2/c3/orchestrator.py src/food_agent_v2/c3/delta_planner.py src/food_agent_v2/c4/__init__.py tests/c3/test_orchestrator.py tests/c3/test_delta_planner.py tests/c4/test_context_manifest.py
git diff --check
```

Expected: all selected tests and Ruff PASS; diff check is clean. Do not run or modify the legacy fixed-BUILD R-002 file.

- [ ] **Step 14: Record evidence and commit**

Append RED/GREEN commands, results, changed files, limitations, and commit hash to this plan's SDD report. Commit production code and tests:

```powershell
git add src/food_agent_v2/c3/orchestrator.py src/food_agent_v2/c3/delta_planner.py src/food_agent_v2/c4/__init__.py tests/c3/test_orchestrator.py tests/c3/test_delta_planner.py tests/c4/test_context_manifest.py
git commit -m "feat: connect deterministic replace restore context"
```
