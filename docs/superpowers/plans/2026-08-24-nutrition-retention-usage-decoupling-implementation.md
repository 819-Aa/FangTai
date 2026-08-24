# Nutrition Retention/Usage Decoupling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不重建 9,618 条既有营养参考数据的前提下，将 occurrence 用途与实际留存正式解耦，修正数量/可食比例计算门禁，生成独立待审队列，并落地用户已批准的 7 项密度规则。

**Architecture:** B1 使用两个正式 occurrence 决定文件分别解析 `usage_code` 与 `retained_in_dish`，`RecipeNutritionInputView` 2.1.0 同时投影细分审核状态和兼容聚合状态。数量解析只在模糊单值规则分支依赖用途；营养计算先处理留存，再复用既有数量、可食比例、crosswalk 和九维参考进行机械汇总。离线 `nutrition-occurrences` 入口只生成四份 pending CSV，不调用模型、不写正式文件。

**Tech Stack:** Python 3.11、dataclasses、Pydantic v2、Decimal、pytest、Ruff、CSV/JSONL、现有 B1 build/SourceManifest 管线。

## Global Constraints

- 权威设计为 `docs/superpowers/specs/2026-08-24-nutrition-retention-usage-decoupling-design.md`；本计划不得恢复旧的用途/留存绑定。
- 保留 `data/reference/ingredient_nutrition.jsonl` 的 9,618 条现有参考；不得重新生成或用模型改写九维营养值。
- 只计算原始可食投料营养；不计算烹饪得率、营养保留率、吸水率、成品重量、份数或个人摄入量。
- H05 容器、数据库、Qdrant、Redis、API 配置和 ready build 不得修改；本计划只准备 H06 输入与代码。
- RAG、B4 健康硬过滤、B5 时间、C2 菜单、固定套餐和烹饪程序依赖不得改变职责或数据字段。
- 正式运行时和用户回答不新增来源、置信度、模型解释或审核状态。
- 新增生产行为严格执行 TDD：先写测试并观察预期失败，再写最小实现并观察通过。
- 当前 worktree 已有用户改动；每个任务只暂存任务清单中的精确文件，禁止覆盖、格式化或提交无关差异。
- 每个任务执行一遍对应专项测试与静态检查，由独立审查代理做一次任务门禁；全部任务后主代理再做一次全局冲突审查。
- 正式用途与留存文件只让 `approved/modified` 生效，但所有状态都参与 occurrence 唯一性和元数据校验。
- 候选入口必须 pending-only、无 LLM、固定 Schema、公式前缀转义、原子写入，并拒绝正式路径的大小写、相对路径、符号链接和硬链接别名。
- 已批准数量结论固定为 7 项正式、1 项拒绝且不写正式规则、7 项继续 pending；不得自行决定后 7 项的数值。

---

### Task 1: 拆分正式用途/留存契约并升级构建身份

**Files:**
- Modify: `src/food_agent_v2/b1/nutrition_occurrence_rules.py`
- Modify: `src/food_agent_v2/b1/consumer_views.py`
- Modify: `src/food_agent_v2/b1/source_manifest.py`
- Modify: `src/food_agent_v2/b1/rebuild.py`
- Modify: `src/food_agent_v2/b1/quality_gates.py`
- Modify: `src/food_agent_v2/b1/data_review.py`
- Modify: `data/review/ingredient_nutrition_usage_decisions.csv`
- Create: `data/review/ingredient_nutrition_retention_decisions.csv`
- Modify: `tests/b1/test_nutrition_occurrence_rules.py`
- Modify: `tests/b1/test_consumer_view_consistency.py`
- Modify: `tests/b1/test_fixed_recipe_source.py`
- Modify: `tests/test_b1_pipeline.py`

**Interfaces:**
- Produces: `UsageCode = Literal["main", "supporting", "seasoning", "cooking_fat", "cooking_liquid"]`.
- Produces: `NutritionUsageDecisionIndex.get_effective(occurrence_id) -> NutritionUsageDecision | None`.
- Produces: `NutritionRetentionDecisionIndex.get_effective(occurrence_id) -> NutritionRetentionDecision | None`.
- Produces: `derive_nutrition_usage(...) -> NutritionUsageResolution` and `derive_nutrition_retention(...) -> NutritionRetentionResolution`.
- Produces: `NutritionOccurrenceInput.usage_requires_review`, `.retention_requires_review` and serialized `.requires_review` equal to their logical OR.
- Produces: `build_consumer_views(..., nutrition_usage_decisions=..., nutrition_retention_decisions=...)`.
- Produces: `recipe_nutrition_input_views` artifact Schema `2.1.0`; other existing runtime artifact versions remain unchanged.

- [ ] **Step 1: 写独立契约和构建身份的失败测试**

在 `tests/b1/test_nutrition_occurrence_rules.py` 将旧 8 列测试拆成两个契约，并增加以下行为测试：

```python
def test_seasoning_resolves_usage_but_not_retention() -> None:
    usage = derive_nutrition_usage(
        occurrence=_occurrence(), identity=_identity(category="调料"),
        steps=_bound_steps("撒盐调味"), decisions=NutritionUsageDecisionIndex(()),
    )
    retention = derive_nutrition_retention(
        occurrence=_occurrence(), identity=_identity(category="调料"),
        steps=_bound_steps("撒盐调味"), decisions=NutritionRetentionDecisionIndex(()),
    )
    assert usage == NutritionUsageResolution("seasoning", False)
    assert retention == NutritionRetentionResolution(None, True)


def test_usage_and_retention_csvs_have_independent_active_rows(tmp_path: Path) -> None:
    usage_path = tmp_path / "usage.csv"
    usage_path.write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,usage_code,review_status\n"
        "1-1,1,10,盐,,seasoning,approved\n", encoding="utf-8",
    )
    retention_path = tmp_path / "retention.csv"
    retention_path.write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,retained_in_dish,review_status\n"
        "1-1,1,10,盐,,false,modified\n", encoding="utf-8",
    )
    assert load_nutrition_usage_decisions(usage_path).get_effective("1-1").usage_code == "seasoning"
    assert load_nutrition_retention_decisions(retention_path).get_effective("1-1").retained_in_dish is False
```

同时覆盖两个文件各自的非法表头、重复 occurrence（包括 inactive 行）、未知 occurrence、非法枚举/布尔值、metadata drift；在 `tests/b1/test_fixed_recipe_source.py` 断言 retention 文件单独改变 SourceManifest hash，在 `tests/test_b1_pipeline.py` 把 nutrition input view 期望改为 `2.1.0`。

- [ ] **Step 2: 运行测试并确认因缺少独立 retention 契约而失败**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/b1/test_nutrition_occurrence_rules.py tests/b1/test_consumer_view_consistency.py tests/b1/test_fixed_recipe_source.py tests/test_b1_pipeline.py -q
```

Expected: FAIL，至少包含 `ImportError: NutritionRetentionDecisionIndex`、旧 usage 表头仍要求 `retained_in_dish` 或 nutrition view 仍为 `2.0.0`；失败不能来自测试语法或 fixture 错误。

- [ ] **Step 3: 实现两个严格决定加载器与解析结果**

在 `nutrition_occurrence_rules.py` 将正式类型固定为：

```python
@dataclass(frozen=True)
class NutritionUsageDecision:
    occurrence_id: str
    recipe_id: int
    ingredient_id: int
    ingredient_name: str
    normalized_form: str
    usage_code: UsageCode
    review_status: ReviewStatus


@dataclass(frozen=True)
class NutritionRetentionDecision:
    occurrence_id: str
    recipe_id: int
    ingredient_id: int
    ingredient_name: str
    normalized_form: str
    retained_in_dish: bool
    review_status: ReviewStatus


@dataclass(frozen=True)
class NutritionUsageResolution:
    usage_code: UsageCode | None
    requires_review: bool


@dataclass(frozen=True)
class NutritionRetentionResolution:
    retained_in_dish: bool | None
    requires_review: bool
```

两个 Index 均对所有状态先检查 occurrence 唯一，对 `approved/modified` 建有效索引；两个 resolver 都先验证正式决定的 recipe/ingredient/name/form metadata。`derive_nutrition_usage` 只允许类别 `调料` 机械得到 `seasoning`；受控油、液体和其他类别保持待审。`derive_nutrition_retention` 没有类别默认值，缺少正式决定一律返回 `NutritionRetentionResolution(None, True)`。

将 usage 正式表改为 7 列表头，并创建 retention 正式表的 7 列表头；两表此时都不写业务行。

- [ ] **Step 4: 投影细分状态并接入 SourceManifest/rebuild**

将 `NutritionOccurrenceInput` 定义为包含：

```python
usage_requires_review: bool = False
retention_requires_review: bool = False
requires_review: bool = field(init=False)

def __post_init__(self) -> None:
    object.__setattr__(
        self,
        "requires_review",
        self.usage_requires_review or self.retention_requires_review,
    )
```

`_nutrition_inputs()` 分别调用两个 resolver。`source_manifest._BUILD_INPUT_PATHS` 加入 retention 文件；`rebuild.py` 加常量、loader 和 `build_consumer_views` 参数。`artifact_schema_versions()` 精确返回 nutrition input view `2.1.0`，`quality_gates.verify_build_manifest()` 使用同一版本矩阵，不能继续让 rebuild 与 verifier 各自维护冲突集合。`data_review._build_review_views()` 同样加载两套决定。

- [ ] **Step 5: 运行专项测试和 Ruff**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/b1/test_nutrition_occurrence_rules.py tests/b1/test_consumer_view_consistency.py tests/b1/test_fixed_recipe_source.py tests/test_b1_pipeline.py -q
.venv\Scripts\python.exe -m ruff check src/food_agent_v2/b1/nutrition_occurrence_rules.py src/food_agent_v2/b1/consumer_views.py src/food_agent_v2/b1/source_manifest.py src/food_agent_v2/b1/rebuild.py src/food_agent_v2/b1/quality_gates.py src/food_agent_v2/b1/data_review.py tests/b1/test_nutrition_occurrence_rules.py tests/b1/test_consumer_view_consistency.py tests/b1/test_fixed_recipe_source.py tests/test_b1_pipeline.py
```

Expected: PASS；`git diff --check` 无错误。

- [ ] **Step 6: 只提交 Task 1 文件**

```powershell
git add -- src/food_agent_v2/b1/nutrition_occurrence_rules.py src/food_agent_v2/b1/consumer_views.py src/food_agent_v2/b1/source_manifest.py src/food_agent_v2/b1/rebuild.py src/food_agent_v2/b1/quality_gates.py src/food_agent_v2/b1/data_review.py data/review/ingredient_nutrition_usage_decisions.csv data/review/ingredient_nutrition_retention_decisions.csv tests/b1/test_nutrition_occurrence_rules.py tests/b1/test_consumer_view_consistency.py tests/b1/test_fixed_recipe_source.py tests/test_b1_pipeline.py
git commit -m "refactor: decouple nutrition retention decisions"
```

### Task 2: 修正数量、营养和可食比例的真实依赖门禁

**Files:**
- Modify: `src/food_agent_v2/b1/quantity_normalizer.py`
- Modify: `src/food_agent_v2/b1/nutrition_calculator.py`
- Modify: `src/food_agent_v2/b1/nutrition_feature_builder.py`
- Modify: `src/food_agent_v2/b1/edible_fraction_review.py`
- Modify: `src/food_agent_v2/b1/quantity_review.py`
- Modify: `src/food_agent_v2/b1/quantity_rule_review.py`
- Modify: `tests/b1/test_quantity_normalizer.py`
- Modify: `tests/b1/test_raw_nutrition_calculator.py`
- Modify: `tests/b1/test_edible_fraction_review.py`
- Modify: `tests/b1/test_quantity_review.py`

**Interfaces:**
- Consumes: Task 1 的两个细分审核状态和 `UsageCode` 新枚举。
- Produces: `QuantityNormalizationResult(..., reason: QuantityUnresolvedReason | None)`，其中稳定值为 `quantity_unapproved` 或 `usage_unresolved_for_fuzzy`。
- Produces: `RecipeNutritionFeatures.reason` 新稳定值 `retention_unresolved` 和 `usage_unresolved_for_fuzzy`。
- Produces: 可食比例队列只由留存事实控制，不再使用普通 usage 未决作为统一阻塞。

- [ ] **Step 1: 写确定性数量与留存优先级的失败测试**

在 `tests/b1/test_quantity_normalizer.py` 增加 literal 期望：

```python
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("100克", Decimal("100")), ("2个", Decimal("66")), ("250毫升", Decimal("255"))],
)
def test_deterministic_paths_do_not_require_usage(raw, expected, rules) -> None:
    occurrence = _occurrence(raw, usage=None, retained=True, usage_requires_review=True)
    assert normalize_quantity(occurrence, rules, QuantityDecisionIndex(())).standardized_grams == expected


def test_fuzzy_quantity_without_usage_has_specific_reason() -> None:
    result = normalize_quantity(
        _occurrence("少许", usage=None, retained=True, fuzzy_token_class="small_amount"),
        MeasureRuleIndex(()), QuantityDecisionIndex(()),
    )
    assert result.reason == "usage_unresolved_for_fuzzy"
```

测试 fixture 分别提供 `33g/个` 单位重量和 `1.02g/mL` 密度，不使用实现代码计算 expected。

在 `tests/b1/test_raw_nutrition_calculator.py` 增加：

```python
def test_retained_true_mass_calculates_with_usage_unresolved() -> None:
    result = calculate_raw_recipe_nutrition(
        _view(_ingredient("1-1", 10, "甲", "100克", usage_code=None,
                          retained_in_dish=True, usage_requires_review=True)),
        **_complete_dependencies(),
    )
    assert result.available is True


def test_retained_false_needs_no_usage_or_downstream_facts() -> None:
    result = calculate_raw_recipe_nutrition(
        _view(_ingredient("1-1", 10, "甲", "100克", usage_code=None,
                          retained_in_dish=False, usage_requires_review=True)),
        **_empty_dependencies(),
    )
    assert result.reason == "no_retained_ingredients"


def test_retention_unresolved_fails_before_quantity() -> None:
    result = calculate_raw_recipe_nutrition(
        _view(_ingredient("1-1", 10, "甲", "100克", retained_in_dish=None,
                          retention_requires_review=True)),
        **_empty_dependencies(),
    )
    assert result.reason == "retention_unresolved"
```

在 `tests/b1/test_edible_fraction_review.py` 增加 retained true + usage unresolved 仍可命中 fraction 规则、retention unresolved 单独标记 `unresolved_retention`、retained false 完全排除的测试。

- [ ] **Step 2: 运行失败测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/b1/test_quantity_normalizer.py tests/b1/test_raw_nutrition_calculator.py tests/b1/test_edible_fraction_review.py tests/b1/test_quantity_review.py -q
```

Expected: FAIL，显示旧 normalizer 仍在入口检查聚合 `requires_review/usage/retention`，calculator 仍返回 `usage_unresolved`，或 edible queue 仍产生 `unresolved_usage`。

- [ ] **Step 3: 按真实依赖顺序实现数量解析**

`normalize_quantity()` 固定为：occurrence 决定 → 明确质量 → 单位重量 → 密度 → 模糊单值。前四条不检查 usage 或 retention；只有 `fuzzy_token_class is not None and usage_code is None` 返回：

```python
QuantityNormalizationResult(
    standardized_grams=None,
    requires_review=True,
    review_status="pending",
    reason="usage_unresolved_for_fuzzy",
)
```

其他未命中返回 `reason="quantity_unapproved"`，成功返回 `reason=None`。将 `quantity_normalizer.py`、`quantity_review.py`、`quantity_rule_review.py`、`edible_fraction_review.py` 的封闭用途枚举同步从 `retained_liquid` 改为 `cooking_liquid`。

- [ ] **Step 4: 按留存优先实现营养与可食比例门禁**

`calculate_raw_recipe_nutrition()` 先检查 `retention_requires_review or retained_in_dish is None`，再过滤 `retained_in_dish is False`；只对 retained true occurrence 调用数量、fraction、crosswalk。数量专用 reason 原样映射到 recipe reason。

`generate_edible_fraction_candidates()` 不再检查聚合 `requires_review` 或普通 usage 状态：retained false 排除，retention unresolved 只写 `unresolved_retention`，retained true 正常解析规则/决定。`nutrition_feature_builder.build_nutrition_features_from_views()` 增加可选 `edible_fraction_decisions=None` 并传给 calculator，防止直接 facade 与 rebuild 装配语义漂移。

- [ ] **Step 5: 运行专项测试和 Ruff**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/b1/test_quantity_normalizer.py tests/b1/test_raw_nutrition_calculator.py tests/b1/test_edible_fraction_review.py tests/b1/test_quantity_review.py -q
.venv\Scripts\python.exe -m ruff check src/food_agent_v2/b1/quantity_normalizer.py src/food_agent_v2/b1/nutrition_calculator.py src/food_agent_v2/b1/nutrition_feature_builder.py src/food_agent_v2/b1/edible_fraction_review.py src/food_agent_v2/b1/quantity_review.py src/food_agent_v2/b1/quantity_rule_review.py tests/b1/test_quantity_normalizer.py tests/b1/test_raw_nutrition_calculator.py tests/b1/test_edible_fraction_review.py tests/b1/test_quantity_review.py
```

Expected: PASS；`git diff --check` 无错误。

- [ ] **Step 6: 只提交 Task 2 文件**

```powershell
git add -- src/food_agent_v2/b1/quantity_normalizer.py src/food_agent_v2/b1/nutrition_calculator.py src/food_agent_v2/b1/nutrition_feature_builder.py src/food_agent_v2/b1/edible_fraction_review.py src/food_agent_v2/b1/quantity_review.py src/food_agent_v2/b1/quantity_rule_review.py tests/b1/test_quantity_normalizer.py tests/b1/test_raw_nutrition_calculator.py tests/b1/test_edible_fraction_review.py tests/b1/test_quantity_review.py
git commit -m "fix: gate nutrition by retained occurrences"
```

### Task 3: 增加纯离线用途/留存候选工作流

**Files:**
- Create: `src/food_agent_v2/b1/nutrition_occurrence_review.py`
- Modify: `src/food_agent_v2/b1/data_review.py`
- Modify: `src/food_agent_v2/b1/edible_fraction_review.py`
- Modify: `src/food_agent_v2/b1/quantity_review.py`
- Modify: `src/food_agent_v2/cli.py`
- Create: `tests/b1/test_nutrition_occurrence_review.py`
- Modify: `tests/b1/test_data_review.py`
- Modify: `tests/b1/test_edible_fraction_review.py`
- Modify: `tests/b1/test_quantity_review.py`

**Interfaces:**
- Consumes: Task 1 的独立解析结果和 Task 2 的新门禁。
- Produces: `data-review --kind nutrition-occurrences --output <directory>`；其他 kind 仍把 `--output` 解释为单文件。
- Produces: `<directory>/nutrition_usage_candidates.csv`, `nutrition_usage_exceptions.csv`, `nutrition_retention_candidates.csv`, `nutrition_retention_exceptions.csv`。
- Produces: `generate_nutrition_occurrence_review(context) -> NutritionOccurrenceReviewBundle` 和 `write_nutrition_occurrence_review(bundle, output_dir) -> dict[str, int]`。

- [ ] **Step 1: 写纯函数分流和四文件 writer 的失败测试**

`tests/b1/test_nutrition_occurrence_review.py` 使用手工 occurrence/identity/step fixture，断言：

```python
def test_bound_solid_without_discard_risk_is_pending_retention_candidate() -> None:
    bundle = generate_nutrition_occurrence_review(_context(name="鸡肉", step="加入鸡肉炒熟"))
    assert [(row.occurrence_id, row.candidate_retained_in_dish, row.review_status)
            for row in bundle.retention_candidates] == [("1-1", True, "pending")]
    assert bundle.retention_exceptions == ()


def test_discard_and_no_binding_are_separate_retention_exceptions() -> None:
    discard = generate_nutrition_occurrence_review(_context(name="香料", step="煮后捞出香料"))
    missing = generate_nutrition_occurrence_review(_context(name="香料", step=None))
    assert discard.retention_exceptions[0].exception_codes == ("NUTRITION_RETENTION_DISCARD_RISK",)
    assert missing.retention_exceptions[0].exception_codes == ("NUTRITION_RETENTION_NO_STEP_BINDING",)


def test_writer_outputs_four_pending_only_atomic_csvs(tmp_path: Path) -> None:
    counts = write_nutrition_occurrence_review(_bundle_fixture(), tmp_path)
    assert counts == {
        "usage_candidates": 1, "usage_exceptions": 1,
        "retention_candidates": 1, "retention_exceptions": 1,
    }
    assert {path.name for path in tmp_path.iterdir()} == {
        "nutrition_usage_candidates.csv", "nutrition_usage_exceptions.csv",
        "nutrition_retention_candidates.csv", "nutrition_retention_exceptions.csv",
    }
```

另测：确定 seasoning 不重复进入用途候选；受控油只产生 `cooking_fat` pending 候选；无歧义受控液体只产生 `cooking_liquid` pending 候选；其他用途进入 `NUTRITION_USAGE_AMBIGUOUS`；油/液体留存不自动 true；所有文本公式前缀被转义；非法状态、重复 occurrence 和候选/异常交叉重复在 replace 前失败且不破坏旧文件。

- [ ] **Step 2: 写 CLI 注册、no-LLM 和正式路径别名失败测试**

在 `tests/b1/test_data_review.py` 注册新 kind，并用 sentinel 证明 route 不访问 `get_llm_client`、`load_config` 或任一 `LLM*`。分别覆盖 retention 正式文件本身、大小写同名、`data/review` 子路径、符号链接和可用平台上的硬链接；期望在构建 review context 前抛 `ValueError("候选不得写入正式规则或决定文件")`。

- [ ] **Step 3: 运行测试并确认新模块/路由缺失**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/b1/test_nutrition_occurrence_review.py tests/b1/test_data_review.py tests/b1/test_edible_fraction_review.py tests/b1/test_quantity_review.py -q
```

Expected: FAIL，原因是新模块或 `nutrition-occurrences` kind 尚不存在；不能是临时目录或 fixture 错误。

- [ ] **Step 4: 实现固定 Schema、分流和安全 writer**

四类 CSV 的共同身份字段顺序固定为：

```text
occurrence_id,recipe_id,recipe_name,ingredient_id,ingredient_name,source_fragment,
normalized_form,category,quantity_raw,bound_step_indexes,bound_step_text
```

候选文件随后追加 `candidate_usage_code` 或 `candidate_retained_in_dish`、`evidence_codes`、`review_status`；异常文件随后追加 `exception_codes`、`review_status`。所有 code 以排序后的 `;` 拼接，所有行按 `(recipe_id, occurrence_id)` 稳定排序。

用途分流只使用封闭机械证据：类别调料已由 resolver 解决；受控油身份生成 `cooking_fat` pending；受控水/汤液体身份生成 `cooking_liquid` pending；其余未决进入 `NUTRITION_USAGE_AMBIGUOUS`。留存分流：正式已解决记录不进入队列；无 occurrence-local 步骤绑定进入 `NUTRITION_RETENTION_NO_STEP_BINDING`；绑定步骤含倒掉、弃去、过滤、滤出、捞出、沥干、去除或倒出时进入 `NUTRITION_RETENTION_DISCARD_RISK`；油和液体即使无风险也进入 `NUTRITION_RETENTION_AMBIGUOUS`；其余有绑定且无风险的固体只生成 `candidate_retained_in_dish=true, review_status=pending`，绝不自动写正式文件。

writer 使用同目录 `NamedTemporaryFile`，四个文件全部验证成功后再逐个 `replace()`；写入前和 replace 前都重新执行正式路径 alias 防护。任何失败删除临时文件并保留已有目标。

- [ ] **Step 5: 接入 CLI 和统一正式路径防护**

新增 `_build_review_context()` 返回 views、occurrence facts、identity facts 和 recipe facts；现有 `_build_review_views()` 继续返回 `.views`，避免破坏其他 kind。新 kind 将 `--output` 解释为目录，返回四类计数 JSON。

将 `ingredient_nutrition_retention_decisions.csv` 同时加入：

- `data_review._refuse_formal_review_target()`；
- `edible_fraction_review._formal_review_paths()`；
- `quantity_review._is_formal_measure_rule_path()` 使用的正式路径集合。

直接调用 writer 和通过 CLI 调用都必须受到同等保护。

- [ ] **Step 6: 运行专项测试和 Ruff**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/b1/test_nutrition_occurrence_review.py tests/b1/test_data_review.py tests/b1/test_edible_fraction_review.py tests/b1/test_quantity_review.py -q
.venv\Scripts\python.exe -m ruff check src/food_agent_v2/b1/nutrition_occurrence_review.py src/food_agent_v2/b1/data_review.py src/food_agent_v2/b1/edible_fraction_review.py src/food_agent_v2/b1/quantity_review.py src/food_agent_v2/cli.py tests/b1/test_nutrition_occurrence_review.py tests/b1/test_data_review.py tests/b1/test_edible_fraction_review.py tests/b1/test_quantity_review.py
```

Expected: PASS；`git diff --check` 无错误。

- [ ] **Step 7: 只提交 Task 3 文件**

```powershell
git add -- src/food_agent_v2/b1/nutrition_occurrence_review.py src/food_agent_v2/b1/data_review.py src/food_agent_v2/b1/edible_fraction_review.py src/food_agent_v2/b1/quantity_review.py src/food_agent_v2/cli.py tests/b1/test_nutrition_occurrence_review.py tests/b1/test_data_review.py tests/b1/test_edible_fraction_review.py tests/b1/test_quantity_review.py
git commit -m "feat: generate nutrition occurrence review queues"
```

### Task 4: 落地 7 项已批准密度规则并验证 7/1/7 边界

**Files:**
- Modify: `data/review/ingredient_measure_rules.csv`
- Create: `tests/b1/test_approved_measure_rule_data.py`

**Interfaces:**
- Consumes: Task 2 的密度路径不依赖 usage。
- Produces: 7 个 `schema_version=2.0.0`, `rule_type=density`, `normalized_form=""`, `normalized_unit="毫升"`, `review_status=approved` 的正式规则。
- Leaves absent: 水淀粉 id 100 以及 7 个 owner-pending 键。

- [ ] **Step 1: 写正式数据行为失败测试**

`tests/b1/test_approved_measure_rule_data.py` 从真实正式 CSV 加载规则，使用公开索引接口断言：

```python
EXPECTED_DENSITIES = {
    111: ("纯净水", Decimal("1.000")),
    72: ("醋", Decimal("1.000")),
    122: ("牛奶", Decimal("1.020")),
    154: ("柠檬汁", Decimal("1.020")),
    155: ("色拉油", Decimal("0.920")),
    20: ("橄榄油", Decimal("0.913")),
    996: ("烧烤汁", Decimal("1.130")),
}

def test_only_owner_approved_stable_density_rules_are_effective() -> None:
    rules = load_measure_rules(REPO_ROOT / "data/review/ingredient_measure_rules.csv")
    for ingredient_id, (_name, expected) in EXPECTED_DENSITIES.items():
        assert rules.get_density(ingredient_id, "").mass_density_g_per_ml == expected
    for ingredient_id in (100, 166, 583, 607, 125, 42, 474, 567):
        assert rules.get_density(ingredient_id, "") is None
```

对 unit-weight pending IDs 125/42/474/567 使用 `get_unit_weight()` 分别断言对应“勺/个/根/个”为空；另断言 id 72 的规则不会命中白醋 432、陈醋 173 或香醋 607。

- [ ] **Step 2: 运行测试并确认正式文件尚无规则**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/b1/test_approved_measure_rule_data.py -q
```

Expected: FAIL，第一项批准 density 查找返回 `None`。

- [ ] **Step 3: 写入精确的 7 行正式密度规则**

使用稳定唯一 rule ID：

```text
density-111-default
density-72-default
density-122-default
density-154-default
density-155-default
density-20-default
density-996-default
```

每行 `usage_code`、`fuzzy_token_class` 和 `to_grams` 必须为空；不得写 id 100 水淀粉的 rejected 行，不得写黄酒、辣椒油、香醋、鸡精、蛋清、香蕉或皮蛋的候选值。

- [ ] **Step 4: 运行正式数据测试、数量回归和 Ruff**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/b1/test_approved_measure_rule_data.py tests/b1/test_quantity_normalizer.py tests/b1/test_quantity_rule_review.py -q
.venv\Scripts\python.exe -m ruff check tests/b1/test_approved_measure_rule_data.py
```

Expected: PASS；`git diff --check` 无错误。

- [ ] **Step 5: 只提交 Task 4 文件**

```powershell
git add -- data/review/ingredient_measure_rules.csv tests/b1/test_approved_measure_rule_data.py
git commit -m "data: approve reviewed ingredient densities"
```

## Post-Task Verification

四个任务全部通过独立审查后，由主代理执行一次：

```powershell
.venv\Scripts\python.exe -m pytest tests/b1/test_nutrition_occurrence_rules.py tests/b1/test_quantity_normalizer.py tests/b1/test_raw_nutrition_calculator.py tests/b1/test_edible_fraction_review.py tests/b1/test_nutrition_occurrence_review.py tests/b1/test_data_review.py tests/b1/test_fixed_recipe_source.py tests/b1/test_approved_measure_rule_data.py -q
.venv\Scripts\python.exe -m ruff check src/food_agent_v2 tests/b1 tests/test_b1_pipeline.py
.venv\Scripts\food-agent-v2.exe data-review --kind nutrition-occurrences --output reports/data_review/nutrition_occurrences
.venv\Scripts\food-agent-v2.exe data-review --kind edible-fractions --output reports/data_review/edible_fraction_candidates.csv
```

验收输出必须满足：

- 四份 occurrence 报告存在且所有行 `review_status=pending`；
- 正式 usage/retention/edible/crosswalk 文件在候选生成前后 SHA-256 不变；
- 可食比例报告不再把普通 usage 未决作为统一异常；
- 正式数量索引恰好包含批准的 7 项 density，水淀粉和 7 个 owner-pending 键均无有效规则；
- 9,618 条营养参考文件的 SHA-256 不变；
- H05 三个运行容器及数据库中的 1,932 条现有 nutrition row 不变；
- 不在 dirty worktree 上运行正式 `data-rebuild`；只生成离线审核报告。

本计划完成后，按既有 `2026-08-23-nutrition-review-rules-implementation.md` 的 Task 6 继续审核并晋升留存/可食比例决定，再进入 `2026-08-23-nutrition-coverage-implementation.md` 完成 crosswalk 和 1932/1932 的最终机械营养汇总。
