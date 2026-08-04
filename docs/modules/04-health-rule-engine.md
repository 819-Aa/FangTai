# V2健康规则与审查引擎设计

- 状态：`IN_REVIEW`
- 日期：2026-08-04
- 适用项目：`program_v2`
- 上游依据：[系统总体设计](../00-system-overview.md)、[全局不变量](../contracts/global-invariants.md)、[模块边界](../contracts/module-boundaries.md)、[推荐请求生命周期](../scenarios/recommendation-lifecycle.md)、[数据工程模块](01-data-engineering.md)、[用户健康档案模块](02-user-health-profile.md)、[菜品与食材处理模块](03-recipe-ingredient-processing.md)

## 1. 模块目的

B4健康规则与审查引擎把B2生成的参与者有效健康硬约束、B3生成的菜品完整标准食材集合以及审核通过的食材健康关系连接起来，对RAG召回候选和最终所选菜单执行确定性健康审查。

本模块只回答“某道菜是否命中当前任一参与者的健康硬约束”，输出不可由模型改写的`PASS`或`EXCLUDE`及完整证据。RAG负责找到用户想要的真实菜品，模型负责主动调用工具、审查证据、判断规划动作和组织菜单；B4负责健康硬结论。三者不能互相替代。

B4位于RAG召回之后、菜单规划之前，并在菜单决策之后再次执行最终健康校验。任何营养估算、检索分数、菜品风险标签、名称子串或模型判断都不能形成或抵消健康硬命中。

## 2. 已确认前提

- 项目只处理已有固定用户档案、固定菜品和固定标准食材，不扩展到真实世界人群或开放食材知识。
- 固定数据仍需经过完整离线清洗、标准化、关系构建和质量门禁，不能由Agent在线解析原始文件。
- B2只输出当前参与者的有效约束，不判断菜品是否安全。
- B3只输出菜品与标准食材事实，不判断食材是否适合用户。
- 健康硬关系最终必须落实为全局唯一`constraint_code → ingredient_id`的精确关系。
- 食材族和类别可以辅助离线整理待审核关系，但运行时不能通过食材族、类别、名称或模型推断健康命中。
- 疾病、异常指标、过敏、特殊生理阶段和明确禁忌形成硬约束；健康目标、口味和一般营养倾向保持软条件。
- 疾病事实和异常指标确实对应同一健康限制时共享一个全局唯一`constraint_code`，同时保留全部来源证据。
- 用户明确“不能吃”的食材必须先由B3严格解析为标准`ingredient_id`，随后执行直接ID匹配。
- 菜品健康食材集合包含必需食材、可选食材、全部替代成员和审核通过的复合组成。
- 任一食材命中任一参与者的任一有效健康硬约束时，整道菜排除。
- 食材出现即参与硬匹配，数量多少、数量是否可解析和菜品份量都不能减弱或取消命中。
- 多人共享菜单对每位参与者分别审查，只允许全员均为`PASS`的菜品进入菜单规划。
- B4结果只有`PASS`和`EXCLUDE`；规则、数据或证据不完整进入`failed`，不能作为第三种健康状态继续推荐。
- 模型自主决定工具调用顺序和时机；工作流不预调用、不代替模型补调用，只在角色提交时校验完成条件。
- 初次批量审查和最终菜单校验使用同一B4确定性引擎与同一固定关系数据，不维护两套健康标准。
- 不建设运行时规则版本、发布切换或多版本校验机制。
- 不设置健康回答兜底，不执行技术自动重试或自动模型切换。

## 3. 职责

B4负责：

1. 定义食材健康关系、关系证据和约束覆盖的领域Schema；
2. 定义B2全局唯一`constraint_code`与B4关系匹配的契约；
3. 从B3可推荐菜品健康视图派生离线健康食材全集；
4. 校验每个允许健康约束代码对固定健康食材全集的覆盖完整性；
5. 只激活绑定标准`ingredient_id`且审核通过的硬关系；
6. 接收B2的`ParticipantHealthConstraintSet`引用；
7. 接收RAG候选批次引用并通过B3公开端口重新加载权威食材视图；
8. 对每位参与者、每道候选菜执行精确关系匹配；
9. 对明确食材禁忌执行标准`ingredient_id`直接匹配；
10. 生成逐参与者、逐菜品的二元健康结果；
11. 形成多人场景的全员安全菜品交集；
12. 保存全部约束、食材、关系和食材路径证据；
13. 为健康规划模型、菜单决策模型、回答模型和最终审计生成权限受限的投影视图；
14. 为健康规划模型提供候选批次健康审查工具；
15. 为菜单决策模型提供所选菜单最终健康校验工具；
16. 在最终校验时重新加载当前约束和菜品事实并重新计算；
17. 区分健康内容冲突、无安全候选和系统完整性失败；
18. 为最终结果与强制健康审计的原子提交提供证据产物。

## 4. 非职责

B4不负责：

- 读取或修改原始用户健康档案；
- 判断指标是否异常或从指标推断标准约束代码；
- 修改、撤销或放宽B2生成的有效健康约束；
- 解析原始菜谱、食材文本、别名、可选项或替代项；
- 自动生成复合食材组成；
- 通过名称子串、词根、模糊匹配、向量相似度或模型推断食材身份；
- 执行RAG召回、融合或重排序；
- 使用菜名、菜品标签或检索文档判断健康安全；
- 使用营养估算、营养置信度、个人摄入或`per_serving`形成硬筛选；
- 计算烹饪营养损耗、人数份量或采购量；
- 对健康目标、口味偏好、营养软分和时间进行菜单优化；
- 选择最终菜单或生成最终自然语言回答；
- 让模型创建、修改、批准或激活食材健康关系；
- 直接写WorkflowState、菜单表或最终请求状态；
- 执行运行时规则版本切换、版本兼容或旧规则回退；
- 在关系、工具或证据失败后继续工作流。

## 5. 上游输入

### 5.1 B2参与者有效约束

B4只通过B2公开接口读取`ParticipantHealthConstraintSet`，至少包含：

```text
ParticipantHealthConstraintSet
├── participant_ref
├── hard_constraints[]
│   └── CodedHealthConstraint | ExplicitFoodTabooConstraint
├── unresolved_signal_refs[]
└── projection_level
```

两类硬约束使用可判别联合：

```text
CodedHealthConstraint
├── constraint_kind: coded
├── constraint_id
├── constraint_type: condition | physiological_status | allergy | metric
├── constraint_code
├── effect: hard_exclude
├── scope
└── source_refs[]

ExplicitFoodTabooConstraint
├── constraint_kind: explicit_food_taboo
├── constraint_id
├── constraint_type: explicit_food_taboo
├── ingredient_id
├── effect: hard_exclude
├── scope
└── source_refs[]
```

通用健康约束只能提供`constraint_code`，不能提供`ingredient_id`；明确食材禁忌只能提供`ingredient_id`，不能提供`constraint_code`。两者同时存在、同时为空或`constraint_kind`与字段不一致都返回`HEALTH_CONSTRAINT_SET_INVALID`。B4不接收原始疾病文本、原始指标数值、完整档案JSON或模型自由填写的健康结论。

存在未解决且会影响当前健康判断的临时信号时，B2应先进入`needs_clarification`，不得把不完整约束集交给B4形成健康`PASS`。

### 5.2 B3菜品健康食材视图

B4只通过`RecipeCatalogService.get_health_ingredient_view(recipe_ids)`读取：

```text
RecipeHealthIngredientView
├── recipe_id
├── catalog_eligibility
├── ingredient_ids[]
├── ingredient_evidence_paths[]
├── unresolved_occurrence_count
└── composition_expansion_status
```

`ingredient_ids[]`必须是必需、可选、全部替代成员和审核复合组成的去重并集。B4不重新解析原始食材，也不接受模型提交一份缩小后的食材列表。

### 5.3 RAG候选批次

初次健康审查接收经过Schema校验的`RetrievalResult`引用。该引用至少能够确定候选批次、候选`recipe_id`、检索路径和请求范围。B4只使用候选ID定位菜品事实，不读取RAG分数作为健康输入。

### 5.4 可行菜单与所选方案

最终健康校验接收已存在的`plan_id`、`FeasibleMenuArtifact`引用和当前约束集引用。工具根据`plan_id`从Artifact读取菜品集合，不接受模型重新提交一份自由菜品ID列表。

### 5.5 离线关系构建产物

B4离线输入包括：

- B2允许输出的全局唯一健康约束代码注册表；
- B3生成的全部可推荐菜品健康食材视图；
- 待审核食材健康关系；
- 关系审核状态和证据引用；
- 约束覆盖审核结果。

外部数据来源获取不属于本模块；B4只规定关系进入正式健康链路前必须满足的结构、审核和完整性条件。

## 6. 下游输出

### 6.1 候选批次健康回执

```text
HealthEvaluationReceipt
├── evaluation_id
├── request_id
├── retrieval_result_ref
├── constraint_set_refs[]
├── recipe_results[]
├── safe_recipe_ids[]
├── excluded_recipe_ids[]
├── input_fingerprint
└── evidence_refs[]
```

回执是确定性工具产物。健康规划模型可以引用、审查和总结它，但不能修改其中任何健康状态或证据。

### 6.2 参与者—菜品健康结果

```text
ParticipantRecipeHealthResult
├── participant_ref
├── recipe_id
├── status: PASS | EXCLUDE
├── constraint_refs[]
├── evaluated_ingredient_ids[]
├── ingredient_set_evidence_paths[]
├── coverage_refs[]
├── exclusion_hits[]
└── input_fingerprint
```

### 6.3 菜品全员结果

```text
RecipeGroupHealthResult
├── recipe_id
├── participant_result_refs[]
└── group_status: PASS | EXCLUDE
```

只有全部参与者结果均为`PASS`时，`group_status`才为`PASS`。

### 6.4 最终健康校验产物

```text
FinalValidationArtifact
├── validation_id
├── request_id
├── plan_id
├── menu_artifact_ref
├── participant_refs[]
├── recipe_ids[]
├── participant_recipe_results[]
├── relation_evidence_refs[]
├── menu_hash
├── input_fingerprint
└── status: PASS | EXCLUDE
```

`input_fingerprint`和`menu_hash`只用于确认同一请求内的候选、约束、菜单和工具回执没有引用错位，不承担固定数据版本切换或跨版本兼容职责。

### 6.5 权限投影视图

- 健康规划模型视图：匿名参与者约束摘要、逐菜结果、命中食材和内部证据引用；
- 菜单决策模型视图：已有方案、初次健康回执引用和最终校验所需最小事实；
- 回答模型视图：最终菜单、公开健康摘要和可公开食材冲突说明；
- SSE与前端视图：阶段状态、通过当前项目规则审查的说明和最小披露结果；
- 强制审计视图：完整约束、食材、关系、覆盖、工具回执和最终校验证据。

## 7. 数据模型与数据所有权

### 7.1 全局唯一约束代码

`constraint_code`是B2与B4之间唯一的通用健康关系匹配键。`constraint_type`说明约束来源类型，不参与食材关系主键。

疾病事实与异常指标对应同一健康限制时可以形成相同`constraint_code`。B4按该代码执行一次食材匹配，同时把多个`constraint_id`和全部`source_refs`保留在评估证据中。若两类事实对应的食材限制不同，则必须使用不同代码。

B4不能自行把两个健康代码合并，也不能根据名称相似度选择关系集合。

### 7.2 健康食材全集

离线健康食材全集定义为：

```text
HealthIngredientUniverse
=
所有catalog_eligibility=eligible的RecipeHealthIngredientView
中ingredient_ids的去重并集
```

它包含必需、可选、全部替代成员、审核复合组成成员，以及没有审核完整组成但以原子身份出现在菜品中的复合食材。它不包含非食用材料和只存在于不可推荐记录且未被可推荐菜品引用的食材。

`HealthIngredientUniverse`是离线质量门禁中的派生集合，不作为新的运行时业务模块或独立人工维护数据源。

### 7.3 食材健康关系

```text
IngredientHealthRelation
├── relation_id
├── constraint_code
├── ingredient_id
├── effect: hard_exclude
├── relation_basis: direct | reviewed_family_expansion | reviewed_category_expansion
├── review_status: approved | pending | rejected
├── hard_filter
├── public_reason_code
└── evidence_refs[]
```

关系进入在线硬筛选必须同时满足：

```text
ingredient_id存在于B3标准食材注册表
AND review_status = approved
AND hard_filter = true
AND effect = hard_exclude
AND constraint_code存在于B2允许代码注册表
```

`relation_basis`只记录离线关系如何形成。即使关系来自审核食材族或类别展开，在线关系仍必须绑定具体`ingredient_id`。运行时不得沿食材族或类别继续扩散。

### 7.4 关系证据

```text
IngredientHealthRelationEvidence
├── evidence_id
├── relation_id
├── evidence_type
├── evidence_ref
└── note
```

证据允许多条，但缺少有效证据的关系不能进入正式硬筛选。模型不能创建、补写或批准证据。

### 7.5 约束—食材审核清单

覆盖完整性不能只通过数量证明。B4必须保存每个通用约束代码对健康食材全集中每个食材的确定审核决定：

```text
HealthConstraintIngredientReview
├── review_id
├── constraint_code
├── ingredient_id
├── decision: hard_exclude | no_hard_relation
├── review_status: approved | pending
├── relation_id | null
└── evidence_refs[]
```

约束如下：

- `(constraint_code, ingredient_id)`全局唯一；
- `decision=hard_exclude`时必须关联唯一且已批准激活的`relation_id`；
- `decision=no_hard_relation`时`relation_id`必须为空，但仍需保留确定审核依据；
- `review_status=pending`只表示审核未完成，不能计入覆盖，也不能进入正式运行数据；
- 被拒绝的关系候选在最终审核清单中表现为经过批准的`no_hard_relation`决定，候选过程记录可以留在离线审查产物中；
- 模型和运行时服务不能创建或修改审核决定。

该清单是完整覆盖的权威依据；`IngredientHealthRelation`是其中`decision=hard_exclude`行的一对一硬关系投影。

### 7.6 约束覆盖

```text
HealthConstraintCoverage
├── constraint_code
├── coverage_status: complete | incomplete
├── universe_ingredient_count
├── reviewed_ingredient_count
├── relation_count
└── evidence_ref
```

`HealthConstraintCoverage`是审核清单的派生汇总，不是覆盖完整性的唯一证据。`complete`必须满足：该代码所有`approved`审核行的`ingredient_id`集合与`HealthIngredientUniverse`精确相等，不多、不少且无重复；全部`hard_exclude`决定都能一对一关联生效关系；不存在影响该代码的`pending`审核。

固定目录确实没有命中食材时允许`relation_count=0`，但仍必须为全集中每个食材保存`approved + no_hard_relation`决定，且`reviewed_ingredient_count`等于`universe_ingredient_count`。

没有覆盖记录或覆盖不完整时，B4不能把“没有查到关系”解释为健康未命中。

### 7.7 排除命中

```text
HealthExclusionHit
├── constraint_ids[]
├── constraint_code | null
├── taboo_ingredient_id | null
├── ingredient_id
├── relation_id | null
├── ingredient_evidence_paths[]
├── relation_evidence_refs[]
└── public_reason_code
```

通用健康关系命中必须包含`constraint_code`和`relation_id`；明确食材禁忌直接命中时包含`taboo_ingredient_id`，不要求`relation_id`。

明确食材禁忌直接命中的`public_reason_code`固定为`DIRECT_FOOD_TABOO_CONFLICT`，由公开投影器转换为“该食材与当前明确健康限制冲突”。模型不能为直接禁忌临时编写新的原因码或暴露原始用户表述。

### 7.8 存储所有权

B4拥有或定义：

- `ingredient_health_relations`；
- `ingredient_health_relation_evidence`；
- `health_constraint_ingredient_reviews`；
- `health_constraint_coverage`；
- 健康评估领域结果和证据Schema；
- 健康工具回执Schema；
- 最终健康审计中属于B4的证据字段。

B1负责离线编排和初始化这些固定产物；B4定义Schema、校验和在线只读语义。运行时关系和覆盖表只读。

请求中间评估先保存为Artifact和WorkflowState引用，不为每次尝试立即写永久成功记录。最终结果由Application提交服务把菜单与强制健康审计放入同一MySQL事务。

## 8. 核心处理流程

### 8.1 离线关系构建

```text
B2允许约束代码注册表
+ B3全部可推荐菜品健康视图
→ 派生HealthIngredientUniverse
→ 生成或读取待审核关系
→ 食材族和类别只辅助形成候选
→ 审核并固化constraint_code × ingredient_id
→ 校验关系证据和激活状态
→ 为每个允许代码和全集食材保存唯一审核决定
→ 以审核决定集合精确相等派生覆盖状态
→ 执行跨模块质量门禁
→ 初始化MySQL正式关系和覆盖数据
```

任一阶段失败时，不得把部分关系或部分覆盖结果交给在线链路。

### 8.2 初次候选批次健康审查

```text
模型主动取得B2健康约束回执
→ 模型主动调用evaluate_recipe_health
→ 工具校验request、session和participant范围
→ 根据RetrievalResult重新读取候选recipe_id
→ 通过B3公开端口加载完整健康食材视图
→ 校验全部约束代码覆盖完整
→ 逐参与者、逐菜品执行精确匹配
→ 形成PASS/EXCLUDE矩阵
→ 计算全员safe_recipe_ids
→ 返回不可改写HealthEvaluationReceipt
```

### 8.3 明确食材禁忌

```text
TemporaryHealthConstraint.ingredient_id
∈ RecipeHealthIngredientView.ingredient_ids
→ EXCLUDE
```

明确食材禁忌不经过通用疾病关系表，不使用别名或自然语言再次匹配。食材身份解析已经由B2调用B3严格解析器完成。

### 8.4 多人安全交集

```text
safe_recipe_ids
=
所有participant_ref对该recipe_id均为PASS的集合
```

任何只对部分参与者安全的菜品都不能进入共享菜单。B4保留逐人证据，但对健康规划模型之外的角色执行隐私裁剪。

### 8.5 扩展召回后的审查

健康规划模型可以根据安全候选数量和菜单需求判断是否调用一次扩展召回。新批次必须形成新的`RetrievalResult`引用并再次主动调用B4；旧批次的健康回执不能自动覆盖新增菜品。

是否扩展由模型在允许预算内判断，工作流只校验调用权限和`retrieval_expansion_count`上限，不替模型发起调用。

### 8.6 最终菜单健康校验

```text
菜单决策模型选择已有plan_id
→ 模型主动调用validate_selected_menu_health
→ 工具从FeasibleMenuArtifact读取该plan_id的菜品
→ 重新读取当前有效约束集
→ 重新加载B3完整健康食材视图
→ 使用同一B4算法重新计算全部参与者和菜品
→ 校验menu_hash和Artifact引用
→ 形成FinalValidationArtifact
```

最终校验不是复用初次`PASS`布尔值，也不是第二套模型意见。它使用同一固定关系重新计算，以发现菜单重组、引用错位、参与者丢失或健康证据不一致。

## 9. 算法与确定性规则

### 9.1 评估前置条件

对任一成功评估，必须同时满足：

- 当前请求至少有一位合法参与者；
- 约束回执属于当前请求和参与者范围；
- 每条硬约束符合`CodedHealthConstraint | ExplicitFoodTabooConstraint`互斥Schema；
- 候选批次或菜单引用属于当前请求；
- 每个候选`recipe_id`存在且具备目录资格；
- 每道菜的健康食材集合完整；
- 每道菜的`composition_expansion_status`为`atomic`或`complete`；
- 每个通用`constraint_code`的覆盖状态为`complete`；
- 所有在线关系满足批准和硬筛选条件；
- 输入不含营养、RAG健康标签或自由文本规则。

任一前置条件失败时停止，不产生成功`PASS/EXCLUDE`矩阵。

### 9.2 精确匹配算法

对每位参与者：

1. 按`constraint_code`对通用硬约束分组，保留全部`constraint_id`和证据；
2. 按`ingredient_id`对明确食材禁忌分组；
3. 对每道菜读取B3已去重的完整`ingredient_ids`；
4. 将通用代码对应的审核关系食材集合与菜品食材集合求交集；
5. 将明确禁忌ID集合与菜品食材集合求交集；
6. 为每个交集成员生成`HealthExclusionHit`；
7. 有任一命中则该参与者对该菜为`EXCLUDE`，否则为`PASS`；
8. 对所有参与者结果执行AND交集形成菜品全员结果。

算法不依赖规则顺序、严重度或分数。任一合法硬命中已经足以排除整道菜。

### 9.3 二元结果语义

- `EXCLUDE`：至少存在一条完整、合法、可追溯的硬命中；
- `PASS`：食材完整、约束完整、覆盖完整且全部精确匹配没有命中。

以下情况不是`PASS`：关系查询失败、覆盖缺失、食材不完整、证据失效、输入范围错误或模型没有调用工具。

### 9.4 多重命中与去重

同一参与者、同一道菜可以命中多个食材、多个约束或同一约束的多个证据。菜品只形成一个`EXCLUDE`结果，但保留全部唯一命中。

命中去重键至少包括：

```text
participant_ref
recipe_id
constraint_code或taboo_ingredient_id
ingredient_id
relation_id或direct_taboo
```

疾病与异常指标共享同一`constraint_code`时不重复执行关系匹配，但`constraint_ids[]`和来源证据不能丢失。

### 9.5 禁止的健康输入

B4的输入Schema和运行时断言必须拒绝：

- `energy_kcal`、钠、碳水、嘌呤估算或其他营养数值；
- 任何`per_serving`字段；
- 营养置信度、匹配方法和烹饪损耗；
- RAG词法分数、向量分数、融合分数和重排分数；
- 菜品`risk_tag`、`health_tag`或营养标签；
- `LIKE`、正则、字符串包含或模糊食材查询；
- 模型生成的自由文本医疗判断；
- 允许模型修改关系状态的参数。

### 9.6 批量执行

候选健康审查按候选批次执行，先批量加载食材视图、关系集合和覆盖状态，再在确定性领域服务中构建集合索引。不得由模型为每道菜逐条编写健康判断，也不应为每个“参与者—菜品—食材”组合执行独立数据库往返。

批量优化不能改变证据粒度。即使内部使用集合交集，也必须能够恢复到参与者、菜品、食材、约束和关系的完整命中路径。

## 10. 模型工具与角色协作

### 10.1 自主调用与完成条件

“必需工具”表示角色成功完成任务的后置条件，不表示工作流提前或自动调用工具。

```text
工作流进入模型节点
→ 注入角色允许的工具和受控范围
→ 模型自主判断调用顺序和时机
→ 模型主动发起调用并审查回执
→ 模型提交Artifact
→ 工作流校验角色完成条件和有效回执
```

模型漏调时返回`REQUIRED_TOOL_NOT_CALLED`。工作流不能补调、伪造回执或用固定结果继续。

### 10.2 候选健康审查工具

```text
evaluate_recipe_health(
    retrieval_result_ref,
    constraint_set_receipt_ref
) -> HealthEvaluationReceipt
```

工具权限范围中的`request_id`、`session_id`和参与者集合由工作流注入。模型不能通过参数扩大范围，也不能直接提交食材列表或健康关系。

### 10.3 最终菜单校验工具

```text
validate_selected_menu_health(
    plan_id,
    menu_artifact_ref,
    constraint_set_receipt_ref
) -> FinalValidationArtifact
```

该工具只允许菜单决策角色使用。回答模型和统一审查模型不能借此补齐前序角色漏掉的最终校验。

### 10.4 健康与菜单规划模型

该模型负责：

- 主动调用B2健康约束工具和B4候选健康审查工具；
- 审查回执是否覆盖当前参与者和完整候选批次；
- 理解被排除菜品和剩余安全候选；
- 判断是否使用一次扩展召回；
- 只使用`safe_recipe_ids`调用菜单规划能力；
- 形成引用确定性回执的`HealthEvaluationArtifact`和可行菜单产物。

该模型不能改写工具结论、删除命中证据或把未审查菜品写入方案。

### 10.5 菜单决策模型

该模型负责：

- 只从已有可行`plan_id`中选择；
- 主动调用最终菜单健康校验工具；
- 审查最终校验结果；
- 只有取得`PASS`回执后才能提交成功选择Artifact。

该模型不能重新拼装菜品、修改参与者或在校验时提交缩小后的菜品集合。

### 10.6 Artifact一致性

模型产物可以引用和总结工具回执，但不能复制一份可独立修改的健康结论。工作流至少检查：

- Artifact引用的`evaluation_id`真实存在；
- 候选批次、参与者和约束引用一致；
- 模型声明的安全菜品集合等于工具回执中的集合；
- 可行菜单只包含工具回执中的安全菜品；
- 最终选择的菜单哈希与最终校验哈希一致。

任一不一致进入`failed`，不由工作流自动修正。

## 11. 证据、审计与隐私投影

### 11.1 `PASS`证据

`PASS`不是单纯布尔值。内部证据必须能够证明：

- 使用了哪些参与者约束；
- 检查了哪些标准食材；
- 食材集合为何完整；
- 对应约束覆盖为何完整；
- 评估输入属于哪个候选批次或菜单。

不需要为每个未命中的笛卡尔积组合生成自然语言，但必须保存可重建该检查范围的结构化引用。

### 11.2 `EXCLUDE`证据

每个命中至少关联：

```text
participant_ref
recipe_id
constraint_ids[]
constraint_code或明确禁忌ingredient_id
matched ingredient_id
relation_id或direct_taboo
ingredient_evidence_paths[]
relation_evidence_refs[]
```

### 11.3 模型投影

健康规划模型可以读取职责需要的匿名约束摘要和命中食材；菜单决策模型只读取选择和最终校验所需内容；回答模型不读取原始指标、完整疾病事实或其他参与者身份。

工具回执和Artifact不保存模型隐藏思维过程，只保存结构化依据、调用结果和用户可见分析摘要。

### 11.4 用户可见表述

正常回答可以说明：

- 已按当前参与者健康约束检查全部候选食材；
- 最终菜单通过当前项目规则范围内的全员健康审查；
- 部分候选因与当前健康限制冲突被排除。

用户询问某道菜为何被排除时，可以说明公开菜名、公开食材名和“与当前健康约束冲突”，但不能暴露具体参与者、疾病名称、原始指标值或身份信息。

### 11.5 最终原子审计

最终成功提交必须在同一MySQL事务中保存：

```text
最终菜单与menu_hash
+ 当前参与者约束引用
+ 最终健康校验结果
+ 食材集合和路径证据
+ 命中relation_id与覆盖引用
+ 必需工具回执引用
+ completed终态
```

强制审计写入失败时整体回滚，不保留成功结果。

## 12. 依赖方向与模块边界

允许依赖：

```text
B1离线构建器 → B4固定关系Schema与质量校验
B4 HealthEvaluationService → B2健康约束公开端口
B4 HealthEvaluationService → B3菜品健康食材公开端口
健康工具适配器 → B4公开Application Service
健康规划模型 → B2/B4授权工具
菜单决策模型 → B4最终校验工具
Application提交服务 → B4审计投影
```

禁止依赖：

```text
B4 → 原始用户档案文件
B4 → 原始菜谱解析函数
B4 → Qdrant或RAG内部实现
B4 → B6营养评分
B4 → API Schema或前端类型
B4 → 模型供应商客户端
Agent模型 → B4 Repository或MySQL连接
RAG/菜单/回答 → B4内部关系表
```

B4领域服务不直接修改WorkflowState。工具回执通过工作流校验后，由State Reducer保存引用。

## 13. 异常、终态与有界修订

### 13.1 错误码

| 错误码 | 触发条件 | 处理 |
|---|---|---|
| `HEALTH_CONSTRAINT_SET_INVALID` | B2约束缺失、结构损坏、参与者不完整或通用约束缺少代码 | `failed` |
| `HEALTH_CONSTRAINT_COVERAGE_MISSING` | 当前有效代码没有覆盖记录 | `failed` |
| `HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE` | 当前有效代码覆盖未完成 | `failed` |
| `UNAPPROVED_HEALTH_RELATION` | 未批准、未激活或证据无效的关系试图参与硬筛选 | `failed` |
| `HEALTH_RELATION_DATA_INVALID` | 关系重复、悬空、代码未知或状态矛盾 | `failed` |
| `HEALTH_INGREDIENT_SET_INCOMPLETE` | B3视图遗漏可选、替代、审核组成或证据路径，或复合展开状态为`invalid` | `failed` |
| `RECIPE_NOT_ELIGIBLE` | 健康链路引用不可推荐记录 | `failed` |
| `HEALTH_EVALUATION_SCOPE_INVALID` | 请求、会话、候选批次或参与者范围不一致 | `failed` |
| `HEALTH_EVIDENCE_INVALID` | 约束、食材、关系或路径证据缺失或失效 | `failed` |
| `NUTRITION_HEALTH_BOUNDARY_VIOLATION` | 营养或份量字段试图进入硬筛选 | `failed` |
| `RAG_HEALTH_AUTHORITY_VIOLATION` | RAG结果携带或声明健康安全结论 | `failed` |
| `REQUIRED_TOOL_NOT_CALLED` | 模型提交角色产物时缺少应有健康工具回执 | `failed` |
| `FINAL_HEALTH_VALIDATION_FAILED` | 合法最终校验得到`EXCLUDE`且修订不可用或再次失败 | 受控修订一次后`failed` |
| `AUDIT_COMMIT_FAILED` | 最终结果和强制健康证据无法原子提交 | 回滚并`failed` |

### 13.2 `no_safe_menu`

只有健康工具完成全部前置校验、形成完整证据，并且当前可用候选范围的`safe_recipe_ids=[]`时，才能进入`no_safe_menu`。

关系缺失、数据库失败、工具漏调和食材不完整都不能伪装为`no_safe_menu`。

### 13.3 一次扩展召回

初次安全候选不足时，健康规划模型可以自主决定调用一次扩展召回。工作流只检查`retrieval_expansion_count`是否允许；新批次仍需模型主动调用B4。

### 13.4 最终健康内容冲突

最终校验形成证据完整的`EXCLUDE`且`health_replan_count=0`时，工作流可以把失败`plan_id`和命中证据返回健康规划节点一次：

```text
FinalValidationArtifact.status = EXCLUDE
→ health_replan_count = 1
→ 旧失败方案保留审计
→ 健康规划模型重新取得B2当前约束回执
→ 对当前候选批次重新调用evaluate_recipe_health
→ 只使用新回执中的safe_recipe_ids生成新plan_id
→ 菜单决策重新选择
→ 再次最终健康校验
```

旧`safe_recipe_ids`在回流后视为失效，不能只删除最终校验已知命中菜品后继续沿用。若重新批量审查后没有安全候选，按完整证据进入`no_safe_menu`；第二次最终校验仍为`EXCLUDE`时进入`failed`。已命中菜品不能在修订中被恢复。

### 13.5 不允许业务修订的错误

菜单哈希不一致、证据失效、覆盖不完整、权限越界、工具失败、Schema错误、漏调工具和数据库异常属于系统完整性错误，立即`failed`。这些错误不能通过重新规划菜单解决。

### 13.6 禁止的重试和兜底

- 不执行SDK自动重试；
- 不执行工具或数据库自动重试；
- 不自动切换模型；
- 不切换旧规则或旧项目结果；
- 不由工作流补调模型遗漏工具；
- 不通过删除参与者、约束、可选食材或替代成员继续；
- 不输出固定模板回答掩盖失败；
- 不允许无限返回规划节点。

## 14. 构建与初始化要求

### 14.1 构建顺序

```text
B2标准约束代码和固定约束产物通过验证
→ B3全部可推荐菜品健康食材视图通过验证
→ 派生HealthIngredientUniverse
→ 构建并审核精确食材健康关系
→ 构建约束覆盖记录
→ 执行B4 Schema和质量门禁
→ 执行B2/B3/B4跨模块契约测试
→ 生成MySQL初始化产物
→ 初始化V2固定关系表
→ 执行在线最小健康审查验证
```

### 14.2 原子构建语义

- 固定输入和旧项目产物保持只读；
- B4中间产物写入V2生成目录，不覆盖正式目录；
- 关系和覆盖必须同时通过后才能进入正式初始化产物；
- 任何`incomplete`覆盖或悬空关系使B4构建失败；
- 不能把部分新关系与旧关系混合运行；
- 修正处理逻辑后由开发者显式重新执行完整离线构建；
- 正式运行期不更新固定关系，不执行版本切换。

### 14.3 正式查询视图

在线Repository只能读取审核硬关系视图，其语义等价于：

```text
review_status = approved
AND hard_filter = true
AND effect = hard_exclude
```

待审核和拒绝关系不能因为调用方漏写过滤条件而进入健康引擎。实现时应通过专用Repository查询或数据库只读视图固化该边界。

## 15. 测试和验收标准

### 15.1 约束与覆盖

- B2允许的每个通用硬约束代码都有覆盖记录；
- 通用约束只含`constraint_code`，明确食材禁忌只含`ingredient_id`，双填或双空稳定失败；
- 50份固定档案实际形成的全部有效硬约束均覆盖完整；
- B2允许的临时疾病、过敏和指标代码均覆盖完整；
- 每个代码的审核清单键集合与健康食材全集精确相等；
- 每个代码—食材对只有一条最终审核决定；
- `hard_exclude`决定与生效关系一对一对应，`no_hard_relation`不关联关系；
- `reviewed_ingredient_count`等于健康食材全集数量且只是集合校验后的派生指标；
- 固定目录没有命中食材时允许`relation_count=0`，但仍需存在覆盖全集的批准否定决定；
- 未覆盖代码不能得到健康`PASS`；
- 疾病和异常指标共享代码时只执行一套关系并保留多来源证据。

### 15.2 关系质量

- 每条生效关系绑定合法标准`ingredient_id`和全局约束代码；
- 未批准、未激活或证据缺失关系不能命中；
- 重复、悬空和状态矛盾关系使构建失败；
- 运行时不按食材族、类别或名称继续扩展；
- 模型没有关系写权限和审核权限。

### 15.3 食材边界回归

- 虾等确认食材可以通过标准ID触发对应排除；
- “蟹味菇”不因名称包含“蟹”误判；
- “蒸鱼豉油”不因名称包含“鱼”误判；
- 可选花生命中时整道菜排除；
- 任一替代成员命中时整道菜排除；
- 食材数量未知或用量很少时，标准食材命中仍稳定排除整道菜；
- 审核复合组成成员命中可以传播到菜品；
- 未审核复合组成不由B4、名称或模型自动展开；
- `composition_expansion_status=atomic|complete`可以按各自语义评估，`invalid`稳定失败；
- 非食用材料不进入健康结果。

### 15.4 二元评估

- 合法硬命中稳定得到`EXCLUDE`；
- 食材、约束和覆盖全部完整且无命中时才得到`PASS`；
- 不存在成功`UNKNOWN`、`caution`或`prefer`状态；
- 多重命中只排除一次但保留全部证据；
- 健康结果不包含分数、严重度或营养值。

### 15.5 多人和最终校验

- 任一参与者`EXCLUDE`使菜品全员结果为`EXCLUDE`；
- 菜单规划输入只包含全员`PASS`菜品；
- 菜单决策只能校验已有`plan_id`；
- 最终校验重新加载约束和B3食材事实；
- 最终菜单哈希、参与者、菜品和证据完全一致；
- 合法最终`EXCLUDE`最多触发一次重新规划；
- 重新规划前模型重新取得B2约束回执并对当前候选批次形成新的B4健康回执；
- 新`plan_id`只使用新回执中的`safe_recipe_ids`，旧安全集合不能继续沿用；
- 哈希或证据错误不会错误进入重新规划。

### 15.6 工具与模型

- 模型主动调用健康工具，工作流不预调用；
- 角色可以在白名单和预算内决定调用时机；
- 必需健康回执缺失时Artifact不能成功；
- 工作流不会替模型补调用；
- 健康规划模型不能修改回执安全集合；
- 回答和统一审查模型不能补调前序健康工具。

### 15.7 健康与营养、RAG隔离

- 高低置信度营养值都不能触发或抵消健康命中；
- `per_serving`和烹饪损耗字段不能进入B4输入；
- RAG分数和标签不能进入关系匹配；
- 被排除菜品不能通过偏好、营养或检索分数重新进入菜单。

### 15.8 隐私与审计

- 内部评估能够追溯约束、食材、关系和路径；
- 回答和SSE不暴露具体参与者疾病、指标或身份；
- 最终结果与强制健康审计同事务提交；
- 审计失败完整回滚；
- 不保存或输出模型隐藏思维过程。

### 15.9 诊断指标而非安全门禁

质量报告可以记录每位用户排除菜品数、排除比例、剩余安全候选数、规则命中分布和`no_safe_menu`场景，但这些指标不能用于放宽审核关系。

“每位用户必须还有菜”“排除比例不得超过某阈值”不再是健康正确性的强制门禁。完整规则确实排除全部候选时，正确结果是`no_safe_menu`。

## 16. 旧实现与目标实现差异

| 维度 | 旧实现 | V2目标 |
|---|---|---|
| 约束输入 | 通用`user_tags` | B2类型化有效约束集 |
| 食材输入 | 名称列表、过敏原组、菜品标签 | B3完整标准`ingredient_id`并集 |
| 关系匹配 | 名称子串、类别推断、过敏原组和风险标签 | 审核`constraint_code → ingredient_id`精确关系 |
| 疾病处理 | 部分营养阈值或菜品标签 | 固定代码覆盖和食材关系 |
| 指标处理 | 营养阈值反向判断 | B2生成约束，B4只匹配审核食材关系 |
| 结果语义 | `exclude/caution/prefer`和分数 | `PASS/EXCLUDE`二元结果 |
| 缺失数据 | 经常被视为规则未命中 | 覆盖、食材或证据缺失直接失败 |
| 多人 | 依赖后续优化器解释 | B4直接形成全员安全交集 |
| 最终校验 | 主要复用先前结果 | 选择菜单后重新计算 |
| 工具调用 | Repository或工作流直接调用 | 模型主动调用，完成条件校验 |
| 版本 | `health_rules_v4`等运行字段 | 不建设运行时规则版本机制 |
| 验收 | 候选数量和排除比例参与通过 | 关系正确性与覆盖完整性为门禁 |

旧验证报告的`passed`只代表旧规则和旧统计门禁通过，不能证明V2疾病、异常指标、生理状态、可选替代食材和最终校验已经完整。

## 17. 迁移分类

| 旧实现 | 分类 | 处理结论 |
|---|---|---|
| `data_pipeline/health_rules.py` | `REMOVE` | 删除通用目标类型、营养阈值、`caution/prefer`和规则版本常量；由B4类型化关系构建替代 |
| `health/models.py` | `REWRITE` | 使用约束、关系、覆盖、逐人逐菜结果和工具回执模型 |
| `health/engine.py` | `REWRITE` | 删除名称、标签、营养和评分判断；实现精确ID集合匹配 |
| `health/repository.py` | `REWRITE` | 拆除档案、菜品、营养和规则混合查询；只通过B2/B3公开端口和B4关系Repository协作 |
| `health_rules`表 | `REMOVE` | 被类型化食材健康关系和覆盖表替代 |
| `ingredient_allergens`表 | `REWRITE` | 过敏关系并入统一`constraint_code → ingredient_id`关系体系 |
| 健康用途`ingredient_tags` | `REMOVE` | 不再用风险、营养或过敏标签制造硬结论 |
| 健康用途`recipe_tags` | `REMOVE` | RAG非健康标签由C1独立定义，B4不读取 |
| `CandidateEvaluation.health_rule_score` | `REMOVE` | B4不产生健康软分 |
| `caution_reasons`、`health_reasons` | `REMOVE` | 使用结构化命中、公开摘要和模块化软评分替代 |
| `validate_health_rules.py` | `REWRITE` | 从候选数量门禁改为关系、覆盖、边界和固定场景验证 |
| 旧工作流健康调用 | `REFACTOR` | 改为角色工具自主调用、回执校验和最终重新计算 |
| 旧健康单元测试 | `REWRITE` | 保留真实正反例，增加B2/B3/B4契约、多人、工具和失败测试 |

## 18. 审查后清理项

以下内容只有在B2、B3、B4、B6、C1、C2、C3、回答、API和数据库初始化全部迁移并通过验收后才能清理：

- 旧`HEALTH_RULES`、`HEALTH_RULE_VERSION`和通用规则构造函数；
- `target_type`、`target_value`、`threshold_value`、`severity`和`action`旧字段；
- `ingredient_name`、`allergen_group`、`recipe_risk_tag`和`recipe_nutrient_max/min`健康分支；
- 按名称或类别自动生成硬过敏关系的旧推断器；
- 菜品风险标签和营养标签对健康引擎的消费者；
- `health_rule_score`、`caution_reasons`、`preferred`和“未命中规则”的自然语言原因；
- 旧`ingredient_allergens`、健康标签和混合Repository查询；
- 以“所有用户仍有候选”或“排除比例上限”为通过标准的旧报告门禁；
- 已由新B4回执、Artifact和审计Schema替代且确认无消费者的兼容字段。

清理前必须执行消费者搜索、Schema契约测试、数据库初始化测试和端到端最终健康校验。不得为了简化旧代码先删除仍被在线链路读取的字段。

## 19. 与全局流程和后续模块的关系

```text
B2参与者有效健康约束
+ B3菜品完整健康食材视图
+ B4审核食材健康关系和覆盖
→ 模型主动调用B4完成RAG候选健康审查
→ 全员PASS菜品进入B5/B6/C2
→ C2生成多个已有plan_id
→ 菜单决策模型主动调用B4最终校验
→ PASS后进入回答和统一审查
→ Application原子提交结果与健康证据
```

- C1只能提供非健康RAG候选和扩展候选；
- B5和B6只能在B4安全候选范围内提供时间与营养软信息；
- C2不能重新判断健康关系或加入未审查菜品；
- C3定义模型节点、工具白名单、State流转和循环计数，但不能复制B4匹配算法；
- C4必须把有效健康约束和当前菜单作为不可压缩核心块；
- D1和D2只能输出B4公开投影，不得重建健康结论；
- D3必须覆盖离线关系门禁、运行评估、最终校验、模型工具和原子审计。

B4是健康硬结论的唯一领域权威，但不是开放医疗知识系统、用户档案模块、菜品目录、营养系统、RAG、菜单生成器或回答模型。后续模块可以细化自己的实现，不能重新引入名称子串、营养阈值、风险标签、模型自由判断、规则版本切换或失败兜底来改变B4结论。
