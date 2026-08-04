# V2用户健康档案模块设计

- 状态：`IN_REVIEW`
- 日期：2026-08-04
- 适用项目：`program_v2`
- 上游依据：[系统总体设计](../00-system-overview.md)、[全局不变量](../contracts/global-invariants.md)、[模块边界](../contracts/module-boundaries.md)、[数据工程模块](01-data-engineering.md)

## 1. 模块目的

用户健康档案模块把数据工程生成的固定用户档案组织成类型化健康事实，并依据确定性规则形成每位参与者当前有效的健康约束。它同时负责验证对话中明确出现的临时健康信号、合并证据、控制约束作用域和生成按角色裁剪的隐私安全视图。

本模块回答“当前参与者有哪些可信事实和有效约束”，不回答“某道菜是否安全”。标准食材与健康约束如何匹配、何时排除整道菜属于`04-health-rule-engine.md`。该分界保证档案处理、食材关系和模型判断不会混成一套无法审计的逻辑。

## 2. 已确认前提

- 项目中的50份用户健康档案是确定数据，但必须经过B1清洗和标准化后才能使用。
- 固定档案在运行时只读；对话不能修改疾病、过敏、指标或其他永久健康事实。
- 缺失检测指标保持`null`，模型、规则和Repository都不能填补。
- 明确疾病事实不会因为当前指标处于正常范围而失效。
- 异常指标通过确定性阈值规则形成派生约束，模型不能判断指标是否异常。
- 同一有效约束可以保留多个证据来源，但不能重复生效。
- 健康目标和口味偏好默认是软条件，不直接等同于食材健康硬约束。
- 用户明确“不能吃”的标准食材形成临时硬约束。
- 对话可以新增临时健康约束，也可以撤销本会话中自己新增的约束，但不能删除、放宽或覆盖永久健康硬约束。
- B2按参与者分别输出约束，B4逐人审查菜品后形成全员安全交集。

## 3. 职责

用户健康档案模块负责：

1. 读取数据工程验证通过的固定用户档案；
2. 将疾病、特殊生理阶段、过敏、指标、身体事实、健康目标和偏好分开建模；
3. 对固定指标执行格式、单位和异常状态的确定性判断；
4. 根据原始事实和指标状态生成固定档案派生约束；
5. 接收模型提取的临时健康信号候选并进行确定性校验；
6. 校验参与者引用、信号类型、标准代码、数值、单位、行为和作用域；
7. 应用临时约束的新增和合法撤销；
8. 拒绝任何永久健康硬约束覆盖请求；
9. 合并永久、指标派生、会话临时和本轮临时约束；
10. 去重约束并保留全部有效证据引用；
11. 为B4、健康规划模型、回答模型和上下文模块生成不同权限的投影视图；
12. 在多人场景中保持参与者约束隔离和匿名引用。

## 4. 非职责

用户健康档案模块不负责：

- 在线修改固定用户档案；
- 根据缺失指标、年龄或其他字段推测疾病；
- 把模糊自述转成精确检测值或医疗诊断；
- 根据用户约束直接判定菜品安全；
- 建立标准食材、食材别名或菜品食材关系；
- 推断食材与疾病、过敏或异常指标之间的关系；
- 使用营养估算生成健康硬约束；
- 使用RAG、向量相似度或字符串包含生成健康事实；
- 把健康档案写入Qdrant或聊天向量记忆；
- 将完整原始健康档案投影给模型、SSE或最终回答；
- 管理会话存储、WorkflowState或最终审计事务。

## 5. 上游输入

### 5.1 固定档案输入

由B1提供并通过质量门禁：

- 稳定用户ID；
- 性别、年龄、活动水平等基础事实；
- 特殊人群或明确健康状况；
- 过敏信息；
- 血压、血糖、尿酸和血脂等结构化指标；
- 身高、体重和BMI等身体事实；
- 健康目标；
- 口味偏好；
- 缺失字段和解析质量信息；
- 原始来源引用。

### 5.2 请求输入

- 当前参与者`participant_ref`与固定`user_id`的受控映射；
- 本轮模型提取的`TemporaryHealthSignalCandidate`；
- 上下文模块保存的有效会话临时约束；
- 当前请求允许撤销的临时约束引用；
- 角色投影请求。

模型提交的临时信号只是候选输入，未通过B2确定性验证前不属于健康事实或健康约束。

## 6. 下游输出

### 6.1 参与者有效约束集

每位参与者输出独立的`ParticipantHealthConstraintSet`，包含：

- `participant_ref`；
- 有效硬约束；
- 有效软健康目标；
- 每条约束的作用域；
- 每条约束的证据引用；
- 是否存在未解决的临时信号；
- 面向下游的隐私等级。

### 6.2 角色投影视图

- B4视图：标准约束代码、硬软属性、作用域和证据引用；
- 健康规划模型视图：标准化约束摘要和健康工具回执引用；
- 查询理解模型视图：参与者引用及已存在临时信号的最小摘要；
- 回答模型与SSE视图：不含疾病名称、原始指标和参与者身份的公开说明。

B2不输出菜品ID、食材命中结果或健康`PASS`。

## 7. 数据模型

### 7.1 固定健康事实

```text
UserProfile
├── user_id
├── basic_facts
├── health_facts
├── health_metrics
├── body_facts
├── health_goals
├── taste_preferences
└── data_quality
```

`HealthFact`至少包含：

```text
fact_id
user_id
fact_type: condition | physiological_status | allergy
normalized_code
source_ref
```

`HealthMetric`至少包含：

```text
metric_id
user_id
metric_code
value | null
unit
classification: normal | abnormal | unknown
source_ref
```

`BodyFact`保存年龄、身高、体重、BMI和活动水平。这些事实可以参与后续软排序，但默认不能形成食材健康硬约束。

### 7.2 健康目标与偏好

`HealthGoal`和`TastePreference`必须与健康硬事实分开：

```text
HealthGoal
├── normalized_code
├── effect: soft_prefer
└── source_ref

TastePreference
├── normalized_code
└── source_ref
```

“控糖”“控钠”“补钙”“补铁”等目标默认只形成软目标。只有独立存在的疾病、异常指标、过敏、明确禁忌或特殊生理阶段的审核硬关系可以触发健康硬排除。

### 7.3 档案派生约束

`ProfileHealthConstraint`至少包含：

```text
constraint_id
user_id
constraint_type: condition | physiological_status | allergy | metric
normalized_code
effect: hard_exclude
scope: permanent
source_refs[]
```

固定档案中的疾病、特殊生理阶段、过敏和异常指标一旦形成健康约束，其效果只能是`hard_exclude`，不能在B2或下游被降为软条件。健康目标和口味偏好不会写入`ProfileHealthConstraint`。指标派生规则属于B2确定性配置，它只负责把指标值转换为标准约束代码；该约束对应哪些食材由B4审核关系决定。

### 7.4 临时信号候选

模型只能生成：

```text
TemporaryHealthSignalCandidate
├── participant_ref
├── action: add | remove
├── signal_type: condition | allergy | metric | explicit_food_taboo
├── normalized_code或结构化指标值
├── requested_scope: turn | session | unspecified
└── source_text
```

模型给出的归一值、作用域和信心描述均不具备权威性。B2必须重新校验后生成`TemporaryHealthConstraint`。

### 7.5 已验证临时约束

```text
TemporaryHealthConstraint
├── constraint_id
├── participant_ref
├── constraint_type
├── normalized_code或ingredient_id
├── effect: hard_exclude
├── scope: turn | session
├── source_ref
└── created_from_signal_id
```

对话中的疾病、过敏和指标默认作用于当前会话；明确“这顿不能吃”的食材禁忌默认只作用于当前请求。用户明确说明持续范围时，B2在允许范围内调整。任何对话信号都不能升级为永久档案事实。

## 8. 存储与数据所有权

### 8.1 固定数据表

建议使用：

- `user_profiles`：稳定用户ID和基础档案；
- `user_health_facts`：疾病、特殊生理阶段和过敏；
- `user_health_metrics`：类型化指标、数值、单位和分类；
- `user_body_facts`：身高、体重、BMI和活动水平；
- `user_health_goals`：标准健康目标；
- `user_preferences`：口味和普通偏好；
- `user_health_constraints`：由固定档案生成的有效约束和证据引用。

这些表由数据工程初始化，运行时归B2只读查询。旧`raw_json`不需要复制到在线档案表；原始文件和离线来源引用已经提供追溯能力，减少在线隐私暴露面。

### 8.2 临时约束

临时约束由B2创建和验证，但由上下文与记忆模块按照`turn`或`session`作用域保存。B2不直接写Redis或会话表，而是返回经过验证的约束和受控State更新意图，由工作流应用。

### 8.3 有效约束视图

有效约束集按请求构建，不另建“用户—菜品安全”或“用户—禁忌菜品”表。这样避免把档案模块与菜品目录、食材关系和B4健康结论绑定。

## 9. 核心处理流程

### 9.1 固定档案处理

```text
读取固定档案事实
→ 校验参与者与用户映射
→ 读取类型化事实和指标
→ MetricRuleEvaluator判断指标状态
→ 生成档案派生约束
→ 按标准约束代码去重
→ 合并全部来源引用
```

### 9.2 临时信号验证

```text
接收TemporaryHealthSignalCandidate
→ 校验participant_ref属于当前请求
→ 校验action和signal_type组合
→ 校验标准健康代码或结构化指标
→ 明确食材禁忌调用B3食材身份解析接口
→ 确定允许的turn/session作用域
→ 生成临时约束或撤销意图
```

无法确定参与者、指标含义、食材身份或用户真实意图时进入`needs_clarification`。模型不能用低信心结果、自由文本或最相似词代替确认。

### 9.3 合并有效约束

硬约束按并集合并：

```text
永久档案硬约束
∪ 异常指标派生硬约束
∪ 会话临时硬约束
∪ 本轮临时硬约束
```

去重键至少包括：

```text
participant_ref
constraint_type
normalized_code或ingredient_id
effect
```

重复约束只执行一次，但保留所有`source_refs`。固定疾病与异常指标产生相同约束时不得互相覆盖。

### 9.4 撤销临时约束

- `remove`必须对应当前参与者和当前会话中存在的临时`constraint_id`；
- 本轮约束在当前请求结束后自动失效；
- 会话临时约束在用户明确撤销或会话结束时失效；
- 永久约束和由永久事实派生的约束不接受`remove`；
- “忽略我的疾病”“不用考虑我的过敏”等请求返回`PERMANENT_CONSTRAINT_OVERRIDE_DENIED`并停止当前请求。

## 10. 指标处理规则

- 指标值和单位必须与固定Schema一致；
- 血压等复合指标拆分成明确字段后再分类；
- 缺失值分类为`unknown`，不形成指标派生约束；
- 异常状态由项目确定的阈值配置计算，不由模型或自然语言提示词计算；
- 疾病事实与指标分类相互独立，正常指标不能移除疾病约束；
- 指标值异常可以生成标准指标约束，即使档案中没有同名疾病标签；
- B2只生成标准约束代码，不推断禁忌食材；
- 没有B4审核食材关系的指标约束保留在约束集中，但不能凭空排除菜品。

## 11. 临时信号语义

### 11.1 用户自述与检测数值

- “我有高血压”是用户明确自述的临时健康状况；
- “血压150/95”是结构化临时指标；
- “最近可能有点高”在无法可靠归类时需要澄清；
- 模型不能把模糊表达转成精确数值或诊断；
- 原始`source_text`只进入受控验证和审计引用，不进入下游模型上下文。

### 11.2 明确食材禁忌

- “这顿不能吃花生”是本轮明确食材禁忌；
- 必须解析到标准`ingredient_id`才能形成硬约束；
- B2通过B3公开的`IngredientIdentityResolver`端口请求解析，不导入B3内部实现；
- 多个可能食材、仅有宽泛类别或别名未确认时需要澄清；
- 字符串包含和模型猜测不能激活硬约束。

### 11.3 作用域

模型可以提取用户表达的时间范围，但最终作用域由确定性规则决定：

- 疾病、过敏、指标自述默认`session`；
- 明确当前餐食禁忌默认`turn`；
- 用户明确“之后都要避开”时可使用`session`；
- 不支持由对话直接创建`permanent`作用域。

## 12. 多人、权限与隐私

- 所有事实查询先通过当前请求的`participant_ref → user_id`受控映射；
- B2不接受模型自由填写`user_id`；
- 不同参与者的约束集分别构建，不能先合并后失去归属；
- B4可以读取标准约束代码和证据引用，不读取完整原始档案；
- 健康规划模型只读取职责需要的约束摘要和工具回执；
- 查询理解模型不读取永久原始档案，只读取参与者引用和最小临时信号摘要；
- 回答模型、SSE和群组输出不接收具体参与者疾病、指标或真实身份；
- Qdrant不保存用户档案、健康约束或临时信号；
- 跨会话或跨参与者访问返回`CROSS_SESSION_ACCESS_DENIED`。

## 13. 模块组件与公开接口

### 13.1 内部组件

```text
HealthProfileRepository
MetricRuleEvaluator
TemporarySignalService
EffectiveConstraintService
HealthContextProjector
```

每个组件只承担一个职责：Repository读取固定事实；指标评估器生成指标状态；临时服务验证新增和撤销；约束服务合并和去重；投影器执行角色隐私裁剪。

### 13.2 领域接口

```text
get_profile_facts(participant_ref)
build_effective_constraints(participant_refs, temporary_signals)
validate_temporary_signal(signal_candidate)
remove_temporary_constraint(constraint_id)
project_health_context(role, constraint_sets)
```

具体字段以实现阶段共享Schema为准，Repository不能直接返回API响应类型。

### 13.3 Agent工具

健康与菜单规划模型的必需工具：

```text
get_health_constraints(participant_refs)
```

工具只接收工作流注入的当前参与者范围，输出匿名参与者约束集、硬软类型、作用域、证据引用和未解决信号状态。缺少有效工具回执时遵守`REQUIRED_TOOL_NOT_CALLED`并停止。

## 14. 异常、终态与停止条件

| 错误码 | 触发条件 | 处理 |
|---|---|---|
| `HEALTH_PROFILE_NOT_FOUND` | 当前参与者没有固定档案 | `failed` |
| `HEALTH_PROFILE_DATA_INVALID` | 固定事实、指标或来源引用结构损坏 | `failed` |
| `HEALTH_METRIC_INVALID` | 指标格式、单位或复合字段无法验证 | `failed` |
| `HEALTH_SIGNAL_AMBIGUOUS` | 无法确定参与者、健康含义、指标或食材身份 | `needs_clarification` |
| `PARTICIPANT_SCOPE_INVALID` | 临时信号引用当前请求之外的参与者 | `failed` |
| `TEMPORARY_SIGNAL_REMOVE_INVALID` | 撤销目标不是当前会话有效临时约束 | `failed` |
| `PERMANENT_CONSTRAINT_OVERRIDE_DENIED` | 对话尝试删除、放宽或忽略永久健康硬约束 | `failed` |
| `CROSS_SESSION_ACCESS_DENIED` | 尝试读取其他会话或未授权参与者档案 | `failed` |
| `HEALTH_CONTEXT_PROJECTION_FAILED` | 角色投影包含超出权限的健康字段 | `failed` |

Repository、指标评估、临时信号校验和上下文投影失败不自动重试，不用模型猜测补齐，也不通过删除约束继续工作流。

## 15. 测试和验收标准

### 15.1 固定档案

- 50份档案均映射到稳定用户ID；
- 疾病、特殊生理阶段、过敏、指标、目标和偏好类型不混用；
- 缺失指标保持`null`；
- 血压、孕周和数值单位严格解析；
- BMI源值与计算值校验但不自动覆盖；
- 在线表不依赖完整`raw_json`才能工作。

### 15.2 约束生成

- 明确过敏形成硬约束；
- 确定性异常指标形成对应约束；
- 正常指标不形成异常指标约束；
- 疾病不会被正常指标抵消；
- 疾病和指标生成同一约束时只生效一次并保留多条证据；
- 健康目标和口味保持软条件；
- B2输出中不存在菜品安全`PASS`。

### 15.3 临时信号

- 明确疾病、过敏和指标默认使用会话作用域；
- 当前餐食明确禁忌默认使用本轮作用域；
- 临时约束可以被同一参与者明确撤销；
- 永久约束不能被撤销、放宽或忽略；
- 模糊参与者、指标和食材身份进入澄清；
- 临时食材禁忌未得到标准`ingredient_id`前不能生效；
- 本轮约束按请求边界失效，会话约束按会话边界失效。

### 15.4 多人与隐私

- 每位参与者形成独立约束集；
- 工作流注入的参与者范围不能被模型扩大；
- B4视图不包含无关原始档案字段；
- 查询理解、回答和SSE视图不泄露其他参与者疾病和指标；
- Qdrant不存在用户健康字段；
- 跨会话读取稳定返回`CROSS_SESSION_ACCESS_DENIED`。

### 15.5 跨模块契约

- B1产物能够完整初始化B2固定表；
- B3能够通过公开端口解析临时食材禁忌；
- B4只通过`ParticipantHealthConstraintSet`读取健康约束；
- 上下文模块只保存B2已经验证的临时约束；
- Agent工具漏调不会生成成功健康规划Artifact。

## 16. 旧实现迁移分类

| 旧实现 | 分类 | 处理结论 |
|---|---|---|
| `data_pipeline/preprocess_users.py` | `REFACTOR` | 保留严格字段解析、稳定ID、BMI校验和缺失值不填补；输出改为类型化事实 |
| `user_profiles`旧表 | `REFACTOR` | 保留基础字段；移出通用标签、完整`raw_json`和数据版本字段 |
| `user_tags` | `REWRITE` | 拆为健康事实、健康目标和偏好等类型化表 |
| `repositories/profile_repository.py` | `REWRITE` | Repository返回领域模型，解除对`api.schemas`的反向依赖 |
| `TemporaryHealthSignal` | `REWRITE` | 拆成模型候选信号和B2验证后的临时约束 |
| `health/normalizer.py` | `REFACTOR` | 保留受控词表思想，增加参与者、指标、食材身份、行为和作用域校验 |
| `QueryIntent.UPDATE_PROFILE` | `REMOVE` | 对话不修改固定健康档案；相关表达转为临时信号候选 |
| `context/builder.py`健康部分 | `REFACTOR` | 只投影最小临时信号摘要，不暴露原始档案和`source_text` |
| `health/repository.py`档案读取部分 | `REWRITE` | 用户事实与约束构建迁入B2；菜品读取和健康匹配留给B4 |
| `health/engine.py`营养阈值部分 | `REMOVE` | 营养估算不能形成健康硬筛选，后续由B6执行软评分 |
| RAG健康元数据和健康过滤 | `REMOVE` | RAG不读取健康档案，不判定健康安全 |

## 17. 审查后清理项

以下内容在对应消费者迁移并通过测试后清理：

- 通用`user_tags`读写逻辑；
- `profile_version`和数据发布相关档案字段；
- 在线`user_profiles.raw_json`及其API暴露；
- `UPDATE_PROFILE`意图、提示词和工作流分支；
- 模型可自由填写并直接生效的临时健康字段；
- Repository对API Schema的反向依赖；
- RAG中读取健康档案、过敏字段或健康风险标签的逻辑；
- 旧健康Repository中同时读取档案、菜品、营养和规则的混合查询。

清理前必须确认B2、B3、B4、上下文模块和API均已迁移到新契约，不能先删后补。

## 18. 与全局流程和后续模块的关系

```text
B1固定档案产物
→ B2类型化事实与有效约束
→ B4食材健康审查
→ C2安全候选菜单规划
→ C3最终健康校验与Agent工作流
```

- `03-recipe-ingredient-processing.md`提供临时明确食材禁忌的标准身份解析；
- `04-health-rule-engine.md`消费B2约束并判断标准食材是否命中；
- `09-agent-workflow.md`注入参与者范围并强制`get_health_constraints`工具回执；
- `10-memory-and-context.md`保存已验证的临时约束并执行角色投影；
- `11-api-and-sse.md`和`12-answer-and-frontend.md`只能消费隐私安全公开视图。

后续模块可以细化实现，但不得让模型、RAG、营养估算或对话覆盖永久健康硬约束，也不得把B2重新变成菜品安全判定模块。
