# V2 菜单规划模块设计

- 状态：`APPROVED`
- 日期：2026-08-09
- 适用项目：`program_v2`
- 上游依据：[系统总体设计](../00-system-overview.md)、[全局不变量](../contracts/global-invariants.md)、[模块边界](../contracts/module-boundaries.md)、[推荐请求生命周期](../scenarios/recommendation-lifecycle.md)、[健康规则与审查引擎](04-health-rule-engine.md)、[时间与步骤规划](05-time-and-steps.md)、[营养评分](06-nutrition-scoring.md)、[混合RAG检索](07-rag-retrieval.md)

## 0. 2026-08-09 批准的实现基线

- C2 只能接受带有效 B4 回执的安全候选；不得在 B4 不可用、覆盖不全或工具失败时生成所谓安全 fallback 菜单。
- 未明确菜数时默认 5 道；用户明确菜数时按精确值执行。安全候选不足则返回 `NO_FEASIBLE_MENU`，不得悄悄减少菜数或放宽健康约束。
- 规划器只能选择既有 `recipe_id`，不得删去可选食材、选择替代项、改写步骤或创造“更健康变体”来规避食材闭包。
- 严格时间只接受 B5 的 `strict_time_feasible=true`；`unknown` 不能入硬时限可行菜单。软证据 `unavailable` 时禁用该维度并对其余权重重归一化。
- `plan_id`、菜单成员排序、评分分解、约束快照和散列必须确定性生成；相同输入产生相同候选方案和证据。
- 生产不设跨模块 fallback；模型只能请求确定性规划器重算，不能自行拼装方案。

## 1. 模块目的

C2 在 B4 输出的安全候选菜品集合内，综合考虑硬约束（菜单结构、严格时间、锁定菜品、仅现有食材、去重）和软目标（RAG 相关性、口味偏好、营养目标、软时间、多样性、历史反馈），生成 3 至 5 个具有实际差异的可行菜单方案（`plan_id`）。

C2 只回答"有哪些安全且可行的菜单方案，各自如何权衡"，不回答"应该选哪个"。最终选择由 C3 菜单决策模型完成。C2 不能把未通过 B4 健康审查的菜品加入任何方案，也不能重新判断健康关系。

## 2. 已确认前提

- 所有进入菜单规划的菜品必须来自 B4 输出的 `safe_recipe_ids`（全员 `PASS`）；任何时候都不能使用未审查或审查失败的菜品。
- 多人场景中每道菜必须对全部参与者均为 `PASS`；不生成参与者专属菜品，不放松任一参与者的健康硬约束。
- C2 不重新判断健康关系、食材身份或约束覆盖——这些是 B4 和 B3 的职责，C2 只消费它们的结论。
- 菜单修改、替换和恢复均重新执行 B4 健康校验，不能只改方案不重审。
- 菜品库存模式中，默认可用基础调料为水、盐、食用油；其余食材均需用户声明已持有。
- 营养软分只参与软排序，不形成硬约束、健康标签或排除证据。
- 严格时间约束只使用 B5 输出的三值 `strict_time_feasible`；硬时限候选只有 `true` 可接受，`false/unknown` 均不能组成满足时限的方案。
- C2 的优化器是确定性算法，不依赖模型判断"哪道菜更好"。模型（健康与菜单规划模型）负责理解用户需求、审查方案差异、判断是否需要调整约束，并将调整后的需求交给确定性优化器重新计算。
- C2 不保存完整的模型隐藏思维过程；用户可见分析摘要通过 Artifact 的 `user_visible_analysis` 字段传递。

## 3. 职责

C2 负责：

1. 接收 B4 输出的 `safe_recipe_ids` 及对应的健康证据引用；
2. 从 B5、B6、C1 加载每道安全菜品的软评分数据（RAG 分数、营养分数、时间范围、口味和多样性标签）；
3. 解析当前请求的菜单硬约束：菜数、汤数、主食数、饮品/甜点数、锁定菜品、拒绝菜品、严格时间限制、仅现有食材模式；
4. 构建并求解多目标菜单优化问题——硬约束必须满足，软目标加权评分；
5. 生成 3 至 5 个具有实际差异的 `plan_id`，每个方案包含完整的菜品集合、结构化差异事实和评分分解；
6. 为每个方案生成 `FeasibleMenu`，包含方案哈希、评分分解、结构化差异事实（`differing_recipe_ids[]`、`dominant_objective`）；
7. 将多个方案打包为 `FeasibleMenuArtifact` 供 C3 菜单决策模型选择；`user_visible_analysis` 自然语言摘要由健康与菜单规划模型在审查方案后填入；
8. 对无法满足硬约束的情况形成 `no_feasible_menu` 终态所需的完整证据；
9. 对锁定菜品强制入选、拒绝菜品强制排除，两者冲突时优先拒绝（即拒绝覆盖锁定）；
10. 支持菜单内菜品的替换、恢复操作（由健康与菜单规划模型发起，C2 生成新方案，随后触发 B4 重新校验）。

## 4. 非职责

C2 不负责：

- 重新判定任何菜品的健康安全（属于 B4）；
- 修改健康约束、食材身份或菜品食材集合；
- 从安全候选集之外选取菜品；
- 将不同方案的菜品重新拼装成未经完整审查的新方案（属于 C3 决策范围，且受 INV-005 约束）；
- 选择最终推荐的菜单方案（属于 C3 菜单决策模型）；
- 执行最终健康校验（属于 B4，由 C3 调用）；
- 生成最终回答文本或自然语言方案差异说明（模型在 C3 和回答阶段完成，C2 只提供结构化方案数据和差异事实）；
- 直接写 WorkflowState、菜单决策 Artifact 或最终请求状态；
- 向模型暴露优化器内部权重和完整评分矩阵（模型获取方案摘要和差异说明，不获取逐菜品评分细节）。

## 5. 上游输入

### 5.1 B4 安全候选

```
HealthEvaluationReceipt
├── safe_recipe_ids[]
├── excluded_recipe_ids[]
├── participant_recipe_results[] (逐参与者、逐菜品健康结果)
├── constraint_set_refs[]
├── input_fingerprint
└── evidence_refs[]
```

C2 只读取 `safe_recipe_ids[]` 和健康证据引用。排除菜品的证据仅用于生成 `user_visible_analysis` 中的冲突说明，不作为菜单输入。

### 5.2 软评分数据

| 数据 | 来源 | 内容 |
|---|---|---|
| RAG 相关性分数 | C1 `RetrievalResult` | 每道候选的 `rerank_score` 和 `source_paths[]` |
| 营养评分分解 | B6 `NutritionScoreDecomposition` | 每道安全菜品的分维度营养分和加权总分 |
| 时间范围 | B5 `RecipeTimeProfile` | 每道安全菜品的 `total_elapsed_range`、`overall_confidence` |
| 多样性标签 | B3 `RecipeRetrievalBuildView` | 菜系、烹饪方式、口味、菜品类型、`ingredient_family`（食材族）；若 B3 RecipeRetrievalBuildView 不直接包含食材族，C2 通过 `RecipeCatalogService` 查询 |
| goal_weight（来自 B6） | B2 `HealthGoal` 优先级 | 用于营养评分加权，默认均权 |

### 5.3 菜单硬约束

由查询理解模型从用户需求中提取，经工作流校验后注入 C2：

```
MenuHardConstraints
├── dish_count: integer | null (null 表示尝试默认值 5)
├── soup_count: integer (默认 0，最大 1)
├── staple_count: integer (默认 1，最大 1)
├── drink_or_dessert_count: integer (默认 0，最大 1；仅用户明确提出时 >0)
├── locked_recipe_ids[] (用户明确要求保留的菜品)
├── rejected_recipe_ids[] (用户明确要求排除的菜品)
├── strict_time_limit_seconds | null
├── strict_time_feasibility_required: boolean
├── strict_ingredients_mode: boolean (是否仅使用现有食材)
├── available_ingredient_ids[] (strict_ingredients_mode=true 时，用户声明的可用食材)
├── cuisine_preference | null
├── cooking_method_preference | null
└── meal_time | null
```

菜数默认规则：
- 用户未明确菜数时目标固定为 5 道；
- 安全候选不足以支撑 5 道时返回 `no_feasible_menu`，不得静默减少菜数；
- 用户明确菜数（如"三菜一汤"）时严格执行，不足同样返回 `no_feasible_menu`。

汤品、主食默认各不超过 1 道。饮品/甜点只在用户明确提出时加入，合计最多 1 道，且不替代蔬菜菜与蛋白主体。明确的"几菜一汤"按用户声明的数量执行。

### 5.4 口味偏好与历史反馈

| 数据 | 来源 | 内容 |
|---|---|---|
| 正向口味偏好 | B2 `TastePreference` | 每位参与者的口味标签（喜辣、喜清淡等） |
| 历史反馈 | C4 上下文模块 | 近期被拒绝的 `recipe_id`、被点赞的 `recipe_id`（可选，降权而非排除） |

## 6. 下游输出

### 6.1 可行菜单

```
FeasibleMenu
├── plan_id
├── recipe_ids[]
├── menu_hash
├── score_decomposition
│   ├── total_score
│   ├── rag_score
│   ├── preference_score
│   ├── nutrition_score
│   ├── time_score
│   ├── diversity_score
│   └── historical_score
├── differing_recipe_ids[] (与其他方案不同的菜品 ID)
├── dominant_objective (该方案最突出的偏好方向：balanced | preference | nutrition | quick | diverse)
├── strict_time_feasible: true | false | unknown
├── makespan_seconds | null (来自 B5 MenuScheduleResult)
└── user_visible_analysis (由健康与菜单规划模型在审查后填入)
    ├── title
    ├── summary
    └── evidence_refs[]
```

### 6.2 可行菜单 Artifact

```
FeasibleMenuArtifact
├── artifact_id
├── request_id
├── safe_recipe_ids_ref
├── constraint_set_refs[]
├── menus[] (3-5 个 FeasibleMenu)
├── common_to_all[] (所有方案均包含的菜品)
├── exclusive_to_plan[] (每个方案独有的菜品)
├── trade_off_summary (方案间的权衡摘要，由模型填入)
├── hard_constraints_applied
├── soft_objectives_applied
└── input_fingerprint
```

### 6.3 消费者视图

- **C3 菜单决策模型**：完整的 `FeasibleMenuArtifact`（所有方案及评分分解）；
- **健康与菜单规划模型**（回流路径）：方案差异和约束满足情况，供判断是否需要调整；
- **回答模型**：不直接消费 C2 产物（通过 C3 选择的方案间接获取）；
- **B4 最终校验**：所选方案的 `plan_id` 和 `menu_hash`。

## 7. 数据模型与数据所有权

### 7.1 菜单结构规则

```
MenuStructureRule
├── rule_id
├── condition (触发条件，如"用户未明确菜数"或"用户明确要求饮品")
├── dish_count_range [min, max]
├── soup_max: 1
├── staple_max: 1
├── drink_dessert_max: 1
├── requires_explicit_request: boolean (饮品/甜点是否需用户明确提出)
└── adaptive: boolean (安全候选不足时是否允许降至下限)
```

菜单结构规则是固定构建配置，不在运行时由模型修改。

### 7.2 多样性维度

```
DiversityDimension
├── dimension: cuisine | cooking_method | taste | dish_type | ingredient_family
├── weight_in_diversity_score
└── intra_menu_penalty (同一维度重复出现的扣分)
```

多样性维度是固定配置。单道菜的信息来自 B3 和 C1 的标签字段。

### 7.3 软目标权重

```
SoftObjectiveWeights
├── rag_weight: 0.30
├── preference_weight: 0.25
├── nutrition_weight: 0.20
├── time_weight: 0.10
├── diversity_weight: 0.10
└── historical_weight: 0.05
```

默认权重为初始值，可在构建配置中调整。优化器对 3-5 个方案使用不同的权重偏移，以产生有实际差异的方案（见 §9.4）。权重不在请求期间由模型修改。

### 7.4 方案差异要求

任意两个 `plan_id` 之间必须存在可辨识差异：

- 至少 1 道菜品不同；或
- 菜系/口味/烹饪方式的结构性偏好不同（如方案A偏川菜辣味、方案B偏粤菜清淡）。

仅评分不同而菜品集合完全相同的不构成独立方案。

### 7.5 数据所有权

C2 拥有：
- `FeasibleMenu` 和 `FeasibleMenuArtifact` 的 Schema；
- 菜单结构规则、默认 5 道和候选不足时不放宽逻辑；
- 软目标权重配置和差异约束；
- 确定性多目标优化算法实现。

C2 不拥有任何菜品、食材、健康约束或营养数据——这些事实的权威属于 B3、B2、B4、B6。

## 8. 核心处理流程

### 8.1 菜单生成

```
输入：safe_recipe_ids + 软评分数据 + MenuHardConstraints
→ 构建候选菜品池（标注每道菜的软分和标签）
→ 若 strict_time_limit 存在：排除 B5 单菜总时长中高置信度超出限制的菜品
→ 生成差异化的软目标权重配置（3-5 组，见 §9.4）
→ 对每组权重：
    1. 应用硬约束过滤：
       - 锁定菜品强制入选
       - 拒绝菜品强制排除（与锁定冲突时拒绝优先，标记冲突）
       - strict_ingredients_mode：排除需要非可用食材且非基础调料的菜品
    2. 贪心构造初始菜单（按综合软分依次选取，同时检查结构约束）
    3. 局部搜索改进（交换菜品以提升多样性或综合分）
    4. 调用 B5 compute_menu_schedule 验证整套菜单的时间可行性
    5. 若时间不可行，替换最长非锁定菜品并重新调度（最多 3 次替换尝试）
    6. 生成 FeasibleMenu（含 menu_hash、评分分解和 differing_recipe_ids[]）
→ 去重：移除菜品集合完全相同的方案；仅差 1 道且同菜系同类型的合并
→ 若去重后不足 3 个方案 → 调整差异权重 → 重新生成
→ 若仍不足 3 个 → 返回实际数量（最少 1 个可行方案即可，0 个进入 no_feasible_menu）
→ 打包为 FeasibleMenuArtifact
```

### 8.2 无法组成菜单

```
if safe_recipe_ids 非空 AND 无法满足硬约束（菜数不足、严格时间不可满足、仅现有食材不足等）:
    → 形成完整证据（哪些硬约束无法满足、当前可用安全候选数量）
    → 终态: no_feasible_menu
```

`no_feasible_menu` 与 `no_safe_menu` 的区别：
- `no_safe_menu`：B4 审查后没有 `PASS` 的菜品（健康冲突）；
- `no_feasible_menu`：有安全菜品，但无法组成满足结构的菜单（数量、时间、食材限制）。

### 8.3 菜单调整（替换/恢复）

由健康与菜单规划模型或 C3 发起：

```
输入：当前 FeasibleMenuArtifact + 调整指令（替换 recipe_id_X 或恢复 recipe_id_Y）
→ C2 基于同一 safe_recipe_ids 生成包含替换或恢复菜品的新方案
→ 新方案重新分配 plan_id
→ 新方案必须触发 B4 重新校验（通过 C3 的最终校验工具）
→ 旧方案保留审计
```

C2 不自行判断替换是否安全——安全判断始终由 B4 重新执行。

## 9. 算法与确定性规则

### 9.1 硬约束检查顺序

```
1. 候选菜品池 = safe_recipe_ids
2. 移除 rejected_recipe_ids（与 locked 冲突时，拒绝优先，输出冲突警告）
3. 强制包含 locked_recipe_ids（若 locked 菜品不在 safe_recipe_ids 中 → no_feasible_menu）
4. 若 strict_time_limit 存在：排除 B5 单菜 total_elapsed_range 中高置信度超出限制的菜品
   （注意：此步排除单菜明显超时的候选，但不替代 B5 整套菜单级调度验证）
5. 若 strict_ingredients_mode：
   对每道候选：其非基础调料食材 ⊆ available_ingredient_ids
   不满足的菜品排除（此排除属于菜单可行性，不属于健康排除）
6. 菜品池数量 < 最低菜数 → no_feasible_menu
7. 按菜数、汤数、主食数、饮品数构建菜单结构模板
8. 去重：同一菜单内不能出现食材和烹饪方式高度重合的两道菜
```

基础调料集合固定为：水、盐、食用油。此集合是固定构建配置，不随用户输入变化。

### 9.2 贪心构造

```
按当前权重配置计算每道安全菜品的综合软分：
  composite_score =
    rag_normalized × w_rag +
    preference_match × w_pref +
    nutrition_normalized × w_nutr +
    time_normalized × w_time

按 composite_score 降序排列候选
→ 依次选取，每选一道检查结构约束
→ 菜数达到目标后停止
→ 逐项填充汤、主食、饮品（按各自候选子集）
```

汤、主食、饮品的候选子集依赖 B3 的 `dish_type` 分类。需跨模块确认 B3 的菜品类型分类覆盖了汤品 `soup`、主食 `staple`、饮品 `drink`、甜点 `dessert` 四个槽位所需的分类维度。

### 9.3 局部搜索改进

```
对已生成的初始菜单：
  for each non-locked dish in menu:
    尝试用候选池中未被选中的 top-5 菜品替换
    若替换后综合分提升 → 保留替换
    检查多样性约束（同一 cuisine 不超过 2 道等软限制）
→ 迭代直到无改进或达到最大轮次
```

### 9.4 差异化权重配置

默认权重为基准配置。为产生有实际差异的方案，使用以下 5 组权重偏移：

| 方案偏向 | RAG | 偏好 | 营养 | 时间 | 多样性 | 历史 |
|---|---|---|---|---|---|---|
| 均衡 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| 偏好优先 | 0.9 | 1.5 | 0.8 | 0.8 | 0.9 | 0.9 |
| 营养优先 | 0.8 | 0.8 | 1.5 | 0.9 | 0.9 | 0.9 |
| 快捷优先 | 0.9 | 0.9 | 0.8 | 1.5 | 0.8 | 0.8 |
| 多样探索 | 0.8 | 0.9 | 0.9 | 0.8 | 1.5 | 0.9 |

每一组权重产生一个方案。如果去重后不足 3 个，调整偏移幅度后重新生成。差异权重配置是固定构建配置，不在运行时由模型选择或修改。

模型收到的方案摘要中不暴露权重数值。`dominant_objective` 字段提供结构化方向标签（`balanced` / `preference` / `nutrition` / `quick` / `diverse`），健康与菜单规划模型据此填入自然语言差异说明。

### 9.5 去重规则

```
菜品集合完全相同 → 保留先产生的方案
菜品集合相差 1 道且该差异菜属于同一 cuisine + 同一 dish_type → 合并为同一方案（选综合分高的）
```

### 9.6 多样性评分

```
diversity_score = 1 - normalized(Σ intra_dimension_penalty / max_penalty)

每道菜与其他菜在同一维度上重复时累积扣分：
- 相同 cuisine：-0.3
- 相同 cooking_method：-0.2
- 相同 taste_profile：-0.2
- 相同 primary_ingredient_family：-0.3
```

多样性评分是软目标，不硬性排除菜品。在多样探索方案中权重放大。

### 9.7 时间可行性

```
调用 B5 compute_menu_schedule(menu_recipe_ids, strict_time_limit)
→ 若 strict_time_feasible = true → 通过
→ 若 strict_time_feasible = false → 该方案不可行，尝试替换（最多 3 次）
→ 若 strict_time_feasible = unknown → 标记但不确定排除（视为软信号）
```

## 10. 公开接口与模型工具

### 10.1 领域服务

```
MenuPlanningService
├── generate_feasible_menus(
│       safe_recipe_ids,
│       soft_scores,
│       hard_constraints
│   ) → FeasibleMenuArtifact
├── adjust_menu(
│       existing_artifact,
│       adjustment_instruction
│   ) → FeasibleMenuArtifact (新方案)
└── get_menu_evidence(plan_id) → MenuEvidence
```

### 10.2 Agent 工具

健康与菜单规划模型的必需工具：

```
generate_feasible_menus(
    health_evaluation_receipt_ref,
    query_plan_ref
) → FeasibleMenuArtifact
```

- 工具由健康与菜单规划模型自主调用；
- `request_id`、`session_id` 和参与者范围由工作流注入；
- 模型不能传入自定义权重或修改候选菜品列表；
- 工具回执包含 3-5 个方案的摘要、评分分解、`differing_recipe_ids[]` 和 `dominant_objective`（不含内部评分矩阵和权重数值）。

菜单替换工具（健康与菜单规划模型的允许工具）：

```
adjust_menu_plan(
    plan_id,
    action: replace | restore,
    target_recipe_id,
    replacement_recipe_id | null
) → FeasibleMenuArtifact (新版本)
```

## 11. 依赖方向

允许依赖：

```
C2 MenuPlanningService → B4 HealthEvaluationReceipt (safe_recipe_ids, 只读)
C2 MenuPlanningService → B5 TimeProfileService (单菜时间 + 菜单调度)
C2 MenuPlanningService → B6 NutritionScoringService (营养软分)
C2 MenuPlanningService → C1 RetrievalResult (RAG 分数和 source_paths)
C2 MenuPlanningService → B2 TastePreference (口味标签)
C2 MenuPlanningService → B3 RecipeCatalogService (菜品标签、菜系和 dish_type 分类，只读)
C2 MenuPlanningService → B3 PublicRecipeView (最终公开菜名验证)
健康与菜单规划模型工具 → C2 generate_feasible_menus()
健康与菜单规划模型工具 → C2 adjust_menu_plan()
C3 菜单决策模型 → C2 FeasibleMenuArtifact (只读)
```

禁止依赖：

```
C2 → B4 健康关系表或覆盖数据（只能通过公开回执消费结论）
C2 → 原始用户健康档案（只能通过 B2 公开约束接口消费）
C2 → Qdrant 或 C1 内部检索实现
C2 → API Schema 或前端类型
C2 → 模型供应商客户端
C2 → WorkflowState 直接写入
Agent 模型 → C2 内部评分矩阵或权重配置
```

## 12. 异常、错误码与停止条件

| 错误码 | 触发条件 | 处理 |
|---|---|---|
| `NO_SAFE_RECIPES` | 输入的 `safe_recipe_ids` 为空 | 不进入 C2，B4 已进入 `no_safe_menu` |
| `INSUFFICIENT_RECIPES_FOR_STRUCTURE` | 安全候选数量少于最低菜数要求 | `no_feasible_menu` |
| `STRICT_TIME_INFEASIBLE` | 所有候选组合均无法满足严格时间限制 | `no_feasible_menu` |
| `STRICT_INGREDIENTS_INFEASIBLE` | `strict_ingredients_mode=true` 且可用食材不足以组成菜单 | `no_feasible_menu` |
| `LOCKED_RECIPE_NOT_SAFE` | 用户锁定的菜品不在安全候选集中 | `no_feasible_menu` |
| `LOCKED_REJECTED_CONFLICT` | 同一菜品同时出现在锁定和拒绝列表中 | 拒绝优先，标记冲突，继续生成 |
| `PLAN_GENERATION_FAILED` | 优化器内部错误（不可恢复） | `failed` |
| `PLAN_DIVERSITY_INSUFFICIENT` | 生成不足 3 个有差异的方案 | 返回实际数量（≥1），不视为失败 |
| `MENU_STRUCTURE_INVALID` | 硬约束解析失败或结构模板不合法 | `failed` |
| `PLAN_HASH_COLLISION` | 两个不同方案产生相同 `menu_hash` | `failed`（系统错误） |

`no_feasible_menu` 只在实际尝试生成并确认无法满足硬约束后返回。不能在没有尝试的情况下提前返回。

## 13. 构建与初始化要求

- 菜单结构规则、软目标权重、差异权重配置、多样性维度和基础调料集合为固定构建配置；
- 配置变更时重新运行 C2 单元测试和场景测试，不重新构建数据管道；
- C2 不维护自己的持久化数据表（方案是请求内 Artifact，不写入固定业务表）；
- 正式运行期间不更新配置。

## 14. 测试和验收标准

### 14.1 菜单结构

- 用户未明确菜数时生成 5 道菜（安全候选充足）；
- 安全候选不足以满足默认 5 道时进入 `no_feasible_menu`；
- 用户明确"三菜一汤"时严格执行 3 菜 + 1 汤；
- 汤品不超过 1 道、主食不超过 1 道；
- 未提饮品时不出现饮品；
- 明确提出饮品时合计最多 1 道，不替代蔬菜或蛋白主体。

### 14.2 硬约束

- 锁定菜品出现在所有方案中；
- 拒绝菜品不在任何方案中；
- 锁定与拒绝冲突时拒绝优先并标记冲突；
- `strict_ingredients_mode` 下所有菜品只使用可用食材 + 基础调料；
- 严格时间不可行时进入 `no_feasible_menu`；
- 安全候选充足但无法满足菜数时进入 `no_feasible_menu`。

### 14.3 方案差异

- 生成 3 至 5 个方案（安全候选充足时）；
- 任意两个方案至少 1 道菜品不同或结构偏好不同；
- 仅评分不同的重复方案被去重；
- 去重后不足 3 个时返回实际数量；
- 每个方案有明确的 `differing_recipe_ids[]` 和 `dominant_objective`。

### 14.4 软评分

- RAG 相关性和口味偏好匹配的菜品在对应权重方案中排名靠前；
- 营养目标匹配的菜品在营养优先方案中排名靠前；
- 短时菜品在快捷优先方案中排名靠前；
- 多样性得分在多样探索方案中最高；
- 不同权重方案确实产生不同的菜品排序和选择。

### 14.5 多人场景

- 所有方案的每道菜均来自 B4 全员 `PASS` 安全交集；
- 不出现仅部分参与者安全的菜品；
- 不生成参与者专属菜品。

### 14.6 菜单调整

- 替换菜品后产生新 `plan_id` 和新 `menu_hash`；
- 旧方案保留不被覆盖；
- 替换操作生成的方案标记为需要 B4 重新校验；
- 替换回路最多 3 次尝试。

### 14.7 跨模块契约

- B4 `safe_recipe_ids` 是 C2 的唯一菜品输入来源；
- B5 调度结果用于严格时间判断和软时间评分；
- B6 营养分只作为软目标，不改变菜单结构决策；
- C1 RAG 分数不用于硬约束；
- `FeasibleMenuArtifact` 不包含健康约束细节或营养数值；
- B3 的 `dish_type` 分类覆盖汤、主食、饮品、甜点四个槽位所需的区分。

## 15. 旧实现与目标实现差异

| 维度 | 旧实现 | V2 目标 |
|---|---|---|
| 安全候选来源 | B4 健康审查后的安全集 | 同，但 V2 的 B4 健康审查更严格（二元结果 + 全员交集） |
| 优化方法 | `optimization/` 目录中的单人排序和多人组合 | 确定性多目标优化 + 差异化权重配置 |
| 方案数量 | 可能只生成单一推荐 | 3-5 个有实际差异的方案供选择 |
| 菜单结构 | 部分硬编码规则 | 结构化 MenuHardConstraints + 明确/默认菜数严格执行 |
| 软目标 | RAG 分、偏好、可能包含营养 | 系统化的 RAG + 偏好 + 营养 + 时间 + 多样性 + 历史 |
| 饮品/甜点 | 正餐不明显加入 | 明确"仅用户提出时出现" |
| 仅现有食材 | 基础调料为水、盐、油 | 同，显式化为 `strict_ingredients_mode` 和 `available_ingredient_ids` |
| 时间处理 | 简化公式 | 通过 B5 完整调度结果 |
| 方案差异 | 未显式约束 | 明确差异要求 + 去重规则 + differing_recipe_ids |

## 16. 迁移分类

| 旧实现 | 分类 | 处理结论 |
|---|---|---|
| `optimization/single_person.py` | `REWRITE` | 改为多目标优化 + 差异化方案；消费 V2 的 B4/B5/B6/C1 输出 |
| `optimization/multi_person.py` | `REWRITE` | 改为全员安全交集 + 多目标优化；不再处理"部分可食用"的旧语义 |
| `optimization/scoring.py` | `REWRITE` | 使用 V2 模块化软分（RAG/C1、营养/B6、时间/B5、偏好/B2） |
| 旧菜单结构硬编码 | `REWRITE` | 改为结构化可配置的 MenuHardConstraints 和差异权重 |
| 旧菜单相关测试 | `REWRITE` | 保留菜单结构和边界案例；增加方案差异、候选不足不放宽、多人交集、仅现有食材测试 |

## 17. 审查后清理项

以下内容在 C2、C3 和回答模块全部迁移并通过验收后清理：

- 旧单人/多人优化器中与 V2 健康审查语义不一致的"部分可食用"逻辑；
- 旧硬编码菜单结构规则；
- 旧优化器对 RAG 内部函数和健康规则模块的直接依赖；
- 旧的单一推荐输出逻辑（不再需要）；
- 已被新 C2 Schema 替代且确认无消费者的旧优化相关字段和适配器。

## 18. 与全局流程和后续模块的关系

```
B4 安全候选 + B5 时间 + B6 营养 + C1 RAG 分数 + B2 偏好
→ C2 多目标优化
→ FeasibleMenuArtifact (3-5 个 plan_id，含 differing_recipe_ids[] 和 dominant_objective)
→ C3 菜单决策模型从中选择
→ B4 最终健康校验（C3 调用）
→ 回答模型生成最终结果
```

- C2 必须消费 B4 的安全候选结论，不能自行判断健康安全；
- C3 只能从 C2 生成的已有 `plan_id` 中选择，不能重新拼装菜品；
- C3 选择后必须通过 B4 最终校验才能进入回答阶段；
- `user_visible_analysis` 自然语言摘要由健康与菜单规划模型在审查方案后填入，C2 只提供结构化差异事实；
- D3 测试与验收必须覆盖菜单结构边界、方案差异、多人交集、候选不足不放宽和硬约束失败终态。

## 19. 待考量事项

以下问题不影响当前设计通过，但在进入实现阶段前需要进一步确认。在此留痕供后续回顾。

### 19.1 贪心+爬山的局部最优风险

当前搜索策略是贪心构造一次通过、局部搜索只接受改进——标准爬山，会卡在局部最优。不过实际安全候选通常在几十到一百多道的量级，搜索空间不大，局部最优和全局最优的差距可能很小。

**决定**：采用随机重启策略（跑 3 次取最好），而非单次爬山。3 次随机初始化种子不同，取综合分最高的那次结果。安全候选规模小（几十到一百多道），3 次重启的成本可接受。

### 19.2 差异权重偏移幅度缺少数据依据

五个方案的权重偏移（1.5/0.8）是初始设定，没有真实数据支撑。1.5 和 0.8 到底能不能产生足够差异的菜单，取决于实际数据里 RAG 分、营养分、时间分的分布——如果所有安全菜品的时间分都很接近，快捷优先方案跟均衡方案的差别可能就一道菜。

**建议**：实现阶段用真实安全候选集跑一遍，验证五组偏移确实产生菜品集合有差异的方案。偏移幅度作为可配置参数保留，不硬编码。如果某组偏移长期不产生独立方案，可动态跳过或自动加大偏移。

### 19.3 多样探索方案的服务逻辑不同于其他四组

其他四组方案（均衡、偏好优先、营养优先、快捷优先）是"按用户需求优化"。多样探索方案放大 diversity 权重到 1.5，本质上是"给你看看不一样的"——如果当前用户没有明确菜系/口味偏好，这种探索有价值；但如果用户有明确偏好，多样性方案可能显得随机。

**建议**：C2 在多样探索方案的 `dominant_objective` 中标记为 `diverse`，健康与菜单规划模型在审查时应将其与其他方案区分对待——不是"更优"而是"更有变化"。如果用户偏好非常明确（偏好分远高于其他维度），可考虑跳过多样探索方案。

### 19.4 时间可行性替换回路的震荡风险

流程是"构造菜单 → 调 B5 → 不可行则替换最长菜 → 重调 B5"。单菜的排除在贪心构造前已完成，但整套菜单的 makespan 受设备互斥影响，单菜短不代表菜单短。替换回路如果每次替换后 makespan 变化方向不定，可能震荡。

**决定**：最多 3 次替换尝试。每次替换后 must 比较 makespan——如果 makespan 增加（新菜与其他菜设备冲突更严重），回退该次替换并停止替换回路。此规则写入 C2 确定性代码，不依赖实现阶段判断。

### 19.5 汤/主食/饮品的候选子集依赖 B3 dish_type 分类

C2 需要区分"汤品""主食""饮品""甜点"来填充对应槽位。如果 B3 把"番茄蛋汤"标记为 `dish` 而非 `soup`，C2 的汤品槽位就会漏掉它。同理，如果"炒饭"被标记为 `dish` 而非 `staple`，主食槽位也会漏。

**建议**：B3 的 `dish_type` 在设计时需覆盖 C2 的四个槽位分类需求。两个模块之间增加交叉验收测试——确认所有明显属于汤/主食/饮品/甜点的菜品在 B3 中都有正确的 `dish_type` 标记。

### 19.6 FeasibleMenu 中确定性数据与模型填充内容的边界

`FeasibleMenu` 的 `differing_recipe_ids[]` 是 C2 确定性算法能算出来的（哪些菜品不同）。`dominant_objective` 是 C2 根据权重配置能填的结构化标签。但自然语言的差异说明（"方案A更注重口味匹配，方案B更注重营养均衡"）不是优化器能写的——这需要模型来生成。

**建议**：当前设计中 C2 输出 `differing_recipe_ids[]`（确定性）和 `dominant_objective`（确定性标签），自然语言的 `user_visible_analysis` 和 `trade_off_summary` 由健康与菜单规划模型在审查方案后填入。实现时确保 C2 的 Schema 不包含需要自然语言生成的字段，避免优化器越界。
