# Nutrition Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 food-origin、正式 crosswalk、九维原始投料计算和异常门禁，使 H06 的 1932 道菜全部发布 `available=true` 营养画像。

**Architecture:** 营养参考保留原始缺失值，只有批准的 food-origin 决定可以产生结构性零值；crosswalk 用 exact、approved_alias、approved_comparable 三种正式方法唯一选中参考。计算继续 all-or-nothing，覆盖率和异常闭环由 BuildManifest 驱动的动态门禁保证。

**Tech Stack:** Python 3.11、Pydantic 2、Decimal、CSV/JSONL、MySQL 构建 Artifact、pytest、ruff。

## Global Constraints

- 前置计划 `2026-08-23-nutrition-review-rules-implementation.md` 必须冻结 `NutritionOccurrenceInput` V2 和数量/可食比例 resolver。
- 九项营养固定为能量、蛋白质、脂肪、碳水、膳食纤维、钠、钙、铁、胆固醇。
- 不计算烹饪损耗、成品得率、份数或个人摄入。
- 模型不得生成九项营养数值；缺失值不得用均值、0或模型估计填充。
- 运行时营养 Artifact 不增加来源、置信度、match method 或解释字段。
- H06 必须达到 `available_count == eligible_recipe_count == 1932`。
- 每个任务只提交列出的文件，不夹带工作区已有改动。

---

### Task 1: 建立 food-origin 和质量异常正式决定

**Files:**
- Create: `src/food_agent_v2/b1/nutrition_review_decisions.py`
- Create: `tests/b1/test_nutrition_review_decisions.py`
- Create: `data/review/nutrition_reference_food_origins.csv`
- Create: `data/review/quality_exception_decisions.jsonl`
- Modify: `src/food_agent_v2/b1/source_manifest.py`
- Modify: `tests/b1/test_fixed_recipe_source.py`

**Interfaces:**
- Consumes: `reference_id` 全集和构建中生成的稳定异常。
- Produces: `NutritionReferenceFoodOriginIndex`、`QualityExceptionDecisionIndex`。

`nutrition_reference_food_origins.csv` 表头固定为：

```text
reference_id,food_origin,review_status
```

`quality_exception_decisions.jsonl` 每行必须且只能包含：

```json
{"exception_id":"...","check_code":"...","entity_type":"recipe","entity_id":"...","observed_value":"...","threshold":"...","final_decision":"approved","review_status":"approved"}
```

- [ ] **Step 1: 写正式决定加载失败测试**

```python
def test_only_effective_origin_decisions_can_create_structural_zeroes() -> None:
    index = NutritionReferenceFoodOriginIndex((
        NutritionReferenceFoodOriginDecision(
            reference_id="plant-ref", food_origin="plant", review_status="approved"
        ),
        NutritionReferenceFoodOriginDecision(
            reference_id="animal-ref", food_origin="animal", review_status="modified"
        ),
        NutritionReferenceFoodOriginDecision(
            reference_id="pending-ref", food_origin="plant", review_status="pending"
        ),
    ))
    assert index.get_effective("plant-ref") == "plant"
    assert index.get_effective("animal-ref") == "animal"
    assert index.get_effective("pending-ref") is None
```

覆盖重复/未知 reference ID、非法 origin、异常决定字段与本次重算不一致时拒绝关闭。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/b1/test_nutrition_review_decisions.py tests/b1/test_fixed_recipe_source.py -q`

Expected: FAIL，正式决定模块和 SourceManifest 输入尚不存在。

- [ ] **Step 3: 实现决定模型和索引**

```python
FoodOrigin = Literal["plant", "animal", "mixed", "unknown"]

class NutritionReferenceFoodOriginDecision(BaseModel):
    reference_id: str
    food_origin: FoodOrigin
    review_status: ReviewStatus

class QualityExceptionDecision(BaseModel):
    exception_id: str
    check_code: str
    entity_type: str
    entity_id: str
    observed_value: Decimal | str
    threshold: str
    final_decision: Literal["approved", "modified", "rejected"]
    review_status: ReviewStatus
```

只有 `approved/modified` 生效；异常决定必须完整匹配本次生成的 code、entity、observed value 和 threshold。

- [ ] **Step 4: 纳入 SourceManifest**

将两个正式决定文件加入 `_BUILD_INPUT_PATHS`。测试修改任一字节都会改变 source manifest hash；这两个文件不成为运行时19类Artifact。

- [ ] **Step 5: 运行专项测试**

Run: `uv run pytest tests/b1/test_nutrition_review_decisions.py tests/b1/test_fixed_recipe_source.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/food_agent_v2/b1/nutrition_review_decisions.py tests/b1/test_nutrition_review_decisions.py data/review/nutrition_reference_food_origins.csv data/review/quality_exception_decisions.jsonl src/food_agent_v2/b1/source_manifest.py tests/b1/test_fixed_recipe_source.py
git commit -m "feat: add nutrition review decisions"
```

### Task 2: 将结构性零值与原始参考分离

**Files:**
- Modify: `src/food_agent_v2/b1/nutrition_reference.py`
- Modify: `tests/b1/test_nutrition_reference.py`
- Modify: `tests/b1/test_food_origin_review.py`

**Interfaces:**
- Consumes: Task 1 的 `NutritionReferenceFoodOriginIndex`。
- Produces: `load_nutrition_references(path, food_origins=...) -> NutritionReferenceIndex`。

- [ ] **Step 1: 写结构性零值失败测试**

```python
def test_origin_is_applied_only_while_building_the_reference_index() -> None:
    raw = _reference("plant-ref", fiber="3", cholesterol=None)
    assert raw.per_100g.cholesterol_mg is None
    index = NutritionReferenceIndex((raw,), food_origins=_origins(plant_ref="plant"))
    assert index.get("plant-ref").per_100g.cholesterol_mg == Decimal("0")
```

覆盖 plant 只补胆固醇、animal 只补纤维、mixed/unknown/pending 不补零，原始对象保持原值。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/b1/test_nutrition_reference.py tests/b1/test_food_origin_review.py -q`

Expected: FAIL，现有 Pydantic validator 在解析参考时直接补零。

- [ ] **Step 3: 移除内嵌自动补零并在索引加载阶段应用决定**

```python
def _with_structural_zeroes(
    reference: IngredientNutritionReference,
    origin: FoodOrigin | None,
) -> IngredientNutritionReference:
    values = reference.per_100g.model_dump()
    if origin == "plant" and values["cholesterol_mg"] is None:
        values["cholesterol_mg"] = Decimal("0")
    if origin == "animal" and values["fiber_g"] is None:
        values["fiber_g"] = Decimal("0")
    return reference.model_copy(update={"per_100g": NutritionVector(**values)})
```

- [ ] **Step 4: 运行专项测试**

Run: `uv run pytest tests/b1/test_nutrition_reference.py tests/b1/test_food_origin_review.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/food_agent_v2/b1/nutrition_reference.py tests/b1/test_nutrition_reference.py tests/b1/test_food_origin_review.py
git commit -m "fix: approve structural nutrition zeroes offline"
```

### Task 3: 支持 approved_comparable 正式映射

**Files:**
- Modify: `src/food_agent_v2/b1/nutrition_reference.py`
- Modify: `src/food_agent_v2/b1/usda_crosswalk_review.py`
- Modify: `src/food_agent_v2/b1/data_review.py`
- Modify: `tests/b1/test_nutrition_reference.py`
- Modify: `tests/b1/test_usda_crosswalk_review.py`

**Interfaces:**
- Consumes: 标准食材类别、标准 form、九维参考。
- Produces: `NutritionCrosswalkIndex` 三种正式方法和只读键集合。

- [ ] **Step 1: 写 comparable 边界失败测试**

```python
def test_approved_comparable_requires_same_category_form_and_complete_vector() -> None:
    decision = NutritionCrosswalkDecision(
        ingredient_id=10, ingredient_name="青菜", form="raw",
        reference_id="vegetable-ref", match_method="approved_comparable",
        review_status="approved", ingredient_category="vegetable",
        reference_category="vegetable",
    )
    index = NutritionCrosswalkIndex((decision,), _complete_references())
    assert index.get(10, "raw").reference_id == "vegetable-ref"
```

覆盖跨类别、跨 form、九维不完整、branded、重复键拒绝；exact 名称约束和 approved_alias 同一食材语义保持。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/b1/test_nutrition_reference.py tests/b1/test_usda_crosswalk_review.py -q`

Expected: FAIL，当前只允许 exact/approved_alias。

- [ ] **Step 3: 扩展正式模型和索引校验**

```python
NutritionFoodCategory = Literal[
    "grain", "tuber", "legume", "vegetable", "fungus_algae", "fruit",
    "nut_seed", "livestock", "poultry", "egg", "fish_shellfish", "dairy",
    "fat_oil", "condiment", "beverage", "prepared_food", "other",
]

MatchMethod = Literal["exact", "approved_alias", "approved_comparable"]

@property
def approved_keys(self) -> frozenset[tuple[int, str]]: ...

@property
def selected_reference_ids(self) -> frozenset[str]: ...
```

候选中的 `reference_alias` 仍是 pending 候选标识，不能直接进入正式 crosswalk。

- [ ] **Step 4: 更新候选生成和审阅输出**

候选保留 ingredient/reference 类别、form、九维完整性和 branded 状态，使主审可以机械判断是否满足 comparable 边界。

- [ ] **Step 5: 运行专项测试**

Run: `uv run pytest tests/b1/test_nutrition_reference.py tests/b1/test_usda_crosswalk_review.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/food_agent_v2/b1/nutrition_reference.py src/food_agent_v2/b1/usda_crosswalk_review.py src/food_agent_v2/b1/data_review.py tests/b1/test_nutrition_reference.py tests/b1/test_usda_crosswalk_review.py
git commit -m "feat: approve comparable nutrition mappings"
```

### Task 4: 实现九维计算硬合法性和复核异常

**Files:**
- Create: `src/food_agent_v2/b1/nutrition_quality.py`
- Create: `tests/b1/test_nutrition_quality.py`
- Modify: `src/food_agent_v2/b1/nutrition_calculator.py`
- Modify: `src/food_agent_v2/b1/nutrition_feature_builder.py`
- Modify: `tests/b1/test_raw_nutrition_calculator.py`
- Modify: `tests/b1/test_nutrition_availability.py`

**Interfaces:**
- Consumes: 前置 resolver、正式 crosswalk 和 Task 1 的异常决定索引。
- Produces: `build_nutrition_quality_exceptions(...)` 和 `unresolved_quality_exceptions(...)`。

- [ ] **Step 1: 写手算、硬合法性和异常测试**

```python
def test_quality_exception_identity_is_stable() -> None:
    item = build_nutrition_quality_exceptions(
        (_feature(recipe_id=7, energy="760", fat="81", sodium="5100"),),
        dish_type_by_recipe_id={7: "main"},
    )
    assert {x.check_code for x in item} >= {
        "NUTRITION_ENERGY_GT_750", "NUTRITION_FAT_GT_80", "NUTRITION_SODIUM_GT_5000",
    }
    assert all(x.exception_id == stable_exception_id(x) for x in item)
```

覆盖 nearest-rank Q1/Q3、组不足20回退全体、旧 observed value/threshold 决定不能关闭异常、硬错误不能审批绕过。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/b1/test_raw_nutrition_calculator.py tests/b1/test_nutrition_availability.py tests/b1/test_nutrition_quality.py -q`

Expected: FAIL，缺少质量模块或新版 edible resolver 接线。

- [ ] **Step 3: 保持 Decimal all-or-nothing 计算**

```text
批准克重
→ occurrence/通用可食比例
→ 唯一 crosswalk
→ 九维完整参考
→ Decimal 累加
→ 最终 ROUND_HALF_UP 两位
```

任一前置缺失继续返回稳定 reason，三个营养输出全部 `None`。计算器不重复解释 optional、one_of 或过程材料。

- [ ] **Step 4: 实现硬合法性和异常生成**

硬失败：可食重量 `<=0`、NaN/Infinity/负值、四项宏量任一 `>100g/100g`、四项合计 `>110g/100g`、能量 `>900kcal/100g`。

复核异常：dish type 组 `3×IQR`，以及能量 `>750`、脂肪 `>80g`、钠 `>5000mg`。

- [ ] **Step 5: 运行专项测试**

Run: `uv run pytest tests/b1/test_raw_nutrition_calculator.py tests/b1/test_nutrition_availability.py tests/b1/test_nutrition_quality.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/food_agent_v2/b1/nutrition_quality.py src/food_agent_v2/b1/nutrition_calculator.py src/food_agent_v2/b1/nutrition_feature_builder.py tests/b1/test_nutrition_quality.py tests/b1/test_raw_nutrition_calculator.py tests/b1/test_nutrition_availability.py
git commit -m "feat: validate raw nutrition quality"
```

### Task 5: 将 G15 改为 BuildManifest 驱动的全覆盖门禁

**Files:**
- Modify: `src/food_agent_v2/contracts/build.py`
- Modify: `src/food_agent_v2/b1/quality_gates.py`
- Modify: `src/food_agent_v2/b1/rebuild.py`
- Modify: `tests/contracts/test_build_manifest.py`
- Modify: `tests/b1/test_quality_gate_authority.py`
- Modify: `tests/test_b1_pipeline.py`

**Interfaces:**
- Consumes: `eligible_recipe_ids`、营养特征和未关闭异常。
- Produces: `BuildManifest.eligible_recipe_count`、G14/G15/G18。

- [ ] **Step 1: 写门禁失败矩阵**

```python
def test_g15_requires_every_manifest_eligible_recipe() -> None:
    report = run_quality_gates(
        eligible_recipe_ids=set(range(1, 1933)),
        nutrition_features=_available_features(range(1, 1932)),
        manifest_eligible_recipe_count=1932,
    )
    assert report.gate("G15_NUTRITION_COVERAGE").status == "failed"
```

覆盖 1931/1932、重复 recipe ID、任一 unavailable、manifest count 不一致、G18 未关闭异常。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/contracts/test_build_manifest.py tests/b1/test_quality_gate_authority.py tests/test_b1_pipeline.py -q`

Expected: FAIL，G15 当前基线为0且 Manifest 无 eligible count。

- [ ] **Step 3: 扩展 BuildManifest 并写入动态计数**

```python
class BuildManifest(BaseModel):
    eligible_recipe_count: int = Field(gt=0)
```

构建时从四类消费者视图共同 recipe 集合计算，H06 另断言为 1932。

- [ ] **Step 4: 实现 G14/G15/G18**

删除 `APPROVED_NUTRITION_AVAILABLE_BASELINE = 0`。G15 要求：

```python
available_recipe_ids == eligible_recipe_ids
len(eligible_recipe_ids) == manifest_eligible_recipe_count == 1932
unavailable_count == 0
```

G18 要求所有复核异常为空或由当前有效决定关闭，并把数量、可食比例、mapping、nutrient completeness 未解决计数写入 quality metrics。

- [ ] **Step 5: 运行专项测试**

Run: `uv run pytest tests/contracts/test_build_manifest.py tests/b1/test_quality_gate_authority.py tests/test_b1_pipeline.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/food_agent_v2/contracts/build.py src/food_agent_v2/b1/quality_gates.py src/food_agent_v2/b1/rebuild.py tests/contracts/test_build_manifest.py tests/b1/test_quality_gate_authority.py tests/test_b1_pipeline.py
git commit -m "feat: require complete nutrition coverage"
```

### Task 6: 完成正式 food-origin、Crosswalk 和参考数据

**Files:**
- Modify: `data/review/nutrition_reference_food_origins.csv`
- Modify: `data/review/ingredient_nutrition_crosswalk.jsonl`
- Modify when authoritative data is required: `data/reference/ingredient_nutrition.jsonl`
- Modify: `data/review/quality_exception_decisions.jsonl`
- Update generated: `reports/data_review/nutrition_crosswalk_candidates.csv`
- Update generated: `reports/data_review/usda_nutrition_candidates.csv`
- Update generated: `reports/data_review/food_origin_candidates.csv`

**Interfaces:**
- Consumes: 1790 个 ingredient/form 键、9618 条参考、2014 条主候选、1392 条 USDA 候选路径。
- Produces: 每个正式 ingredient/form 唯一且九维完整的映射。

- [ ] **Step 1: 重新生成候选并固定输入统计**

Run: `uv run food-agent-v2 data-review --kind nutrition --output reports/data_review/nutrition_crosswalk_candidates.csv`

Run: `uv run food-agent-v2 data-review --kind usda-nutrition --output reports/data_review/usda_nutrition_candidates.csv`

Run: `uv run food-agent-v2 data-review --kind food-origins --output reports/data_review/food_origin_candidates.csv`

Expected: 每个 ingredient/form 键唯一出现在覆盖审计中；候选保持 pending。

- [ ] **Step 2: 按 exact → alias → comparable 批量批准**

exact 必须名称/form 完全一致；alias 必须同一食材/form；comparable 必须同类别/form、九维完整、非 branded。101 个多候选键按形态完整性和参考优先级唯一决策。

- [ ] **Step 3: 补齐剩余无候选键**

先用项目已有中国成分参考，再用 USDA Foundation/SR Legacy/FNDDS。仍无同形态参考时查找权威资料并新增完整参考记录；不得从模型生成数值。跨类别、跨生熟/干湿、物种或复合食品提交用户决定。

- [ ] **Step 4: 只为需要结构性零值的选中参考批准 origin**

选中参考若 cholesterol/fiber 原始缺失，必须存在有效 origin 决定；九维原本完整的参考无需为了展示而添加 origin。结构性零值后仍不完整则改选或补权威参考。

- [ ] **Step 5: 运行映射和参考审计**

Run: `uv run pytest tests/b1/test_nutrition_review_decisions.py tests/b1/test_nutrition_reference.py tests/b1/test_usda_crosswalk_review.py tests/b1/test_review_audit.py -q`

Expected: 正式 crosswalk 键唯一；选中参考全部九维完整；mapping/nutrient completeness 未解决数为0。

- [ ] **Step 6: 提交**

```bash
git add data/review/nutrition_reference_food_origins.csv data/review/ingredient_nutrition_crosswalk.jsonl data/reference/ingredient_nutrition.jsonl data/review/quality_exception_decisions.jsonl reports/data_review/nutrition_crosswalk_candidates.csv reports/data_review/usda_nutrition_candidates.csv reports/data_review/food_origin_candidates.csv
git commit -m "data: complete nutrition reference mappings"
```

### Task 7: 构建并闭环 1932 道营养画像

**Files:**
- Generated: `.staging/h06-nutrition-complete/*`
- Modify only if review is required: `data/review/quality_exception_decisions.jsonl`

**Interfaces:**
- Consumes: Tasks 1–6 和前置数量/可食比例计划的全部正式输入。
- Produces: 1932 条 `available=true` 的 `nutrition_features` 和可验证 BuildManifest。

- [ ] **Step 1: 运行完整离线构建**

Run: `uv run food-agent-v2 data-rebuild --staging-dir .staging/h06-nutrition-complete`

Expected: 构建若失败，稳定报告 quantity、edible、mapping、nutrient、quality exception 的精确未解决项；不得生成 ready manifest。

- [ ] **Step 2: 处理复核异常**

硬合法性错误必须修正规则、映射或参考。仅对计算值正确但分布极端的记录写入与本次 observed value/threshold 完全一致的批准决定，然后重新构建。

- [ ] **Step 3: 验证 Manifest 和覆盖率**

Run: `uv run food-agent-v2 data-verify --manifest .staging/h06-nutrition-complete/build_manifest.json`

Run: `uv run food-agent-v2 validate-data --manifest .staging/h06-nutrition-complete/build_manifest.json`

Expected: `eligible_recipe_count=1932`、`nutrition available=1932`、G14/G15/G18 passed、全部 unavailable reason 为0。

- [ ] **Step 4: 手算抽样复核**

从高蛋白、低钠、高纤维、低脂、高钙、高铁各抽至少3道菜，逐食材复算总可食克重、九项总量和每100克；与 Artifact 的两位小数结果一致。

- [ ] **Step 5: 运行 B1 全量测试和静态检查**

Run: `uv run pytest tests/b1 tests/contracts/test_build_manifest.py tests/test_b1_pipeline.py -q`

Run: `uv run ruff check src/food_agent_v2/b1 src/food_agent_v2/contracts tests/b1 tests/contracts/test_build_manifest.py`

Expected: PASS。

- [ ] **Step 6: 提交最终异常决定（如有）**

```bash
git add data/review/quality_exception_decisions.jsonl
git commit -m "data: close reviewed nutrition outliers"
```

主代理在本计划结束时审核：1932覆盖证据、手算样例、全部正式映射、SourceManifest 和非营养 Artifact 差异。通过后才能执行运行时计划。
