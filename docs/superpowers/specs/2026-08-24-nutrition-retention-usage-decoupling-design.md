# 营养用途与实际留存解耦增量设计

> 状态：IN_REVIEW
>
> 日期：2026-08-24
>
> 适用项目：`program_v2`
>
> 修订设计：`2026-08-23-nutrition-completion-h06-design.md`

## 1. 决策摘要

H06 将食材 occurrence 的“营养用途分类”和“是否实际留在菜中”拆成两个独立事实、两个独立审核生命周期和两个独立运行时门禁。

- `usage_code` 只描述食材在配方中的语义用途；
- `retained_in_dish` 只描述该次投料是否进入原始可食投料营养；
- 用途明确不能证明实际留存；
- 实际留存明确时，确定性克重计算不再等待用途结论；
- `retained_in_dish=false` 时直接排除该 occurrence，不再要求补一个没有计算意义的用途；
- 只有模糊用量单值规则需要 `usage_code` 参与规则键匹配。

本文件是 H06 设计的增量修订，只替换原设计第 4、5、7、10、13 节中与用途、留存、数量和可食比例门禁有关的约定。营养计算公式、九维营养、B4/B6 职责、RAG 边界、固定套餐依赖和 H06 独立发布方式不变。

## 2. 采用方案与未采用方案

### 2.1 采用：独立正式文件与独立运行时状态

用途和留存分别进入正式审核文件。运行时分别返回是否待审，计算器只在真正依赖某项事实时检查它。

该方案增加一个正式文件和少量类型字段，但能让 10,417 条当前未决 occurrence 按真实阻塞原因分流，不需要为了计算明确克重而虚构用途。

### 2.2 未采用：在同一个 CSV 中允许两个字段独立为空

该方案文件较少，但一行只有一个 `review_status` 时无法表达“留存已批准、用途仍待审”，两个状态字段又会形成隐含的双生命周期，重复键、驳回和修改语义容易产生歧义。

### 2.3 未采用：维持用途与留存绑定

该方案改动最小，但会继续要求为 6,791 条缺少可审计用途证据的 occurrence 猜测用途，也会让明确质量、密度和单位重量换算被无关事实阻塞，因此不采用。

## 3. 现状审计与修订依据

本轮离线审计得到以下构建事实：

- 10,417 条 occurrence 的用途/留存尚未解决；
- 其中 7,989 条已经是质量单位，506 条是体积单位，1,589 条是计数单位；
- 只有 2,424 条与当前数量候选相交，另外 7,993 条不应因主料/辅料分类待审而阻塞确定性数量路径；
- 现有逻辑把 6,403 条调料默认标为 `seasoning + retained=true`，但“调料”只能机械支持用途分类，不能证明实际留存；
- 在这批默认留存记录中，至少 350 条步骤上下文含倒掉、过滤等风险，1,135 条没有步骤绑定；
- 可用作“疑似留存”待审候选的记录约 4,857 条：固体、存在步骤绑定且未命中倒掉/过滤风险；该结论仍只是待审候选，不得自动写入正式决定；
- 当前可食比例候选覆盖 16,820 条 occurrence、2,065 个 `ingredient_id + normalized_form` 键；旧计划中的 1,790 键已过时；
- 14,516 条 occurrence 的形态为空，184 种食材存在多形态，不能用统一 `1.0` 覆盖；
- `optional`、未选中的 `one_of` 和过程材料已在营养输入视图上游排除，不应再通过 `retained=false` 重复表达。

因此，本次修订的目标不是放宽 H06 的 all-or-nothing 门禁，而是让每个门禁只检查它真正依赖的事实。

## 4. 正式数据契约

### 4.1 用途决定

`data/review/ingredient_nutrition_usage_decisions.csv` 升级为 2.0 契约，字段固定为：

```text
occurrence_id,
recipe_id,
ingredient_id,
ingredient_name,
normalized_form,
usage_code,
review_status
```

`usage_code` 使用封闭枚举：

- `main`
- `supporting`
- `seasoning`
- `cooking_fat`
- `cooking_liquid`

原 H06 设计中的 `retained_liquid` 改为 `cooking_liquid`，避免用途名称预先包含留存结论。H05 不迁移、不读取该新枚举。

### 4.2 留存决定

新增 `data/review/ingredient_nutrition_retention_decisions.csv`，1.0 契约字段固定为：

```text
occurrence_id,
recipe_id,
ingredient_id,
ingredient_name,
normalized_form,
retained_in_dish,
review_status
```

`retained_in_dish` 只接受规范布尔值；空值不得被解释为 `false`。

### 4.3 两个正式文件的共同规则

- 只有 `approved` 和 `modified` 记录对构建生效；
- 无论状态为何，`occurrence_id` 重复都使构建失败，不能依赖行顺序覆盖；
- occurrence ID 必须存在于当前 BuildManifest 对应的营养输入视图；
- `recipe_id`、`ingredient_id`、名称和标准形态必须与当前 occurrence 一致，否则 fail closed；
- 正式文件不保存来源、置信度、模型解释或面向用户的说明；
- 两个文件都纳入 SourceManifest，内容哈希和 Schema 版本参与构建身份；
- 当前用途正式文件只有表头，没有有效决定，因此切换 2.0 表头不会丢失已批准数据。

## 5. 运行时类型与兼容

运行时拆成两个解析结果：

```text
NutritionUsageResolution(
    usage_code: UsageCode | None,
    requires_review: bool,
)

NutritionRetentionResolution(
    retained_in_dish: bool | None,
    requires_review: bool,
)
```

`NutritionOccurrenceInput` 增加：

```text
usage_requires_review: bool
retention_requires_review: bool
```

现有聚合字段 `requires_review` 在 H06 过渡期保留为只读兼容值：

```text
requires_review = usage_requires_review or retention_requires_review
```

业务逻辑不得继续使用聚合字段决定所有营养步骤是否执行；它只用于旧审计消费者展示“仍有任一待审事实”。`RecipeNutritionInputView` 从 2.0.0 升到 2.1.0，新消费者必须读取两个细分状态。

## 6. 推导、覆盖与审核边界

解析优先级固定为：

1. 当前 occurrence 的正式 `approved/modified` 决定；
2. 明确、可复验的结构化机械规则；
3. 无法唯一确定时返回对应领域的待审状态。

用途和留存分别执行以上优先级，不能用一方结果推导另一方。

用途允许的机械规则仅限无歧义输入：

- 标准类别明确为调料时，可以解析为 `seasoning`；
- 其他油脂、主料/辅料分组和液体标记只生成待审用途候选，除非步骤与受控身份规则能唯一确定；
- 冲突类别、多种可能用途或缺少可审计证据时标记 `NUTRITION_USAGE_AMBIGUOUS`；
- 模型可以离线提出候选，不能在线写入或覆盖正式用途。

留存不设置“按类别自动为 true”的规则：

- 调料、主料、辅料、油和液体均不能仅凭类别自动认定留存；
- 有步骤绑定、未命中倒掉/过滤风险的固体可以生成 `true` 待审候选，但不自动生效；
- 出现倒掉、过滤、捞出、弃用等上下文时进入风险异常；
- 没有步骤绑定时进入无绑定异常；
- 只有正式决定可以让不确定留存进入 H06 ready build。

## 7. 数量标准化的新门禁

数量标准化优先级保持为：

1. occurrence 级批准克重；
2. 明确质量单位换算；
3. 批准的单位重量规则；
4. 批准的密度规则；
5. 批准的模糊用量单值规则；
6. 未命中则进入数量异常。

各路径的依赖改为：

| 数量路径 | 需要留存结论 | 需要用途结论 |
|---|---:|---:|
| occurrence 批准克重 | 否 | 否 |
| 明确质量单位 | 否 | 否 |
| 单位重量 | 否 | 否 |
| 密度 | 否 | 否 |
| 模糊用量单值 | 否 | 是 |

数量解析器负责“这次投料是多少克”，不负责决定该投料是否计入营养。留存门禁由营养计算器在调用数量、可食比例和 crosswalk 前处理。

当且仅当原始数量必须命中 `ingredient_id + normalized_form + usage_code + fuzzy_token_class` 时，用途未决才返回 `NUTRITION_USAGE_UNRESOLVED_FOR_FUZZY`。确定性数量不能再返回通用的 `usage_unresolved`。

## 8. 营养计算与可食比例门禁

每个 occurrence 按以下顺序处理：

```text
留存未决
→ 整道菜返回 NUTRITION_RETENTION_UNRESOLVED

retained_in_dish = false
→ 直接排除；不要求用途、数量、可食比例、crosswalk 或参考营养

retained_in_dish = true
→ 解析数量
→ 解析可食比例
→ 解析唯一 crosswalk 和九维参考
→ 进入原始可食投料营养求和
```

当留存为 `true` 且数量可通过 occurrence 决定、质量、单位重量或密度确定时，即使用途为空也允许计算。只有模糊用量路径因规则键需要用途而暂停。

可食比例候选生成遵循同一边界：

- 已确认 `retained=false` 的 occurrence 不生成可食比例候选；
- 留存未决单独报告，不伪装成可食比例缺失；
- 已确认 `retained=true` 时，可食比例判断不再被普通用途未决阻塞；
- 只有严格明确的净料、去皮、去骨、去壳、去核等形态可以提出 `1.0` 候选；
- 切片、切块、熟制、干制等处理状态本身不能证明可食比例为 `1.0`；
- 空白形态、多形态冲突、整蛋、骨、壳、鱼、皮、核和坚果类结构风险继续进入人工审阅。

H06 的 all-or-nothing 仍保持：任何已确认留存的 occurrence 缺少可用克重、可食比例、唯一 crosswalk 或完整九维参考时，整道菜不可发布营养画像。

## 9. 离线候选工作流

新增一个离线入口：

```text
data-review --kind nutrition-occurrences
```

该入口复用同一份 occurrence 证据构建器，但输出四个相互独立的待审文件：

- `nutrition_usage_candidates.csv`
- `nutrition_usage_exceptions.csv`
- `nutrition_retention_candidates.csv`
- `nutrition_retention_exceptions.csv`

候选文件只保存审阅所需的结构化事实，例如 occurrence 身份、食材类别、原始数量、步骤绑定、受控证据码和风险码；不保存置信度、数据来源说明或模型自由文本解释。

工作流约束：

- 所有候选初始状态必须为 `pending`；
- 不读取在线模型配置，不发起模型调用；
- 不得写入正式审核文件，候选到正式决定必须经过明确审核动作；
- 输出 Schema 严格固定，文本公式前缀必须转义，文件原子写入；
- 正式路径及其大小写、相对路径、符号链接等别名均不得作为候选输出目标；
- 用途候选和留存候选分别审核、分别晋升，不能以一个共同状态同时批准；
- 无争议的批量候选可由实施子代理提出、主代理审核；关键高影响异常再提交用户决定。

稳定异常码至少包括：

- `NUTRITION_RETENTION_UNRESOLVED`
- `NUTRITION_RETENTION_DISCARD_RISK`
- `NUTRITION_RETENTION_NO_STEP_BINDING`
- `NUTRITION_USAGE_AMBIGUOUS`
- `NUTRITION_USAGE_UNRESOLVED_FOR_FUZZY`

## 10. 已审数量规则的处理

本轮 15 个稳定数量候选按已批准的分组结论处理。

可以写入正式规则的 7 项：

| 食材/规则 | 正式值 |
|---|---:|
| 水密度 | 1.000 g/mL |
| 普通稀醋密度 | 1.000 g/mL |
| 牛奶密度 | 1.020 g/mL |
| 柠檬汁密度 | 1.020 g/mL |
| 色拉油密度 | 0.920 g/mL |
| 橄榄油密度 | 0.913 g/mL |
| 烧烤酱密度 | 1.130 g/mL |

淀粉水/水淀粉不得建立通用密度规则，因为配比变化会直接改变密度；该候选保持拒绝，不写正式规则。

以下 7 项继续保留在人工待审队列，不在本设计中指定正式数值：

- 黄酒密度；
- 辣椒油密度；
- 香醋密度；
- 鸡精每勺重量；
- 蛋清每个重量；
- 香蕉每个重量；
- 皮蛋每个重量。

正式数量规则同样不增加来源和置信度字段。审核依据只保留在离线审计材料中，不进入运行时营养 Artifact 或用户回答。

## 11. SourceManifest、构建身份与迁移

H06 SourceManifest 在原营养输入基础上增加并哈希：

- 用途决定 2.0；
- 留存决定 1.0；
- `RecipeNutritionInputView` 2.1.0；
- 已有数量决定、数量规则、可食比例规则/决定、crosswalk、食物组成类型和质量异常决定。

构建规则：

- 任一正式输入变化都产生新的 build ID；
- 同一构建不得混用旧用途 Schema 和新留存 Schema；
- H05 文件、容器、视图和运行进程保持不变；
- 先升级解析器和门禁，再重新生成用途、留存、数量和可食比例候选；
- 在用户批准前，候选生成不得改变现有正式文件哈希；
- 正式决定补齐后才重建 H06 营养输入视图和 1932 道营养画像。

## 12. 测试与验收

### 12.1 契约测试

- 用途和留存加载器分别校验 Schema、封闭枚举、布尔值和 review status；
- 两个文件分别拒绝重复 occurrence、错误 recipe/ingredient 身份和构建外记录；
- SourceManifest 同时哈希两个文件；
- 2.1.0 输入视图保留兼容聚合字段，并发布两个细分待审状态。

### 12.2 计算测试

- `retained=true + 明确克重 + usage=None` 可以计算；
- `retained=false + usage=None` 直接排除且不产生后续缺失错误；
- `retention=None` 返回留存未决并阻止整道菜发布；
- 模糊用量在 usage 未决时返回专用错误；
- 单位重量和密度路径不要求 usage；
- 调料可以机械得到 `seasoning`，但不能因此自动得到 `retained=true`；
- 倒掉/过滤风险和无步骤绑定进入不同异常；
- 已排除的 optional、未选 one_of 和过程材料不重复进入留存审核。

### 12.3 离线与回归测试

- `nutrition-occurrences` 输出四个 pending-only 文件；
- 候选入口无模型调用、无正式写入且拒绝正式路径别名；
- 重新生成后不再把普通 usage 未决作为所有可食比例记录的统一阻塞原因；
- 15 个稳定数量候选精确落入 7 项正式、1 项拒绝、7 项待审；
- H05 数据和容器不变；
- RAG 文档不新增营养、留存、时间或健康字段；
- B4、B5、B6、C1、C2、固定套餐和烹饪程序依赖无职责或主键回归；
- 最终仍以 1932/1932 营养可用、九维完整和同 build ID 为 H06 ready 条件。

## 13. 实施顺序与检查点

1. 先升级正式契约、运行时类型和 SourceManifest；
2. 再拆分用途/留存推导与数量计算门禁；
3. 增加离线 occurrence 候选入口并重新生成四类队列；
4. 按已批准分组写入 7 项数量规则，保留 1 项拒绝和 7 项待审；
5. 主代理审查留存候选批次，关键风险项交用户决定；
6. 留存明确后重新生成可食比例队列，再完成形态、crosswalk 和九维参考补全；
7. 重建 H06 营养画像并执行一次专项自检；
8. 最后从全局检查 RAG、健康、时间、菜单、依赖、构建身份、容器隔离和回滚路径。

每完成一个阶段只做一遍对应专项自检；最终再做一次全局冲突审查，不做三遍重复自检。

## 14. 保持不变的边界

- 只计算原始可食投料营养；
- 不计算烹饪得率、营养保留率、吸水率、成品重量、份数或个人摄入量；
- 用量统一到克只服务营养计算，不承担人数或分量建议；
- 营养和时间在 RAG、B4 健康过滤之后处理；
- 营养只能影响安全候选之间的软排序，不能推翻健康硬过滤；
- 原始菜品、原 label、固定套餐内容和烹饪程序依赖不因本修订改变；
- 运行时和用户回答不展示来源、置信度或审核状态；
- 会话前是否加载健康档案仍不在本次数据工程范围内。

## 15. 停止条件与完成定义

以下事项仍需停下提交用户决定：

- 7 项尚未确定的稳定数量规则；
- 留存候选中会显著改变高油、高钠或总能量排序的高影响记录；
- 步骤证据互相冲突，无法判断是否倒掉、过滤或留存的记录；
- 必须改变菜品身份、原 label、固定组合或程序依赖才能补全的记录；
- 权威参考不足且模型估算会实质改变营养排序的记录。

本增量设计完成实施的定义是：用途和留存已独立建模、独立审核和独立门禁；确定性数量不再被普通用途未决阻塞；正式输入可复验且纳入同一构建身份；1932 道菜最终仍满足 H06 原设计的完整营养与全局兼容验收。
