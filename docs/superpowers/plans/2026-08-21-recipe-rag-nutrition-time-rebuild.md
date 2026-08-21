# Recipe RAG、原始营养与任务图时长重建实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 B4 健康裁决权威边界的前提下，把 V2 菜品标签完整投影到 RAG，增加每次推荐前的语义重写，并建立可审阅的食材克重、九维原始营养和单值任务图时长链路。

**Architecture:** 唯一菜谱事实来自 `data/raw/recipes_sample_2000.csv`。B1 将原始标签、经批准的画像补全、食材条件/默认项、数量决定、营养映射和步骤任务图编译成同一 `build_id` 下的固定 Artifact；C3 先把口语请求重写为封闭的 `QueryPlanArtifact`，C1 用标签 payload 做硬过滤和混合召回，B4 对候选做唯一健康裁决，B5/B6 仅对安全候选计算预计时长和目标相关营养软分，C2 规划后再由 B4 终检。MySQL 与 Qdrant 只接收同一份已通过质量门禁的构建。

**Tech Stack:** Python 3.11、Pydantic v2、pytest、Qdrant Client、OR-Tools CP-SAT、MySQL、现有 LangChain/OpenAI 兼容模型接口。

## Global Constraints

- `datas` 不作为事实来源，不复制其中标签、克重、营养或步骤时长。
- 原始 CSV 保持 GBK 与内容不变；新增人工/模型决定只写入 `data/review/`，外部营养事实写入 `data/reference/`。
- 运行时 `rag_documents`、`nutrition_features`、`step_tasks` 不包含 `confidence`、`source`、`evidence`、估算区间或解释字段；来源 URL 和审阅状态只存在离线参考/审阅文件。
- B4 不消费营养或时间值；C1 不消费 `step_tasks`；B5/B6 只能消费 B4 的 `safe_recipe_ids`。
- 同字段多选是 OR，跨字段是 AND，排除项是 NOT；餐次过滤不允许静默放宽。
- 模糊克重必须得到用户 `approved` 或 `modified` 才能参与营养计算。未通过审阅不阻止 RAG，但对应菜品营养必须整体 `available=false`。
- 任一参与计算的食材缺批准克重、可食比例、唯一营养映射或九维营养值时，该菜全部营养数值为 `null`。
- 时间模型只补充原子步骤元数据，不能覆盖显式时长；任务图不完整、ID 不全、资源非法或成环时构建失败。
- 在线 `QueryPlanArtifact.schema_version` 固定为 `2.0.0`；构建清单中的 `rag_documents`、`nutrition_features`、`step_tasks` 固定为 `2.0.0`。其他固定 Artifact 保持 `1.0.0`，除非任务中的契约测试证明必须同步升级。
- 每个任务先写失败测试，再实现，再运行局部测试；每个阶段结束后审视模块边界；最后执行全局不变量、全量测试和 MySQL/Qdrant 原子发布演练。

---

## Task 1: 固化 V2.0 契约与失败语义

**Files:**

- Modify: `src/food_agent_v2/contracts/artifacts.py`
- Modify: `src/food_agent_v2/contracts/status.py`
- Modify: `src/food_agent_v2/contracts/__init__.py`
- Modify: `src/food_agent_v2/c2/schemas.py`
- Modify: `src/food_agent_v2/c3/state.py`
- Modify: `src/food_agent_v2/d1/schemas.py`
- Modify: `src/food_agent_v2/d1/__init__.py`
- Test: `tests/contracts/test_artifact_schemas.py`
- Test: `tests/c3/test_state_reducer.py`

- [ ] **Step 1: 写失败的 QueryPlan 和菜单时间契约测试**

  在 `tests/contracts/test_artifact_schemas.py` 增加：

  ```python
  def test_query_plan_v2_carries_closed_retrieval_facets() -> None:
      plan = query_plan(
          rewritten_query="老人 清淡 晚餐",
          meal_types=("晚餐",),
          population_tags=("老人",),
          taste_tags=("清淡",),
          scenario_tags=("日常",),
          include_ingredients=("豆腐",),
          exclude_ingredients=("辣椒",),
          nutrition_goal_codes=("low_sodium",),
      )
      assert plan.meal_types == ("晚餐",)
      assert plan.population_tags == ("老人",)

  def test_feasible_menu_uses_expected_time_boolean() -> None:
      menu = feasible_menu(
          estimated_time_feasible=True,
          estimated_makespan_seconds=1800,
      )
      assert menu.estimated_time_feasible is True
      assert not hasattr(menu, "strict_time_feasible")
  ```

  同时增加 extra field 拒绝测试，证明模型不能写出未定义 facet。

- [ ] **Step 2: 运行测试并确认因字段不存在而失败**

  Run: `uv run pytest tests/contracts/test_artifact_schemas.py -q`

- [ ] **Step 3: 改造契约**

  `QueryPlanArtifact` 使用以下检索字段，保留 `health_exclusions` 与时间秒数边界：

  ```python
  schema_version: Literal["2.0.0"] = "2.0.0"
  rewritten_query: str
  meal_types: tuple[str, ...] = ()
  population_tags: tuple[str, ...] = ()
  dish_types: tuple[str, ...] = ()
  taste_tags: tuple[str, ...] = ()
  cuisine_tags: tuple[str, ...] = ()
  scenario_tags: tuple[str, ...] = ()
  include_ingredients: tuple[str, ...] = ()
  exclude_ingredients: tuple[str, ...] = ()
  health_exclusions: tuple[str, ...] = ()
  nutrition_goal_codes: tuple[str, ...] = ()
  dish_count_requested: int | None = None
  time_constraint_seconds: int | None = None
  time_constraint_policy: Literal["flexible", "hard"] = "flexible"
  ```

  把在线菜单字段统一为：

  ```python
  estimated_time_feasible: bool
  estimated_makespan_seconds: int
  ```

  从 RequestStatus、C3/D1 枚举和终态转换中移除 `strict_time_indeterminate`。构建失败或 B5 不可用属于系统失败，不能伪装为用户时间语义不确定。

- [ ] **Step 4: 更新状态测试并运行契约测试**

  Run: `uv run pytest tests/contracts/test_artifact_schemas.py tests/c3/test_state_reducer.py -q`

- [ ] **Step 5: 检查旧字段没有残留于核心契约**

  Run: `rg -n "strict_time_feasible|strict_time_indeterminate|makespan_seconds" src/food_agent_v2/contracts src/food_agent_v2/c2/schemas.py src/food_agent_v2/c3/state.py src/food_agent_v2/d1`

  Expected: 只允许迁移说明或测试夹具中明确标记的兼容代码，不允许运行时契约继续声明旧字段。

- [ ] **Step 6: 提交**

  ```bash
  git add src/food_agent_v2/contracts src/food_agent_v2/c2/schemas.py src/food_agent_v2/c3/state.py src/food_agent_v2/d1 tests/contracts/test_artifact_schemas.py tests/c3/test_state_reducer.py
  git commit -m "refactor: define v2 query and estimated time contracts"
  ```

## Task 2: 建立菜品标签画像、条件食材与审阅输入

**Files:**

- Create: `data/review/recipe_profile_enrichment.jsonl`
- Create: `data/review/ingredient_condition_defaults.csv`
- Modify: `src/food_agent_v2/b1/consumer_views.py`
- Create: `src/food_agent_v2/b1/review_inputs.py`
- Modify: `src/food_agent_v2/b3/repository.py`
- Modify: `src/food_agent_v2/b3/recipe_views.py`
- Modify: `src/food_agent_v2/b4/schemas.py`
- Modify: `src/food_agent_v2/b4/engine.py`
- Modify: `src/food_agent_v2/c3/tool_handler.py`
- Modify: `src/food_agent_v2/cli.py`
- Test: `tests/b1/test_recipe_profile_enrichment.py`
- Modify: `tests/b1/test_consumer_view_consistency.py`
- Modify: `tests/b3/test_repository_views.py`
- Modify: `tests/b4/test_health_engine_matrix.py`

- [ ] **Step 1: 写失败测试，证明原始 label 全量保留且敏感标签不能凭空生成**

  覆盖以下断言：

  ```python
  assert set(recipe.label_tags) == set(raw_row.label.split("、"))
  assert "晚餐" in recipe.meal_tags
  assert enrichment.generated_sensitive_tags == ()
  ```

  增加“老人晚餐”样例和无 label 样例；无 label 只能补 `meal/dish/taste/cuisine/scenario` 的非敏感 facet，不能自动补 `老人/儿童/孕妇/控糖/低钠/功效`。

- [ ] **Step 2: 运行失败测试**

  Run: `uv run pytest tests/b1/test_recipe_profile_enrichment.py tests/b1/test_consumer_view_consistency.py -q`

- [ ] **Step 3: 定义固定审阅文件格式与加载器**

  `recipe_profile_enrichment.jsonl` 每行严格为：

  ```json
  {"recipe_id": 1, "meal_tags": ["晚餐"], "dish_type_tags": ["热菜"], "taste_tags": ["清淡"], "cuisine_tags": [], "cooking_method_tags": ["蒸"], "texture_tags": ["软嫩"], "scenario_tags": ["日常"], "population_tags": [], "review_status": "approved"}
  ```

  `ingredient_condition_defaults.csv` 列为：

  ```text
  recipe_name,choice_group,selected_ingredient,retained_alternatives,review_status
  ```

  加载器必须拒绝重复 recipe/facet 决定、未知 review status、敏感生成标签以及 CSV 中不存在的菜品。

- [ ] **Step 4: 写入已批准的默认食材决定**

  包含设计文档中已批准的九组决定：蜜豆、菠菜粉、牛肩肉、食用绿色素、梅花肉、红腐乳汁、葡萄干、凉白开水及保留花椒粉。其他 one-of 组若无批准值，写 `pending` 而不是猜测后直接生效。

- [ ] **Step 5: 扩展 B1 事实与视图**

  `RecipeFact` 增加完整标签及结构化 facets；`IngredientOccurrenceFact` 增加：

  ```python
  condition_type: Literal["required", "optional", "one_of"]
  choice_group: str | None
  selected_for_base: bool
  is_process_material: bool
  added_from_step: bool
  ```

  `RecipeHealthIngredientView` 保留 required、optional 与已批准默认 one-of 项的条件信息，未选中的 one-of 备选只保留在菜品画像/RAG；`RecipeNutritionInputView` 只包含 required 与已批准 `selected_for_base` 的 one-of 项；optional 与 `is_process_material=true` 不进入基础营养。

- [ ] **Step 6: 让 B3/B4 显式消费条件关系并保留兼容入口**

  B3 health view 增加 `ingredient_relations`，每项含 `ingredient_id/condition_type/choice_group/is_default_choice/is_process_material`；`ingredient_ids` 保留为兼容投影。B4 新增 `evaluate_recipe_occurrences()` 作为在线入口，旧 `evaluate_recipe(recipe_id, ingredient_ids, ...)` 仅作为全部 required 的兼容包装。optional 命中健康硬约束时当前系统保守排除并在证据中标记 conditional；未选中的 one-of 不能造成误排除。B4 仍不得读取克重、营养或时长。

- [ ] **Step 7: 实现画像候选生成命令**

  在 CLI 增加 `food-agent-v2 data-review --kind profiles --output <jsonl>`。命令只生成候选，敏感标签候选一律保持空或 pending，不能自行写 approved。

- [ ] **Step 8: 运行视图和画像测试**

  Run: `uv run pytest tests/b1/test_recipe_profile_enrichment.py tests/b1/test_consumer_view_consistency.py tests/b3/test_repository_views.py tests/b4/test_health_engine_matrix.py -q`

- [ ] **Step 9: 边界审视**

  确认 B4 仍能看到 optional 条件和默认 one-of，营养视图没有同时累计 one-of 多个选项，RAG 可看到全部备选食材词但不会把它们当作必然投料。

- [ ] **Step 10: 提交**

  ```bash
  git add data/review/recipe_profile_enrichment.jsonl data/review/ingredient_condition_defaults.csv src/food_agent_v2/b1/consumer_views.py src/food_agent_v2/b1/review_inputs.py src/food_agent_v2/b3/repository.py src/food_agent_v2/b3/recipe_views.py src/food_agent_v2/b4/schemas.py src/food_agent_v2/b4/engine.py src/food_agent_v2/c3/tool_handler.py src/food_agent_v2/cli.py tests/b1/test_recipe_profile_enrichment.py tests/b1/test_consumer_view_consistency.py tests/b3/test_repository_views.py tests/b4/test_health_engine_matrix.py
  git commit -m "feat: add reviewed recipe facets and ingredient conditions"
  ```

## Task 3: 实现克重标准化与用户审阅门禁

**Files:**

- Create: `data/review/ingredient_measure_rules.csv`
- Create: `data/review/ingredient_edible_fraction_rules.csv`
- Create: `data/review/ingredient_quantity_decisions.csv`
- Create: `src/food_agent_v2/b1/quantity_normalizer.py`
- Create: `src/food_agent_v2/b1/quantity_review.py`
- Modify: `src/food_agent_v2/b1/consumer_views.py`
- Modify: `src/food_agent_v2/cli.py`
- Test: `tests/b1/test_quantity_normalizer.py`
- Test: `tests/b1/test_quantity_review.py`

- [ ] **Step 1: 写数量规则失败测试**

  必须覆盖：`100克→100`、`100-200克→150`、`约100克→100`、`200毫升牛奶→密度换算`、`2个鸡蛋→单位重量换算`、`少许盐→pending`、步骤中新增但无量的食材→pending。明确数值最终都输出 `Decimal` 克，不保留运行时范围。

- [ ] **Step 2: 运行失败测试**

  Run: `uv run pytest tests/b1/test_quantity_normalizer.py tests/b1/test_quantity_review.py -q`

- [ ] **Step 3: 定义规则和审阅文件**

  `ingredient_measure_rules.csv`：

  ```text
  rule_id,ingredient_name,rule_type,from_unit,to_grams,mass_density_g_per_ml,review_status
  ```

  `ingredient_edible_fraction_rules.csv`：

  ```text
  ingredient_name,form,edible_fraction,review_status
  ```

  `ingredient_quantity_decisions.csv`：

  ```text
  occurrence_id,recipe_id,recipe_name,ingredient_name,raw_quantity,step_context,deterministic_calculation,candidate_basis,candidate_grams,decision_grams,review_status
  ```

  `review_status` 仅允许 `pending/approved/modified/rejected`。规则文件只有 approved 行可批量应用；quantity decisions 只有 approved/modified 行可成为最终克重。

- [ ] **Step 4: 实现纯函数标准化**

  主接口固定为：

  ```python
  def normalize_quantity(
      occurrence: NutritionOccurrenceInput,
      measure_rules: MeasureRuleIndex,
      decisions: QuantityDecisionIndex,
  ) -> QuantityNormalizationResult:
      ...
  ```

  结果包含 `standardized_grams: Decimal | None` 和 `review_status`，但写入最终 `nutrition_features` 时不携带来源、置信度或解释。

- [ ] **Step 5: 实现候选包生成命令**

  在 CLI 增加：

  ```text
  food-agent-v2 data-review --kind quantities --output <csv>
  ```

  对模糊用量使用一次整菜上下文模型调用生成单个 `candidate_grams`；该命令只写候选 CSV，不改批准决定。模型不可直接把 status 写成 approved。

- [ ] **Step 6: 运行局部测试与静态检查**

  Run: `uv run pytest tests/b1/test_quantity_normalizer.py tests/b1/test_quantity_review.py -q`

  Run: `uv run ruff check src/food_agent_v2/b1/quantity_normalizer.py src/food_agent_v2/b1/quantity_review.py`

- [ ] **Step 7: 提交**

  ```bash
  git add data/review/ingredient_measure_rules.csv data/review/ingredient_edible_fraction_rules.csv data/review/ingredient_quantity_decisions.csv src/food_agent_v2/b1/quantity_normalizer.py src/food_agent_v2/b1/quantity_review.py src/food_agent_v2/b1/consumer_views.py src/food_agent_v2/cli.py tests/b1/test_quantity_normalizer.py tests/b1/test_quantity_review.py
  git commit -m "feat: normalize ingredient quantities behind review gates"
  ```

## Task 4: 建立九维营养参考、唯一映射与结构性零值

**Files:**

- Create: `data/reference/ingredient_nutrition.jsonl`
- Create: `data/review/ingredient_nutrition_crosswalk.jsonl`
- Create: `src/food_agent_v2/b1/nutrition_reference.py`
- Modify: `src/food_agent_v2/b1/nutrition_feature_builder.py`
- Modify: `src/food_agent_v2/cli.py`
- Test: `tests/b1/test_nutrition_reference.py`

- [ ] **Step 1: 写失败测试**

  覆盖九维键完整、单位固定、同一 ingredient/form 只能有一个 approved 映射、纯植物 `cholesterol=0`、纯动物 `fiber=0`、其他缺失保持 `None`、品牌数据只能 exact match。

- [ ] **Step 2: 运行失败测试**

  Run: `uv run pytest tests/b1/test_nutrition_reference.py -q`

- [ ] **Step 3: 实现严格模型**

  ```python
  class NutritionVector(BaseModel):
      energy_kcal: Decimal | None
      protein_g: Decimal | None
      fat_g: Decimal | None
      carbohydrate_g: Decimal | None
      fiber_g: Decimal | None
      sodium_mg: Decimal | None
      calcium_mg: Decimal | None
      iron_mg: Decimal | None
      cholesterol_mg: Decimal | None

  class IngredientNutritionReference(BaseModel):
      reference_id: str
      canonical_name: str
      form: str
      per_100g: NutritionVector
      source_name: str
      source_url: str
  ```

  来源字段只保存在 `data/reference/ingredient_nutrition.jsonl`，不进入最终菜品画像。

- [ ] **Step 4: 实现来源优先级和 crosswalk 验证**

  优先级固定为中国疾控食物成分表、USDA Foundation、SR Legacy、FNDDS、exact Branded/厂家。匹配必须同时核对食材身份、形态和 raw/cooked/dried/hydrated 状态。禁止用近似食材、模型生成营养或 `0` 补普通缺失值。

- [ ] **Step 5: 导入已有中国成分表中满足条件的行**

  通过解析脚本机械转换 `data/reference/china_food_composition.jsonl` 中可唯一识别且字段合法的记录；新增外部记录必须记录页面 URL 和来源名称。对关键 crosswalk 的写入先保持 `pending`，用户批准后才改为 `approved`。

  同时实现 `food-agent-v2 data-review --kind nutrition --output <csv>`，只输出匹配候选和缺失原因，不直接批准 crosswalk。

- [ ] **Step 6: 运行测试和覆盖率报告**

  Run: `uv run pytest tests/b1/test_nutrition_reference.py -q`

  生成报告需列出九维各自非空率、approved crosswalk 数和 unresolved ingredient 数，但不得把缺失值改为零。

- [ ] **Step 7: 提交**

  ```bash
  git add data/reference/ingredient_nutrition.jsonl data/review/ingredient_nutrition_crosswalk.jsonl src/food_agent_v2/b1/nutrition_reference.py src/food_agent_v2/b1/nutrition_feature_builder.py src/food_agent_v2/cli.py tests/b1/test_nutrition_reference.py
  git commit -m "feat: add reviewed nine-dimension nutrition references"
  ```

## Task 5: 计算菜品原始投料营养并重做 B6 目标评分

**Files:**

- Create: `src/food_agent_v2/b1/nutrition_calculator.py`
- Modify: `src/food_agent_v2/b1/nutrition_feature_builder.py`
- Modify: `src/food_agent_v2/b6/__init__.py`
- Modify: `src/food_agent_v2/c2/planner.py`
- Modify: `src/food_agent_v2/c3/tool_handler.py`
- Replace: `tests/b1/test_nutrition_availability.py`
- Create: `tests/b1/test_raw_nutrition_calculator.py`
- Modify: `tests/b6/test_soft_evidence_availability.py`

- [ ] **Step 1: 写 all-or-nothing 和 Decimal 计算失败测试**

  使用两个食材的手算夹具验证：

  ```python
  edible_g = standardized_g * edible_fraction
  total_nutrient = sum(edible_g / Decimal("100") * per_100g)
  raw_per_100g = total_nutrient / total_edible_g * Decimal("100")
  ```

  中间不得 round，最终九维在序列化边界四舍五入到两位。任一参与食材缺上述前置条件时断言 `available is False`、`raw_nutrition_total is None`、`raw_nutrition_per_100g is None`。

- [ ] **Step 2: 运行失败测试**

  Run: `uv run pytest tests/b1/test_raw_nutrition_calculator.py tests/b1/test_nutrition_availability.py -q`

- [ ] **Step 3: 实现最终运行时模型与计算器**

  ```python
  class RecipeNutritionFeatures(BaseModel):
      build_id: str
      source_manifest_hash: str
      recipe_id: int
      available: bool
      raw_edible_input_weight_g: Decimal | None
      raw_nutrition_total: NutritionVector | None
      raw_nutrition_per_100g: NutritionVector | None
      reason: str | None
  ```

  `reason` 只允许稳定枚举（如 `quantity_unapproved`、`edible_fraction_missing`、`mapping_missing`、`nutrient_incomplete`），不包含 confidence/source/explanation。

- [ ] **Step 4: 重做 B6**

  B6 主接口：

  ```python
  def score_candidates(
      self,
      safe_recipe_ids: tuple[int, ...],
      goal_codes: tuple[str, ...] = (),
  ) -> dict[int, NutritionScoreDecomposition]:
      ...
  ```

  只对本次 B4 safe 集合内、available 菜的 `raw_nutrition_per_100g` 计算 percentile。映射固定为：`high_protein/protein_g/higher`、`low_sodium/sodium_mg/lower`、`high_fiber/fiber_g/higher`、`low_fat/fat_g/lower`、`high_calcium/calcium_mg/higher`、`high_iron/iron_mg/higher`。无目标时 `weighted_total=None`；不可用时 C2 把 nutrition 维度加入 unavailable 并重归一化其他权重。

- [ ] **Step 5: 让 C2 从 QueryPlan 传入目标代码**

  `MenuPlanner.plan()` 或其评分上下文必须显式接收 `nutrition_goal_codes`，禁止从菜名或健康约束反推营养目标。

- [ ] **Step 6: 运行局部测试**

  Run: `uv run pytest tests/b1/test_raw_nutrition_calculator.py tests/b1/test_nutrition_availability.py tests/b6/test_soft_evidence_availability.py tests/c2/test_deterministic_plans.py -q`

- [ ] **Step 7: 边界审视**

  用测试替身证明 B4 输入只有 ingredient/condition，未读取 `nutrition_features`；B6 输入来自 B4 safe 集合而不是原始 RAG 集合。

- [ ] **Step 8: 提交**

  ```bash
  git add src/food_agent_v2/b1/nutrition_calculator.py src/food_agent_v2/b1/nutrition_feature_builder.py src/food_agent_v2/b6/__init__.py src/food_agent_v2/c2/planner.py src/food_agent_v2/c3/tool_handler.py tests/b1/test_nutrition_availability.py tests/b1/test_raw_nutrition_calculator.py tests/b6/test_soft_evidence_availability.py tests/c2/test_deterministic_plans.py
  git commit -m "feat: calculate raw recipe nutrition and goal-aware scores"
  ```

## Task 6: 增加每次推荐前的封闭式语义重写

**Files:**

- Modify: `src/food_agent_v2/c3/query_normalizer.py`
- Modify: `src/food_agent_v2/c3/fast_intent.py`
- Modify: `src/food_agent_v2/c3/orchestrator.py`
- Modify: `src/food_agent_v2/c3/delta_planner.py`
- Modify: `tests/c3/test_query_normalizer.py`
- Modify: `tests/c3/test_fast_intent.py`
- Modify: `tests/c3/test_fast_intent_health_boundary.py`
- Modify: `tests/c3/test_orchestrator.py`

- [ ] **Step 1: 写失败测试**

  必须证明：

  - “给我推荐一些老人吃的晚餐”不再因“老人”触发参与者归属澄清；输出 `population_tags=("老人",)` 和 `meal_types=("晚餐",)`。
  - “不要辣，想吃豆腐”分别进入 exclude/include。
  - “30 分钟内”输出 `max_time_minutes=30`，适配器转换为 1800 秒。
  - 模型保持否定，不返回菜品 ID、菜名或健康 PASS/FAIL。
  - 新推荐即使 FastIntentRouter 能识别，也必须调用一次重写模型。
  - 模型超时/非法 JSON 最多重试两次后走确定性 fallback；fallback 不能丢失显式餐次、排除项和时间。

- [ ] **Step 2: 运行失败测试**

  Run: `uv run pytest tests/c3/test_query_normalizer.py tests/c3/test_fast_intent.py tests/c3/test_fast_intent_health_boundary.py tests/c3/test_orchestrator.py -q`

- [ ] **Step 3: 定义模型输出 Schema**

  ```python
  class SemanticRewrite(BaseModel):
      model_config = ConfigDict(extra="forbid")
      retrieval_query: str
      meal_types: tuple[str, ...] = ()
      population_tags: tuple[str, ...] = ()
      dish_types: tuple[str, ...] = ()
      taste_tags: tuple[str, ...] = ()
      cuisine_tags: tuple[str, ...] = ()
      scenario_tags: tuple[str, ...] = ()
      include_ingredients: tuple[str, ...] = ()
      exclude_ingredients: tuple[str, ...] = ()
      health_constraints: tuple[str, ...] = ()
      nutrition_goal_codes: tuple[str, ...] = ()
      max_time_minutes: int | None = None
      dish_count: int | None = None
  ```

  Prompt 明确：只做语义解释，不推荐菜、不做健康判断、不消解用户否定；多轮输入附上前一份 QueryPlan 而不是整段自由文本。

- [ ] **Step 4: 调整编排顺序**

  FastIntentRouter 只负责 action、安全冲突和 replace/restore 路由。所有 `new_recommendation` 以及需要重新检索的 delta 都在 C1 前调用 `QueryNormalizer`。`_build_query_plan` 只从验证后的 `SemanticRewrite` 构造 artifact，`_retrieval_query()` 直接返回 `rewritten_query`。

- [ ] **Step 5: 运行局部和 R-002 回归**

  Run: `uv run pytest tests/c3/test_query_normalizer.py tests/c3/test_fast_intent.py tests/c3/test_fast_intent_health_boundary.py tests/c3/test_orchestrator.py tests/c3/test_r002_temp_constraint_loop.py -q`

- [ ] **Step 6: 提交**

  ```bash
  git add src/food_agent_v2/c3/query_normalizer.py src/food_agent_v2/c3/fast_intent.py src/food_agent_v2/c3/orchestrator.py src/food_agent_v2/c3/delta_planner.py tests/c3/test_query_normalizer.py tests/c3/test_fast_intent.py tests/c3/test_fast_intent_health_boundary.py tests/c3/test_orchestrator.py
  git commit -m "feat: rewrite recommendation queries into closed semantic plans"
  ```

## Task 7: 重建 RAG 文档、标签 payload 与硬过滤

**Files:**

- Create: `src/food_agent_v2/c1/filters.py`
- Modify: `src/food_agent_v2/b1/rag_document_builder.py`
- Modify: `src/food_agent_v2/b1/consumer_views.py`
- Modify: `src/food_agent_v2/c1/__init__.py`
- Modify: `src/food_agent_v2/c1/qdrant_client.py`
- Modify: `src/food_agent_v2/c3/tool_handler.py`
- Modify: `tests/c1/test_full_hybrid_retrieval.py`
- Create: `tests/c1/test_retrieval_filters.py`
- Modify: `tests/integration/test_real_qdrant_retrieval.py`

- [ ] **Step 1: 写失败测试**

  核心场景：查询“老人晚餐”时所有返回项都同时含 `晚餐` meal tag 和 `老人` population tag，但该人群标签不能替代 B4；“不吃辣椒”在 BM25 和 Qdrant 两路都排除；`早餐 OR 早午餐` 与 `清淡 AND 汤` 语义正确；无餐次/人群结果时返回空集，不能去掉硬 filter 重搜。

- [ ] **Step 2: 运行失败测试**

  Run: `uv run pytest tests/c1/test_retrieval_filters.py tests/c1/test_full_hybrid_retrieval.py -q`

- [ ] **Step 3: 定义统一过滤器**

  ```python
  @dataclass(frozen=True)
  class RetrievalFilters:
      meal_tags: tuple[str, ...] = ()
      population_tags: tuple[str, ...] = ()
      dish_type_tags: tuple[str, ...] = ()
      taste_tags: tuple[str, ...] = ()
      cuisine_tags: tuple[str, ...] = ()
      scenario_tags: tuple[str, ...] = ()
      include_ingredients: tuple[str, ...] = ()
      exclude_ingredients: tuple[str, ...] = ()
  ```

  `matches_payload()` 是内存/BM25 的唯一真值；`to_qdrant_filter()` 必须生成等价的 `must`/`must_not`，同字段用 `MatchAny`。

- [ ] **Step 4: 重建 RagBuildDocument**

  最终 payload 字段固定为：`recipe_id`、`name`、`label_tags`、`meal_tags`、`population_tags`、`dish_type_tags`、`taste_tags`、`cuisine_tags`、`cooking_method_tags`、`texture_tags`、`scenario_tags`、`ingredient_names`、`ingredient_ids`、`catalog_eligibility`、`build_id`、`source_manifest_hash`、`source_row_sha256`。`searchable_text` 只拼菜名、标准食材、完整原 label 与结构化 facets；不得拼完整步骤或 step summary。

- [ ] **Step 5: 删除 C1 时间耦合并接入过滤**

  从 `RecipeRetrievalService.load()` 移除 `step_tasks` 加载和 `_time_lookup`；删除 `_apply_time_boost`。接口改为：

  ```python
  def retrieve(
      self,
      query: str,
      *,
      filters: RetrievalFilters,
      top_k: int = 20,
      exclude_ids: set[int] | None = None,
  ) -> RetrievalResult:
      ...
  ```

  BM25 候选在打分前限制为匹配 payload 的文档；向量检索直接传 Qdrant filter；两路 RRF 和 rerank 后再次执行同一个 predicate 作防御性校验。

- [ ] **Step 6: 建 Qdrant payload indexes**

  staging collection 创建后为所有 tag 数组、ingredient names/ids、eligibility、build_id 建 keyword/integer payload index。`index_documents` 必须写入与 JSONL 完全一致的 payload。

- [ ] **Step 7: 从 QueryPlan 映射过滤器**

  `_retrieve_recipes` 从 `ctx.previous_results["query_plan"]` 构造过滤器；工具参数中的自由字段不能覆盖 QueryPlan。多人偏好子查询共享相同硬过滤器。

- [ ] **Step 8: 运行局部与真实 Qdrant 测试**

  Run: `uv run pytest tests/c1/test_retrieval_filters.py tests/c1/test_full_hybrid_retrieval.py tests/c3/test_mc02_online_b2.py -q`

  Run when Qdrant is available: `uv run pytest tests/integration/test_real_qdrant_retrieval.py -q`

- [ ] **Step 9: 提交**

  ```bash
  git add src/food_agent_v2/c1/filters.py src/food_agent_v2/b1/rag_document_builder.py src/food_agent_v2/b1/consumer_views.py src/food_agent_v2/c1/__init__.py src/food_agent_v2/c1/qdrant_client.py src/food_agent_v2/c3/tool_handler.py tests/c1/test_retrieval_filters.py tests/c1/test_full_hybrid_retrieval.py tests/integration/test_real_qdrant_retrieval.py
  git commit -m "feat: filter rag retrieval with complete recipe labels"
  ```

## Task 8: 原子化步骤并解析所有显式时长

**Files:**

- Create: `src/food_agent_v2/b1/step_atomizer.py`
- Rewrite: `src/food_agent_v2/b1/step_time_builder.py`
- Modify: `src/food_agent_v2/b1/schemas.py`
- Replace: `tests/b1/test_step_authority.py`
- Create: `tests/b1/test_step_atomizer.py`
- Create: `tests/b1/test_explicit_duration_parser.py`

- [ ] **Step 1: 写失败测试**

  覆盖秒/分/小时、小数、“半小时/一刻钟”、范围中点、隔夜，以及 `准备好/备用/享用` 的 non-task 零值。覆盖复合句“放入烤箱，烤30分钟”拆成设备启动与 `unattended_equipment` 两个 atom。相同原步骤必须产生稳定 `atom_id`。

- [ ] **Step 2: 运行失败测试**

  Run: `uv run pytest tests/b1/test_step_atomizer.py tests/b1/test_explicit_duration_parser.py tests/b1/test_step_authority.py -q`

- [ ] **Step 3: 定义中间与最终 Schema**

  ```python
  class StepAtom(BaseModel):
      atom_id: str
      source_step_index: int
      text: str
      explicit_duration_seconds: int | None
      duration_locked: bool

  class StepTask(BaseModel):
      atom_id: str
      text: str
      duration_seconds: int
      task_type: Literal[
          "manual", "attended_equipment", "unattended_equipment", "passive", "non_task"
      ]
      resources: tuple[str, ...]
      depends_on: tuple[str, ...]
  ```

  `duration_locked` 只用于构建中间态；最终 `step_tasks` 不含 source/confidence/range。

- [ ] **Step 4: 实现确定性 atomizer 和 duration parser**

  显式范围取数学中点并转换为整数秒；`隔夜` 使用已批准常量 8 小时。atom ID 使用 `recipe_id + source_step_index + normalized_atom_text` 的内容散列，重排以外的同内容重建保持稳定。

- [ ] **Step 5: 运行测试与 schema 扫描**

  Run: `uv run pytest tests/b1/test_step_atomizer.py tests/b1/test_explicit_duration_parser.py tests/b1/test_step_authority.py -q`

  Run: `rg -n "duration_confidence|time_source|minimum_seconds|maximum_seconds" src/food_agent_v2/b1/step_atomizer.py src/food_agent_v2/b1/step_time_builder.py`

  Expected: 最终序列化路径无旧字段。

- [ ] **Step 6: 提交**

  ```bash
  git add src/food_agent_v2/b1/step_atomizer.py src/food_agent_v2/b1/step_time_builder.py src/food_agent_v2/b1/schemas.py tests/b1/test_step_authority.py tests/b1/test_step_atomizer.py tests/b1/test_explicit_duration_parser.py
  git commit -m "feat: atomize recipe steps and parse explicit durations"
  ```

## Task 9: 用一次整菜模型调用补全任务图并独立校验

**Files:**

- Create: `src/food_agent_v2/b1/time_graph_profiler.py`
- Rewrite: `src/food_agent_v2/b1/llm_time_profiler.py`
- Modify: `src/food_agent_v2/cli.py`
- Create: `data/cache/.gitkeep`
- Create: `tests/b1/test_time_graph_profiler.py`
- Modify: `tests/b1/test_step_authority.py`

- [ ] **Step 1: 写失败测试**

  使用 fake model 覆盖：整菜只调用一次生成器；所有 atom 都得到 task_type/resources/deps；显式时长不被覆盖；缺一个 ID、重复 ID、环、非法资源、负时长和越界估算均失败；生成器输出经第二个 verifier 调用；相同 cache key 不重复调用模型。

- [ ] **Step 2: 运行失败测试**

  Run: `uv run pytest tests/b1/test_time_graph_profiler.py -q`

- [ ] **Step 3: 定义模型接口和 cache key**

  ```python
  def profile_recipe_time_graph(
      recipe_id: int,
      recipe_name: str,
      atoms: tuple[StepAtom, ...],
      model: StructuredModel,
      verifier: StructuredModel,
      cache: TimeGraphCache,
  ) -> RecipeTimeProfile:
      ...
  ```

  cache key 严格为 `sha256(recipe_id, ordered_atoms_hash, prompt_version, model_id)`。cache 是可再生构建缓存，不是事实输入，不加入 source manifest。

  CLI 增加 `food-agent-v2 data-review --kind time-graphs --output <jsonl>`：批量生成或复用 `data/cache/recipe_time_graphs.jsonl`，只把程序或 verifier 判定的冲突写入 output，不直接发布 Artifact。普通 `data-rebuild` 读取并验证 cache，不在构建事务中临时发起 1914 道菜的网络请求。

- [ ] **Step 4: 约束生成器和 verifier**

  生成器必须为每个 atom 输出单一 `duration_seconds`、task type、资源、依赖；显式 duration_locked 值由程序覆盖模型值。模型估算上限：manual/attended `21600` 秒，unattended/passive `604800` 秒；显式合法时长可超限。verifier 只返回封闭问题代码和关联 atom IDs，程序决定是否通过，不接收自由解释。

- [ ] **Step 5: 实现程序级图验证**

  合法资源固定为 `cook/burner/oven/steamer/microwave/blender/fridge/counter`。检查 atom 覆盖一一对应、依赖存在、自依赖禁止、DAG、资源与 task type 组合合法、non_task duration=0。任一冲突都不输出 ready profile。

- [ ] **Step 6: 运行测试**

  Run: `uv run pytest tests/b1/test_time_graph_profiler.py tests/b1/test_step_authority.py -q`

- [ ] **Step 7: 提交**

  ```bash
  git add src/food_agent_v2/b1/time_graph_profiler.py src/food_agent_v2/b1/llm_time_profiler.py src/food_agent_v2/cli.py data/cache/.gitkeep tests/b1/test_time_graph_profiler.py tests/b1/test_step_authority.py
  git commit -m "feat: generate and verify recipe time task graphs"
  ```

## Task 10: 用 CP-SAT 计算单菜和菜单预计时长

**Files:**

- Create: `src/food_agent_v2/b5/scheduler.py`
- Rewrite: `src/food_agent_v2/b5/__init__.py`
- Modify: `src/food_agent_v2/c2/planner.py`
- Modify: `src/food_agent_v2/c3/tool_handler.py`
- Modify: `src/food_agent_v2/c3/orchestrator.py`
- Modify: `src/food_agent_v2/c3/runner.py`
- Replace: `tests/b5/test_strict_time_semantics.py`
- Create: `tests/b5/test_task_graph_scheduler.py`
- Modify: `tests/c2/test_menu_hard_constraints.py`
- Modify: `tests/c3/test_mc01_safe_candidates.py`
- Modify: `tests/e2e/test_full_chain_failure_paths.py`

- [ ] **Step 1: 写 CP-SAT 失败测试**

  覆盖依赖关键路径、同一 cook 互斥、两个 burner 并行、单 oven 互斥、unattended oven 不占 cook、passive 只受依赖、两道菜共享资源的 menu makespan、30 分钟 expected hard filter。

- [ ] **Step 2: 运行失败测试**

  Run: `uv run pytest tests/b5/test_task_graph_scheduler.py tests/c2/test_menu_hard_constraints.py -q`

- [ ] **Step 3: 实现厨房容量与调度结果**

  ```python
  @dataclass(frozen=True)
  class KitchenCapacity:
      cook: int = 1
      burner: int = 2
      oven: int = 1
      steamer: int = 1
      microwave: int = 1
      blender: int = 1

  @dataclass(frozen=True)
  class ScheduleResult:
      recipe_ids: tuple[int, ...]
      active_seconds: int
      estimated_makespan_seconds: int
      estimated_time_feasible: bool
      schedule_hash: str
  ```

  fridge/counter 不设 cumulative capacity。manual 占 cook；attended_equipment 占 cook+device；unattended_equipment 只占 device；passive 只建 interval 和依赖。

  `active_seconds` 定义为全部 manual 与 attended_equipment 任务时长之和，不是 makespan；未给时间上限时 `estimated_time_feasible=True`，给定上限时才比较预计 makespan。

- [ ] **Step 4: 接入 B5/C2/C3**

  `MenuHardConstraints.strict_time_limit` 重命名为 `max_estimated_time_seconds`；`MenuPlanner` 只对 B4 safe IDs 调 B5。硬时限比较 `estimated_makespan_seconds <= max_estimated_time_seconds`。删除 confidence、authority、missing_facts、time_source 和旧 unknown 分支。回答字段和文案统一使用“预计 X 分钟”，不得承诺“保证完成”。

- [ ] **Step 5: 更新失败状态与回归测试**

  旧 `strict_time_indeterminate` 测试改为：固定构建缺时间图时 readiness/build fail；在线已 ready 的系统只产生 true/false。B5 仓储失败走现有工具失败路径。

- [ ] **Step 6: 运行局部测试**

  Run: `uv run pytest tests/b5/test_task_graph_scheduler.py tests/c2/test_menu_hard_constraints.py tests/c2/test_deterministic_plans.py tests/c3/test_mc01_safe_candidates.py tests/e2e/test_full_chain_failure_paths.py -q`

- [ ] **Step 7: 提交**

  ```bash
  git add src/food_agent_v2/b5/scheduler.py src/food_agent_v2/b5/__init__.py src/food_agent_v2/c2/planner.py src/food_agent_v2/c3/tool_handler.py src/food_agent_v2/c3/orchestrator.py src/food_agent_v2/c3/runner.py tests/b5/test_strict_time_semantics.py tests/b5/test_task_graph_scheduler.py tests/c2/test_menu_hard_constraints.py tests/c3/test_mc01_safe_candidates.py tests/e2e/test_full_chain_failure_paths.py
  git commit -m "feat: schedule recipe task graphs with cp-sat"
  ```

## Task 11: 把审阅数据和 V2 Artifact 接入完整 B1 rebuild

**Files:**

- Modify: `src/food_agent_v2/b1/rebuild.py`
- Modify: `src/food_agent_v2/b1/consumer_views.py`
- Modify: `src/food_agent_v2/b1/quality_gates.py`
- Modify: `src/food_agent_v2/b1/cross_domain_validator.py`
- Modify: `tests/test_b1_pipeline.py`
- Modify: `tests/b1/test_fixed_recipe_source.py`
- Modify: `tests/b1/test_consumer_view_consistency.py`

- [ ] **Step 1: 写失败的 schema version、覆盖率和边界门禁测试**

  断言 19 个固定 Artifact 仍完整，`rag_documents/nutrition_features/step_tasks` version 为 2.0.0，在线 QueryPlan 契约测试另行断言 2.0.0；RAG/时间覆盖全部 eligible IDs；每个 RAG 文档至少一个 meal tag；时间图全部 ready；营养 unavailable 行数值全 null；运行时三种 Artifact 均不存在 confidence/source/range；nutrition coverage 不得在无批准的情况下相对基线下降。

- [ ] **Step 2: 运行失败测试**

  Run: `uv run pytest tests/test_b1_pipeline.py tests/b1/test_fixed_recipe_source.py tests/b1/test_consumer_view_consistency.py -q`

- [ ] **Step 3: 扩展 source manifest 和构建输入**

  将以下人工批准文件纳入 canonical source manifest：画像补全、条件默认项、measure rules、edible fractions、quantity decisions、nutrition crosswalk、nutrition reference。模型 cache 不纳入；其验证后的最终 time graph 内容通过 `step_tasks` artifact hash 进入 build identity。

- [ ] **Step 4: 接入 T07 构建顺序**

  顺序固定：source/identity → reviewed facets/conditions → quantity normalization → nutrition calculation → rag document → step atom → validated time-graph cache。RAG 不等待营养 available；营养 pending 只使单菜 unavailable；cache 缺当前 key 或任何时间图验证失败都使整个 build fail，普通 rebuild 不隐式调用在线模型。

- [ ] **Step 5: 更新质量门禁**

  新增：

  - `G12_RAG_LABEL_AND_MEAL_COVERAGE`
  - `G13_RAG_RUNTIME_FIELD_BOUNDARY`
  - `G14_NUTRITION_ALL_OR_NOTHING`
  - `G15_NUTRITION_COVERAGE_NON_REGRESSION`
  - `G16_TIME_GRAPH_COMPLETE_AND_ACYCLIC`
  - `G17_B4_B5_B6_CONSUMER_BOUNDARY`

  `cross_domain_validator.py` 不再读取旧 `data/cleaned/nutrition_profiles.jsonl` 或 duration confidence，而应接受 manifest artifact paths 并执行相同的跨域不变量。

- [ ] **Step 6: 运行局部重建测试**

  Run: `uv run pytest tests/test_b1_pipeline.py tests/b1 -q`

- [ ] **Step 7: 提交**

  ```bash
  git add src/food_agent_v2/b1/rebuild.py src/food_agent_v2/b1/consumer_views.py src/food_agent_v2/b1/quality_gates.py src/food_agent_v2/b1/cross_domain_validator.py tests/test_b1_pipeline.py tests/b1/test_fixed_recipe_source.py tests/b1/test_consumer_view_consistency.py
  git commit -m "feat: rebuild reviewed rag nutrition and time artifacts"
  ```

## Task 12: 更新 MySQL/Qdrant 原子初始化与运行时 readiness

**Files:**

- Modify: `src/food_agent_v2/b1/database_loader.py`
- Modify: `src/food_agent_v2/b1/seed_writer.py`
- Modify: `src/food_agent_v2/c1/qdrant_client.py`
- Modify: `src/food_agent_v2/application/readiness.py`
- Modify: `db/schema_mysql.sql`
- Modify: `tests/integration/test_staging_initialization.py`
- Modify: `tests/integration/test_mysql_qdrant_parity.py`
- Modify: `tests/integration/test_api_readiness.py`

- [ ] **Step 1: 写失败的 staging/parity/readiness 测试**

  验证 Qdrant payload 与 `rag_documents` tag 数组逐项一致、MySQL/Qdrant recipe ID 集合相同、alias 只在两边 staging 都成功后切换、旧 build 仍可回滚、readiness 检查三个固定数据 Artifact 的 schema version 2.0.0 和固定 19 Artifact。

- [ ] **Step 2: 运行失败测试**

  Run: `uv run pytest tests/integration/test_staging_initialization.py tests/integration/test_mysql_qdrant_parity.py tests/integration/test_api_readiness.py -q`

- [ ] **Step 3: 更新存储结构**

  MySQL `nutrition_profiles` 存 `available`、`raw_edible_input_weight_g`、`raw_nutrition_total`、`raw_nutrition_per_100g`、`reason`；`time_profiles` 存 `active_seconds`、`estimated_elapsed_seconds`、`step_tasks`。移除旧 confidence/match_method/coverage ratio 权威读取路径。固定 Artifact 原始 JSON 仍保存在 `fixed_artifact_records` 供重放。

- [ ] **Step 4: 更新 staging 发布**

  Qdrant staging collection 先建 payload index，再批量写入，再核对 point count 与 payload recipe IDs；任一步失败都不得切 alias 或提交 MySQL build。成功时才同时激活新 build。

- [ ] **Step 5: 运行集成测试**

  Run: `uv run pytest tests/integration/test_staging_initialization.py tests/integration/test_mysql_qdrant_parity.py tests/integration/test_api_readiness.py -q`

- [ ] **Step 6: 提交**

  ```bash
  git add src/food_agent_v2/b1/database_loader.py src/food_agent_v2/b1/seed_writer.py src/food_agent_v2/c1/qdrant_client.py src/food_agent_v2/application/readiness.py db/schema_mysql.sql tests/integration/test_staging_initialization.py tests/integration/test_mysql_qdrant_parity.py tests/integration/test_api_readiness.py
  git commit -m "feat: publish v2 artifacts atomically to mysql and qdrant"
  ```

## Task 13: 生成实际审阅包并暂停用户审批

**Files:**

- Modify: `data/review/recipe_profile_enrichment.jsonl`
- Modify: `data/review/ingredient_condition_defaults.csv`
- Modify: `data/review/ingredient_measure_rules.csv`
- Modify: `data/review/ingredient_edible_fraction_rules.csv`
- Modify: `data/review/ingredient_quantity_decisions.csv`
- Modify: `data/review/ingredient_nutrition_crosswalk.jsonl`
- Modify: `data/reference/ingredient_nutrition.jsonl`
- Create: `reports/data_review/quantity_candidates.csv`
- Create: `reports/data_review/profile_candidates.jsonl`
- Create: `reports/data_review/nutrition_crosswalk_candidates.csv`
- Create: `reports/data_review/time_graph_conflicts.jsonl`
- Create: `reports/data_review/coverage_summary.json`

- [ ] **Step 1: 从 V2 原始 CSV 生成全部候选，不读取 datas**

  Run:

  ```bash
  uv run food-agent-v2 data-review --kind profiles --output reports/data_review/profile_candidates.jsonl
  uv run food-agent-v2 data-review --kind quantities --output reports/data_review/quantity_candidates.csv
  uv run food-agent-v2 data-review --kind nutrition --output reports/data_review/nutrition_crosswalk_candidates.csv
  uv run food-agent-v2 data-review --kind time-graphs --output reports/data_review/time_graph_conflicts.jsonl
  ```

- [ ] **Step 2: 自动审查候选**

  检查重复 occurrence、越界克重、未知单位、敏感标签生成、one-of 多选、非唯一营养映射、来源缺 URL、九维缺失、结构零误用，以及时间图 ID/依赖/资源/边界/verifier 冲突。所有自动失败项保持 pending/rejected。将各类总数、approved/pending/rejected 数及按菜覆盖率写入 `reports/data_review/coverage_summary.json`。

- [ ] **Step 3: 向用户提交关键审阅包**

  必须暂停并让用户审阅：

  - 所有 `适量/少许/若干/几滴` 和步骤新增食材的单值克重；
  - 新增 density/unit-weight/edible-fraction 批量规则；
  - 无 exact match 的营养 crosswalk；
  - 新生成的敏感人口/健康/功效标签（默认应为空）；
  - 未在已批准九项内的 one-of 默认项。
  - 时间生成器与独立 verifier 无法自动收敛的关键步骤或并行冲突。

  未经用户回复，不得把对应 status 改为 approved，也不得开始正式 nutrition build 发布。

- [ ] **Step 4: 合并用户决定并再验证**

  用户修改写入 `decision_grams` 或对应规则值，status 写 `modified`；同意则写 `approved`；拒绝保留 `rejected`。重新运行候选审查，确保不存在程序自行批准的行。

- [ ] **Step 5: 提交已审批事实**

  ```bash
  git add data/review data/reference/ingredient_nutrition.jsonl reports/data_review
  git commit -m "data: approve recipe quantity and nutrition decisions"
  ```

## Task 14: 文档、全链路回归和全局冲突审计

**Files:**

- Create: `docs/decisions/0007-estimated-task-graph-time-semantics.md`
- Modify: `docs/modules/05-time-and-steps.md`
- Modify: `docs/modules/06-nutrition-scoring.md`
- Modify: `docs/modules/07-rag-retrieval.md`
- Modify: `docs/modules/09-agent-workflow.md`
- Modify: `tests/contracts/invariant_traceability.yaml`
- Modify: `tests/acceptance/test_run_full_acceptance_contract.py`
- Modify: `tests/e2e/test_full_chain_real.py`

- [ ] **Step 1: 写 ADR 并同步模块文档**

  ADR-0007 明确 supersede ADR-0005 的在线时间三值/高置信权威部分，但保留旧 ADR 作为历史。文档写清“预计单值”、默认厨房、CP-SAT、原始营养非烹饪后营养、B4 唯一健康裁决、RAG 餐次硬过滤及语义重写边界。

- [ ] **Step 2: 更新不变量追踪**

  至少加入以下不变量到 `invariant_traceability.yaml`：标签全量投影、meal fail-closed、B4 与 B5/B6 边界、营养 all-or-nothing、time graph complete、runtime no provenance/confidence、MySQL/Qdrant build parity。

- [ ] **Step 3: 执行完整固定数据重建到新的临时目录**

  PowerShell：

  ```powershell
  $reviewBuildDir = Join-Path $env:TEMP ("food-agent-v2-review-" + [guid]::NewGuid())
  uv run food-agent-v2 data-rebuild --staging-dir $reviewBuildDir
  uv run food-agent-v2 data-verify --manifest (Join-Path $reviewBuildDir "build_manifest.json")
  ```

  不删除或覆盖当前 active build。

- [ ] **Step 4: 运行分层测试**

  ```bash
  uv run pytest tests/contracts tests/b1 tests/b5 tests/b6 tests/c1 tests/c2 tests/c3 -q
  uv run pytest tests/integration -q
  uv run pytest tests/acceptance tests/e2e -q
  uv run ruff check src tests
  ```

- [ ] **Step 5: 执行全局冲突扫描**

  ```bash
  rg -n "strict_time_indeterminate|strict_time_feasible|duration_confidence|overall_confidence|time_source|coverage_ratio|match_method" src tests docs/modules docs/decisions
  rg -n "step_tasks|_time_lookup|_apply_time_boost" src/food_agent_v2/c1
  rg -n "nutrition_features|raw_nutrition" src/food_agent_v2/b4
  ```

  Expected:

  - 旧字段只允许出现在历史 ADR 或迁移说明；
  - C1 不读取 step_tasks；
  - B4 不读取 nutrition/time；
  - B5/B6 的在线调用点都位于 B4 safe 集合之后；
  - RAG、营养、时间的 recipe IDs 与 eligible 集合一致；
  - 运行时 Artifact 不含 confidence/source/range；
  - 用户原句“老人晚餐”端到端返回的所有候选都通过晚餐标签硬过滤，再进入 B4。

- [ ] **Step 6: 原子初始化演练**

  在测试容器上运行 `data-initialize`，核对 staging collection、payload indexes、MySQL build、Qdrant alias 与 rollback。未显式获得生产/当前 active 环境发布授权时，不切换真实 active build。

- [ ] **Step 7: 最终审视与提交**

  记录测试命令、通过数、构建 ID、三类 coverage、所有 pending review 数和未执行的外部依赖测试原因。

  ```bash
  git add docs tests/contracts/invariant_traceability.yaml tests/acceptance/test_run_full_acceptance_contract.py tests/e2e/test_full_chain_real.py
  git commit -m "docs: finalize reviewed rag nutrition and time rebuild"
  ```

## Execution Checkpoints

1. Tasks 1–5 后检查数据契约：条件食材、数量审阅与营养 all-or-nothing 不冲突。
2. Tasks 6–7 后检查在线检索：重写只解释语义，RAG 只筛菜，B4 仍是健康权威。
3. Tasks 8–10 后检查时间：显式时长不可覆盖，模型只补单值任务图，CP-SAT 才计算总时长。
4. Tasks 11–12 后检查构建：schema version、质量门禁、MySQL/Qdrant build identity 一致。
5. Task 13 是强制人工审阅点；未批准的模糊克重和营养映射不得越过。
6. Task 14 从全局检查旧字段、模块反向依赖、数据覆盖率和发布原子性。

## Completion Criteria

- “推荐老人吃的晚餐”先得到结构化 population/meal facets，C1 返回项全部满足晚餐标签，之后才进入 B4。
- RAG 文档包含完整原 label 和结构化画像，不含完整步骤；C1 完全移除时间查表与时间 boost。
- 每个 eligible recipe 都有 RAG 文档和完整合法时间图；营养允许 unavailable，但 all-or-nothing 且 coverage 不得未经批准下降。
- 九维营养只计算原始可食投料总量与每 100g，不计算得率、保留率、份数或人均。
- 时长只有单个预计值，无置信度/来源/区间；单菜和多菜菜单都通过默认厨房资源 CP-SAT 计算。
- B4 不读取营养/时间；B5/B6 只读取 B4 safe 集合；C2 对 unavailable 软维度重分配权重；终选仍由 B4 复核。
- 新 build 在 MySQL/Qdrant staging 均通过后才能原子激活，失败可回滚且不影响当前 active build。
- 所有用户需审阅决定有可读候选包，且没有任何程序自行把 pending 改为 approved。
