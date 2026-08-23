# Nutrition Runtime Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让明确营养目标在 B4 安全候选中实质影响 B6/C2 排序，同时阻止语义重写模型凭空增加目标，并修复餐次和审计权威缺口。

**Architecture:** 用户原话和上一轮已验证 QueryPlan 是营养目标的唯一权威，模型只改写检索表达。B6 保持安全候选内百分位评分，C2 保留现有15%–50%多策略权重并显式记录维度启用状态；G12 从原始CSV和批准 enrichment 独立重算餐次。

**Tech Stack:** Python 3.11、Pydantic 2、dataclasses、pytest、FastAPI/Agent 现有模块。

## Global Constraints

- 本计划只消费已经完成的 1932 条营养画像，不修改营养数值、数量或 crosswalk。
- B4 是健康硬过滤唯一权威；B6/C2 只能消费当前请求的 `safe_recipe_ids`。
- 无明确营养目标时必须禁用营养维度，不生成通用健康分。
- 模型不得从“健康一点、营养均衡”推导具体营养目标。
- 原 label 已有餐次时禁止增加；原 label 完全为空时只允许 approved enrichment。
- 保留 `nutrition_score: float` 兼容字段，但只有 enabled 状态为 true 时才有评分语义。
- 每个任务只提交列出的文件。

---

### Task 1: 修复 G12 餐次独立权威

**Files:**
- Modify: `src/food_agent_v2/b1/quality_gates.py`
- Modify: `src/food_agent_v2/b1/rebuild.py`
- Modify: `tests/b1/test_quality_gate_authority.py`
- Modify: `tests/b1/test_recipe_profile_enrichment.py`

**Interfaces:**
- Consumes: 固定原始 recipe rows 和 `load_recipe_profile_enrichments(...)` 的 approved 记录。
- Produces: `expected_meal_tags_by_recipe_id`，供 G12 独立验证 retrieval 和 profile view。

- [ ] **Step 1: 写 raw-empty 自我授权失败测试**

```python
def test_g12_rejects_unapproved_meal_tags_when_raw_label_is_empty() -> None:
    report = run_consumer_view_gates(
        raw_rows=(_raw_recipe(recipe_id=1, labels_raw=""),),
        approved_profile_enrichments={},
        retrieval_views=(_retrieval(recipe_id=1, meal_tags=("晚餐",)),),
        recipe_profiles=(_profile(recipe_id=1, meal_tags=("晚餐",)),),
    )
    assert report.gate("G12_RAG_PROFILE_CONSISTENCY").status == "failed"
```

覆盖 raw 非空时 enrichment 不能增加、raw 空且 approved enrichment 时通过、pending/rejected enrichment 不生效。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/b1/test_quality_gate_authority.py tests/b1/test_recipe_profile_enrichment.py -q`

Expected: FAIL，当前 expected 值会回退到 retrieval 自身。

- [ ] **Step 3: 独立重算 expected meal tags**

```python
def expected_meal_tags(
    raw_labels: str,
    approved_enrichment: RecipeProfileEnrichment | None,
) -> tuple[str, ...]:
    raw = meal_tags_from_raw_labels(raw_labels)
    return raw if raw else tuple(approved_enrichment.meal_tags if approved_enrichment else ())
```

G12 直接接收该独立映射，删除 `raw_meal_tags or retrieval_meal_tags`。

- [ ] **Step 4: 运行专项测试**

Run: `uv run pytest tests/b1/test_quality_gate_authority.py tests/b1/test_recipe_profile_enrichment.py tests/b1/test_consumer_view_consistency.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/food_agent_v2/b1/quality_gates.py src/food_agent_v2/b1/rebuild.py tests/b1/test_quality_gate_authority.py tests/b1/test_recipe_profile_enrichment.py
git commit -m "fix: verify meal tags from independent authority"
```

### Task 2: 将营养目标锁定到用户显式语义

**Files:**
- Modify: `src/food_agent_v2/c3/query_normalizer.py`
- Modify: `tests/c3/test_query_normalizer.py`
- Modify: `tests/c3/test_orchestrator.py`

**Interfaces:**
- Consumes: 当前 message 和上一轮已验证 `previous_query_plan.nutrition_goal_codes`。
- Produces: `extract_explicit_nutrition_goals(...)`、当前原话证据和模型不可扩大的 `SemanticRewrite.nutrition_goal_codes`。

- [ ] **Step 1: 写模型凭空增加目标的失败测试**

```python
def test_model_cannot_add_a_nutrition_goal_without_explicit_user_words() -> None:
    llm = _FakeLLM(responses=[json.dumps({
        "retrieval_query": "健康晚餐",
        "meal_types": ["晚餐"],
        "nutrition_goal_codes": ["low_sodium", "high_protein"],
    }, ensure_ascii=False)])
    rewrite = QueryNormalizer(llm).normalize("推荐健康一点的晚餐", ("p1",))
    assert rewrite.nutrition_goal_codes == ()
    assert rewrite.nutrition_goal_evidence == {}
```

再覆盖“低钠/低盐/少盐/控钠”“高蛋白”“高纤维”“低脂”“高钙”“高铁/补铁”、否定表达、未知 code、上一轮已验证目标延续和明确取消。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/c3/test_query_normalizer.py tests/c3/test_orchestrator.py -q`

Expected: FAIL，当前 subset 校验允许模型增加 code。

- [ ] **Step 3: 实现受控显式提取器**

```python
def extract_explicit_nutrition_goals(
    message: str,
    previous_goal_codes: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], dict[str, str]]:
    evidence = {
        code: match.group(0)
        for pattern, code in _NUTRITION_GOAL_PATTERNS
        if (match := pattern.search(message)) and not _is_negated(match, message)
    }
    cancelled = extract_cancelled_nutrition_goals(message)
    previous = () if _cancels_all_nutrition_goals(message) else tuple(
        code for code in previous_goal_codes if code not in cancelled
    )
    codes = tuple(dict.fromkeys((*previous, *evidence.keys())))
    return codes, evidence
```

所有返回 code 必须属于 `food_agent_v2.b6.GOAL_SPECS`。`nutrition_goal_evidence: dict[str, str]` 只保存当前 message 中规范化前的原话片段；上一轮目标的权威来自上一轮已验证 QueryPlan，不伪造当前轮证据。明确“取消低钠/不需要高蛋白”只删除对应目标；“普通就行/取消营养要求”清空全部。

- [ ] **Step 4: 让模型只贡献改写，不贡献目标权威**

在 `SemanticRewrite` 和模型封闭输出中增加 `nutrition_goal_evidence`。解析模型输出后，将 `nutrition_goal_codes` 和证据字段都覆盖为受控提取结果；`_preserves_explicit_semantics` 对营养目标要求相等而非 subset。模型仍可生成 retrieval query、meal/population/taste 等允许字段，但不得删除用户明确约束。

- [ ] **Step 5: 运行专项测试**

Run: `uv run pytest tests/c3/test_query_normalizer.py tests/c3/test_orchestrator.py tests/c3/test_runner_chain.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/food_agent_v2/c3/query_normalizer.py tests/c3/test_query_normalizer.py tests/c3/test_orchestrator.py
git commit -m "fix: ground nutrition goals in user language"
```

### Task 3: 显式记录 C2 营养维度启用状态

**Files:**
- Modify: `src/food_agent_v2/c2/schemas.py`
- Modify: `src/food_agent_v2/c2/planner.py`
- Modify: `tests/c2/test_deterministic_plans.py`
- Modify: `tests/c2/test_menu_hard_constraints.py`

**Interfaces:**
- Consumes: B6 `NutritionScoreDecomposition`。
- Produces: internal `FeasibleMenu` 的 enable/disable、reason、unavailable dimensions 和实际归一化权重。

- [ ] **Step 1: 写禁用状态和权重失败测试**

```python
def test_no_goal_disables_nutrition_without_treating_zero_as_a_score() -> None:
    plan = make_planner().plan(MenuHardConstraints(dish_count=5))[0]
    assert plan.nutrition_score == 0.0
    assert plan.nutrition_dimension_enabled is False
    assert plan.nutrition_disabled_reason == "no_explicit_nutrition_goal"
    assert "nutrition" in plan.unavailable_dimensions
    assert "nutrition" not in plan.weights_applied
    assert sum(plan.weights_applied.values()) == pytest.approx(1.0)
```

覆盖明确目标且全菜可用时 enabled、明确目标但任一菜 unavailable 时 reason、15%–50%策略权重和 B4 safe set 不变。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/c2/test_deterministic_plans.py tests/c2/test_menu_hard_constraints.py -q`

Expected: FAIL，internal FeasibleMenu 尚无审计字段。

- [ ] **Step 3: 实现活动维度和归一化权重**

```python
def _active_weights(
    weights: dict[str, float], dims: dict[str, float | None]
) -> dict[str, float]:
    active = {name: weights[name] for name, value in dims.items() if value is not None}
    total = sum(active.values())
    return {name: value / total for name, value in active.items()}
```

`_score_menu` 同时保存 `weights_applied` 和 `unavailable_dimensions`。没有 goal 与数据 unavailable 使用不同稳定 reason。

- [ ] **Step 4: 运行专项测试**

Run: `uv run pytest tests/c2/test_deterministic_plans.py tests/c2/test_menu_hard_constraints.py tests/b6/test_soft_evidence_availability.py -q`

Expected: PASS；B6 百分位算法无需改变。

- [ ] **Step 5: 提交**

```bash
git add src/food_agent_v2/c2/schemas.py src/food_agent_v2/c2/planner.py tests/c2/test_deterministic_plans.py tests/c2/test_menu_hard_constraints.py
git commit -m "feat: audit nutrition scoring availability"
```

### Task 4: 将营养审计状态投影到正式 Artifact

**Files:**
- Modify: `src/food_agent_v2/contracts/artifacts.py`
- Modify: `src/food_agent_v2/c3/runner.py`
- Modify: `src/food_agent_v2/application/menu_projection.py`
- Modify if used by projection: `src/food_agent_v2/application/commit_service.py`
- Modify: `tests/contracts/test_artifact_schemas.py`
- Modify: `tests/c3/test_artifact_grounding.py`
- Modify: `tests/application/test_menu_projection.py`

**Interfaces:**
- Consumes: Task 3 的 internal `FeasibleMenu`。
- Produces: `MenuScoreDecomposition` 的可复验审计状态。

- [ ] **Step 1: 写正式 Artifact 失败测试**

```python
def test_menu_artifact_distinguishes_disabled_nutrition_from_zero_score() -> None:
    score = MenuScoreDecomposition(
        total_score=0.7, rag_score=0, preference_score=0.5,
        nutrition_score=0.0, time_score=0.8, diversity_score=0.8,
        historical_score=0, nutrition_dimension_enabled=False,
        nutrition_disabled_reason="no_explicit_nutrition_goal",
        unavailable_dimensions=("nutrition",),
        weights_applied={"time": 0.5, "diversity": 0.5},
    )
    assert score.nutrition_dimension_enabled is False
```

覆盖 enabled/disabled 字段一致性、weights sum、runner 不再丢弃 internal 字段、旧 numeric score 仍可序列化。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/contracts/test_artifact_schemas.py tests/c3/test_artifact_grounding.py tests/application/test_menu_projection.py -q`

Expected: FAIL，runner 当前只投影数值分。

- [ ] **Step 3: 增量扩展 Artifact 并接线**

```python
nutrition_dimension_enabled: bool
nutrition_disabled_reason: str | None = None
```

校验：enabled 时 reason 必须为空且 nutrition 不在 unavailable；disabled 时必须有 reason 且 nutrition 在 unavailable。C3 从 C2 原样投影 `weights_applied/unavailable_dimensions`。

- [ ] **Step 4: 更新应用投影和兼容测试**

正常回答和前端仍不得显示内部营养值或评分；新增字段仅用于内部 Artifact 审计和提交一致性。

- [ ] **Step 5: 运行专项测试**

Run: `uv run pytest tests/contracts/test_artifact_schemas.py tests/c3/test_artifact_grounding.py tests/application/test_menu_projection.py tests/c3/test_runner_chain.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/food_agent_v2/contracts/artifacts.py src/food_agent_v2/c3/runner.py src/food_agent_v2/application/menu_projection.py src/food_agent_v2/application/commit_service.py tests/contracts/test_artifact_schemas.py tests/c3/test_artifact_grounding.py tests/application/test_menu_projection.py
git commit -m "feat: project nutrition audit state"
```

### Task 5: 运行完整推荐链回归

**Files:**
- Verify only: Tasks 1–4 修改文件。

**Interfaces:**
- Consumes: 已完成的 H06 营养 Artifact 和运行时接线。
- Produces: H06 发布计划可使用的稳定在线行为。

- [ ] **Step 1: 运行 B1/B6/C2/C3 专项回归**

Run: `uv run pytest tests/b1/test_quality_gate_authority.py tests/b1/test_recipe_profile_enrichment.py tests/b6 tests/c2 tests/c3/test_query_normalizer.py tests/c3/test_orchestrator.py tests/c3/test_runner_chain.py tests/c3/test_artifact_grounding.py tests/contracts/test_artifact_schemas.py -q`

Expected: PASS。

- [ ] **Step 2: 运行四个行为场景**

```text
老人晚餐 → meal/population进入C1，随后B4
低钠晚餐 → B6启用low_sodium
高蛋白低脂晚餐 → 两目标等权
健康一点的晚餐 → nutrition_goal_codes为空
```

用集成测试 fixture 断言 C1→B4→B6/C2 顺序和最终 B4 复核，禁止依赖自然语言回答判断。

- [ ] **Step 3: 运行静态检查**

Run: `uv run ruff check src/food_agent_v2/b1 src/food_agent_v2/b6 src/food_agent_v2/c2 src/food_agent_v2/c3 src/food_agent_v2/contracts tests/b1 tests/b6 tests/c2 tests/c3 tests/contracts`

Expected: PASS。

- [ ] **Step 4: 主代理审核**

核对无模型新增目标、无目标结果不变、营养不能越过B4、Artifact禁用语义明确、G12独立权威。通过后进入H06发布计划。
