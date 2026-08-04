# V2菜品与食材处理模块设计

- 状态：`IN_REVIEW`
- 日期：2026-08-04
- 适用项目：`program_v2`
- 上游依据：[系统总体设计](../00-system-overview.md)、[全局不变量](../contracts/global-invariants.md)、[模块边界](../contracts/module-boundaries.md)、[数据工程模块](01-data-engineering.md)、[用户健康档案模块](02-user-health-profile.md)

## 1. 模块目的

菜品与食材处理模块把B1清洗后的2,000条固定菜谱记录转换成可信、只读、可复用的菜品目录、标准食材和菜品—食材事实。它负责回答“这条记录是什么”“某道菜明确列出了哪些食材”“某段食材文本对应哪个标准食材”“该记录能否进入推荐目录”，不回答“该食材是否适合某位用户”。

B3位于所有下游菜品消费者之前。C1混合RAG通过它获得可推荐菜品视图，B4通过它获得完整食材集合，B5通过它绑定步骤食材，B6通过它获得营养计算输入，回答模块通过它读取最终菜品的公开事实。所有消费者必须使用同一组稳定`recipe_id`和`ingredient_id`，不能各自重新解析原始菜谱。

B1定义整条离线数据工程如何编排、校验和停止；B3定义菜品及食材事实的具体领域模型、确定性处理规则、目录资格和公开查询边界。

## 2. 已确认前提

- 原始菜谱固定为项目内2,000条记录，B3不负责获取或扩展现实世界数据。
- 2,000条原始记录全部保留稳定`recipe_id`、原始顺序和原始文本；不具备推荐资格不等于删除。
- 同名菜品保留为不同变体，不按名称合并。
- 独立菜品、准备项、组合菜单、料理程序和测试记录必须分开建模。
- 标准食材保留具体身份，同时关联食材族、类别和形态属性，不能为了健康匹配把具体品种全部压成宽泛名称。
- 只有真正同义表达进入别名表；切法、温度、干湿状态和预处理方式属于形态属性。
- 可选和替代食材必须分别解析为真实标准食材；B4健康审查对其并集执行硬筛选。
- 系统不通过删除可选食材或自动选择安全替代项挽救菜品；将来若支持替换，必须产生独立、可完整审查的菜品变体。
- 准备项不可直接推荐，但经过确认后可以作为复合食材的配方来源。
- 复合食材只能沿审核通过且完整的组成关系展开；名称、模型、步骤和现实常识不能自动生成组成关系。
- 食材身份不明确会阻止菜品进入推荐目录；数量未知只保留`null`，不阻止健康食材审查。
- 非食用材料保留给步骤模块，但不进入健康食材集合或营养输入。
- B3只提供严格、权威的食材身份解析。查询理解和RAG中的模糊语义召回属于C1，不是B3解析器的另一种模式。
- 当前系统不处理个人份量、人数份量、采购量、烹饪营养损耗或食材替换。
- 固定菜品目录运行时只读，不建设运行时数据发布、版本切换或多版本目录。

## 3. 职责

B3负责：

1. 接收B1验证通过的固定菜谱清洗产物；
2. 保留稳定`recipe_id`、原始行引用、原始菜名、原始食材、原始步骤和原始标签；
3. 将记录分类为独立菜品、准备项、组合菜单、料理程序或测试记录；
4. 保留同名菜品的独立变体身份；
5. 拆分食材清单并保留每次食材出现的原始顺序和分组；
6. 分离食材身份、数量、单位、形态、可食角色和选择关系；
7. 把食材解析到稳定的标准`ingredient_id`；
8. 维护标准食材、食材族、类别和审核别名；
9. 建模必需、可选和替代食材以及替代组；
10. 标记非食用材料并防止其进入健康和营养视图；
11. 建立经过确认的准备项产物和复合食材组成关系；
12. 根据记录类型和数据质量确定性派生目录资格；
13. 为B2、B4、B5、B6、C1、菜单规划和回答模块提供职责受限的只读视图；
14. 对身份、别名、选择组、组成关系、跨表引用和消费者视图执行质量门禁；
15. 生成菜品与食材处理质量报告和旧实现迁移清单。

## 4. 非职责

B3不负责：

- 读取用户健康档案或参与者约束；
- 判断食材是否触碰疾病、过敏、异常指标或其他健康约束；
- 生成菜品健康`PASS`、排除结论或多人安全交集；
- 维护食材—健康约束关系；
- 执行RAG词法检索、向量检索、融合或重排序；
- 用模糊匹配、向量相似度或模型猜测创建权威食材身份；
- 根据菜名、步骤或标签推断未列出的食材；
- 自动生成复合调味品配方；
- 选择替代食材、删除可选食材或修改菜谱；
- 计算菜品营养分、健康红线、制作时间或菜单调度；
- 计算`per_serving`、个人摄入、人数份量、采购量或烹饪损耗；
- 写入WorkflowState、会话记忆、最终菜单或健康审计；
- 向模型暴露Repository、MySQL连接或目录写权限；
- 在固定目录损坏时通过默认菜品、模型补全或降级数据继续在线工作。

## 5. 上游输入

### 5.1 固定菜谱清洗产物

B3只接收B1通过Schema和质量校验的清洗记录，至少包含：

```text
source_row_number
recipe_id
name_raw
ingredients_raw
steps_raw
labels_raw
source_quality_refs[]
```

`recipe_id`以原始记录位置形成稳定映射。清洗、重新排序或消费者变化不能重新编号。B3不直接打开原始CSV，不在在线请求中解析原始文件。

### 5.2 受控构建配置

```text
recipe_record_classification_registry
ingredient_registry
ingredient_family_registry
ingredient_category_registry
ingredient_alias_registry
ingredient_form_rules
non_edible_occurrence_rules
preparation_output_registry
ingredient_composition_registry
```

这些配置属于项目内固定、可审查的构建资产。它们可以在V2开发期间修正并触发全量重建，但不能由模型或在线用户请求写入。

### 5.3 在线只读查询输入

在线接口只接收稳定ID、审核别名或明确的食材文本候选：

```text
recipe_ids[]
ingredient_id
name_or_alias
consumer_role
```

`consumer_role`由调用端口或工作流权限确定，不由模型自由填写以扩大视图。

## 6. 下游输出

### 6.1 权威固定事实

- 菜品记录与记录类型；
- 菜品同名变体关系；
- 标准食材、食材族、类别和显示名称；
- 审核通过的唯一别名；
- 菜品食材出现记录；
- 必需、可选和替代食材关系；
- 食材形态和非食用角色；
- 准备项产物关系；
- 审核通过的复合食材组成关系；
- 目录资格和质量问题。

### 6.2 消费者视图

- `RecipeRetrievalBuildView`：供C1构建RAG文档；
- `RecipeHealthIngredientView`：供B4健康审查；
- `RecipeStepBindingView`：供B5步骤关联；
- `RecipeNutritionInputView`：供B6营养软评分；
- `PublicRecipeView`：供已验证菜单的回答和前端投影；
- `IngredientResolutionResult`：供B2或其他确定性调用方完成严格身份解析。

### 6.3 质量产物

```text
recipe_catalog_quality_report
ingredient_identity_quality_report
alias_conflict_report
choice_group_quality_report
composition_quality_report
consumer_projection_quality_report
```

B3不输出用户、参与者、健康约束、健康评估、营养评分、菜单或工作流Artifact。

## 7. 数据模型

### 7.1 菜品记录

`RecipeRecord`至少包含：

```text
recipe_id
source_row_number
name
record_type: dish | preparation | meal_bundle | cooking_program | test_record
variant_group_key
variant_index
variant_count
ingredients_raw
steps_raw
labels_raw
identity_status
ingredient_integrity_status
required_fact_status
catalog_eligibility
eligibility_reasons[]
source_refs[]
```

`variant_group_key`只用于表达同名或明确同组记录，不替代`recipe_id`。同名记录的食材和步骤不同也不能互相覆盖。

### 7.2 记录类型

| 类型 | 含义 | 可直接推荐 | 其他用途 |
|---|---|---:|---|
| `dish` | 可以作为菜单中独立菜品的记录 | 通过质量门禁后可以 | RAG、B4、B5、B6、回答 |
| `preparation` | 酱料、馅料、面团、碎料等准备项 | 否 | 经确认后作为复合食材配方来源 |
| `meal_bundle` | 原数据中已经组合的套餐或同烹合集 | 否 | 原始追溯，不拆成系统菜单 |
| `cooking_program` | 料理程序、设备操作或工艺记录 | 否 | 原始追溯或B5参考 |
| `test_record` | 测试和设备验证数据 | 否 | 数据审计 |

记录类型由离线确定性规则和显式审核覆盖共同形成。模型输出、RAG标签和在线请求不能改变记录类型。

### 7.3 标准食材、食材族和类别

`StandardIngredient`至少包含：

```text
ingredient_id
canonical_key
display_name
family_id | null
category_id | null
identity_status: active | quarantined
source_refs[]
```

身份分层遵循：

```text
具体标准食材 → 食材族 → 食材类别
```

例如：

```text
基围虾 → 虾类 → 水产
小龙虾 → 虾类 → 水产
低筋面粉 → 小麦粉类 → 烘焙原料
马苏里拉芝士 → 奶酪类 → 乳制品
```

“同属一个健康或营养类别”不能成为合并标准食材的理由。食材族和类别可以帮助下游离线展开审核规则，但健康硬关系最终仍必须绑定明确`ingredient_id`。

### 7.4 审核别名

`IngredientAlias`至少包含：

```text
alias_id
alias_normalized
alias_display
ingredient_id
alias_type: synonym | regional_name
review_status: approved | rejected
source_ref
```

别名规范化后必须全局唯一。切丝、切片、切丁、冷冻、泡发、去皮等不是别名；它们在解析食材身份前分离为形态属性。`IngredientIdentityResolver`只使用标准名称和`approved`别名。

### 7.5 菜品食材出现记录

`RecipeIngredientOccurrence`表示某个食材在某条菜谱中的一次出现，至少包含：

```text
occurrence_id
recipe_id
source_order
group_name | null
raw_text
raw_name
ingredient_id | null
relation_type: required | optional | alternative
choice_group_id | null
form_attributes[]
amount_text | null
amount_min | null
amount_max | null
unit | null
amount_status: exact | range | qualitative | missing
identity_status: resolved | ambiguous | unresolved
classification_status: classified | other
consumption_role: edible | non_edible
source_ref
```

同一道菜多次出现同一`ingredient_id`时保留多条Occurrence，以保存分组、步骤顺序和原始数量。面向B4的视图可以按食材ID去重，但不得删除证据路径。

### 7.6 可选和替代食材

`IngredientChoiceGroup`至少包含：

```text
choice_group_id
recipe_id
group_type: alternative
member_occurrence_ids[]
source_ref
```

可选食材使用单条`relation_type=optional`；替代食材拆成多条Occurrence并由同一ChoiceGroup连接。健康视图始终返回`required ∪ optional ∪ alternative_members`，不替菜品选择某个安全分支。

替代组至少包含两个已解析食材，成员不能同时标记为`required`。无法可靠拆分的替代表达使对应菜品的`ingredient_integrity_status`失败。

### 7.7 形态与数量

`form_attributes`保存切法、干湿状态、温度状态和预处理，例如：

```text
cut: sliced | shredded | diced | minced
state: fresh | frozen | dried | soaked | cooked
preparation: peeled | pitted | thawed | washed
```

形态属性不创建新的标准食材。数量解析只保存原始事实和可直接确认的范围，不估算个人份量、人数份量或采购量。数量未知不会降低食材身份可信度。

### 7.8 非食用材料

非食用角色按Occurrence判断，避免把同一名称在所有场景中永久压成不可食。`consumption_role=non_edible`的记录：

- 保留在菜谱原始事实和B5步骤视图；
- 不进入B4健康食材集合；
- 不进入B6营养输入；
- 不作为RAG“主要食材”字段；
- 不能因被忽略而删除原始证据。

### 7.9 准备项产物与复合组成

`PreparationOutput`至少包含：

```text
preparation_recipe_id
output_ingredient_id
review_status: approved | rejected
evidence_ref
```

`IngredientCompositionSet`表示一个父食材配方的整体完整性，至少包含：

```text
composition_set_id
parent_ingredient_id
composition_status: verified | partial | unresolved
evidence_ref
```

`IngredientCompositionMember`表示配方中的一条组成边，至少包含：

```text
composition_set_id
child_ingredient_id
review_status: approved | rejected
evidence_ref
```

只有CompositionSet整体`composition_status=verified`且全部成员`review_status=approved`时，下游才可以把组成视为完整展开。`partial`和`unresolved`只用于诊断，不能被当成完整配方。组成图必须无环。

组成关系只表达“包含”，不负责组成比例、营养分摊、份量或采购量。没有确认组成关系的酱料、调味品和半成品保持原子标准食材，B4仍可以使用该食材自身已有的审核健康关系。

### 7.10 目录资格

`catalog_eligibility`由记录类型和质量状态确定性派生：

```text
record_type = dish
AND identity_status = resolved
AND ingredient_integrity_status = passed
AND required_fact_status = passed
→ eligible
```

其他结果至少区分：

```text
ineligible_record_type
ineligible_identity
ineligible_ingredient_integrity
ineligible_required_fact
```

可以在查询视图中输出`recommendable_as_dish`派生布尔值以方便消费者，但它不能成为第二个可独立写入的权威字段。

## 8. 数据所有权与存储

### 8.1 建议固定事实表

```text
recipes
ingredients
ingredient_families
ingredient_categories
ingredient_aliases
recipe_ingredient_occurrences
ingredient_choice_groups
preparation_outputs
ingredient_composition_sets
ingredient_composition_members
recipe_quality_issues
```

B1离线构建负责生成初始化数据并执行门禁；B3拥有这些事实的领域语义和运行时只读Repository接口。在线模块不能取得写入这些表的Repository实现。

### 8.2 稳定ID

- `recipe_id`来自固定原始记录映射；
- `ingredient_id`来自独立标准食材注册表；
- 不能继续按食材显示名称排序后临时编号；
- 显示名称调整和新增别名不改变既有`ingredient_id`；
- `occurrence_id`由`recipe_id`和原始出现顺序稳定生成；
- ChoiceGroup和组成关系引用稳定ID，不复制名称作为身份。

V2不建设运行时目录版本、蓝绿数据发布或多版本兼容查询。开发期间处理逻辑变化时执行全量重建和完整质量校验。

V2首次迁移可以根据固定数据生成注册表候选，但必须经过身份冲突、过度合并和过度拆分审查后固化为显式注册表。后续全量重建读取该注册表，不根据当次排序重新分配ID。

### 8.3 Repository边界

Repository返回B3领域模型或消费者查询DTO，不返回API Schema、RAG内部对象、健康评估模型或WorkflowState。基础设施适配器只能实现查询端口，不能决定目录资格或重新解析食材。

## 9. 核心离线处理流程

```text
B1清洗菜谱记录
→ 保留原始字段和稳定recipe_id
→ 记录类型分类
→ 食材清单分段和顺序保留
→ 分离分组、数量、单位、形态和选择表达
→ 严格解析标准ingredient_id
→ 建立Occurrence和ChoiceGroup
→ 标记非食用角色
→ 关联食材族、类别和审核别名
→ 校验准备项产物和复合组成
→ 派生目录资格
→ 生成消费者视图
→ 执行跨视图质量门禁
→ 生成MySQL初始化数据和B3质量报告
```

任一强制门禁失败时，不输出可供在线使用的部分目录，也不允许C1继续构建Qdrant索引。

## 10. 确定性处理规则

### 10.1 记录分类

记录分类优先使用显式审核注册表，其次使用可审查的确定性规则。分类依据可以包括菜名结构、原始记录内容和已确认清单，但不能使用运行时模型结论。

当前旧实现得到1,935条`recipe`、29条`preparation`、26条`meal_bundle`、8条`cooking_program`和2条`test_record`，这些数字只是迁移审计基线。V2重新处理后的差异必须在质量报告中逐条解释，不能为了保持旧计数而保留错误分类。

### 10.2 食材清单拆分

- 先按原始明确分隔符拆分Occurrence；
- 保存主料、辅料、调料及A/B/C料等分组；
- 识别括号中的数量、形态和可选说明；
- 分数、范围、家庭单位和直接质量单位分别解析；
- `或`、明确斜线替代和其他选择表达拆成ChoiceGroup；
- 数量中的`1/2`等斜线不能误判为替代分隔符；
- 无法确定选择结构时标记完整性失败，不生成虚假标准食材。

### 10.3 食材身份规范化

处理顺序为：

```text
去除分组前缀
→ 分离明确数量和单位
→ 分离括号说明
→ 分离形态属性
→ 标准名称精确匹配
→ approved别名精确匹配
→ 唯一ingredient_id或失败
```

允许的合并包括真正同义表达，例如“西红柿→番茄”“马铃薯→土豆”。不允许把“基围虾、小龙虾、对虾”全部合并为“虾”，也不允许把“低筋、高筋、中筋面粉”全部合并为“小麦粉”。共同语义通过食材族表达。

### 10.4 严格身份解析

`IngredientIdentityResolver`只有一套权威语义：

```text
canonical exact match
OR approved alias exact match
→ exactly one ingredient_id
```

没有匹配返回`not_found`，多个候选返回`ambiguous`。它不提供拼写相似、子串包含、词根、向量相似或模型补全。C1可以在自己的非权威检索流程中理解模糊表达，但不能把检索候选写回B3或用于健康硬关系。

### 10.5 可选、替代和复合食材健康并集

B3不执行健康判断，但必须为B4提供确定性食材闭包：

```text
required occurrences
∪ optional occurrences
∪ all alternative members
∪ verified composition children
```

每条闭包结果保留从`recipe_id`到Occurrence、ChoiceGroup或Composition边的证据路径。模型、菜单规划和回答不能通过删除某条路径改变闭包。

### 10.6 原始标签与检索标签

原始标签原样保存在固定事实中，但不能直接进入RAG。B3只向C1提供审核后的非健康字段候选，例如餐次、口味、菜系、烹饪方式、菜品类型、温度、口感和场景。

疾病、过敏、特殊人群健康状态、医疗效果、健康风险标签、营养红线和内部营养值不进入`RecipeRetrievalBuildView`。C1负责最终RAG文档Schema和完整混合检索实现。

## 11. 消费者视图

### 11.1 B2严格解析视图

`IngredientResolutionResult`：

```text
status: resolved | not_found | ambiguous
ingredient_id | null
canonical_name | null
matched_alias_id | null
candidate_refs[]
```

B2只接受`resolved`结果生成明确食材临时约束；其他结果按B2契约进入澄清或失败。

### 11.2 B4健康食材视图

`RecipeHealthIngredientView`至少包含：

```text
recipe_id
catalog_eligibility
ingredient_ids[]
ingredient_evidence_paths[]
unresolved_occurrence_count
composition_expansion_status
```

该视图包含必需、可选、替代和审核复合组成的并集，不含营养值、RAG分数或用户信息。B4据此关联审核健康关系并生成评估证据。

### 11.3 B5步骤绑定视图

包含原始步骤、食材Occurrence顺序、标准显示名、审核别名和形态属性。B5可以使用标准ID、审核别名和最长精确匹配建立步骤引用，但不能修改B3食材事实或用宽泛子串制造关键步骤缺失。

### 11.4 B6营养输入视图

只包含`consumption_role=edible`的食材ID、原始数量、范围、单位和数量状态。B6决定营养参考匹配、受控估算和软评分；B3不生成营养值或置信度。

### 11.5 C1检索构建视图

只包含`catalog_eligibility=eligible`的独立菜品，以及：

- `recipe_id`和菜名；
- 标准食材显示名；
- 审核后的非健康检索字段候选；
- 原始步骤摘要输入和时间引用；
- 构建RAG文档所需的只读事实引用。

它不包含过敏原、疾病、健康`PASS`、健康风险标签、内部营养值或参与者信息。

### 11.6 公开菜品视图

只为最终已验证菜单提供菜名、原始食材表达、明确的可选/替代说明、原始步骤和允许公开的非健康描述。它不允许回答模型改变配方、选择替代分支或隐藏已列食材。

## 12. 公开接口与工具边界

### 12.1 领域服务

```text
RecipeCatalogService
├── get_recipe_facts(recipe_ids)
├── get_public_recipe_view(recipe_ids)
├── get_health_ingredient_view(recipe_ids)
├── get_step_binding_view(recipe_ids)
└── get_nutrition_input_view(recipe_ids)

IngredientIdentityResolver
└── resolve_exact(name_or_alias)
```

消费者通过公开端口读取，不导入B3内部解析函数或Repository实现。

### 12.2 Agent工具

B3默认不直接向模型暴露目录工具。健康、时间、菜单和回答模型使用各自模块定义的受控工具，由这些工具在内部调用B3只读端口。这样避免模型绕过B4直接解释食材健康，也避免回答模型取得未授权的内部目录字段。

如果后续角色确实需要公开菜品事实工具，其参数只能是工作流注入或Artifact引用中的`recipe_id`，并遵守角色白名单、调用预算和回执要求；不能接收任意SQL条件或目录写入参数。

### 12.3 跨模块流转

```text
QueryPlanArtifact
→ C1按非健康需求召回recipe_id
→ B4通过B3重新加载权威食材事实
→ B4执行全员健康审查
→ B5/B6为安全菜品提供时间和营养软信息
→ C2生成多个可行菜单
→ 菜单决策选择已有plan_id并执行最终校验
→ 回答模块通过PublicRecipeView描述最终菜品
```

Qdrant元数据不能代替B3权威事实。WorkflowState保存稳定ID、Artifact和证据引用，不保存允许模型自由改写的完整菜品副本。

## 13. 构建与质量门禁

### 13.1 身份和记录完整性

- 2,000条固定记录全部存在且`recipe_id`唯一；
- 原始顺序、原始菜名、食材、步骤和标签均可追溯；
- 每条记录有明确`record_type`；
- 同名菜品变体不被合并；
- 食材注册表ID不依赖名称排序；
- 显示名称和别名调整不会重编号既有食材。

### 13.2 可推荐菜品食材完整性

- 每个`eligible`菜品至少存在一个可食Occurrence；
- 所有可食Occurrence均解析到唯一标准ID；
- 可选和替代食材全部拆分；
- 数量或单位后缀不泄漏到食材身份；
- 同一食材多次出现不会丢失原始证据；
- `identity_status!=resolved`的菜品不能进入RAG视图。

### 13.3 别名、类别和形态

- 审核别名规范化后唯一；
- “姜丝、姜片、姜末”等形成同一食材加不同形态，而不是多个实体；
- “小龙虾、基围虾、对虾”保持具体身份并共享食材族；
- “低筋、高筋、中筋面粉”保持具体身份并共享食材族；
- 类别为“其他”不自动等同于身份失败，但必须保留分类状态和质量报告；
- “海鲜菇、蟹味菇、红酒醋、素蚝油”不得因字符串包含映射到无关食材。

### 13.4 选择、非食用和组成

- 替代组成员不少于两个且全部解析；
- 分数数量中的斜线不被误判为替代；
- 非食用Occurrence不进入B4和B6视图；
- 组成关系父子ID存在、审核状态有效且无环；
- `partial`或`unresolved`组成不作为完整展开；
- 未确认复合配方不会由名称或模型补齐。

### 13.5 消费者一致性

- RAG文档的`recipe_id`集合等于B3可推荐菜品集合；
- RAG文档不包含健康关系、过敏原、风险标签和内部营养值；
- B4视图食材并集与Occurrence、ChoiceGroup和审核组成一致；
- B5步骤引用只指向当前菜品存在的Occurrence；
- B6输入只包含可食食材；
- PublicRecipeView不能增加、删除或替换食材；
- MySQL和所有构建视图不存在悬空引用。

### 13.6 构建停止条件

强制门禁失败时：

```text
停止B3构建
→ 不生成可供在线启动的部分目录
→ 不继续构建C1 RAG文档和Qdrant索引
→ 输出具体质量错误和定位信息
```

系统不调用模型修复固定数据，不自动忽略问题记录，也不通过旧目录或降级目录继续。

## 14. 异常、终态与映射

### 14.1 离线构建错误

| 错误码 | 触发条件 | 处理 |
|---|---|---|
| `RECIPE_NORMALIZATION_FAILED` | 稳定ID、记录类型或必要菜品事实无法形成 | 停止B3菜品阶段 |
| `INGREDIENT_NORMALIZATION_FAILED` | 食材清单无法形成合法Occurrence或标准身份 | 停止B3食材阶段 |
| `INGREDIENT_ALIAS_CONFLICT` | 审核别名规范化后指向多个食材 | 停止 |
| `INGREDIENT_CHOICE_GROUP_INVALID` | 替代组成员、关系或来源不合法 | 停止 |
| `INGREDIENT_COMPOSITION_INVALID` | 组成未审核、引用不存在、被误当完整或存在循环 | 停止 |
| `CROSS_DOMAIN_REFERENCE_FAILED` | 菜品、食材或消费者视图存在悬空引用 | 停止 |
| `DATA_QUALITY_GATE_FAILED` | 任一强制质量门禁未通过 | 不生成在线产物 |

### 14.2 在线只读错误

| 错误码 | 触发条件 | 处理 |
|---|---|---|
| `RECIPE_NOT_FOUND` | 请求引用不存在的`recipe_id` | `failed` |
| `RECIPE_NOT_ELIGIBLE` | 在线推荐链路引用不可推荐记录 | `failed` |
| `INGREDIENT_IDENTITY_NOT_FOUND` | 用户食材文本没有标准名或审核别名 | 由调用模块决定澄清或失败 |
| `INGREDIENT_IDENTITY_AMBIGUOUS` | 用户食材文本存在多个合法候选 | 由B2或查询理解流程进入`needs_clarification` |
| `CATALOG_DATA_INVALID` | 运行时读取到损坏、矛盾或缺失的固定目录事实 | `failed` |

固定数据错误不能映射为`no_safe_menu`或`no_feasible_menu`。只有B4完成健康审查后没有安全菜品才是`no_safe_menu`。

Repository、存储和目录查询失败不自动重试，不切换旧数据，不调用模型补写，也不进入回答节点。

## 15. 测试和验收标准

### 15.1 菜品身份与分类

- 2,000条记录和稳定`recipe_id`完整保留；
- 同名“红烧肉”等变体保持不同ID；
- `preparation`、`meal_bundle`、`cooking_program`和`test_record`不进入RAG；
- 目录资格始终由类型和质量状态派生；
- 新旧可推荐数量差异具有逐条质量说明。

### 15.2 食材解析

- “生抽2t”“盐1/2茶匙”“红豆3两”等数量不会污染身份；
- “温水、冷水、全蛋液”等审核表达稳定解析；
- “姜丝、姜片、姜末”等分离形态属性；
- 未知数量保留`null`；
- 分类为“其他”的食材仍保留明确身份和质量状态；
- 未指明食材不能进入可推荐菜品。

### 15.3 可选与替代

- “蜜豆或芝麻”形成两个Occurrence和一个ChoiceGroup；
- “牛肩肉或牛腩”形成两个明确标准食材；
- “花椒（可选）”形成`optional`关系；
- `1/2茶匙`不形成ChoiceGroup；
- B4视图包含全部可选和替代成员；
- 模型不能通过选择安全替代项改变食材并集。

### 15.4 食材层级与误判

- 小龙虾、基围虾、对虾具有不同ID但共享食材族；
- 低筋、高筋、中筋面粉具有不同ID但共享食材族；
- 马苏里拉芝士保留具体身份并归入奶酪族；
- “海鲜菇、蟹味菇、红酒醋、素蚝油”不会被无关短词命中；
- 审核别名冲突必然使构建失败。

### 15.5 非食用与复合组成

- 荷叶、粽叶、棉线、牙签等按Occurrence角色进入步骤视图但不进入健康和营养视图；
- 准备项不直接推荐；
- 经确认的卡仕达酱等准备项可以形成复合食材组成；
- 未确认烧烤酱等不会由名称自动生成配方；
- 部分组成不能冒充完整组成；
- 循环组成在构建阶段失败。

### 15.6 跨模块与失败

- Qdrant候选必须重新通过B3加载权威食材事实；
- B2严格解析不返回模糊“最佳匹配”；
- B4视图不含RAG分数和营养值；
- B5、B6不能改写菜品事实；
- PublicRecipeView与最终菜单引用一致；
- 固定目录损坏进入`failed`，不进入健康无菜或回答兜底；
- 模型节点无法取得B3 Repository写权限。

## 16. 旧实现迁移分类

| 旧实现 | 分类 | 处理结论 |
|---|---|---|
| `data_pipeline/preprocess_recipes.py` | `REFACTOR` | 保留稳定菜品ID、原始字段和审计思路；移除缺失步骤自动补写 |
| `data_pipeline/catalog.py` | `REWRITE` | 拆分记录分类、食材身份、别名、形态、选择关系、组成和质量校验；移出健康、营养和RAG职责 |
| `data_pipeline/build_profiles.py` | `REWRITE` | 改为编排独立B3构建器，不继续调用巨型目录模块形成跨域画像 |
| `data_pipeline/derived_tags.py` | `REMOVE` | 删除营养阈值反向修改风险标签和健康含义的逻辑；非健康标签由C1独立处理 |
| `db/schema_mysql.sql`菜品食材部分 | `REWRITE` | 使用稳定食材注册表、Occurrence、ChoiceGroup、PreparationOutput和Composition新Schema |
| `data_pipeline/build_database.py`菜品食材部分 | `REWRITE` | 不按名称排序临时生成食材ID；按B3固定事实和所有权生成初始化数据 |
| `data_pipeline/build_recipe_knowledge.py` | `REFACTOR` | C1只消费B3检索构建视图；移除过敏原、健康特征、风险标签和内部营养字段 |
| `health/repository.py`菜品读取部分 | `REWRITE` | B4通过B3公开健康食材视图读取，不再混合档案、营养和菜品SQL |
| 旧RAG食材元数据 | `REFACTOR` | 保留可推荐菜品ID和非健康检索字段；删除健康结论性字段 |
| 旧数据流水线测试 | `REFACTOR` | 保留稳定ID、数量解析、别名误判、非菜品排除等案例；增加替代、复合、视图和失败测试 |

## 17. 审查后清理项

以下内容只有在B4、B5、B6、C1、菜单规划、回答和数据库初始化均迁移并通过验收后才能清理：

- `catalog.py`中的混合常量、健康规则、营养标签和RAG标签推断；
- 按食材名称排序临时分配ID的逻辑；
- 将可选或替代文本保存为单个食材实体的旧记录；
- 可独立写入并可能与记录类型冲突的`recommendable_as_dish`源字段；
- 旧`ingredient_allergens`、`ingredient_tags`和菜品风险字段在B3/RAG中的消费者；
- 缺失步骤自动补写及外部补充字段；
- RAG文档中的`allergen_types`、`health_features`、`risk_tags`和内部营养特征；
- 旧健康Repository中的菜品、营养、档案和规则混合查询；
- `per_serving`、菜品份量、采购量和烹饪损耗相关字段；
- 已被新B3事实表替代且确认无消费者的旧画像JSONL和兼容适配器。

清理前必须通过消费者搜索、跨模块契约测试和端到端健康审查验证。不能为了减少旧字段先删除仍被在线链路读取的内容。

## 18. 与全局流程和后续模块的关系

```text
B1清洗后的固定菜谱
→ B3菜品目录与标准食材事实
├── B2严格解析临时食材禁忌
├── B4按完整食材并集执行健康审查
├── B5建立步骤任务与食材引用
├── B6形成内部营养软评分输入
└── C1构建非健康RAG检索文档
    → C2只使用安全菜品生成菜单
    → C3强制工具和Artifact流转
    → D2只描述最终已验证菜品事实
```

B3是菜品事实权威源，不是健康规则引擎、检索系统、营养系统、时间系统或菜单生成器。后续模块可以细化自己的算法，但不得重新解析原始菜谱、用Qdrant元数据替代B3事实、让模型修改菜品食材、忽略可选或替代食材、用未审核复合组成制造健康结论，或在目录损坏时通过兜底继续推荐。
