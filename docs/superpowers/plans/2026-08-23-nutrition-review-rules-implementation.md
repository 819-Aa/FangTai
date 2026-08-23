# Nutrition Review Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可复验的数量、用途和可食比例规则体系，并把 4372 条数量候选与全部营养 occurrence 解析成营养计算可消费的 V2 输入。

**Architecture:** B1 先从标准食材、形态和结构化步骤确定用途与留存语义，再依次应用 occurrence 决定、确定性换算和版本化批量规则。所有模型结果只生成 pending 候选；正式构建只读取 SourceManifest 中的 approved/modified 决定。

**Tech Stack:** Python 3.11、dataclasses、Pydantic 2、Decimal、CSV/JSONL、pytest、ruff。

## Global Constraints

- 原始菜品表中的 eligible dish 固定为 1932 道。
- 不计算烹饪得率、营养保留率、成品重量或份数。
- 模型不得直接批准规则或生成正式营养值。
- 明确质量单位机械换算；模糊量必须命中批准规则或 occurrence 决定。
- `optional`、未选中的 `one_of` 和过程材料不得进入基础营养输入。
- 每个任务只提交自己列出的文件，不夹带工作区已有改动。
- 每个任务完成一遍专项测试；整份计划结束后再运行一次子系统回归。

---

### Task 1: 冻结营养 occurrence V2 语义

**Files:**
- Create: `src/food_agent_v2/b1/nutrition_occurrence_rules.py`
- Create: `tests/b1/test_nutrition_occurrence_rules.py`
- Modify: `src/food_agent_v2/b1/consumer_views.py`
- Modify: `tests/b1/test_consumer_view_consistency.py`

**Interfaces:**
- Consumes: `IngredientOccurrenceFact`、`IngredientIdentityFact`、`StructuredStep`、现有步骤绑定。
- Produces: `UsageCode`、`FuzzyTokenClass`、`NutritionUsageDecisionIndex`、`derive_nutrition_usage(...)`、`NutritionOccurrenceInput` V2。

- [ ] **Step 1: 写用途、模糊量和留存语义的失败测试**

```python
def test_fuzzy_token_and_usage_are_closed_and_deterministic() -> None:
    assert classify_fuzzy_token("少许", "撒少许盐") == "small_amount"
    assert classify_fuzzy_token("几滴", "滴入香油") == "few_drops"
    resolution = derive_nutrition_usage(
        occurrence=_salt_occurrence(),
        identity=_identity(category="condiment"),
        steps=_bound_steps("撒少许盐调味"),
        decisions=NutritionUsageDecisionIndex(()),
    )
    assert resolution.usage_code == "seasoning"
    assert resolution.retained_in_dish is True
```

同时覆盖 `main/supporting/cooking_fat/retained_liquid`、无法唯一推导时要求 occurrence 决定、重复决定和非法枚举拒绝。

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/b1/test_nutrition_occurrence_rules.py tests/b1/test_consumer_view_consistency.py -q`

Expected: FAIL，缺少新模块、V2 字段或推导函数。

- [ ] **Step 3: 实现封闭枚举和正式决定加载器**

```python
UsageCode = Literal["main", "supporting", "seasoning", "cooking_fat", "retained_liquid"]
FuzzyTokenClass = Literal["as_needed", "small_amount", "several_count", "few_drops"]

@dataclass(frozen=True)
class NutritionUsageDecision:
    occurrence_id: str
    usage_code: UsageCode
    retained_in_dish: bool
    review_status: ReviewStatus

def derive_nutrition_usage(
    *, occurrence: IngredientOccurrenceFact, identity: IngredientIdentityFact,
    steps: tuple[StructuredStep, ...], decisions: NutritionUsageDecisionIndex,
) -> NutritionUsageResolution: ...
```

只允许 `approved/modified` 生效。类别和步骤无法唯一确定时返回稳定 `requires_review=True`，禁止猜测。

- [ ] **Step 4: 升级消费者视图**

给 `IngredientIdentityFact` 投影 `category`，给 `NutritionOccurrenceInput` 增加：

```python
normalized_form: str
usage_code: UsageCode
fuzzy_token_class: FuzzyTokenClass | None
retained_in_dish: bool
```

先完成步骤绑定，再构建营养输入；过程材料、`optional` 和未选中的 `one_of` 沿用现有排除逻辑。

- [ ] **Step 5: 运行专项测试**

Run: `uv run pytest tests/b1/test_nutrition_occurrence_rules.py tests/b1/test_consumer_view_consistency.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/food_agent_v2/b1/nutrition_occurrence_rules.py src/food_agent_v2/b1/consumer_views.py tests/b1/test_nutrition_occurrence_rules.py tests/b1/test_consumer_view_consistency.py
git commit -m "feat: version nutrition occurrence semantics"
```

### Task 2: 实现 MeasureRule V2 和确定性数量解析

**Files:**
- Modify: `src/food_agent_v2/b1/quantity_normalizer.py`
- Modify: `tests/b1/test_quantity_normalizer.py`
- Modify: `data/review/ingredient_measure_rules.csv`

**Interfaces:**
- Consumes: Task 1 的 `NutritionOccurrenceInput` V2。
- Produces: 三类互斥规则索引和保持签名的 `normalize_quantity(...) -> QuantityNormalizationResult`。

正式 `ingredient_measure_rules.csv` V2 表头固定为：

```text
schema_version,rule_id,rule_type,ingredient_id,ingredient_name,normalized_form,normalized_unit,usage_code,fuzzy_token_class,to_grams,mass_density_g_per_ml,review_status
```

- [ ] **Step 1: 写 V2 规则键和优先级失败测试**

```python
def test_fuzzy_rule_requires_exact_form_usage_and_token() -> None:
    rules = MeasureRuleIndex((_fuzzy_rule(
        ingredient_id=10, form="净料", usage="seasoning",
        token="small_amount", grams="2.5",
    ),))
    assert normalize_quantity(
        _occurrence(form="净料", usage="seasoning", token="small_amount"),
        rules, QuantityDecisionIndex(()),
    ).standardized_grams == Decimal("2.5")
    assert normalize_quantity(
        _occurrence(form="净料", usage="main", token="small_amount"),
        rules, QuantityDecisionIndex(()),
    ).requires_review is True
```

同时覆盖 occurrence 决定最高优先级、kg/斤/两、半个、密度只用于 ml/L、单位重量只用于个/片/根/勺、旧表头拒绝、重复键和 pending 规则拒绝。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/b1/test_quantity_normalizer.py -q`

Expected: FAIL，现有索引仍是 `(ingredient_name, unit)`。

- [ ] **Step 3: 实现 V2 Schema 和三个查询接口**

```python
@dataclass(frozen=True)
class MeasureRule:
    schema_version: Literal["2.0.0"]
    rule_type: Literal["unit_weight", "density", "fuzzy_single_value"]
    ingredient_id: int
    normalized_form: str
    normalized_unit: str | None
    usage_code: UsageCode | None
    fuzzy_token_class: FuzzyTokenClass | None
    to_grams: Decimal | None
    mass_density_g_per_ml: Decimal | None
    review_status: ReviewStatus
```

实现 `get_unit_weight`、`get_density`、`get_fuzzy_single_value`，构造时校验每类必填字段和唯一键。

- [ ] **Step 4: 实现确定性解析优先级**

```text
occurrence approved/modified decision
→ 克/千克/斤/两直接换算
→ 明确数量 + unit_weight
→ ml/L + density
→ fuzzy_single_value
→ pending
```

“半、一、两 + 明确单位”进入确定性数值解析；“数、几”必须留在模糊分类。

- [ ] **Step 5: 运行专项测试和静态检查**

Run: `uv run pytest tests/b1/test_quantity_normalizer.py -q`

Run: `uv run ruff check src/food_agent_v2/b1/quantity_normalizer.py tests/b1/test_quantity_normalizer.py`

Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add src/food_agent_v2/b1/quantity_normalizer.py tests/b1/test_quantity_normalizer.py data/review/ingredient_measure_rules.csv
git commit -m "feat: add versioned nutrition measure rules"
```

### Task 3: 生成规则级数量候选和稳定异常码

**Files:**
- Create: `src/food_agent_v2/b1/quantity_rule_review.py`
- Create: `tests/b1/test_quantity_rule_review.py`
- Modify: `src/food_agent_v2/b1/quantity_review.py`
- Modify: `src/food_agent_v2/b1/data_review.py`
- Modify: `tests/b1/test_quantity_review.py`

**Interfaces:**
- Consumes: `QuantityReviewCandidate` 和 `MeasureRuleIndex`。
- Produces: `generate_quantity_rule_candidates(...)`、`write_quantity_rule_candidates(...)` 和异常码。

- [ ] **Step 1: 写统计公式和异常分流失败测试**

```python
def test_rule_candidate_requires_five_samples_and_cv_at_most_point_15() -> None:
    rows = tuple(_candidate(value) for value in ("9", "10", "10", "10", "11"))
    result = generate_quantity_rule_candidates(rows, MeasureRuleIndex(()))
    assert result[0].sample_count == 5
    assert result[0].coefficient_of_variation <= Decimal("0.15")
    assert result[0].review_status == "pending"
```

覆盖 sample standard deviation、nearest-rank Q1/Q3、30% 偏差、IQR、分组键冲突、主料和盐油糖高影响模糊量。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/b1/test_quantity_rule_review.py tests/b1/test_quantity_review.py -q`

Expected: FAIL，缺少规则级候选层。

- [ ] **Step 3: 实现 Decimal 统计和稳定分组**

```python
def coefficient_of_variation(values: tuple[Decimal, ...]) -> Decimal:
    mean = sum(values) / Decimal(len(values))
    variance = sum((v - mean) ** 2 for v in values) / Decimal(len(values) - 1)
    return variance.sqrt() / mean

def nearest_rank(values: tuple[Decimal, ...], p: Decimal) -> Decimal:
    ordered = sorted(values)
    rank = (p * Decimal(len(ordered))).to_integral_value(rounding=ROUND_CEILING)
    index = max(1, int(rank)) - 1
    return ordered[index]
```

所有比较在 `Decimal` 上进行；只有最终 CSV 展示值允许量化。

- [ ] **Step 4: 实现异常码和候选输出**

固定异常码：

```text
QTY_MAIN_UNRESOLVED
QTY_HIGH_IMPACT_FUZZY
QTY_RULE_DEVIATION_GT_30PCT
QTY_RULE_IQR_OUTLIER
QTY_FORM_USAGE_AMBIGUOUS
```

模型候选和统计候选只能输出 `review_status=pending`，不能直接写入正式规则文件。

- [ ] **Step 5: 运行专项测试**

Run: `uv run pytest tests/b1/test_quantity_rule_review.py tests/b1/test_quantity_review.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/food_agent_v2/b1/quantity_rule_review.py src/food_agent_v2/b1/quantity_review.py src/food_agent_v2/b1/data_review.py tests/b1/test_quantity_rule_review.py tests/b1/test_quantity_review.py
git commit -m "feat: generate reviewable quantity rules"
```

### Task 4: 增加 occurrence 级可食比例闭环

**Files:**
- Create: `src/food_agent_v2/b1/edible_fraction_review.py`
- Create: `tests/b1/test_edible_fraction_review.py`
- Modify: `src/food_agent_v2/b1/quantity_normalizer.py`
- Modify: `src/food_agent_v2/b1/nutrition_calculator.py`
- Modify: `tests/b1/test_raw_nutrition_calculator.py`
- Modify: `data/review/ingredient_edible_fraction_rules.csv`
- Create: `data/review/ingredient_edible_fraction_decisions.csv`

**Interfaces:**
- Consumes: `NutritionOccurrenceInput` V2。
- Produces: `EdibleFractionDecisionIndex` 和 `resolve_edible_fraction(...) -> EdibleFractionResolution`。

正式文件表头固定为：

```text
ingredient_edible_fraction_rules.csv:
ingredient_id,ingredient_name,normalized_form,edible_fraction,review_status

ingredient_edible_fraction_decisions.csv:
occurrence_id,recipe_id,ingredient_id,ingredient_name,decision_form,edible_fraction,review_status
```

- [ ] **Step 1: 写 occurrence 优先级失败测试**

```python
def test_occurrence_decision_overrides_generic_fraction() -> None:
    resolution = resolve_edible_fraction(
        _occurrence(id="1-1", ingredient_id=7, form="带皮"),
        EdibleFractionRuleIndex((_rule(7, "带皮", "0.80"),)),
        EdibleFractionDecisionIndex((_decision("1-1", "0.65", "modified"),)),
    )
    assert resolution.edible_fraction == Decimal("0.65")
```

覆盖 `0 < fraction <= 1`、唯一键、pending 不生效、空白形态冲突和烹饪得率字段不存在。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/b1/test_edible_fraction_review.py tests/b1/test_raw_nutrition_calculator.py -q`

Expected: FAIL，现有实现仅支持通用 `(name, form)` 规则。

- [ ] **Step 3: 实现正式 Schema 和 resolver**

```python
def resolve_edible_fraction(
    occurrence: NutritionOccurrenceInput,
    rules: EdibleFractionRuleIndex,
    decisions: EdibleFractionDecisionIndex,
) -> EdibleFractionResolution:
    decision = decisions.get_effective(occurrence.occurrence_id)
    if decision is not None:
        return EdibleFractionResolution(decision.edible_fraction, False)
    rule = rules.get(occurrence.ingredient_id, occurrence.normalized_form)
    return EdibleFractionResolution(rule, rule is None)
```

- [ ] **Step 4: 接入营养计算器**

计算器只调用 resolver；未命中继续稳定返回 `edible_fraction_missing` 和全空营养结果。

- [ ] **Step 5: 运行专项测试**

Run: `uv run pytest tests/b1/test_edible_fraction_review.py tests/b1/test_raw_nutrition_calculator.py tests/b1/test_nutrition_availability.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/food_agent_v2/b1/edible_fraction_review.py src/food_agent_v2/b1/quantity_normalizer.py src/food_agent_v2/b1/nutrition_calculator.py tests/b1/test_edible_fraction_review.py tests/b1/test_raw_nutrition_calculator.py tests/b1/test_nutrition_availability.py data/review/ingredient_edible_fraction_rules.csv data/review/ingredient_edible_fraction_decisions.csv
git commit -m "feat: resolve occurrence edible fractions"
```

### Task 5: 将 V2 审核输入纳入固定构建

**Files:**
- Create: `data/review/ingredient_nutrition_usage_decisions.csv`
- Modify: `src/food_agent_v2/b1/source_manifest.py`
- Modify: `src/food_agent_v2/b1/rebuild.py`
- Modify: `src/food_agent_v2/b1/consumer_views.py`
- Modify: `tests/b1/test_fixed_recipe_source.py`
- Modify: `tests/test_b1_pipeline.py`

**Interfaces:**
- Consumes: Tasks 1–4 的加载器和 resolver。
- Produces: `recipe_nutrition_input_views` Schema `2.0.0`，其所有审核输入受 SourceManifest 哈希保护。

`ingredient_nutrition_usage_decisions.csv` 表头固定为：

```text
occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,usage_code,retained_in_dish,review_status
```

- [ ] **Step 1: 写构建身份失败测试**

```python
def test_h06_nutrition_review_inputs_are_hashed() -> None:
    paths = {item.relative_path for item in canonical_build_input_manifest().files}
    assert "data/review/ingredient_nutrition_usage_decisions.csv" in paths
    assert "data/review/ingredient_edible_fraction_decisions.csv" in paths
```

同时断言 `recipe_nutrition_input_views == "2.0.0"`，修改任一审核文件会改变 source manifest hash。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/b1/test_fixed_recipe_source.py tests/test_b1_pipeline.py -q`

Expected: FAIL，新增文件尚未纳入构建身份。

- [ ] **Step 3: 接线构建加载和 Schema 版本**

在 `rebuild.py` 中定义新增路径，加载 usage/edible occurrence decisions，并传给 `publish_downstream_build_views`；将营养输入视图 Schema 升到 `2.0.0`。

- [ ] **Step 4: 运行测试**

Run: `uv run pytest tests/b1/test_fixed_recipe_source.py tests/test_b1_pipeline.py tests/b1/test_consumer_view_consistency.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add data/review/ingredient_nutrition_usage_decisions.csv src/food_agent_v2/b1/source_manifest.py src/food_agent_v2/b1/rebuild.py src/food_agent_v2/b1/consumer_views.py tests/b1/test_fixed_recipe_source.py tests/test_b1_pipeline.py
git commit -m "feat: bind nutrition review inputs to builds"
```

### Task 6: 生成并批准数量与可食比例数据

**Files:**
- Modify: `data/review/ingredient_measure_rules.csv`
- Modify: `data/review/ingredient_quantity_decisions.csv`
- Modify: `data/review/ingredient_nutrition_usage_decisions.csv`
- Modify: `data/review/ingredient_edible_fraction_rules.csv`
- Modify: `data/review/ingredient_edible_fraction_decisions.csv`
- Create: `reports/data_review/quantity_rule_candidates.csv`
- Create: `reports/data_review/quantity_rule_exceptions.csv`
- Create: `reports/data_review/edible_fraction_candidates.csv`
- Create: `reports/data_review/edible_fraction_exceptions.csv`

**Interfaces:**
- Consumes: 当前 4372 条数量候选、1790 个 ingredient/form 键和 Tasks 1–5 的候选生成器。
- Produces: 正式 approved/modified 规则与仅剩关键异常的机器可读队列。

- [ ] **Step 1: 重新生成 V2 候选**

Run: `uv run food-agent-v2 data-review --kind quantity-rules --output reports/data_review/quantity_rule_candidates.csv`

Run: `uv run food-agent-v2 data-review --kind edible-fractions --output reports/data_review/edible_fraction_candidates.csv`

Expected: 候选覆盖所有未被确定性解析的 occurrence；正式数据文件不被命令自动批准。

- [ ] **Step 2: 批准确定性和稳定规则组**

仅将满足已批准 Schema、`sample_count>=5`、`CV<=0.15`、无用途/形态冲突且通过主审的规则写入正式文件。盐、油、糖、酱油、主料模糊量和所有稳定异常码保留在 exception CSV。

- [ ] **Step 3: 处理 occurrence 级异常**

使用已有原文和步骤形成单值决定；跨形态、主料无依据和高影响模糊量由主代理审核，只有无法可靠判断的记录提交用户。所有正式行必须是 `approved/modified`，不得把 pending 候选复制进正式文件。

- [ ] **Step 4: 运行规则审计**

Run: `uv run pytest tests/b1/test_quantity_normalizer.py tests/b1/test_quantity_rule_review.py tests/b1/test_edible_fraction_review.py tests/b1/test_review_audit.py -q`

Expected: PASS；重复键、非法状态、未闭环 occurrence 均为 0。

- [ ] **Step 5: 生成一次不发布的构建诊断**

Run: `uv run food-agent-v2 data-rebuild --staging-dir .staging/h06-nutrition-review-dry-run`

Expected: 数量和可食比例失败计数均为 0；后续允许出现 `mapping_missing`，因为营养 crosswalk 在下一份计划处理。

- [ ] **Step 6: 提交**

```bash
git add data/review/ingredient_measure_rules.csv data/review/ingredient_quantity_decisions.csv data/review/ingredient_nutrition_usage_decisions.csv data/review/ingredient_edible_fraction_rules.csv data/review/ingredient_edible_fraction_decisions.csv reports/data_review/quantity_rule_candidates.csv reports/data_review/quantity_rule_exceptions.csv reports/data_review/edible_fraction_candidates.csv reports/data_review/edible_fraction_exceptions.csv
git commit -m "data: approve nutrition quantity and edible rules"
```

### Task 7: 第一份计划子系统回归

**Files:**
- Verify only: files changed in Tasks 1–6。

**Interfaces:**
- Consumes: 全部 V2 数量/可食比例输入。
- Produces: 可交给营养映射与计算计划的冻结接口。

- [ ] **Step 1: 运行 B1 专项回归**

Run: `uv run pytest tests/b1/test_nutrition_occurrence_rules.py tests/b1/test_quantity_normalizer.py tests/b1/test_quantity_rule_review.py tests/b1/test_quantity_review.py tests/b1/test_edible_fraction_review.py tests/b1/test_raw_nutrition_calculator.py tests/b1/test_nutrition_availability.py tests/b1/test_consumer_view_consistency.py tests/b1/test_fixed_recipe_source.py tests/test_b1_pipeline.py -q`

Expected: PASS。

- [ ] **Step 2: 运行静态检查**

Run: `uv run ruff check src/food_agent_v2/b1 tests/b1`

Expected: PASS。

- [ ] **Step 3: 核对数据闭环指标**

Run: `uv run food-agent-v2 validate-data --manifest .staging/h06-nutrition-review-dry-run/build_manifest.json`

Expected: dry-run manifest 可验证；`quantity_unapproved=0`、`edible_fraction_missing=0` 必须成立，mapping 相关 unavailable 可以留给下一份计划，不能降低数量或可食比例门禁。

- [ ] **Step 4: 记录审核结果**

主代理核对测试证据、正式规则行状态、异常队列和 Git diff。接口冻结后才能开始营养计算器合并。
