# V2 上下文与记忆模块设计

- 状态：`APPROVED`
- 日期：2026-08-09
- 适用项目：`program_v2`
- 上游依据：[系统总体设计](../00-system-overview.md)、[全局不变量](../contracts/global-invariants.md)、[模块边界](../contracts/module-boundaries.md)、[用户健康档案模块](02-user-health-profile.md)、[Agent工作流与编排模块](09-agent-workflow.md)

## 0. 2026-08-09 批准的实现基线

- 请求启动先由 B2 形成完整有效约束，再构造 `ContextManifest`；不得先建上下文后追加健康事实，避免模型看到不完整快照。
- MySQL 持久化已提交事实，Redis 只保存可恢复的运行快照、锁和游标；两者都必须有真实 Repository，内存实现仅限单元测试。
- `ContextManifest` 列出核心块引用、哈希、版本和角色可见性；压缩前后逐块核对，不以 token 总量近似完整性。
- 会话锁覆盖同一 `session_id` 的读取、运行和提交，拥有 fencing token/TTL；过期执行者不能覆盖新请求。
- 会话事实与最终结果在 Application 事务中一起提交；回滚或失败不留下新菜单版本、临时约束或成功记忆。
- 模型上下文只使用匿名参与者引用和最小事实投影，不保存隐藏思维过程或其他人的具体健康信息。

## 1. 模块目的

C4 管理推荐系统在请求内和请求间需要记住的一切：当前会话的对话历史、多轮累积的约束状态、用户已有的菜单版本、以及每次请求时为特定模型角色裁剪的上下文投影。

C4 回答三个问题："这次请求发生时，系统已经知道什么？""这个模型角色有权看到什么？""上下文太长时，什么可以压缩、什么绝对不能丢？"

C4 不决定"用户应该得到什么推荐"——那是 B4、C2、C3 的职责。C4 只负责信息的存储、检索、投影和生命周期管理。

## 2. 已确认前提

- MySQL 保存永久健康事实和已提交会话记忆的最终记录；Redis 保存运行时 WorkflowState、会话锁和短期上下文。
- 约束优先级：永久健康事实 → 当前明确健康要求 → 当前明确修改 → 已提交会话事实 → 历史事件 → 模型推断。
- 不可压缩内容：当前消息、有效健康约束、当前菜单、待澄清事项。压缩前后这些块的引用和哈希必须一致（INV-009）。
- 模型只接收按角色裁剪的只读投影，不接收完整原始档案、其他参与者的健康详情或原始指标值（INV-013）。
- 永久健康硬约束不能被对话覆盖、删除或放宽（INV-016）。
- 会话临时约束由 B2 验证，由 C4 按作用域（`turn` / `session`）保存和失效。
- 菜单版本保留最近 5 个，旧版本在提交新版本时自动滚动淘汰。
- 模型隐藏思维过程不保存、不传递（系统总览 §9）。
- C4 不参与 Qdrant 的向量化存储——那个是 C1 的领域。
- C4 不执行 SQL 查询或 Redis 命令——通过基础设施适配器的 Repository 接口。

## 3. 职责

C4 负责：

1. 管理会话生命周期：创建 `session_id`、关联参与者、记录会话级元数据；
2. 存储和检索 `ConversationEvent`（用户消息、系统响应、菜单变更、约束变更、终态事件）；
3. 管理 `ContextManifest`——当前会话有效上下文的结构化索引和完整性哈希；
4. 构建 `SharedWorkflowContext`——一次请求内所有节点共享的上下文根对象；
5. 按 C3 的要求为每个模型角色投影 `ModelContext`（只读、裁剪后的视图）；
6. 管理多轮约束状态：继承、修改、删除和清空；
7. 保存 B2 验证后的临时健康约束，按作用域自动失效；
8. 管理菜单版本：保存最近 5 个菜单版本，支持 C3 和回答模型引用当前菜单；
9. 执行上下文压缩：在 Token 预算不足时按优先序压缩，确保核心块完整；
10. 在请求终态时提交会话状态变更（`commit_session_state` / `record_session_event`）；
11. 确保投影视图不暴露原始指标值、疾病名称或参与者真实身份；
12. 在上下文完整性校验失败时返回 `CONTEXT_INTEGRITY_FAILED` 而非继续。

## 4. 非职责

C4 不负责：

- 判定健康约束的有效性（属于 B2）；
- 保存或检索菜品、食材、营养数据（属于 B3、B6）；
- 执行向量相似度搜索或管理 Qdrant 索引（属于 C1）；
- 管理 WorkflowState、Worker Claim 或 SSE 事件传输（属于 C3 和 D1）；
- 实现 HTTP 会话管理或 cookie 认证（属于 D1）；
- 保存模型提示词或管理提示词模板（属于 C3 角色策略和基础设施适配器）；
- 生成用户可见文本或分析摘要；
- 直接写入 MySQL 或 Redis（通过 Repository 接口，C4 定义接口契约）。

## 5. 上游输入

### 5.1 会话创建

| 输入 | 来源 | 内容 |
|---|---|---|
| 会话创建请求 | D1 API | `session_id`、`participant_refs` 及其到 `user_id` 的映射 |
| 当前用户消息 | D1 API（经由 C3） | 原始消息文本、时间戳 |
| 已有会话引用 | C3 `context_building` 节点 | 需要恢复的 `session_id` |

### 5.2 请求内输入

| 输入 | 来源 | 内容 |
|---|---|---|
| B2 已验证临时约束 | B2（经由 C3 工具回执） | `TemporaryHealthConstraint`，含作用域和约束 ID |
| B2 约束撤销指令 | B2（经由 C3 工具回执） | 待撤销的临时 `constraint_id` |
| 当前菜单引用 | C3（经由 WorkflowState） | 最近一次成功推荐的 `plan_id` 和 `MenuDecisionArtifact` 引用 |
| 阶段事件 | C3（各节点出口） | `stage_events[]` |
| 终态 Artifact 引用 | C3 `atomic_commit` 节点 | 最终 `MenuDecisionArtifact`、`FinalValidationArtifact`、`ReviewArtifact` 的引用 |

### 5.3 角色投影请求

| 输入 | 来源 | 内容 |
|---|---|---|
| 投影请求 | C3（每个模型节点入口） | `role`、`handoff_message`、`shared_context_ref` |

## 6. 下游输出

### 6.1 SharedWorkflowContext

```
SharedWorkflowContext
├── request_id
├── session_id
├── participant_refs[]
├── participant_user_id_mapping (仅工作流持有，不进入模型投影)
├── current_message
│   ├── raw_text
│   └── timestamp
├── effective_constraints[]
│   ├── permanent[] (来自 B2 固定档案约束引用)
│   ├── session_temporary[] (来自 B2 已验证，scope=session)
│   ├── turn_temporary[] (来自 B2 已验证，scope=turn)
│   └── unresolved_turn_signals[] (本轮尚未由 B2 验证的候选)
├── current_menu
│   ├── plan_id | null
│   ├── recipe_ids[]
│   └── menu_artifact_ref | null
├── menu_history[] (最近 5 个版本，含 plan_id 和 menu_hash)
├── pending_clarifications[] (等待用户确认的问题)
├── conversation_events[] (当前会话的历史事件，按时间倒序)
├── context_manifest
│   ├── manifest_hash
│   ├── immutable_block_hashes
│   ├── compression_count
│   └── total_token_estimate
└── session_metadata
    ├── created_at
    ├── last_request_at
    └── request_count
```

### 6.2 ModelContext（角色投影）

```
ModelContext
├── role
├── projected_from (SharedWorkflowContext 的 manifest_hash)
├── system_visible
│   ├── task_description (当前节点需要完成的任务)
│   ├── allowed_tools_summary (工具名称和使用约束)
│   └── output_requirements (产出 Artifact 的类型要求)
├── conversation_visible
│   ├── current_message
│   ├── relevant_history[] (按角色过滤的历史事件摘要)
│   └── pending_clarifications (仅查询理解角色可见)
├── constraint_visible
│   ├── 查询理解：仅匿名参与者引用 + 已有临时信号最小摘要
│   ├── 健康菜单规划：标准化约束摘要（constraint_code + effect + scope），不含原始指标值
│   ├── 菜单决策：约束集引用（不展开具体约束内容）
│   ├── 回答：公开健康摘要（如"部分候选因健康限制被排除"），不含疾病名和指标值
│   └── 统一审查：约束集引用（不展开）
├── menu_visible
│   ├── 查询理解：当前菜单 recipe_ids + 公开菜名
│   ├── 健康菜单规划：当前菜单引用 + 历史菜单摘要
│   ├── 菜单决策：全部可行方案的摘要和差异
│   ├── 回答：最终选定菜单的完整公开事实
│   └── 统一审查：当前菜单引用
├── artifact_refs[]
├── token_budget
│   ├── total_allocated
│   ├── used_by_context
│   └── remaining_for_generation
└── projection_timestamp
```

`ModelContext` 是模型在当前节点收到的完整输入。C4 从 `SharedWorkflowContext` 提取并裁剪后生成。模型不接收原始档案字段、具体指标值、其他参与者身份或内部评分数值。

### 6.3 ConversationEvent

```
ConversationEvent
├── event_id
├── session_id
├── request_id
├── event_type: user_message | system_response | menu_committed | constraint_updated
│              | clarification_requested | clarification_resolved | terminal
├── event_summary (结构化摘要，不含原文全量)
├── event_detail_ref (指向完整 Artifact 或工具回执的引用)
├── compressed_original | null (被压缩的原文，仅保留引用时非 null)
├── participant_refs[] (涉及哪些参与者)
├── timestamp
└── token_count_estimate
```

`ConversationEvent` 是会话记忆的基本单位。原始消息全量只在近期事件中保留；被压缩的早期事件将原文替换为结构化摘要，原始内容通过 `compressed_original` 引用保留以供审计。

### 6.4 消费者视图

- **C3 工作流**：`SharedWorkflowContext`（请求级，仅 `context_building` 和终态提交时写入）、`ModelContext`（每个模型节点入口获取）；
- **B2 用户健康档案**：临时约束的存储和失效通知；
- **审计**：完整的 `ConversationEvent` 历史和 `ContextManifest` 变更记录。

## 7. 数据模型与数据所有权

### 7.1 存储分配

| 存储 | 内容 | 生命周期 |
|---|---|---|
| MySQL `sessions` | `session_id`、参与者映射、创建时间、状态 | 永久 |
| MySQL `conversation_events` | 已提交终态的 `ConversationEvent`（已压缩的历史） | 永久 |
| MySQL `menu_versions` | 最近 5 个菜单版本的 `plan_id`、`menu_hash`、`recipe_ids[]`、`committed_at` | 随新版本滚动淘汰 |
| Redis `session:{id}:state` | 当前 `SharedWorkflowContext` 的序列化缓存 | 会话活跃期 + TTL |
| Redis `session:{id}:events` | 近期未压缩的 `ConversationEvent`（最近 N 条） | 会话活跃期 |
| Redis `session:{id}:temp_constraints` | 会话级临时约束索引 | 会话活跃期，过期自动失效 |

MySQL 中的数据在会话结束后仍保留（供审计和历史恢复）。Redis 中的数据在 TTL 到期后由 Redis 自动清理。MySQL 是权威——Redis 丢失不改变已提交的会话事实。

### 7.2 约束作用域与生命周期

| 作用域 | 来源 | 生效范围 | 失效条件 |
|---|---|---|---|
| `permanent` | B2 固定档案 | 永久 | 不可失效（INV-016） |
| `session` | B2 已验证临时约束 | 当前会话的所有请求 | 用户显式撤销 或 会话结束 |
| `turn` | B2 已验证临时约束 | 仅当前请求 | 请求结束自动失效 |

C4 在每次请求开始时清理上一轮的 `turn` 约束。`session` 约束在用户显式撤销或会话超时/关闭时失效。

### 7.3 数据所有权

C4 拥有：
- `SharedWorkflowContext`、`ModelContext`、`ConversationEvent` 的 Schema；
- 上下文压缩算法和优先级规则；
- 角色投影裁剪规则；
- 会话和菜单版本的存储接口契约。

C4 不拥有：健康约束的有效性（B2）、菜单的菜品选择（C2/C3）、WorkflowState（C3）。

## 8. 核心处理流程

### 8.1 context_building（C3 节点触发）

```
C3 调用 C4.build_shared_context(session_id, participant_refs, current_message)
→ 从 MySQL 加载会话元数据（或创建新会话）
→ 从 Redis 加载当前 SharedWorkflowContext 缓存（若存在）
→ 清理上一轮 turn 约束
→ 加载 B2 固定档案约束引用（permanent）
→ 加载会话级临时约束（session_temporary）
→ 加载当前菜单引用和菜单历史
→ 加载近期 ConversationEvent 列表
→ 将 current_message 追加为新的 ConversationEvent (event_type=user_message)
→ 构建 ContextManifest（计算各块的哈希和 Token 估算）
→ 若 Token 估算超过预算 → 执行压缩（见 §9）
→ 返回 SharedWorkflowContext + ContextManifest
```

### 8.2 project_model_context（C3 每个模型节点入口触发）

```
C3 调用 C4.project_model_context(role, handoff_message, shared_context_ref)
→ 根据 shared_context_ref 加载 SharedWorkflowContext
→ 校验 ContextManifest 完整性（核心块哈希一致）
→ 按角色投影规则（见 §10）提取裁剪视图
→ 注入 handoff_message 中的 action_required、artifact_refs、constraints_summary、evidence_summary
→ 计算 token_budget（总分配 - 上下文占用 = 生成可用）
→ 返回 ModelContext
```

### 8.3 约束状态变更

```
B2 返回已验证的 TemporaryHealthConstraint 或撤销指令
→ C4 按 constraint_id 和 scope 更新 effective_constraints
→ session 约束写入 Redis session:{id}:temp_constraints
→ turn 约束仅保留在 SharedWorkflowContext（不跨请求持久化）
→ 记录 ConversationEvent (event_type=constraint_updated)
→ 永久约束覆盖请求 → 拒绝，返回 PERMANENT_CONSTRAINT_OVERRIDE_DENIED（C3 转为 failed）
```

### 8.4 菜单版本管理

```
C3 atomic_commit 成功后调用 C4.commit_session_state()
→ 将当前菜单 Append 到 menu_history[]
→ 若 menu_history.length > 5 → 移除最旧版本（滚动淘汰）
→ 保存 menu_versions 到 MySQL
→ 更新 SharedWorkflowContext.current_menu
→ 记录 ConversationEvent (event_type=menu_committed)
```

### 8.5 会话终态处理

```
C3 请求终态时调用：
- completed  → C4.commit_session_state()：提交菜单、更新会话事实、清除 turn 约束
- cancelled   → C4.record_session_event()：仅记录终态事件，不提交菜单
- failed      → C4.record_session_event()：记录失败原因引用，已产生的阶段事件保留
- needs_clarification → C4.record_session_event()：记录待澄清问题，会话保持活跃
- interrupted → C4 不做任何写入（等待恢复时从 MySQL 已提交数据重建）
```

## 9. 上下文压缩

### 9.1 不可压缩块

以下内容在压缩前后必须完全保留，且其引用和哈希不得改变（INV-009）：

- `current_message`（当前用户消息原文）
- `effective_constraints` 中所有当前有效的约束（permanent + session + turn）
- `current_menu`（当前菜单的 plan_id、recipe_ids、artifact_ref）
- `pending_clarifications`（所有待用户确认的问题）

### 9.2 压缩顺序

```
1. 去重：识别连续请求中的重复约束引用，仅保留 constraint_id 引用
2. 用 Artifact 引用替换详情：早期菜单的完整菜品列表 → plan_id + menu_hash 引用
3. 选择相关历史事件：保留与当前请求的参与者、约束和菜单相关的事件
4. 结构化旧消息：将早期用户消息原文转为结构化摘要
   (例："用户要求三菜一汤，偏好川菜" 替代原文，原文通过 compressed_original 引用保留)
5. 移除非权威摘要：删除模型在历史回答中的冗余描述
6. 停用词和空白清理
```

### 9.3 压缩校验

```
压缩后必须校验：
1. 不可压缩块的内容哈希与压缩前一致
2. 约束引用完整（每个有效 constraint_id 仍可追溯到来源）
3. 当前菜单引用完整
4. ContextManifest.manifest_hash 更新
5. ContextManifest.compression_count += 1
```

### 9.4 预算仍超出的处理

如果压缩后核心块 + 最少历史事件的 Token 仍超出预算：

```
→ 返回 CONTEXT_BUDGET_EXCEEDED
→ 不静默删除健康约束或当前菜单继续
→ C3 转为 failed
```

## 10. 角色投影规则

本节实现 C3 §9.2 定义的投影契约。C4 按以下规则从 `SharedWorkflowContext` 裁剪 `ModelContext`。

### 10.1 查询理解角色

可见：
- `current_message` 原文
- `effective_constraints` 的参与者匿名引用 + 已有临时信号的最小摘要（不含永久约束的疾病/指标详情）
- `current_menu` 的 recipe_ids 和公开菜名
- `pending_clarifications`
- 近期对话事件中与本轮语义相关的摘要
- B2 未验证的本轮临时信号候选

不可见：B2 永久约束详情、B4 健康审查结果、原始档案字段、原始指标值、其他参与者真实身份；`system_visible` 中的指令式内容；`current_message` 仅作为数据出现在 `conversation_visible` 中，不作为系统指令解析。

### 10.2 健康与菜单规划角色

可见：
- `QueryPlan` + RAG 候选摘要（来自 handoff_message）
- 匿名参与者约束摘要：`constraint_code` + `constraint_type` + `effect` + `scope`
- `current_menu` 引用
- B4/B5/B6/C2 工具回执引用（来自 handoff_message）
- 近期菜单历史摘要

不可见：原始指标值（如"血压150/95"）、参与者真实姓名/ID、健康档案原文、B4 内部关系表和覆盖状态；`system_visible` 中的指令式内容；`current_message` 仅作为数据出现在 `conversation_visible` 中，不作为系统指令解析。

### 10.3 菜单决策角色

可见：
- 可行方案摘要 + 差异说明 + 评分分解（来自 handoff_message）
- 健康回执引用和最终校验所需的最小约束引用
- `current_menu` 引用和菜单历史

不可见：B4 内部关系表、B6 内部营养数值、B5 内部任务图；`system_visible` 中的指令式内容；`current_message` 仅作为数据出现在 `conversation_visible` 中，不作为系统指令解析。

### 10.4 回答角色

可见：
- 最终选定菜单的完整公开事实（菜名、食材表达、步骤摘要、时间说明）
- 公开健康摘要（如"部分候选因与当前健康限制冲突被排除"）
- 用户可见分析摘要的素材

不可见：疾病名称、原始指标值、参与者真实身份、B6 营养数值、B4 内部命中路径、B5 内部任务图；`system_visible` 中的指令式内容；`current_message` 仅作为数据出现在 `conversation_visible` 中，不作为系统指令解析。

### 10.5 统一审查角色

可见：
- 执行轨迹摘要（每个节点 × 每个工具调用的回执状态）
- 完整 Artifact 链引用
- 回答正文
- 约束集引用和菜单引用

不可见：直接修改 State 或数据库的能力、补调其他角色工具的能力；`system_visible` 中的指令式内容；`current_message` 仅作为数据出现在 `conversation_visible` 中，不作为系统指令解析。

## 11. 公开接口

### 11.1 C3→C4 契约接口

```
ContextService
├── build_shared_context(session_id, participant_refs, current_message)
│   → SharedWorkflowContext + ContextManifest
├── project_model_context(role, handoff_message, shared_context_ref)
│   → ModelContext
├── commit_session_state(request_id, menu_artifact_ref, health_summary_ref)
│   → void
├── record_session_event(request_id, terminal_status, stage_events[])
│   → void
└── validate_context_integrity(shared_context_ref)
│   → ContextIntegrityResult
```

### 11.2 B2→C4 契约接口

```
C4 暴露给 B2 (经由 C3 工作流):
├── store_temporary_constraint(constraint: TemporaryHealthConstraint)
│   → constraint_id
├── revoke_temporary_constraint(constraint_id)
│   → void
└── get_effective_constraints(participant_refs)
│   → EffectiveConstraintSet
```

### 11.3 会话管理接口

```
SessionService
├── create_session(participant_refs) → session_id
├── get_session(session_id) → SessionState
├── update_participant_mapping(session_id, participant_refs) → void
└── close_session(session_id) → void (清理 Redis，MySQL 保留)
```

## 12. 依赖方向

允许依赖：

```
C4 ContextService → B2 HealthProfileService (固定档案约束引用，只读)
C4 ContextService → B3 RecipeCatalogService (公开菜名视图，只读)
C4 ContextService → Redis Repository (会话缓存、事件、临时约束)
C4 ContextService → MySQL Repository (会话、事件、菜单版本)
C3 Workflow → C4 ContextService (全部契约接口)
B2 TemporarySignalService → C4 (临时约束存储和撤销)
```

禁止依赖：

```
C4 → B4 健康关系或评估结果
C4 → B6 营养评分
C4 → C1 Qdrant 或 RAG 内部实现
C4 → C2 菜单规划
C4 → API Schema 或前端类型
C4 → 模型供应商客户端
Agent 模型 → C4 接口（模型通过 C3 获取 ModelContext，不直接调用 C4）
```

## 13. 异常、错误码与停止条件

| 错误码 | 触发条件 | 处理 |
|---|---|---|
| `CONTEXT_INTEGRITY_FAILED` | ContextManifest 核心块哈希与 SharedWorkflowContext 不一致 | `failed`，不允许重建或删除约束继续 |
| `CONTEXT_BUDGET_EXCEEDED` | 压缩后核心块 + 最小历史的 Token 仍超预算 | `failed` |
| `SESSION_NOT_FOUND` | 引用的 `session_id` 不存在 | 新建会话而非失败 |
| `SESSION_EXPIRED` | 会话超过最大生命周期且无法恢复 | 提示用户开始新会话 |
| `PARTICIPANT_MAPPING_INVALID` | `participant_ref` 无法映射到合法 `user_id` | `failed` |
| `MENU_VERSION_CONFLICT` | 提交菜单时版本号冲突 | 使用乐观锁重试一次 |
| `TEMPORARY_CONSTRAINT_STORAGE_FAILED` | Redis 临时约束写入失败 | 记录警告，约束降为本轮内存有效（C3 转为 `failed` 如果影响健康判断） |
| `CONTEXT_COMPRESSION_FAILED` | 压缩过程中不可压缩块被意外修改 | 回滚压缩，返回压缩前状态，`CONTEXT_INTEGRITY_FAILED` |
| `SENSITIVE_DATA_EXPOSURE` | 角色投影包含超出权限的健康字段 | `failed` |
| `UNTRUSTED_INSTRUCTION_DETECTED` | 用户输入或 RAG 内容出现在 `system_visible` 投影中 | `failed` |

## 14. 构建与初始化要求

- 会话表、事件表和菜单版本表由 B1 离线构建通过 C4 Schema 初始化；
- Redis key 前缀使用 V2 独立命名空间（`v2:session:*`），不冲突旧项目；
- 会话 TTL 和最大事件保留数为可配置参数；
- 开发期间 Schema 变更时重建会话相关 MySQL 表；
- 正式运行期间只追加会话数据，不迁移历史会话。

## 15. 测试和验收标准

### 15.1 会话生命周期

- 新会话正确创建并分配 `session_id`；
- 已有会话正确恢复——`SharedWorkflowContext` 包含近期历史、有效约束和当前菜单；
- 会话超时后 Redis 缓存过期，MySQL 会话元数据保留；
- 多轮对话后 `request_count` 和 `last_request_at` 正确更新。

### 15.2 约束管理

- `permanent` 约束在所有请求中持续存在；
- `session` 约束跨请求保持，用户显式撤销后移除；
- `turn` 约束在请求结束后自动失效；
- 尝试覆盖永久约束 → `PERMANENT_CONSTRAINT_OVERRIDE_DENIED`；
- B2 返回的临时约束正确存储并按作用域索引。

### 15.3 上下文压缩

- 不可压缩块（当前消息、有效约束、当前菜单、待澄清事项）压缩前后哈希一致；
- 早期菜单详情被 `plan_id` + `menu_hash` 引用替代；
- 早期用户消息原文被结构化摘要替代（原文可追溯）；
- 压缩后仍超预算 → `CONTEXT_BUDGET_EXCEEDED`；
- 压缩校验失败 → 回滚，不删除约束。

### 15.4 角色投影

- 查询理解角色不接收 B2 约束详情和原始档案；
- 健康菜单规划角色不接收原始指标值；
- 回答角色不接收疾病名称、指标值、营养数值；
- 统一审查角色不接收直接修改 State 的能力；
- 多人场景下不泄露其他参与者的真实身份和健康信息。

### 15.5 菜单版本

- 新菜单提交后追加到历史；
- 历史超过 5 个版本时最旧版本被淘汰；
- 当前菜单引用始终指向最新提交的版本；
- 菜单替换和恢复产生新版本。

### 15.6 跨模块契约

- C3 的 `build_shared_context` 返回完整的 `SharedWorkflowContext` + 通过校验的 `ContextManifest`；
- C3 的 `project_model_context` 返回符合角色权限的 `ModelContext`；
- C3 终态调用正确触发 `commit_session_state` 或 `record_session_event`；
- B2 临时约束存储通过 C4 接口完成；
- Redis 丢失不改变 MySQL 中已提交的会话事实。

## 16. 旧实现与目标实现差异

| 维度 | 旧实现 | V2 目标 |
|---|---|---|
| 会话记忆结构 | 可能混合约束、偏好和对话原文 | 类型化 ConversationEvent + 结构化摘要 |
| 上下文压缩 | 可能不明确 | 显式压缩顺序 + 不可压缩块 + 压缩校验 |
| 约束生命周期 | turn/session 作用域可能不清晰 | 三级作用域 + 自动失效 + 永久约束不可覆盖 |
| 角色投影 | 可能统一传递 | 五个角色各自裁剪视图 |
| 菜单版本 | 可能仅当前菜单 | 最近 5 个版本滚动保留 |
| 存储分离 | Redis + MySQL 边界可能模糊 | MySQL=权威事实，Redis=运行时缓存 |
| 模型上下文通信 | 可能直接传递完整 State | 通过 HandoffMessage + C4 投影 |

## 17. 迁移分类

| 旧实现 | 分类 | 处理结论 |
|---|---|---|
| `memory/` Redis 会话存储 | `REFACTOR` | 保留乐观版本号和菜单历史思路；升级为 V2 的 ConversationEvent 和三级约束作用域 |
| `context/builder.py` | `REWRITE` | 按 V2 的 SharedWorkflowContext + ContextManifest + 角色投影重写 |
| `repositories/` 会话和事件相关 | `REFACTOR` | 保留 Repository 接口模式；按新 Schema 调整表结构 |
| 旧上下文压缩逻辑 | `REWRITE` | 按 V2 显式压缩顺序和不可压缩块规则重写 |
| 旧约束合并逻辑 | `REFACTOR` | 保留优先级排序思路；移除与 B2 职责重叠的健康约束有效性判断 |

## 18. 审查后清理项

以下内容在 C3、B2、回答模块和 D1 全部迁移并通过验收后清理：

- 旧会话表中与新 Schema 不兼容的字段；
- 旧上下文构建器中直接传递完整原始档案的逻辑；
- 旧约束管理中 turn/session 作用域不清晰的实现；
- Redis key 与旧项目的命名空间冲突；
- 已被新 C4 Schema 替代且确认无消费者的兼容适配器。

## 19. 与全局流程和后续模块的关系

```
C4 ← B2 (固定档案约束引用)
C4 ← C3 (build_shared_context / project_model_context / commit / record)
C4 → C3 (SharedWorkflowContext + ModelContext)
C4 → D1 (会话创建和生命周期管理)
C4 → 审计 (ConversationEvent 历史)
```

- C3 是 C4 的唯一在线调用方——所有上下文操作由 C3 的节点驱动；
- B2 通过 C4 存储已验证的临时约束，不直接写 Redis；
- D1 在创建请求时通过 C4 创建或恢复会话；
- C4 不消费 B4、B5、B6、C1、C2 的任何输出（菜单版本引用除外）；
- D3 测试与验收必须覆盖会话恢复、多轮约束累积、角色投影隐私和上下文压缩完整性。

## 20. 待考量事项

以下问题不影响当前设计通过，但在进入实现阶段前需要进一步确认。在此留痕供后续回顾。

### 20.1 Redis 缓存与 MySQL 权威的同步窗口

`SharedWorkflowContext` 先写 Redis（作为运行时缓存），只在终态时通过 `commit_session_state` 把菜单和事件写入 MySQL。如果 Worker 在两个请求之间崩溃，Redis 中的临时约束和近期事件丢失——但不会丢已提交的菜单（它已在 MySQL 中）。丢失的只是两轮之间的对话事件和未提交的约束变更。

**建议**：这是 Redis 作为运行时缓存的固有特性，当前设计可以接受。如果后续要求"无论是否终态，每轮对话都写 MySQL"，C4 的 `record_session_event` 可以改为每次请求结束时都调用——但会增加热路径写入。实现阶段根据可靠性和性能权衡决定。

### 20.2 上下文压缩的 Token 估算

压缩算法（§9.2）依赖 Token 估算来决定是否需要压缩。估算不准可能导致：压缩启动过晚（已经超预算才压缩，模型调用失败）或启动过早（不必要的压缩丢失信息）。

**建议**：实现阶段使用与模型供应商一致的 tokenizer 做估算（或使用保守的上界估算）。压缩阈值（如"剩余 Token < 20%"）作为可配置参数保留。

### 20.3 会话最大生命周期

当前设计没有定义会话的绝对过期时间——会话在 Redis TTL 内活跃，Redis TTL 到期后自动清理。如果用户长时间不活动后返回，MySQL 中有会话元数据但 Redis 缓存已丢失，此时只能从 MySQL 恢复有限状态（菜单历史、已提交事件），丢失临时约束和近期未压缩对话。

**决定**：Redis TTL 设为 24 小时。超期后 Redis 缓存丢失，MySQL 中仍保留菜单历史和已提交事件。前端在会话超时后提示"上次会话已过期，当前仅保留最近推荐记录"，通过 D1 `GET /v1/sessions/{id}` 判断是否超时。

### 20.4 C3 HandoffMessage 与 C4 ModelContext 的约束信息冲突处理

`HandoffMessage` 中的 `constraints_summary` 由 C3 工作流准备（§9.1），`ModelContext` 中的 `constraint_visible` 由 C4 从 `SharedWorkflowContext` 裁剪（§10）。两者的约束信息来源不同——C3 给的是"本次交接需要的摘要"，C4 投影的是"这个角色有权看到的完整约束"。如果两边不一致（比如 C3 在 HandoffMessage 中漏掉了一个约束，但 C4 的投影规则要求对该角色包含它），应以 `SharedWorkflowContext`（C4 侧）为准——因为 C4 是隐私和完整性的唯一执行点。

**建议**：实现时 C4 在投影 `ModelContext` 时以 `SharedWorkflowContext.effective_constraints` 为权威源，`HandoffMessage.constraints_summary` 仅作为补充（如提示"本次请求新增了某约束"）。如果两边存在冲突——比如 HandoffMessage 声称移除了某个永久约束——C4 必须拒绝并返回 `CONTEXT_INTEGRITY_FAILED`。

**冲突裁定**：若 `HandoffMessage.constraints_summary` 与 `SharedWorkflowContext.effective_constraints` 冲突，以 `SharedWorkflowContext` 为准，返回 `CONTEXT_INTEGRITY_FAILED`。
