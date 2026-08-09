# V2推荐请求生命周期

- 状态：`APPROVED`
- 日期：2026-08-09
- 适用范围：WorkflowState、模型节点、工具、API/SSE和最终提交

## 1. 文档目的

本文从跨模块场景描述一次推荐请求如何创建、流转、修订、失败、取消和提交。它不定义完整字段Schema，而是固定各角色的先后关系、进入下一节点的条件和所有业务循环的上限。

生命周期必须遵守[系统总体设计](../00-system-overview.md)、[全局不变量](../contracts/global-invariants.md)和[模块边界](../contracts/module-boundaries.md)。

## 2. 请求状态

```mermaid
stateDiagram-v2
    [*] --> accepted
    accepted --> running: worker成功认领
    running --> revising: 允许的定向修订
    revising --> running: 修订产物通过校验
    running --> completed: 最终校验、统一审查和提交通过
    running --> needs_clarification: 必要信息无法安全推断
    running --> no_safe_menu: 无健康安全菜品
    running --> no_feasible_menu: 安全菜品无法组成可行菜单
    running --> strict_time_indeterminate: 硬时限缺少高权威可行性证据
    running --> failed: 工具、权限、证据或完整性失败
    revising --> failed: 修订上限耗尽或再次失败
    revising --> no_safe_menu: 回流后重新审查无安全候选
    revising --> no_feasible_menu: 回流后安全候选无法组成菜单
    accepted --> cancelled: 用户在执行前取消
    running --> cancelled: 节点边界确认取消
    revising --> cancelled: 节点边界确认取消
    running --> interrupted: 执行环境终止且无法恢复
```

`accepted`、`running`和`revising`是非终态。其余状态均为终态，终态结果不可被同一`request_id`重新执行或覆盖。

## 3. 正常推荐链路

```mermaid
sequenceDiagram
    actor Client as 客户端
    participant API as API/SSE
    participant WF as Workflow
    participant B2 as 健康档案服务
    participant CTX as Context Service
    participant Q as 查询理解模型
    participant RAG as 混合RAG
    participant HP as 健康与菜单规划模型
    participant HT as 健康/菜单工具
    participant MD as 菜单决策模型
    participant ANS as 回答模型
    participant REV as 统一审查模型
    participant DB as MySQL/Redis
    participant OUT as Outbox Dispatcher

    Client->>API: POST recommendation-request + idempotency key
    API->>DB: 创建accepted请求
    API-->>Client: request_id
    WF->>DB: 唯一Worker Claim
    WF->>B2: 解析参与者并加载完整有效约束
    B2-->>WF: 匿名约束投影 + 证据引用
    WF->>CTX: 构建SharedWorkflowContext
    CTX-->>WF: ContextManifest + 角色投影
    WF->>Q: 当前消息和有效约束
    Q->>RAG: retrieve_recipes(QueryPlan)
    RAG-->>Q: 候选recipe_id和检索证据
    Q->>WF: QueryPlanArtifact + RetrievalResult引用
    WF->>HP: QueryPlan + RetrievalResult + 参与者引用
    HP->>HT: get_health_constraints
    HT-->>HP: 标准化全员健康约束
    HP->>HT: evaluate_recipe_health
    HT-->>HP: 不可改写的健康评估结果和证据回执
    HP->>HT: generate_feasible_menus
    HT-->>HP: 3至5个可行plan_id
    HP->>WF: HealthEvaluationArtifact + FeasibleMenuArtifact
    WF->>MD: 可行菜单和评分分解
    MD->>HT: validate_selected_menu_health(plan_id)
    HT-->>MD: FinalValidationArtifact
    MD->>WF: MenuDecisionArtifact + 校验引用
    WF->>ANS: 已选择且校验通过的菜单事实
    ANS->>WF: AnswerArtifact
    WF->>REV: 执行证据 + AnswerArtifact
    REV->>WF: ReviewArtifact(PASS)
    WF->>DB: 原子提交结果、健康证据、会话事实、outbox和completed
    DB-->>WF: committed
    OUT->>DB: 读取未发布outbox
    OUT->>API: answer_ready / result_committed
    API-->>Client: SSE已提交最终结果
```

正常链路的强制条件是：

1. QueryPlanArtifact通过Schema和约束合并校验；
2. 查询理解模型主动调用初次RAG工具，RAG完成词法、向量、融合和重排序且不包含健康结论；
3. 健康规划模型自主发起角色所需工具调用并形成全部有效回执；
4. FeasibleMenuArtifact只包含全员健康交集中的菜品；
5. 菜单决策模型选择已有`plan_id`并完成最终健康校验；
6. AnswerArtifact完全基于所选菜单和证据；
7. ReviewArtifact为`PASS`；
8. 最终结果、强制健康审计、会话事实和成功 outbox 在同一事务提交；两个最终成功事件都在提交后发布。

阶段性`analysis_ready`事件可以在对应Artifact校验后发布。最终回答正文在`FinalValidationArtifact=PASS`、`ReviewArtifact=PASS`和结果事务提交之前不得发布。

## 4. 健康冲突与菜单不可用

### 4.1 部分菜品健康冲突

```text
RAG候选菜品
→ 加载必需、可选、全部替代成员和审核复合组成的标准食材并集
→ 与所有参与者有效健康约束逐一匹配
→ 命中审核硬关系的菜品整道排除
→ 剩余安全菜品进入菜单规划
```

排除证据保存在健康工具回执并由`HealthEvaluationArtifact`引用，包含`recipe_id`、`ingredient_id`、参与者匿名引用、`constraint_code`、`relation_id`、覆盖引用和食材证据路径。被排除菜品不能通过偏好、营养、检索分数或模型判断重新进入当前候选菜单。

通用健康`PASS`还要求当前代码已经针对所有可推荐菜品健康视图中的标准食材并集完成离线覆盖。没有关系记录只有在覆盖明确`complete`时才表示未命中；覆盖缺失或不完整进入`failed`。

如果菜品食材集合缺少可选食材、任一替代成员、审核通过的复合组成或对应证据路径，返回`HEALTH_INGREDIENT_SET_INCOMPLETE`并进入`failed`。工作流不能把不完整集合当作健康未命中，也不能让模型选择一个看似安全的配方分支继续。

### 4.2 无健康安全菜品

如果全部召回候选均命中健康硬约束，并且允许的一次扩展召回已经使用、无法产生新候选批次，或模型基于当前任务边界判断没有合法扩展动作：

```text
safe_recipe_ids = []
→ health_evidence完整
→ 终态no_safe_menu
```

`no_safe_menu`表示没有可用于规划的安全菜品，不等于工具失败、数据库失败或模型漏调工具。

### 4.3 有安全菜品但无法组成菜单

如果存在安全菜品，但无法满足明确菜数、菜单结构、锁定菜品、严格时间、仅现有食材或其他硬条件：

```text
safe_recipe_ids非空
且 feasible_plan_ids = []
→ 终态no_feasible_menu
```

健康安全与菜单可行性必须分别记录，不能把两种终态合并为“没有推荐结果”。

### 4.4 严格时间无法判定

当请求明确要求硬截止时间，所有候选都没有高权威 `strict_time_feasible=true`，且至少存在一个 `unknown` 时：

```text
strict_time_feasibility_required = true
且 true 候选为空
且 unknown 候选非空
→ 终态 strict_time_indeterminate
```

系统必须说明缺少哪些步骤时长、依赖或设备证据；不得用 LLM 估算、软排序或减少菜数冒充满足截止时间。如果全部高权威候选均为 `false`，则进入 `no_feasible_menu`。

### 4.5 needs_clarification 终态

当 B2 临时信号模糊（例如"不要吃辣的"无法确定是疾病、过敏、健康目标还是当前餐食禁忌），且查询理解无法解析参与者或意图时，工作流进入 `needs_clarification`：

```text
模糊信号或意图无法解析
→ C4.record_session_event(status=needs_clarification)
→ 会话保持活跃，不提交菜单，不清除已积累的约束
→ 通过 SSE 发出 clarification_needed 事件（含待澄清问题）
```

用户提供新消息（创建新的 `request_id`）后，待澄清信号作为新请求输入被重新解析；原请求保持 `needs_clarification` 终态，不自动重跑、不自动沿用未确认的临时约束。

## 5. 一次扩展召回

初次召回只有在安全候选或菜单规划输入不足时，健康与菜单规划模型才可以调用一次`expand_retrieval`：

扩展召回最多一次；是否允许调用由WorkflowState中的`retrieval_expansion_count`决定。

```mermaid
flowchart TD
    A["初次候选不足"] --> B{"retrieval_expansion_count = 0?"}
    B -- 否 --> E["根据现有证据进入对应终态"]
    B -- 是 --> C["生成新的扩展检索请求"]
    C --> D{"返回新候选批次?"}
    D -- 是 --> F["retrieval_expansion_count = 1并重新健康审查"]
    D -- 否 --> E
```

扩展请求不能重复相同工具和相同参数，也不能放宽健康约束。扩展回来的新菜品必须经历完整食材健康审查。

## 6. 最终健康校验失败后的重新规划

菜单决策模型选择已有`plan_id`后自主发起最终校验工具调用。若同一B4确定性引擎重新计算后形成证据完整的`EXCLUDE`：

最终健康失败后的重新规划最多一次，由`health_replan_count`强制限制。

```text
FinalValidationArtifact.status = EXCLUDE
且 health_replan_count = 0
→ health_replan_count = 1
→ 返回健康与菜单规划节点
→ 模型重新调用get_health_constraints取得当前约束回执
→ 模型对当前候选批次重新调用evaluate_recipe_health
→ 只使用新safe_recipe_ids生成新plan_id
→ 再次进入菜单决策和最终校验
```

原健康回执和`safe_recipe_ids`在回流后不能继续作为新方案依据。重新批量审查后没有安全候选时进入`no_safe_menu`；第二次最终校验仍为`EXCLUDE`，或第一次健康内容冲突无法形成合法的新规划输入时，进入`failed`并返回`FINAL_HEALTH_VALIDATION_FAILED`。不能输出上一次选择的菜单，也不能由回答模型修改冲突菜品。

菜单哈希、参与者范围、约束引用、食材集合、覆盖状态或证据不一致属于系统完整性失败，立即进入`failed`，不消耗健康重新规划次数。工具、数据库、权限和Schema错误同样不能通过重新规划解决。

## 7. 统一审查的定向修订

统一审查模型只读执行轨迹、必需工具回执、Artifact链、最终健康校验和回答依据。它输出：

```text
PASS
或 REVISION_REQUIRED(target_node, issue_codes, evidence_refs)
```

当`review_revision_count = 0`时，工作流允许一次定向修订：

- 回答依据或表达问题返回回答模型；
- 菜单选择与已有方案不一致时返回菜单决策模型，并重新执行最终健康校验；
- 上游健康证据缺失、必需工具漏调或权限越界不进入修订，直接失败。

修订后生成新Artifact版本，旧的用户可见阶段摘要通过`analysis_superseded`标记失效。工作流随后只允许一次复审。复审仍不通过时进入`failed`，错误码为`WORKFLOW_RETRY_LIMIT_EXCEEDED`或审查给出的更具体错误。

统一审查模型不能直接修改菜单、回答、State或工具回执，也不能通过自己的工具补齐其他角色漏掉的调用。

## 8. 工具和权限失败

### 8.1 必需工具漏调

```text
模型提交Artifact
→ required_tool_receipts不完整
→ REQUIRED_TOOL_NOT_CALLED
→ failed
```

工作流不代替模型调用工具，不把缺失结果设置为默认值，也不进入回答节点。

### 8.2 越权工具调用

角色调用不在白名单中的工具，或试图扩大参与者、会话和请求范围时：

```text
TOOL_PERMISSION_DENIED
→ 丢弃该调用结果
→ failed
```

越权失败不允许通过切换模型或删除调用记录继续。

### 8.3 相同工具重复调用

同一节点出现相同工具和相同参数哈希时，只允许第一条有效调用进入回执集合；再次调用违反节点预算并进入`WORKFLOW_RETRY_LIMIT_EXCEEDED`。这不影响用户创建新`request_id`进行显式重试。

### 8.4 永久健康约束覆盖请求

用户或模型尝试删除、放宽或忽略固定档案中的疾病、过敏、异常指标或其他永久健康硬约束时：

```text
PERMANENT_CONSTRAINT_OVERRIDE_DENIED
→ 不修改有效约束集
→ failed
```

用户可以撤销当前会话中自己新增的临时约束，但不能把临时撤销扩展到永久档案事实。

## 9. 上下文完整性失败

ContextManifest缺少当前消息、有效健康约束、当前菜单或待澄清事项，或者核心块哈希不一致时：

```text
CONTEXT_INTEGRITY_FAILED
→ failed
```

如果核心块完整但总Token仍超过预算，返回`CONTEXT_BUDGET_EXCEEDED`。两种情况都不能通过删除健康约束继续。

## 10. 幂等、并发和用户显式重试

### 10.1 创建请求

- 相同幂等键、相同规范化载荷：返回已有`request_id`和当前终态或运行状态；
- 相同幂等键、不同载荷：返回`IDEMPOTENCY_KEY_REUSED`；
- 唯一Worker Claim保证同一个请求只有一个执行者；
- 会话锁防止同一会话中的并发修改覆盖当前菜单版本。

### 10.2 用户显式重试

用户明确要求重试时创建新的`request_id`，记录`retry_of=<old_request_id>`。新请求重新构建上下文，不修改旧请求的终态，也不复用旧请求未提交的模型输出。

## 11. SSE断开、重连和取消

### 11.1 SSE断开

SSE连接只是事件消费者。连接断开：

- 不取消工作流；
- 不重新调用模型或工具；
- 不创建新请求；
- 不改变WorkflowState。

客户端使用`Last-Event-ID`恢复事件；服务端从持久事件游标继续发送。心跳只维持连接，不包含业务结论。

### 11.2 用户取消

取消请求只设置受控取消标记。当前模型或工具调用完成后，工作流在节点边界检查标记：

```text
cancel_requested = true
→ 不启动下一节点
→ 写cancelled终态
→ 保留已经产生的执行证据
```

取消不能把已经完成原子提交的请求改回`cancelled`。如果提交事务已经开始，必须由提交服务完成或回滚后再确定终态。

## 12. 执行环境中断

当Worker终止、Redis运行状态不可恢复或必要外部状态丢失时，请求进入`interrupted`，而不是自动重跑。恢复机制只能从MySQL已提交的节点边界和完整Artifact引用继续；无法证明上下文完整时保持终态并等待用户显式新建请求。

## 13. 最终提交失败

最终提交按以下顺序在同一MySQL事务中完成：

```text
验证Artifact链
→ 写最终菜单与AnswerArtifact引用
→ 写参与者约束和食材健康证据引用
→ 写FinalValidationArtifact和ReviewArtifact结果
→ 写completed终态
→ commit
```

任一步失败均回滚并返回`AUDIT_COMMIT_FAILED`或具体数据库错误。运行日志或可观测平台不能补偿这次失败，工作流也不能自动重新提交。

## 14. 生命周期验收场景

实现阶段至少验证：

- 正常单人请求进入`completed`；
- 多人任一参与者食材冲突导致整道菜排除；
- 可选食材、任一替代成员或审核复合组成命中健康约束时整道菜排除；
- 全部菜品不安全进入`no_safe_menu`；
- 有安全菜但严格时间不可满足进入`no_feasible_menu`；
- 扩展召回只发生一次且产生新候选；
- 最终健康校验失败只重新规划一次；
- 统一审查只定向修订和复审一次；
- `REQUIRED_TOOL_NOT_CALLED`直接失败；
- `TOOL_PERMISSION_DENIED`直接失败；
- `PERMANENT_CONSTRAINT_OVERRIDE_DENIED`不修改永久健康约束；
- `CONTEXT_INTEGRITY_FAILED`不删除健康约束继续；
- 未通过离线质量门禁的数据产物不能进入在线链路；
- SSE断开不取消、不重跑；
- 重连按事件ID续传；
- 相同幂等键不同载荷返回`IDEMPOTENCY_KEY_REUSED`；
- 显式重试创建关联的新请求；
- 用户取消在节点边界生效；
- 强制审计提交失败回滚成功结果。
