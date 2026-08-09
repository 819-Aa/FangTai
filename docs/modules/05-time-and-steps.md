# V2 时间与步骤规划模块设计

- 状态：`APPROVED`
- 日期：2026-08-09
- 适用项目：`program_v2`
- 上游依据：[系统总体设计](../00-system-overview.md)、[全局不变量](../contracts/global-invariants.md)、[模块边界](../contracts/module-boundaries.md)、[推荐请求生命周期](../scenarios/recommendation-lifecycle.md)、[数据工程模块](01-data-engineering.md)、[菜品与食材处理模块](03-recipe-ingredient-processing.md)、[健康规则与审查引擎](04-health-rule-engine.md)

## 0. 2026-08-09 批准的实现基线

- B5 只消费 B1/B3 结构化步骤任务和设备事实；禁止重新解析原始步骤、读取构建 JSONL 或在在线请求中调用模型补全。
- 严格时间服从 [ADR-0005](../decisions/0005-strict-time-semantics.md)：输出 `true/false/unknown`，硬截止时间只能接受高权威确定性调度的 `true`。
- 离线 LLM 时间估算固定为低权威 `model_estimate`，只参与软排序；不得使用 40% 并行公式、简单求和或经验常数证明严格可行。
- 缺少时长、依赖或设备事实时保留缺失集合和 `unknown`，不得填默认分钟数。严格请求没有任何 `true` 时由 C2 返回 `NO_FEASIBLE_MENU` 或 `STRICT_TIME_INDETERMINATE`。
- 调度回执必须绑定 `recipe_ids`、`deadline`、`schedule_hash`、`build_id`、证据来源和权威级别，防止跨菜单复用。

## 1. 模块目的

B5 把 B1 清洗产物和 B3 保留的原始制作步骤和 B3 提供的步骤食材绑定，转换为每道菜的结构化任务图；对于单道菜计算置信度分级的制作时长，对于整套菜单基于任务依赖、主动操作互斥、设备互斥和被动等待并行计算总完工时间。

B5 只负责"在给定菜品集合和时间限制下，形成可用的单菜时长和菜单调度结果"，不回答"某道菜是否安全"或"某道菜是否应该入选菜单"。安全候选来自 B4，菜单选择由 C2 和 C3 完成。

## 2. 已确认前提

- 只处理 B4 输出中 `PASS` 的菜品，不接未经过健康审查的候选。
- B3 提供原始步骤、Occurrence 顺序和标准食材绑定；B5 不重新解析原始菜谱文本。
- 固定数据中的旧时间画像为高置信度 636 道、中 491 道、低 873 道；旧总时长不能直接作为严格时间约束依据。
- 缺失步骤不补写，未知时长保留 `null`，不填入固定估算值或伪造精确分钟数。
- 严格时间约束只使用高置信度时间上界；中低置信度时间可以参与软排序，但不能将菜品判定为满足或违反严格时限。
- 一名烹饪者的主动操作时间互斥；同一设备在同一时刻只能被一道菜的一个步骤占用；被动等待在依赖允许时并行；不同设备的主动操作在依赖允许时并行。
- 设备仅作为菜谱声明的调度资源用于菜单内互斥与并行计算，不引入用户设备可用性档案或筛选。
- 步骤食材通过标准 `ingredient_id`、B3 审核别名和最长精确匹配建立关联；只有经过验证的关键步骤食材缺失才阻止执行，不通过宽泛子串包含制造阻断。
- 整套菜单以总完工时间（makespan）为目标；主动操作之和或简化公式不作为最终调度结果。
- 不处理份量、人数份量、采购量和烹饪营养损耗；不计算个人摄入时间。

## 3. 职责

B5 负责：

1. 从 B1 离线构建产物中读取每道可推荐菜品的原始步骤、结构化任务、时间标注和食材绑定；
2. 把原始步骤分类为主动操作、设备占用、被动等待和未知任务；
3. 保留步骤间的顺序依赖，识别并行可能性和设备互斥约束；
4. 为每条步骤记录时间来源、解析方式、原始文本、置信度和时间范围；
5. 未知时长保留 `null` 并标注原因，不填入默认值；
6. 绑定步骤食材到 B3 标准 `ingredient_id`，通过审核别名和最长精确匹配建立引用；
7. 为单道菜生成 `RecipeTimeProfile`，包含各步骤时间、置信度分级和菜品总时长范围；
8. 为整套菜单构建 `MenuScheduleResult`：基于任务图、一名烹饪者、设备互斥和被动等待并行计算总完工时间；
9. 对严格时间约束只使用高置信度时间上界进行可行性判断；
10. 为中低置信度时间生成可供软排序使用的参考时长；
11. 向菜单规划（C2）和菜单决策（C3）提供时间查询接口；
12. 验证关键步骤食材引用完整性，缺失时阻止调度执行；
13. 对 B5 产物执行离线质量门禁和跨模块一致性检查。

## 4. 非职责

B5 不负责：

- 重新解析原始菜品文本或食材清单；
- 判定某道菜是否健康安全；
- 生成或修改菜品、食材、步骤内容；
- 自动补写原始数据中不存在的步骤或时长；
- 为未知时长填入固定估算值；
- 引入用户设备档案、设备可用性筛选或设备数量配置；
- 计算人数份量、采购量、`per_serving` 或个人摄入时间；
- 选择菜单方案或决定菜品是否入选；
- 直接写 WorkflowState、菜单表或最终请求状态；
- 直接向模型暴露任务图内部节点（模型通过 C2/C3 的受控工具获取调度结论和软排序参考时长）。

## 5. 上游输入

### 5.1 离线构建输入

| 输入 | 来源 | 内容 |
|---|---|---|
| 原始步骤文本 | B1 清洗产物 | 每条菜谱的 `steps_raw`，保留原始顺序和行引用 |
| 结构化步骤产物 | B1 离线流水线 | 步骤顺序、依赖关系、主动/设备/等待/未知分类、时长标注、设备类型、步骤食材引用 |
| 步骤食材绑定 | B3 `RecipeStepBindingView` | `occurrence_id`、标准 `ingredient_id`、审核别名、形态属性、原始食材文本 |
| 时间标注配置 | B1 构建配置 | 显式时长解析规则、设备分类注册表、置信度分级阈值 |

### 5.2 在线查询输入

- B4 安全候选 `recipe_id` 列表；
- 最终所选 `plan_id` 对应的菜品集合；
- 用户明确的时间限制（严格或偏好）。

B5 不接收原始自然语言、RAG 检索分数或健康评估结果。

## 6. 下游输出

### 6.1 单菜时间画像

```
RecipeTimeProfile
├── recipe_id
├── steps[]
│   ├── step_index
│   ├── step_type: active | equipment | passive | unknown
│   ├── equipment_type | null
│   ├── duration_seconds | null
│   ├── duration_range [min, max] | null
│   ├── time_source: explicit | derived_from_range | unknown
│   ├── confidence: high | medium | low
│   ├── depends_on[]
│   ├── bound_ingredient_ids[]
│   └── raw_text_ref
├── total_active_seconds | null (仅当全部主动步骤为高置信度时有确定值)
├── total_elapsed_range [min, max] | null
├── overall_confidence: high | medium | low
└── missing_critical_info[]
```

### 6.2 菜单调度结果

```
MenuScheduleResult
├── schedule_id
├── recipe_ids[]
├── task_graph
│   ├── nodes[] (recipe_id, step_index, step_type, equipment_type, duration)
│   ├── edges[] (dependency, equipment_mutex, cook_mutex)
│   └── critical_path
├── makespan_seconds | null
├── makespan_range [min, max] | null
├── schedule_confidence: high | medium | low
├── strict_time_feasible: true | false | unknown (高置信度时返回确定结论)
└── per_recipe_breakdown[]
    ├── recipe_id
    ├── contribution_to_makespan
    └── scheduling_notes
```

### 6.3 消费者视图

- **C2 菜单规划视图**：单菜时长范围、严格时间可行性布尔值、软排序参考时长；
- **C3 菜单决策视图**：所选方案的完整调度结果、makespan 分解和置信度说明；
- **回答模型公开视图**：最终菜单的预计总时间（仅高置信度时给出确定值）、各菜简要时间说明（不暴露内部任务图）。

## 7. 数据模型与数据所有权

### 7.1 步骤任务

```
StepTask
├── step_id (recipe_id + step_index)
├── recipe_id
├── step_index
├── raw_text
├── step_type: active | equipment | passive | unknown
├── equipment_type | null
├── duration_seconds | null
├── duration_min_seconds | null
├── duration_max_seconds | null
├── time_source: explicit | derived_from_range | unknown
├── confidence: high | medium | low
├── depends_on[] (前驱 step_id)
├── bound_occurrence_ids[]
├── bound_ingredient_ids[]
└── binding_method per ingredient: exact_id | approved_alias | longest_exact_match
```

### 7.2 设备分类

```
EquipmentType
├── equipment_code
├── display_name
├── category: cooking | baking | steaming | blending | other
├── mutex_key (同一 key 的设备不能同时使用)
```

设备分类是固定构建配置。常见互斥键例如：
- `stove`：炒、煎、煮、炖共用炉灶；
- `oven`：烤箱；
- `steamer`：蒸箱/蒸锅；
- `blender`：料理机/搅拌机。

同一菜品内的不同步骤使用同一 `mutex_key` 时互斥；被动等待（如腌制、醒面、冷藏）不占用设备。

### 7.3 时间置信度分级

| 置信度 | 条件 | 严格时间 | 软排序 |
|---|---|---|---|
| `high` | 步骤有显式精确时长，且 B3 食材绑定完整 | 使用时间上界 | 参与 |
| `medium` | 步骤有时长范围、近似表达或 B3 食材绑定有少量缺失 | 不参与 | 参与，权重降低 |
| `low` | 步骤无时长、仅有模糊表达、食材绑定大量缺失，或为未知任务 | 不参与 | 不参与或最低权重 |

置信度分级由步骤时长明确性、关键食材绑定完整性和步骤类型共同确定。不根据菜系、烹饪方式或菜名推断。

### 7.4 数据所有权

B5 拥有：
- `step_tasks` 的领域语义和查询接口；
- `equipment_types` 的分类与互斥规则；
- `RecipeTimeProfile` 和 `MenuScheduleResult` 的 Schema；
- 时间置信度分级规则。

B1 负责离线生成 `step_tasks` 初始化数据并通过质量门禁；B5 定义 Schema 和运行时只读语义。B3 拥有步骤食材绑定的事实权威，B5 通过公开端口引用。

## 8. 核心处理流程

### 8.1 离线构建

```
B1 清洗后的原始步骤文本
→ 解析显式时长（"20分钟""1小时""30秒"等）
→ 分离时长范围和近似表达
→ 识别步骤类型（主动/设备/等待/未知）
→ 识别设备类型和 mutex_key
→ 建立步骤依赖链
→ 通过 B3 公开端口绑定步骤食材
→ 计算单菜时间置信度
→ 生成 StepTask 初始化数据
→ 执行质量门禁
```

### 8.2 单菜时间计算

```
加载 StepTask
→ 对于所有步骤为 high 置信度的菜品：total_elapsed = 按依赖拓扑的最短完工时间
→ 对于混合置信度的菜品：形成 [min, max] 范围
→ 标记 overall_confidence
→ 标记 missing_critical_info（关键步骤食材缺失、关键步骤无时长等）
```

### 8.3 菜单调度计算

```
输入：B4 安全候选 recipe_ids（或所选 plan_id 的菜品集合）
→ 加载所有涉及菜品的 StepTask
→ 构建全局任务图：
    - 各菜内部步骤依赖
    - 所有菜的主动操作在同一烹饪者下互斥
    - 相同 mutex_key 的设备步骤互斥
    - 被动等待仅受本菜步骤依赖约束
    - 不同设备的主动操作在依赖允许时并行
→ 计算关键路径和 makespan
→ 如果有严格时间限制：
    - 全部相关步骤为 high 置信度 → 使用时间上界比较 → 明确 true/false
    - 存在中低置信度步骤 → strict_time_feasible = unknown
→ 生成 MenuScheduleResult
```

关键调度约束：

```
∀ 步骤 i, j:
  same_cook ∧ i.type = active ∧ j.type = active → 不能重叠
  i.equipment_mutex_key = j.equipment_mutex_key → 不能重叠
  i 依赖 j → i 在 j 完成后开始
  i.type = passive → 只受自身依赖链约束，不与其他菜的被动等待或主动操作互斥
```

## 9. 算法与确定性规则

### 9.1 时长解析优先级

1. 显式精确数字 + 时间单位：`"20分钟"`、`"1.5小时"`、`"30秒"` → `time_source=explicit`
2. 明确范围：`"15-20分钟"` → `time_source=derived_from_range`，`[min, max]` 均记录
3. 近似表达：`"约半小时"` → 记录为 `duration_range`，`time_source=derived_from_range`
4. 相对或条件表达：`"焖至收汁"`、`"炒至变色"` → `time_source=unknown`

### 9.2 步骤类型判别

- 包含"切、洗、腌、搅拌、揉、擀、包"等且不依赖加热设备 → `active`
- 包含"炒、煎、炸、煮、炖、蒸、烤、煲"等且绑定加热设备 → `equipment`，同时记录 `equipment_type`
- 包含"静置、醒面、发酵、冷藏、浸泡、腌制（等待阶段）" → `passive`
- 无法归类或原始文本不包含可判别信息 → `unknown`

### 9.3 步骤食材绑定

对每条步骤文本，沿 B3 审核别名和标准显示名与当前菜品的 `occurrence_id` 做最长精确匹配：

```
for each step:
    for each occurrence in recipe:
        if occurrence.ingredient_id.canonical_name 存在于 step.raw_text:
            bind(step, occurrence)
        elif any approved_alias 存在于 step.raw_text:
            bind(step, occurrence, method=approved_alias)
```

只使用精确字符串匹配。不使用子串包含、模糊匹配、词根或向量相似度。关键步骤食材缺失指：步骤文本明确提及一种食材，但该食材无法匹配到菜品 Occurrence 中的任何已解析 ID；这触发 `missing_critical_info`。

### 9.4 严格时间可行性

```
if 用户提出严格时间限制 T:
    if 所有涉及步骤 confidence = high:
        取每个 high 步骤的 duration_max（或 duration_seconds 若为精确值）
        计算 makespan_upper_bound
        feasible = (makespan_upper_bound ≤ T)
    else:
        strict_time_feasible = unknown
        (仍可输出 makespan_range 供软排序参考)
```

## 10. 公开接口与模型工具

### 10.1 领域服务

```
TimeProfileService
├── get_recipe_time_profile(recipe_ids) → RecipeTimeProfile[]
├── compute_menu_schedule(recipe_ids, time_limit?) → MenuScheduleResult
└── get_public_time_summary(recipe_ids) → PublicTimeSummary
```

### 10.2 Agent 工具

B5 默认不直接向模型暴露独立工具。时间和调度信息通过 C2 菜单规划工具和 C3 菜单决策工具的受控回执返回，由这些工具在内部调用 B5 公开服务。

- C2 菜单规划工具在生成可行菜单时会消费单菜时间画像和菜单调度结果；
- C3 菜单决策工具在比较已有方案时会消费完整调度分解；
- 回答模型通过公开视图获取最终菜单的时间说明。

这样避免了模型绕过 B5 的置信度规则直接解释原始步骤，也避免了回答模型取得内部任务图细节。

## 11. 依赖方向

允许依赖：

```
B1 离线构建器 → B5 固定 StepTask Schema 与质量校验
B5 TimeProfileService → B3 RecipeStepBindingView (只读)
B5 TimeProfileService → B3 RecipeCatalogService (菜品存在性校验)
C2 菜单规划服务 → B5 单菜时间与调度接口
C3 菜单决策服务 → B5 调度结果分解接口
回答模型公开视图 → B5 公开时间摘要接口
```

禁止依赖：

```
B5 → 原始用户健康档案
B5 → B4 健康关系或约束代码
B5 → B6 营养评分
B5 → Qdrant 或 RAG 内部实现
B5 → API Schema 或前端类型
B5 → 模型供应商客户端
Agent 模型 → B5 内部任务图
```

## 12. 异常、错误码与停止条件

| 错误码 | 触发条件 | 处理 |
|---|---|---|
| `STEP_DATA_INVALID` | 固定步骤数据缺失、结构损坏或依赖链存在环 | 离线构建停止 |
| `STEP_EQUIPMENT_UNKNOWN` | 步骤使用设备但无法归类到已知设备类型 | 标记 `unknown`，不计入设备互斥 |
| `STEP_INGREDIENT_BINDING_INCOMPLETE` | 关键步骤食材无法绑定到 B3 Occurrence | 标记 `missing_critical_info`；达到门禁阈值时离线构建停止 |
| `CRITICAL_STEP_MISSING_TIME` | 串行关键路径上的步骤无时长且无可参考范围 | 菜品 `overall_confidence` 降为 `low` |
| `MENU_SCHEDULE_CYCLE_DETECTED` | 任务图存在循环依赖 | `failed` |
| `RECIPE_NOT_FOUND` | 在线查询引用不存在的 `recipe_id` | `failed` |
| `RECIPE_NOT_ELIGIBLE` | 在线链路引用不可推荐菜品 | `failed` |
| `STRICT_TIME_INDETERMINATE` | 严格时间请求没有任何高权威 `true`，且至少一个候选为 `unknown` | 独立受控终态；不得进入硬时限菜单或改称软排序满足 |

离线构建失败不产生部分产物。在线查询失败不自动重试，不调用模型补齐，不降级为简化公式计算。

## 13. 构建与初始化要求

- B1 固定原始步骤保持只读，B5 派生产物写入 V2 独立生成目录；
- B3 可推荐菜品全部通过质量门禁后，B5 才能开始构建；
- 任何 B3 食材引用悬空或关键步骤食材缺失率达到门禁阈值时停止 B5 构建；
- MySQL 固定表中 `step_tasks` 和 `equipment_types` 由 B1 通过 B5 Schema 初始化；
- 开发期间步骤解析规则变化时执行全量重建；
- 正式运行期间不更新固定步骤数据、设备分类和时间画像。

## 14. 测试和验收标准

### 14.1 时长解析

- 显式精确时长被正确提取（"30分钟""1小时""45秒"等）；
- 时长范围被正确记录 min/max；
- 近似表达不产生虚假精确值；
- 未知时长保持 `null`；
- 旧实现中的固定估算值不再出现。

### 14.2 步骤分类

- 切配、腌制操作归为 `active`；
- 炒、煎、烤、蒸等归为 `equipment` 且绑定正确设备类型；
- 醒面、冷藏、浸泡等待归为 `passive`；
- 无法确定类型的步骤归为 `unknown` 且不阻断调度。

### 14.3 任务图

- 步骤依赖关系与原始顺序一致；
- 同一 `mutex_key` 设备步骤不能在同一时刻重叠；
- 同一烹饪者的主动操作不能重叠；
- 被动等待在依赖允许时可以并行；
- 不同设备操作在依赖允许时可以并行。

### 14.4 置信度

- 1,935 道可推荐菜品均具有 `overall_confidence` 分级；
- 636 道旧高置信度菜品经重新解析后分层原因可追溯（新分级可能与旧不同，差异逐条可查）；
- `low` 置信度不参与严格时间判断和软排序（或最低权重）。

### 14.5 菜单调度

- 单道菜不与其他菜产生虚假依赖；
- 两菜同时需要同一 `mutex_key` 设备时正确串行；
- 两菜分别使用炉灶和烤箱时可以并行；
- makespan 不短于任一条路径的各步骤时间之和；
- 严格时间限制 + 全部高置信度时给出确定结论；
- 存在中低置信度时 `strict_time_feasible = unknown`。

### 14.6 跨模块契约

- B3 步骤食材绑定失效 → B5 标记 `missing_critical_info`；
- B4 未通过的菜品不能出现在 B5 调度输入中；
- C2 从 B5 获取的时间范围不包含内部任务图；
- 回答模型公开视图不暴露内部调度细节。

## 15. 旧实现与目标实现差异

| 维度 | 旧实现 | V2 目标 |
|---|---|---|
| 步骤分类 | 未系统区分主动/设备/等待/未知 | 四类明确分类，设备绑定 mutex_key |
| 缺失时长 | 填入固定估算值 | 保留 `null` |
| 菜单时间 | "主动时间之和 + 非主动时间最大值"简化公式 | 完整任务图调度，考虑依赖和设备互斥 |
| 时间置信度 | 636 高 / 491 中 / 873 低（旧标准） | 重新定级，以步骤时长明确性和食材绑定完整性为准 |
| 严格时间 | 无分级区分 | 只使用高置信度时间上界 |
| 步骤食材绑定 | 未绑定或非精确匹配 | 标准 ID、审核别名和最长精确匹配 |
| 设备处理 | 不区分设备互斥 | mutex_key 串行，不同设备可并行 |
| 烹饪者模型 | 单人隐式 | 一名烹饪者主动操作互斥，显式 |
| 份量/采购量 | 无（旧系统已不处理） | 继续不处理 |

## 16. 迁移分类

| 旧实现 | 分类 | 处理结论 |
|---|---|---|
| `data_pipeline/time_profile.py` | `REFACTOR` | 保留显式时间解析和置信度思路；去掉固定估算填补，升级为步骤任务、设备和依赖结构 |
| 旧时间画像数据 | `REWRITE` | 只作对照基线；V2 按新规则全量重建 |
| 旧菜单时间计算逻辑 | `REWRITE` | 从简化公式改为任务图调度 |
| `db/schema_mysql.sql` 步骤时间相关表 | `REWRITE` | 使用新 StepTask、EquipmentType Schema |
| 旧时间相关测试 | `REFACTOR` | 保留显式时长解析案例；增加步骤分类、设备互斥、并行和置信度测试 |

## 17. 审查后清理项

以下内容在 B5、C2、C3 和回答模块全部迁移并通过验收后清理：

- 旧时间画像中的固定估算值填补逻辑；
- "主动时间之和 + 非主动时间最大值"简化公式；
- 旧 `time_profile` 中与新设备分类冲突的硬编码设备列表；
- 未绑定食材引用的旧步骤时间记录；
- 已被新 B5 Schema 替代且确认无消费者的旧表字段和兼容适配器。

## 18. 与全局流程和后续模块的关系

```
B3 步骤食材绑定
→ B5 结构化任务图与时间画像
├── C2 菜单规划消费单菜时间与调度结果
├── C3 菜单决策消费完整调度分解
└── 回答模型公开视图获取时间摘要
```

- C1 RAG 可检索菜品时长范围，但不能用于健康判断；
- C2 不能把中低置信度时间用于严格时间排除；
- C3 不能绕过 B5 调度结果直接使用旧简化公式；
- 回答模型公开视图不能暴露内部任务图、设备互斥细节和关键路径；
- D3 测试与验收必须覆盖离线构建门禁、运行调度和跨模块置信度传递。
