# V2 营养评分模块设计

- 状态：`APPROVED`
- 日期：2026-08-09
- 适用项目：`program_v2`
- 上游依据：[系统总体设计](../00-system-overview.md)、[全局不变量](../contracts/global-invariants.md)、[模块边界](../contracts/module-boundaries.md)、[数据工程模块](01-data-engineering.md)、[菜品与食材处理模块](03-recipe-ingredient-processing.md)、[健康规则与审查引擎](04-health-rule-engine.md)

## 0. 2026-08-09 批准的实现基线

- 当前全量低置信度、零参考覆盖的营养产物视为不可用，必须从 B3 标准食材和批准的每 100g 参考字段重新构建，不能沿用现状。
- 匹配必须保留参考记录 ID、字段映射、单位、方法和置信度；名称猜测、模型补值和无证据默认值不得进入可信营养特征。
- 无可靠参考时返回显式 `unavailable`，不得用 `0.5`、零值或均值充当中性分数；C2 禁用该维度并对剩余权重重新归一化。
- B6 只对 B4 PASS 菜品输出内部软评分和分解；任何营养值、置信度或“健康效果”不得进入正常回答和前端。
- 不引入家庭计量、份量、采购量和烹饪损耗，保持 V2 已批准非目标。

## 1. 模块目的

B6 消费 B1 离线生成的 `NutritionProfile`（包括每道菜品的营养特征、匹配方法和置信度），并在此基础上为在线请求提供营养软评分。离线营养匹配由 B1 执行（见 B1 §9.5），B6 定义 `NutritionProfile` Schema 和运行时评分语义。B6 回答"在已通过健康审查的菜品之间，哪些更符合参与者的营养目标"，不回答"某道菜是否安全"或"某食材是否适合某用户"。

B6 的评分结果不进入健康硬筛选、不进入 RAG 召回、不进入最终回答正文、不进入前端展示。它只在 C2 菜单规划阶段作为软目标之一，帮助从多个安全方案中选择更符合营养偏好的那一个。

## 2. 已确认前提

- 只处理 B4 输出中 `PASS` 的安全候选菜品；任何时候都不能对未通过健康审查的菜品执行营养评分。
- B3 营养输入视图只包含 `consumption_role=edible` 的食材和原始数量信息；B6 不重新解析食材。
- 营养参考数据来自项目内固定食物成分参考文件；不在默认流水线中联网更新。
- 当前系统不计算烹饪损耗、不就餐人数份量、不计算个人摄入量、不生成采购量。
- 低置信度营养估算只能影响软评分权重，不能形成健康硬约束、健康标签或排除证据。
- 无论置信度高低，营养值都不能进入正常回答文本和前端展示。
- 营养目标（控糖、控钠、补钙等）来自 B2 的 `HealthGoal`，默认效果为 `soft_prefer`；只有独立存在的疾病、异常指标、过敏或明确禁忌的审核硬关系可以形成健康硬排除。
- 营养目标之间可能存在冲突（如同时追求低钠和高能量），冲突由 C2 菜单规划在多目标软优化中处理，B6 只负责输出标准化的单维度评分分解。

## 3. 职责

B6 负责：

1. 从 B3 `RecipeNutritionInputView` 读取每道安全候选菜品的可食食材及原始数量信息；
2. 加载项目内固定营养参考数据，建立标准食材到营养值的匹配索引；
3. 按精确匹配、审核别名匹配、可解释同类估算的优先级，为每条食材匹配建立来源、方法和置信度；
4. 无合理参考时标记为未知，不编造营养值；
5. 对菜品内部的多条食材营养记录，按原始数量和数量状态聚合成菜品级营养特征；
6. 为每道菜品生成 `NutritionProfile`，包含各营养维度的估算值、置信度和数量覆盖比例；
7. 针对当前请求中的参与者营养目标，为安全候选菜品生成标准化的 `NutritionScoreDecomposition`（分维度评分和加权总分）；
8. 根据置信度和数量覆盖比例限制低置信度评分在总分中的权重；
9. 向 C2 菜单规划提供安全候选菜品的营养软分和分解；
10. 对 B6 产物执行离线质量门禁和跨模块一致性检查；
11. 确保 B6 输出 Schema 不包含 `per_serving`、个人摄入、份量、采购量字段。

## 4. 非职责

B6 不负责：

- 判定某道菜或某个食材是否健康安全；
- 生成、修改或激活食材健康硬关系；
- 让营养值参与健康硬筛选、RAG 召回排序或用户可见回答；
- 计算烹饪营养损耗、就餐人数份量、个人摄入量或采购量；
- 生成 `per_serving` 字段、菜品份量或参与者营养汇总；
- 将营养软分直接等同于推荐结论或健康声明；
- 代替用户做出营养取舍决策（冲突目标的权衡由 C2 在多目标优化中处理）；
- 重新解析原始食材文本、修改 B3 食材身份或数量；
- 向模型暴露完整的内部营养值（模型只获取标准化评分分解）；
- 直接写 WorkflowState、菜单表或最终请求状态。

## 5. 上游输入

### 5.1 离线构建输入

| 输入 | 来源 | 内容 |
|---|---|---|
| `NutritionProfile` 初始化数据 | B1 离线构建产物（经精确匹配、别名匹配、可解释估算和置信度标注） | 每道菜品的营养特征（营养维度估算值、置信度、匹配方法、数量覆盖比例和来源引用） |
| B3 营养输入视图 | B3 `RecipeNutritionInputView` | 每条可推荐菜品的可食食材 `ingredient_id`、原始数量/范围/单位、数量状态 |
| 营养匹配配置 | B1 构建配置 | 审核过的同类估算映射表、置信度分级规则、营养维度权重配置 |

### 5.2 在线查询输入

- B4 安全候选 `recipe_id` 列表；
- 当前请求的参与者营养目标（来自 B2 `HealthGoal`，`effect=soft_prefer`）；
- 参与者口味偏好（来自 B2 `TastePreference`，供 C2 综合排序，B6 不直接消费）。

B6 不接收原始健康档案、RAG 检索分数、原始菜谱文本或用户自由输入。

## 6. 下游输出

### 6.1 菜品营养特征

```
NutritionProfile
├── recipe_id
├── nutrient_values[]
│   ├── nutrient_code (energy_kcal, protein_g, fat_g, carbs_g, sodium_mg, ...)
│   ├── estimated_value | null
│   ├── value_range [min, max] | null
│   ├── confidence: high | medium | low
│   ├── match_method: exact_ref | alias_ref | estimated_from_similar | unknown
│   ├── quantity_coverage_ratio (0-1，该营养维度的食材数量覆盖比例)
│   └── source_refs[]
├── overall_confidence: high | medium | low
├── missing_nutrients[]
└── quantity_coverage_summary
```

### 6.2 营养评分分解

```
NutritionScoreDecomposition
├── recipe_id
├── goal_scores[]
│   ├── goal_code (与 B2 HealthGoal.normalized_code 对应)
│   ├── score (标准化分值，方向统一为越高越好)
│   ├── contributing_nutrients[]
│   ├── confidence_penalty_applied (低置信度导致的权重扣减)
│   └── coverage_penalty_applied (数量覆盖不足导致的权重扣减)
├── weighted_total (加权总分，已应用置信度和覆盖扣减)
├── score_confidence: high | medium | low
└── disclaimers[] (解释评分受限原因，如"部分食材营养数据缺失")
```

### 6.3 消费者视图

- **C2 菜单规划视图**：安全候选菜品的 `NutritionScoreDecomposition` 列表，按营养目标维度拆分；
- **C3 菜单决策视图**：所选方案的营养评分依据摘要（不含具体营养数值）；
- **公开视图**：仅供 C2 内部使用的软评分，不进入回答模型、SSE 或前端。

## 7. 数据模型与数据所有权

### 7.1 营养参考条目

```
NutrientReference
├── reference_id
├── ingredient_id | null
├── alias_id | null (通过审核别名匹配时记录)
├── nutrient_code
├── value_per_unit
├── unit
├── source
├── is_primary (精确匹配为 true，估算为 false)
```

营养参考以 `ingredient_id + nutrient_code` 为主键。同一食材可能有多个来源的参考值；精确匹配优先于估算来源。

### 7.2 同类估算映射

```
EstimationMapping
├── mapping_id
├── target_ingredient_id (缺少官方参考的食材)
├── source_ingredient_id (用于估算的相似食材)
├── rationale (可解释的相似性依据，如"同属叶菜类，水分和纤维结构相似")
├── review_status: approved | rejected
└── applicable_nutrients[] (限定哪些营养维度可以借用，不默认全部维度)
```

估算映射是项目内固定、可审查的构建配置。未审核的映射不能进入正式营养特征。映射不声明精确等价，B6 在匹配时必须标注 `match_method=estimated_from_similar` 并降低置信度。

### 7.3 营养维度注册表

```
NutrientDimension
├── nutrient_code
├── display_name (内部使用)
├── unit
├── direction_for_scoring: lower_is_better | higher_is_better
├── goal_mappings[] (关联的 B2 HealthGoal.normalized_code)
```

营养维度是固定的。哪些维度参与评分由当前参与者的营养目标决定；没有对应目标的维度不生成评分，原始估算值仅保留在 `NutritionProfile` 中供审计。

### 7.4 置信度分级规则

| 置信度 | 条件 | 对评分的影响 |
|---|---|---|
| `high` | 所有关键营养维度的主食材均有精确参考匹配，且数量覆盖比例 ≥ 阈值 | 全额权重 |
| `medium` | 存在别名匹配或可解释估算，或少量食材数量未知 | 权重乘以折扣因子 |
| `low` | 多数食材缺少参考、数量大面积未知，或关键维度依赖单一估算来源 | 权重显著降低；评分总分标记为低置信度 |

置信度由食材匹配方法分布、数量覆盖比例和营养维度重要性共同确定。

### 7.5 数据所有权

B6 拥有：
- `NutrientProfile` 和 `NutritionScoreDecomposition` 的 Schema；
- 营养维度注册表和评分方向；
- 置信度分级和权重折扣规则；
- 同类估算映射的语义定义。

B1 负责离线编排营养匹配、生成 `NutrientReference` 初始化数据并通过质量门禁；B6 定义 Schema 和运行时只读语义。B3 拥有食材身份和数量的权威，B6 通过公开端口引用，不自行修改。

## 8. 核心处理流程

### 8.1 离线构建

离线阶段的营养匹配由 B1 执行（见 B1 §9.5）。B6 的 `NutritionProfileService` 在运行时通过 Repository 读取 B1 生成并验证的 `NutritionProfile` 数据。

```
B3 可推荐菜品的可食食材 + 原始数量
→ 加载固定营养参考数据
→ 精确 ingredient_id 匹配
→ 未命中的食材尝试审核别名匹配
→ 仍未命中的食材查询 EstimationMapping
→ 均有匹配 → 标记为已知；部分缺失 → 标记为未知维度
→ 按原始数量和数量状态聚合成菜品级 NutritionProfile
→ 校验数量覆盖比例和跨模块引用
→ 生成 NutritionProfile 初始化数据
→ 执行质量门禁
```

### 8.2 在线评分

```
输入：B4 safe_recipe_ids + 当前参与者营养目标
→ 加载安全菜品的 NutritionProfile
→ 筛选与营养目标相关的营养维度
→ 按评分方向（越低越好/越高越好）将营养值转为标准化分
→ 应用置信度折扣和覆盖折扣
→ 计算分维度分数和加权总分
→ 生成 NutritionScoreDecomposition 列表
→ 输出给 C2 菜单规划
```

评分只在安全候选之间产生相对差异，不具备绝对健康含义。不同营养目标之间的冲突不在此阶段解决——C2 在多目标软优化时负责权衡。

## 9. 算法与确定性规则

### 9.1 食材营养匹配

```
for each occurrence in RecipeNutritionInputView:
    if occurrence.ingredient_id in NutrientReference (is_primary=true):
        match = exact_ref, confidence = high
    elif occurrence.ingredient_id has approved alias in NutrientReference:
        match = alias_ref, confidence = medium
    elif occurrence.ingredient_id in EstimationMapping (review_status=approved):
        match = estimated_from_similar, confidence = medium
        (仅借用 applicable_nutrients 中声明的维度)
    else:
        match = unknown
        (该食材在对应营养维度上贡献为空)
```

### 9.2 数量加权聚合

原始数量信息的处理：

```
if occurrence.amount_status = exact:
    使用 amount_min 作为点估计值
elif occurrence.amount_status = range:
    使用 (amount_min + amount_max) / 2 作为点估计值
elif occurrence.amount_status = qualitative:
    使用该菜品中同类食材的中位数量作为参考，标记为低置信度
elif occurrence.amount_status = missing:
    跳过数量加权，该食材的所有营养维度标记 quantity_coverage_ratio 下降
```

数量仅用于同一菜品内各食材营养贡献的相对加权，不计算出品率、不换算人数份量。

### 9.3 标准化评分

对每个营养目标关联的营养维度：

```
if direction = lower_is_better:
    score = 1 - (value - min_in_candidates) / (max_in_candidates - min_in_candidates)
    (最低值 → 最高分)
elif direction = higher_is_better:
    score = (value - min_in_candidates) / (max_in_candidates - min_in_candidates)
    (最高值 → 最高分)
```

如果该维度有完整可信观测且所有候选观测值确实相同，该维度所有菜品得分为 0.5（中性）；数据缺失或映射失败时必须返回 `unavailable`，不得走本规则。

### 9.4 置信度折扣

```
final_dimension_score = base_score × confidence_discount × coverage_discount

confidence_discount:
    high   → 1.0
    medium → 0.7
    low    → 0.3

coverage_discount:
    ratio ≥ 0.8 → 1.0
    ratio ≥ 0.5 → 0.7
    ratio < 0.5 → 0.3
```

加权总分：

```
weighted_total = Σ (goal_weight × mean(dimension_scores_for_goal))

goal_weight 由 C2 根据用户明确程度和 B2 HealthGoal 优先级传入；
B6 默认使用均权。
```

### 9.5 禁止的输入与输出

B6 必须拒绝：
- 任何 `per_serving` 字段；
- 烹饪损耗系数；
- 个人摄入量计算；
- 人数份量换算；
- 采购量估算；
- B4 健康约束代码（B6 不能将其作为评分输入）；
- RAG 分数或检索排名。

B6 输出 Schema 不得包含：
- `per_serving`、`serving_size`、`portion_count`；
- `personal_intake`、`daily_value_percent`；
- `health_risk_level`、`health_warning`；
- `purchase_quantity`。

## 10. 公开接口与模型工具

### 10.1 领域服务

```
NutritionProfileService
├── get_nutrition_profiles(recipe_ids) → NutritionProfile[]
└── get_audit_profiles(recipe_ids) → NutritionProfile[] (完整内部值，仅审计用)

NutritionScoringService
├── score_candidates(safe_recipe_ids, health_goals) → NutritionScoreDecomposition[]
└── get_score_summary(recipe_ids, health_goals) → ScoreSummary
```

### 10.2 Agent 工具

B6 不直接向任何模型暴露工具。营养软评分通过 C2 菜单规划工具内部消费，模型只看到以下间接效果：

- 在多个可行菜单方案之间，C2 可以引用营养目标满足程度作为选择依据之一；
- C2 的用户可见分析摘要可以表述"方案A更符合控钠目标"，但不暴露具体钠含量数值。

回答模型和前端不接收 `NutritionScoreDecomposition` 或任何含数值的营养数据。

## 11. 依赖方向

允许依赖：

```
B1 离线构建器 → B6 固定 NutritionProfile Schema 与质量校验
B6 NutritionProfileService → B3 RecipeNutritionInputView (只读)
B6 NutritionScoringService → B2 HealthGoal 列表 (只读，仅 soft_prefer 目标)
C2 菜单规划服务 → B6 NutritionScoringService.score_candidates()
审计工具 → B6 NutritionProfileService.get_audit_profiles()
```

禁止依赖：

```
B6 → 原始用户健康档案或 B2 健康硬约束
B6 → B4 健康关系或覆盖数据
B6 → Qdrant 或 RAG 内部实现
B6 → API Schema 或前端类型
B6 → 模型供应商客户端
Agent 模型 → B6 内部营养数值
C3 菜单决策 → B6 内部营养数值（仅通过 C2 的软评分摘要间接获取）
回答模型 → 任何 B6 输出
```

## 12. 异常、错误码与停止条件

| 错误码 | 触发条件 | 处理 |
|---|---|---|
| `NUTRITION_REFERENCE_INVALID` | 固定营养参考数据缺失、结构损坏或单位不兼容 | 离线构建停止 |
| `NUTRITION_ESTIMATION_UNAPPROVED` | 未审核的估算映射试图进入正式 NutritionProfile | 离线构建停止 |
| `NUTRITION_MATCH_INVALID` | 匹配来源或方法不可解释 | 丢弃该匹配并降为未知；结构性问题时停止 |
| `NUTRITION_QUANTITY_COVERAGE_LOW` | 菜品可食食材中大量缺少数量信息 | 标记为低置信度，不停止 |
| `NUTRITION_CONFIDENCE_SCOPE_VIOLATION` | 低置信度营养值试图以全额权重参与评分 | `failed`（Schema 或代码缺陷） |
| `NUTRITION_HEALTH_BOUNDARY_VIOLATION` | 营养值或标签错误进入 B4 健康硬筛选路径 | `failed` |
| `RECIPE_NOT_FOUND` | 在线查询引用不存在的 `recipe_id` | `failed` |
| `NUTRITION_PROFILE_UNAVAILABLE` | 安全候选菜品缺少 NutritionProfile（离线构建遗漏） | `failed` |

离线构建失败不产生部分产物。在线评分失败不自动重试，不降级为随机排序或全均分，不调用模型补写营养值。

## 13. 构建与初始化要求

- 固定营养参考和估算映射保持只读，B6 派生产物写入 V2 独立生成目录；
- B3 可推荐菜品全部通过质量门禁后，B6 才能开始构建；
- 每条营养匹配保留来源食材、匹配方法和置信度；
- 数量覆盖比例低于全局阈值时触发构建警告，低于单菜品最低阈值时标记该菜品为 `overall_confidence=low`；
- MySQL 固定表中营养参考、估算映射和 NutritionProfile 由 B1 通过 B6 Schema 初始化；
- 开发期间营养参考或估算映射变化时执行全量重建；
- 正式运行期间不更新固定营养参考数据。

## 14. 测试和验收标准

### 14.1 营养匹配

- 精确 `ingredient_id` 匹配到官方参考值的食材产出 `confidence=high`；
- 审核别名匹配到参考值的食材产出 `confidence=medium`，`match_method=alias_ref`；
- 可解释同类估算产出 `confidence=medium`，`match_method=estimated_from_similar`；
- 无参考且无估算映射的食材在对应营养维度上标记为 `unknown`；
- 未审核估算映射不能进入正式 NutritionProfile。

### 14.2 置信度与折扣

- `high` 置信度菜品的关键维度获得全额权重；
- `medium` 置信度产生可见的权重折扣；
- `low` 置信度菜品的总分被显著降低；
- 数量覆盖比例不足触发对应维度的覆盖折扣。

### 14.3 隔离边界

- 营养值不进入 B4 健康硬筛选输入；
- `per_serving`、`serving_size`、`portion_count` 不存在于 B6 输出 Schema；
- `personal_intake`、`daily_value_percent` 不存在于任何 B6 产物；
- 烹饪损耗系数不出现在 NutritionalProfile 或评分算法中；
- `NutritionScoreDecomposition` 不进入回答模型、SSE 或前端响应。

### 14.4 评分一致性

- 相同菜品、相同营养目标产生相同评分；
- 候选菜品集不变时评分排序稳定；
- 分维度分数在 [0, 1] 范围内；
- 仅当全部候选都有完整可信观测且数值相同时得分为 0.5；缺失数据必须为 `unavailable`。

### 14.5 跨模块契约

- B3 食材数量和数量状态 → B6 数量加权和覆盖比例，传递不丢失；
- B2 `HealthGoal` 与 B6 营养维度之间存在完整映射，不存在悬空目标；
- C2 只通过 `NutritionScoringService.score_candidates()` 获取评分，不直接读取 `NutritionProfile`；
- 回答 Schema 和前端 Schema 不包含 `NutritionScoreDecomposition` 或任何内部营养值。

## 15. 旧实现与目标实现差异

| 维度 | 旧实现 | V2 目标 |
|---|---|---|
| 营养数据来源 | 混合官方参考和联网抓取 | 仅固定营养参考文件，不联网 |
| 烹饪损耗 | 有损耗计算链路 | 不计算烹饪损耗 |
| `per_serving` | 存在相关字段和计算 | 完全移除 |
| 营养用途 | 部分阈值参与健康判断 | 仅内部软排序，不触碰健康硬筛选 |
| 用户可见性 | 可能在回答中展示 | 不进入回答和前端 |
| 评分方式 | 未系统化为标准化评分分解 | 分维度标准化评分 + 置信度折扣 |
| 数量处理 | 可能存在份量换算 | 仅使用原始数量进行食材间相对加权 |
| 估算透明度 | 估算来源和方法可能不明确 | 每条匹配记录来源、方法和置信度 |

## 16. 迁移分类

| 旧实现 | 分类 | 处理结论 |
|---|---|---|
| `data_pipeline/fetch_food_composition.py` | `REMOVE` | 营养参考固定，不再联网获取 |
| `data_pipeline/build_nutrition.py` | `REFACTOR` | 保留官方参考索引和误匹配防护；删除烹饪损耗和 `per_serving` 链路；增加估算映射审核和置信度分级 |
| 营养相关 `health_rules.py` 阈值 | `REMOVE` | 营养阈值不再参与健康硬筛选 |
| 营养字段在 `catalog.py` 中的推断 | `REMOVE` | 营养推断从菜品目录模块移除，由 B6 独立处理 |
| 回答和前端中的营养展示 | `REMOVE` | 按 DEC-C007，正常回答和前端不再展示营养数值 |
| 旧营养相关测试 | `REFACTOR` | 保留匹配正确性和边界测试；删除 `per_serving`、损耗、健康阈值测试；增加隔离边界和置信度折扣测试 |

## 17. 审查后清理项

以下内容在 B6、C2、C3、回答模块和前端全部迁移并通过验收后清理：

- 烹饪营养损耗计算逻辑和对应系数表；
- `per_serving`、`serving_size`、`portion_count`、`daily_value_percent` 字段及计算链路；
- 营养阈值参与健康判断的代码路径；
- `fetch_food_composition.py` 及其在默认流水线中的调用；
- 回答提示词和前端组件中的营养数值展示逻辑；
- 已由新 B6 Schema 替代且确认无消费者的旧营养表字段和兼容适配器。

## 18. 与全局流程和后续模块的关系

```
B3 可食食材 + 原始数量
→ B6 NutritionProfile (离线)
→ B4 安全候选
→ B6 NutritionScoringService.score_candidates() (在线)
→ C2 菜单规划将营养软分作为多目标之一
→ C3 菜单决策获取评分摘要（不含数值）
```

- B6 只能处理已通过 B4 健康审查的安全菜品；
- C2 消费 B6 评分时不能将其等同于健康结论或唯一排序依据；
- C3 只能获取评分摘要和置信度说明，不能获取完整 NutritionProfile；
- 回答模型、SSE 和前端不接收任何 B6 产物；
- D3 测试与验收必须覆盖离线质量门禁、评分一致性和隔离边界。

## 19. 待考量事项

以下问题不影响当前设计通过，但在进入实现阶段前需要进一步确认。在此留痕供后续回顾。

### 19.1 EstimationMapping 的数据建设工作量

`EstimationMapping` 是一份全新的数据资产——为缺少官方营养参考的食材建立到相似食材的映射，每条需要可解释的 `rationale` 且通过审核。旧项目中没有现成的"相似食材营养映射表"可以直接复用。

**建议**：实现阶段优先覆盖高频缺失食材（按出现频次 × 营养目标关联度排序），不追求全覆盖。映射覆盖率在质量报告中单独列出，不纳入强制门禁。

### 19.2 置信度折扣的乘法叠加效应

当前折扣因子为 0.7（medium）和 0.3（low），且 `confidence_discount × coverage_discount` 乘法叠加。`medium × medium` 已打到 0.49，如果实际数据中 medium 占比偏高，大部分菜品的有效评分差异可能被压缩到很小的范围，导致 C2 端可用的区分信号弱化。

**建议**：实现阶段用实际数据跑一次评分分布。如果有效分差确实过小，可考虑：(a) 将折扣因子调至 0.8/0.5；(b) 或用加法惩罚替代乘法叠加。折扣因子作为可配置参数保留，不硬编码。

### 19.3 未知营养维度菜品的 C2 兜底策略

当前设计规定无参考食材在对应维度上贡献为空。这不会导致菜品被排除（正确），但会导致某些菜品在特定营养维度上缺失评分。C2 在比较菜品时如何处理缺失维度，属于 C2 的决策范围，B6 不做规定。

**建议**：C2 设计时明确兜底策略——例如按该维度候选集均值补入并标记为推测，或跳过该维度仅基于已有维度比较。B6 确保 `NutritionScoreDecomposition` 中明确标记哪些维度来自实际数据、哪些为缺失，避免 C2 将缺失误读为零值。
