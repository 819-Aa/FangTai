# V2 竞赛性能快速路径设计

**日期：** 2026-08-14

**状态：** 待用户复核

**范围：** 在线推荐请求的意图理解、检索、健康审查、菜单生成、回答、SSE 与性能验收；不改变固定数据、健康关系矩阵和营养业务语义

## 0. 本次修订边界与理由

2026-08-14 复核当前代码和实测结果后，本设计按以下边界收敛：

1. **嵌入与重排固定使用现有 SiliconFlow API。** 当前生产调用链已经由 `SiliconFlowEmbedder` 和 `SiliconFlowReranker` 分别调用 `/embeddings`、`/rerank`，本地 BGE 加载代码不在在线链路中；实测两项合计远低于串行主模型耗时，因此本轮只优化 API 客户端复用、超时、预热和调用并行，不新增 CPU/GPU 或本地模型切换。
2. **按人营养摄入计算暂缓。** 该能力属于已知竞赛评分缺口，但 `docs/modules/06-nutrition-scoring.md` 规定当前 B6 不负责个人摄入、份量换算，`docs/modules/12-answer-and-frontend.md` 也禁止公开无权威营养数值。它需要独立的数据、Schema、业务语义和验收设计，不应混入性能快速路径。本设计既不实现该能力，也不声称该评分项已经满足。
3. **公开用例用于确定优化优先级，不作为能力边界。** 竞赛根目录的 20 组公开对话以首次推荐和追加约束为主，但赛题说明还列出局部替换、方案否定、模糊追问、需求矛盾和上下文回溯。明确表达走轻量确定性规则，歧义表达才使用一次模型兜底，避免按样例过拟合或重新引入多轮模型不稳定。
4. **首个可见 Token 与首个权威答案 Token 分开记录。** `answer_started` 可以降低用户感知等待并用于竞赛首 Token 计时，但不能掩盖正式菜单仍然很慢；性能报告必须同时披露权威正文开始时间和端到端终态时间。

## 1. 背景与结论

竞赛评分体系（总分 100）由四部分构成。本设计直接处理推荐安全、多轮行为和性能快速路径；表中的按人营养计算作为已知范围外缺口记录：

| 评分项 | 分值 | 关键要求 |
|---|---:|---|
| 基础推荐 | 20 | 硬约束零违反（过敏/忌口每违反一项扣 5 分）；菜谱真实性（不得幻觉生成） |
| 复杂场景组合 | 20 | 多人约束同时满足；搭配合理（荤素比/冷热比/烹饪多样性）；整桌营养均衡度（按人分别计算） |
| 多轮动态交互 | 30 | 上下文一致性；最小化修改原则；交互自然度（主动确认模糊需求而非盲目猜测） |
| 性能效率 | 30 | 首 Token / 单轮端到端 / 多轮平均，超时不得分 |

验收形式为自动评分 60% + 专家评审 40%（搭配合理性、交互质量、推荐理由解释能力）。本轮不依据评测机器的 CPU/GPU 条件改变嵌入和重排提供方。

性能效率的具体门槛：首 Token 延迟合格小于 5 秒、单轮端到端响应合格小于 15 秒、多轮平均响应合格小于 12 秒，超时直接不得分。

2026-08-14 对当前 `program_v2` 的真实 API 链路进行了分层计时：

| 测试项 | 实测结果 |
|---|---:|
| SiliconFlow 单文本嵌入 | 中位数 0.196 秒 |
| SiliconFlow 30 条候选重排 | 中位数 0.410 秒 |
| 单人完整检索 | 热态中位数 0.780 秒，首次 2.511 秒 |
| 两人多路完整检索 | 2.304 秒 |
| 单轮后台完成 | 31.786 秒 |
| SSE 收到完整回答 | 45.116 秒 |
| 同会话局部替换尝试一 | 7.318 秒后 `REQUIRED_TOOL_NOT_CALLED` |
| 同会话局部替换尝试二 | 38.011 秒后 `SCHEMA_VALIDATION_FAILED` |

单轮成功请求发生 10 次串行主模型调用，模型累计耗时 24.581 秒，占后台端到端耗时约 77%。嵌入和重排 API 已经不是主要瓶颈。当前根因是让模型分多轮决定必需工具调用、回答和审查，以及 SSE 每 15 秒轮询一次新事件。

本设计选择“方案 C：确定性快速路径 + 单次模型兜底”。健康与菜单校验完整保留，但不再由模型决定是否执行。

## 2. 目标与非目标

### 2.1 必须达到的验收底线

1. 首个用户可见响应 Token 小于 5 秒；
2. 成功单轮请求从接收到终态小于 15 秒；
3. 每组成功多轮对话的平均单轮响应小于 12 秒；
4. 性能优化不得减少永久健康约束、临时健康约束、完整食材集审查、最终菜单复核或证据绑定；
5. 推荐菜品仍必须来自当前唯一 ready build，不允许模型新增菜品或修改菜单身份；
6. 局部替换必须遵守最小修改原则，未点名菜品保持不变；
7. 外部模型不稳定时，常见确定性场景仍能完成；复杂场景必须澄清或明确失败，不伪造结果。

### 2.2 争取达到的优秀目标

1. 常见场景首 Token 小于 2 秒；
2. 常见单轮端到端小于 8 秒；
3. 常见多轮平均小于 6 秒。

优秀目标用于设计预算和持续优化，不替代第 2.1 节的强制底线。

### 2.3 明确不做

1. 不为性能关闭向量检索、重排、健康审查或最终复核；
2. 不把纯词法结果、固定候选或模板菜品伪装为完整检索成功；
3. 不修改 2,000 条固定源菜谱、50 份固定健康档案或已批准健康关系；
4. 不建设微服务、Kubernetes、Prometheus/Grafana 等与竞赛门槛无直接关系的设施；
5. 不允许回答润色模型修改 `plan_id`、`menu_hash`、`recipe_ids`、食材、健康结论或时间权威性；
6. 不以立即删除现有 `WorkflowRunner` 作为第一步，先通过可回滚切换完成迁移；
7. 不新增本地嵌入/重排模型加载、CPU/GPU 自适应切换或相关部署变体；
8. 不新增按人营养摄入、份量换算或用户可见营养数值，继续遵守现有 B6 与回答契约。

## 3. 设计原则

1. **确定性能力直接编排。** 检索、约束加载、健康评估、菜单组合和最终校验由代码按固定顺序调用。
2. **模型只处理语言不确定性。** 模型可以把复杂表达归一化为 `QueryPlanArtifact`，但不能控制健康安全工具是否执行。
3. **权威结果不依赖润色。** 系统必须能完全不用回答模型生成可交付的结构化菜单和自然语言摘要。
4. **先固定身份，再生成文字。** 任何用户可见菜名和理由都必须来源于已经最终校验的菜单身份。
5. **时间预算是运行时约束。** 每个外部调用和整个请求都有预算；非权威增强不得耗尽主链预算。
6. **事件即时通知，持久事实可重放。** SSE 使用事件唤醒；Redis/MySQL 仍承担重连和提交后的事实恢复。
7. **失败精确分类。** 模糊输入、外部服务失败、无安全菜单、Schema 错误和提交失败不得互相冒充。

## 4. 总体架构

```text
HTTP 请求
→ ContextLoader：加载会话、当前菜单、参与者和有效约束
→ FastIntentRouter：确定性识别常见意图和本轮 delta
   ├─ 识别成功：直接生成 QueryPlan
   ├─ 缺少关键指代：返回 needs_clarification
   └─ 复杂表达：单次 QueryNormalizer 模型兜底
→ DeterministicRecommendationOrchestrator
   ├─ C1 检索候选
   ├─ B2 加载永久/临时健康约束
   ├─ B4 逐菜健康审查
   ├─ C2 生成并选择可行菜单
   └─ B4 最终菜单复核
→ AuthoritativeAnswerBuilder：生成权威结构化结果和确定性正文
→ 可选 NarrativePolisher：只润色公开解释，受剩余预算限制
→ 原子提交菜单、回答和 outbox
→ EventNotifier 即时唤醒 SSE，发布 answer_ready/result_committed
```

主链中不再存在 `health_menu_planning`、`menu_decision`、`unified_review` 三个模型节点。原有 B2/B4/C1/C2 能力继续使用，改变的是调用者和编排方式，而不是安全规则。

## 5. 组件设计

### 5.1 `FastIntentRouter`

新增一个无外部网络依赖的确定性意图路由器，输入为本轮原文、当前会话摘要和当前菜单，输出为受 Schema 约束的 `IntentDelta`。

竞赛根目录的公开 `对话用例.json` 共 20 组：首次推荐 20/20、约束追加 6、需求矛盾 1（Case20）、显式食材限制 1（Case7）；局部替换、方案否定、模糊追问和上下文回溯未出现。该分布只决定优化与压测优先级，不作为系统能力边界，因为赛题说明仍明确列出这些多轮交互类型。

路由器采用少量通用语义规则，不为每条样例编写专用分支：

| 场景 | 确定性处理 |
|---|---|
| 首次推荐 | `intent=new_recommendation`，提取人数、菜数、口味、时间和显式禁忌 |
| 约束追加 | `intent=add_constraint`，只增加本轮明确约束 |
| 局部替换 | 用户明确点名当前菜单中的菜品或槽位并表达“换掉/不要/改成”时，输出 `intent=replace`，设置 `preserve_unmentioned_items=true` |
| 方案否定 | 用户明确否定整套当前方案时，输出 `intent=reject_plan`，保留仍然有效的参与者和约束事实 |
| 模糊追问 | 目标、指代或约束作用域不唯一时输出 `needs_clarification`，不猜测 |
| 需求矛盾 | 输出 `intent=conflict` 和冲突集合，生成可解释澄清问题 |
| 上下文回溯 | 用户明确引用已有版本时输出 `intent=restore` 并绑定已提交菜单版本，不从模型文字重建历史菜单 |

不符合上述明确模式的表达才交给 `QueryNormalizer`。这样既保留公开用例的零模型快速路径，也不会把评分说明列出的低频场景全部暴露给外部模型稳定性。

`IntentDelta` 至少包含：

```text
intent
target_recipe_id
target_slot
requested_dish_count
preference_additions
preference_removals
temporary_health_signals
time_constraint_policy
time_constraint_seconds
preserve_unmentioned_items
clarification_reason
```

路由器只能解析用户明确表达的语义。疾病、指标和过敏信号仍必须经过 B2 的严格临时信号解析与约束合并，不能由关键词直接形成新的健康决定。

### 5.2 `QueryNormalizer` 模型兜底

只有 `FastIntentRouter` 无法形成无歧义 `IntentDelta` 时才调用一次模型。该模型：

1. 不提供工具定义，不能调用任何业务工具；
2. 只输出 `QueryPlanArtifact` 或明确的澄清请求；
3. 使用严格 JSON Schema；
4. 单次调用，不做模型自修复循环；
5. 超时预算为 3 秒；
6. 超时、空输出或 Schema 错误统一转为 `needs_clarification`，不进入完整推荐链路。

调用模型兜底的同时，可以用经过注入检查的原始请求启动一次推测性 C1 检索。若最终 `QueryPlanArtifact` 的检索表达与推测查询兼容则复用结果；若不兼容，才执行一次纠正检索。推测结果不得绕过最终 QueryPlan、健康审查和菜单复核。

### 5.3 `DeterministicRecommendationOrchestrator`

该编排器直接调用现有领域能力，不通过 LLM tool-calling：

```text
build_effective_constraints
→ retrieve_recipes / retrieve_replacement_candidates
→ evaluate_recipe_health
→ generate_feasible_menus / generate_replacement_plan
→ select_best_plan
→ validate_selected_menu_health
```

固定规则如下：

1. 任一步必需能力失败即停止，不自动跳过；
2. 只有完整健康覆盖且逐菜审查通过的候选能进入 C2；
3. 只有 `FinalValidationArtifact.status=PASS` 的菜单能进入回答和提交；
4. 菜单选择使用 C2 确定性分数和可审查的 tie-break，不再由菜单决策模型选择；
5. 多人场景先取全部参与者约束交集，再进行一次共享菜单规划；
6. 局部替换只重跑目标槽位的召回、健康审查和计划校验，未点名 `recipe_id` 保持不变；
7. 严格时间不可证明时仍返回 `strict_time_indeterminate`，不因性能目标伪造承诺。

### 5.4 `AuthoritativeAnswerBuilder`

新增确定性回答构建器，直接消费已通过的 `MenuDecisionArtifact`、`FinalValidationArtifact`、菜品公开视图和本轮 `IntentDelta`，生成正式 `AnswerArtifact`。

正式回答包括：

1. 已确认菜单及结构化菜名；
2. 本轮新增、删除或替换的菜品；
3. 未修改部分保持不变的说明；
4. 只基于公开菜谱事实生成的搭配理由；
5. 时间数据的权威状态；
6. `plan_id/menu_hash/recipe_ids/menu_ref/final_validation_ref` 完整绑定。

不得输出具体疾病、异常指标、参与者真实身份、无权威营养数值或“适合某疾病”等医学结论。

### 5.5 `NarrativePolisher`

回答润色是可选增强，不是成功条件：

1. 只接收已经去除隐私的公开回答草稿；
2. 只允许返回 `conclusion/menu_summary/reasoning_summary` 三个文字字段；
3. 返回后重新执行菜单名称、`recipe_ids`、禁止字段和数字权威性校验；
4. 第一版默认关闭；只有不启用润色也已通过功能与性能门禁，且当前请求剩余预算不少于 2.5 秒时才允许启用；
5. 调用最多等待 2.0 秒；
6. 超时、失败或校验不通过时直接使用确定性回答，不改变请求成功状态。

这是一种受控的“表现层降级”：只放弃润色，不放弃检索、健康、菜单或提交事实。

### 5.6 持久客户端与连接池

1. `LLMClient` 在进程生命周期内复用一个 OpenAI 客户端和 HTTP keep-alive 连接池；
2. SiliconFlow 嵌入和重排共享持久 HTTP 客户端，不再每次 `urllib.request.urlopen` 新建连接；
3. 外部客户端分别配置连接超时、读取超时和总预算；
4. API 启动时除嵌入/重排探测外，预先连接 Qdrant 并执行一次不改变数据的最小检索，避免首个用户请求承担客户端导入和连接成本；
5. readiness 只报告预热结果和依赖身份，不在每次探测时重复完整初始化；
6. 在线嵌入和重排固定调用 SiliconFlow API，不根据 CPU/GPU 条件切换本地模型，也不在同一请求中静默更换提供方。

选择 API-only 的理由是：当前代码和索引链路已经按该提供方工作，嵌入与 30 条候选重排的实测中位数合计约 0.606 秒，并非 31.786 秒后台耗时的主因。引入本地模型会扩大依赖、镜像、预热和索引一致性范围，却不能解决 10 次串行主模型调用这一根因。

### 5.7 SSE 即时通知

保留 Redis 中的可重放事件事实，新增轻量 `EventNotifier`：

1. 事件写入后立即发送通知；
2. SSE 订阅者被通知后马上读取自 `last_event_id` 之后的持久事件；
3. 15 秒只作为无事件时的 heartbeat 周期，不再作为新事件轮询周期；
4. 多进程部署时使用 Redis Pub/Sub 或 Streams 通知；单进程测试也走同一接口；
5. `answer_started` 可以尽早发送不包含菜单承诺的用户可见开场，例如“我会按当前参与者和已确认要求筛选菜单”，但它只代表请求已开始处理，不代表菜单已生成或校验通过；
6. 权威菜单正文仍只在最终校验和原子提交成功后通过 `answer_ready` 发布；
7. 提交失败时不得把开场事件升级为成功，必须发送精确终态错误。

## 6. 单轮与多轮数据流

### 6.1 常见单轮推荐

```text
请求
→ FastIntentRouter 形成 QueryPlan
→ 立即 answer_started
→ C1 完整混合检索
→ B2/B4/C2 确定性链路
→ AuthoritativeAnswerBuilder
→ 可选、限时 NarrativePolisher
→ 原子提交
→ answer_ready + result_committed
```

典型请求不调用主模型；回答质量由固定模板、菜谱公开事实和确定性菜单结构保证。

### 6.2 复杂单轮请求

```text
请求
→ FastIntentRouter 判为 model_fallback
→ answer_started
→ QueryNormalizer（一次、3 秒预算）与推测性检索并行
→ QueryPlan 校验/必要时纠正检索
→ 与常见单轮相同的确定性后半链
```

模型兜底失败时返回澄清问题，不继续消耗检索、菜单和回答预算。

### 6.3 局部替换

```text
加载当前已提交菜单
→ 解析目标 recipe_id 或槽位
→ 只召回同槽位替代候选
→ 对替代候选执行全员健康审查
→ 保留未点名 recipe_ids，构建新 plan
→ 对整张新菜单执行最终健康复核
→ 提交新版本并回答变更摘要
```

“只换汤”不再重跑整套五模型，也不会因为模型未调用 `get_current_menu` 或 `evaluate_recipe_health` 失败。

### 6.4 需求矛盾与模糊指代

系统在进入检索前检测结构化冲突。能够唯一解释的冲突返回针对性问题；不能安全消解的临时健康信号交给 B2 严格解析，解析失败则 `needs_clarification`。澄清回复也通过即时 SSE 返回，不等待完整推荐链。

## 7. 性能预算

请求建立统一 `PerformanceBudget`，以单调时钟记录剩余时间。建议预算如下：

| 阶段 | 常见路径预算 | 复杂路径预算 |
|---|---:|---:|
| 请求校验、上下文和意图路由 | 0.30 秒 | 0.30 秒 |
| QueryNormalizer | 0 | 3.00 秒 |
| C1 完整检索 | 3.00 秒 | 3.00 秒，可与模型并行；多人查询并行执行 |
| B2/B4/C2 确定性处理 | 1.50 秒 | 1.50 秒 |
| 权威回答构建 | 0.50 秒 | 0.50 秒 |
| 可选润色 | 0–2.00 秒 | 0–2.00 秒，仅使用剩余预算 |
| 原子提交与事件发布 | 0.80 秒 | 0.80 秒 |
| 内部总预算 | 8.00 秒 | 12.00 秒 |

内部预算分别为竞赛单轮优秀线和低于合格线 15 秒的安全余量。阶段预算是上限，不是必须全部花完；全局内部总预算优先于任一阶段上限。C1 预算由 1.50 秒调整为 3.00 秒，因为当前首次单人检索为 2.511 秒、两人多路检索为 2.304 秒；这不是把 API 视为主瓶颈，而是避免用低于现状的预算制造错误超时。任何非权威增强只可使用扣除提交安全余量后的剩余预算，不得推迟确定性回答和提交。

外部调用超时后不在同一请求中进行隐式指数重试。真实请求若需要重试，由上层依据错误类型和剩余预算显式决定；健康与提交类确定性错误不可重试绕过。

## 8. 错误处理与降级边界

| 失败点 | 处理 |
|---|---|
| FastIntentRouter 无法唯一解析 | 单次 QueryNormalizer 或 `needs_clarification` |
| QueryNormalizer 超时/Schema 错误 | `needs_clarification`，不伪造 QueryPlan |
| SiliconFlow 嵌入或重排失败 | `failed`，不回退纯词法 |
| Qdrant 不可用或 build 不一致 | `failed`，不返回旧索引结果 |
| 健康覆盖不完整 | `failed`，不视为无命中 |
| 无安全候选 | `no_safe_menu` |
| 有安全候选但无可行组合 | `no_feasible_menu` |
| 严格时间无法证明 | `strict_time_indeterminate` |
| NarrativePolisher 失败 | 使用确定性回答，主请求可继续成功 |
| 原子提交失败 | 不发布成功菜单，返回提交错误 |
| SSE 连接中断 | 客户端按稳定 event_id 重连并重放 |

## 9. 可观测性与性能测试

### 9.1 必须记录的时间点

每个请求至少记录：

```text
accepted_at
first_visible_token_at
intent_ready_at
retrieval_ready_at
health_ready_at
menu_validated_at
answer_built_at
committed_at
first_authoritative_token_at
terminal_at
```

同时记录外部调用次数、每次角色/端点、输入规模、耗时、超时类型和是否使用模型兜底。日志不得包含 API key、原始健康隐私或模型供应商完整错误正文。

### 9.2 指标定义

1. `visible_ttft_ms = first_visible_token_at - accepted_at`，对应首个实际发送给用户的 SSE 文本事件；
2. `authoritative_ttft_ms = first_authoritative_token_at - accepted_at`，对应已经通过最终校验并绑定菜单身份的正文首 Token；
3. `e2e_ms = terminal_at - accepted_at`；
4. `multi_turn_avg_ms = 同一测试会话各轮 e2e_ms 的算术平均`；
5. 补充记录 p50、p95、最大值、成功率和模型调用次数，但不能用较好的平均值掩盖超时个案；
6. 业务失败不计为性能通过，必须先满足场景预期终态。

竞赛首 Token 门槛按 `visible_ttft_ms` 判定，同时强制披露 `authoritative_ttft_ms`。拆分理由是防止通用 `answer_started` 开场掩盖正式菜单生成延迟，也便于判断后续优化应落在交互传输还是权威主链。

### 9.3 性能环境隔离

性能验收使用独立数据库/Redis key prefix/outbox 命名空间和唯一请求前缀，不复用积累了旧测试 outbox 的环境。不得为清理性能环境删除当前运行环境的数据或卷。

正式计时前执行一次不计分 warmup：连接 MySQL、Redis、Qdrant，建立 BM25 内存索引，调用一次嵌入和重排。另行记录冷启动时间，但竞赛在线请求门槛以 ready 后请求为准。

## 10. 测试与验收

### 10.1 单元与契约测试

1. `FastIntentRouter` 覆盖首次推荐、约束追加、局部替换、方案否定、模糊追问、需求矛盾、上下文回溯及显式食材限制的正例、反例与歧义例；
2. `IntentDelta → QueryPlanArtifact` 映射完整且不能覆盖永久约束；
3. 编排器严格按 C1/B2/B4/C2 顺序调用，任一步失败停止；
4. C2 确定性选择在相同输入下结果稳定；
5. 局部替换只修改目标菜，最终菜单仍全量复核；
6. `AuthoritativeAnswerBuilder` 菜单身份完全绑定且无禁止字段；
7. NarrativePolisher 超时/失败/篡改时回退确定性回答；
8. SSE 通知即时到达、断线续传和事件去重；
9. 持久客户端复用连接且不会跨请求泄漏消息状态；
10. PerformanceBudget 超限时停止可选增强。

### 10.2 真实场景测试

第一层使用竞赛根目录提供的 20 组、每组 1–4 轮公开对话。实际覆盖场景分布为：首次推荐 20/20、约束追加 6、需求矛盾 1、食材限制 1。

第二层补充局部替换、方案否定、模糊追问和上下文回溯的合成回归用例，每类至少包含明确表达、歧义表达和错误目标三种情况。增加这一层不是扩大竞赛业务范围，而是验证 §5.1 的通用规则没有因公开样例分布而退化，并确保低频场景不会重新依赖多轮模型调用。

每轮必须先验证预期终态、硬约束零违反、菜谱存在和菜单身份一致，再判定性能。

### 10.3 性能门禁

以下全部满足才可标记竞赛性能合格：

1. 所有预期成功场景真实完成，不以快速失败计入性能通过；
2. 每个成功请求 `visible_ttft_ms` 小于 5 秒，并逐例披露 `authoritative_ttft_ms`；
3. 每个成功单轮请求端到端小于 15 秒；
4. 每个成功多轮会话平均小于 12 秒；
5. 任一请求的主模型调用次数不超过 2；常见快速路径生成权威结果时为 0，启用可选润色时总调用可为 1；
6. 没有健康约束、菜谱真实性、菜单绑定或最小修改回归；
7. 性能测试环境无历史 pending outbox 干扰；
8. 测试报告给出逐用例数据，不只给汇总平均值。

## 11. 审查后修复与后续任务层级

2026-08-14 对提交 `1c2b755..121204f` 的代码审查表明：P1–P5 已形成可回滚的快速路径原型，但尚未达到可启用状态。当前 `WORKFLOW_MODE` 必须继续默认 `legacy`。详细逐步计划见 `docs/superpowers/plans/2026-08-14-performance-fast-path-remediation.md`。

任务按依赖关系分为四层，上一层门禁未通过时不得启动下一层：

```text
L0 上线阻断修复
├─ L0.1 SSE 等待/通知与多进程事件刷新
├─ L0.2 SiliconFlow 线程安全连接池与真实超时
├─ L0.3 临时健康语义 fail-closed
└─ L0.4 取消、失锁与提交前检查
        ↓ 全部通过并完成独立审查
L1 Agent 决策与多轮完整性
├─ L1.1 类型化 IntentDelta 与确定性澄清/冲突
├─ L1.2 单次 QueryNormalizer 模型兜底
├─ L1.3 追加/替换/否定/恢复 delta 与最小修改
└─ L1.4 工具错误分类、稳定选优和回答绑定
        ↓ 功能/安全/多轮契约通过
L2 性能证据与合格线
├─ L2.1 visible/authoritative TTFT 与阶段预算
├─ L2.2 SSE 驱动的性能 harness
└─ L2.3 20 组真实 API 对话逐轮验收
        ↓ 所有竞赛合格线通过
L3 灰度与交付
├─ L3.1 fast_path 灰度与 legacy 回滚演练
├─ L3.2 默认切换 fast_path
└─ L3.3 更新交付报告和系统文档
```

### 11.1 L0：上线阻断修复

1. 用 C4 公共事件通知接口替代 `api_app.py`、D1 对 Redis 私有成员的直接访问；`wait(timeout_seconds=15)` 必须真正阻塞，持久事件必须先写后通知，多进程订阅者收到通知后强制刷新持久事件；
2. 用线程安全 HTTP 连接池替换模块级单一 `http.client.HTTPConnection`，嵌入与重排继续固定走 SiliconFlow API，并执行并发、超时和连接恢复测试；
3. `FastIntentRouter` 发现过敏、疾病、指标或参与者归属不明确的健康语义时不得继续普通推荐，只能形成可由 B2 严格验证的信号、进入一次模型归一化或返回澄清；
4. 确定性编排在每个外部/领域步骤前后以及原子提交前检查取消标记和 fencing token，取消后不得写菜单、回答或成功 outbox。

**修复理由：** 这四项分别会造成 SSE 热循环、并发检索随机失败、硬健康约束漏排以及取消后仍提交，均可能直接破坏系统正确性或可用性，优先级高于新增 Agent 能力和性能调优。

**层级门禁：** 并发 API、无事件 SSE、跨 worker SSE、临时健康信号、取消竞争和失锁测试全部通过；快路径仍不设为默认。

### 11.2 L1：Agent 决策与多轮完整性

1. 将意图输出收敛为类型化集合：`new_recommendation/add_constraint/replace/reject_plan/restore/conflict/needs_clarification/model_fallback`；已有菜单不能自动等同于追加约束；
2. 只有确定性路由无法唯一解析时调用一次无工具的 `QueryNormalizer`，3 秒硬预算，失败转澄清；
3. 追加约束显式保留当前菜数；局部替换只改变目标槽位；方案否定保留仍有效约束；恢复只绑定已提交菜单版本；所有新菜单重新执行全量 B4 最终复核；
4. 每个必需工具结果先区分基础设施/Schema 错误和合法业务终态，禁止把工具错误伪装为 `no_feasible_menu`；选优使用稳定 tie-break，回答执行现有菜单绑定校验并生成真实变更摘要。

**修复理由：** 当前实现是确定性工作流骨架，但缺少 Agent 所需的歧义处理、状态决策和重规划能力；补齐本层后才可称为“Agent 决策 + 确定性执行”的混合式 Agent。

**层级门禁：** 公开 20 组功能用例以及替换、否定、澄清、冲突、恢复的合成回归用例通过；硬约束零违反；最小修改逐 `recipe_id` 验证；常见路径主模型调用为 0，复杂路径最多 1 次 QueryNormalizer。

### 11.3 L2：性能证据与合格线

1. 完整记录 `accepted_at/first_visible_token_at/first_authoritative_token_at/committed_at/terminal_at` 和各外部调用耗时、输入规模、超时类型；
2. 性能 harness 改为消费 SSE，业务成功后才计入性能通过，分别计算逐轮 E2E、每组多轮平均、p50/p95/max；
3. 在独立命名空间、warmup 后、`WORKFLOW_MODE=fast_path` 下运行 20 组真实 API 对话，不运行本地嵌入/重排模型，不将失败或快速澄清计为性能通过。

**修复理由：** 当前观测只有服务端节点计时和一秒轮询 E2E，无法证明首 Token、权威答案延迟和多轮平均满足竞赛门槛。

**层级门禁：** 成功请求 `visible_ttft_ms < 5000`、单轮 `e2e_ms < 15000`、每组多轮平均 `< 12000`，并逐例披露 `authoritative_ttft_ms`；功能、安全和真实性先于性能判定。

### 11.4 L3：灰度、回滚与后续方向

1. 先在隔离环境显式设置 `WORKFLOW_MODE=fast_path`，验证重启、取消、并发、Redis 暂时不可用和外部 API 超时；
2. 完成一次 `fast_path → legacy → fast_path` 回滚演练，确认不重建、不覆盖固定数据和 ready build；
3. 只有 L0–L2 全部门禁通过后才把默认模式改为 `fast_path`，竞赛交付期继续保留 `legacy` 回滚开关；
4. `NarrativePolisher` 仍为默认关闭的后续增强，只能在权威路径已经达到优秀线且存在剩余预算时单独实施；
5. 按人营养摄入计算继续作为独立范围外项目，不并入本修复计划；CPU/GPU 或本地嵌入/重排模型切换不在后续任务中。

### 11.5 当前状态定义

- 已完成：性能瓶颈定位、特性开关、确定性执行骨架、权威回答骨架和部分追加约束路径；
- 尚未完成：L0 阻断修复、完整 Agent 决策、完整多轮 delta、性能合格证据、默认模式切换；
- 当前可接受用途：默认关闭的开发原型；
- 当前不可接受用途：标记“全部完成”、切换生产默认或作为最终竞赛交付版本。

迁移期继续使用显式 `WORKFLOW_MODE=legacy|fast_path`。切换不改变固定数据、数据库 Schema 或 ready build 身份；出现回归时只回滚代码和模式，不回滚或重建数据。

## 12. 预计代码与文档边界

实施预计涉及：

```text
src/food_agent_v2/c3/fast_intent.py
src/food_agent_v2/c3/orchestrator.py
src/food_agent_v2/c3/authoritative_answer.py
src/food_agent_v2/c3/narrative.py
src/food_agent_v2/c3/llm_client.py
src/food_agent_v2/c1/siliconflow.py
src/food_agent_v2/api_app.py
src/food_agent_v2/d1/
src/food_agent_v2/d2/
frontend/src/api/client.ts
frontend/src/stores/recommendation.ts
tests/performance/
scripts/run_performance_acceptance.ps1
```

同步更新：

```text
docs/modules/07-rag-retrieval.md
docs/modules/09-agent-workflow.md
docs/modules/11-api-and-sse.md
docs/modules/12-answer-and-frontend.md
docs/modules/13-testing-and-acceptance.md
```

不修改 B1 固定数据工程、B3 菜品食材事实、B4 健康关系矩阵和现有 ready build。

## 13. 完成标准

满足以下全部条件后，本设计对应变更才算完成：

1. 快速路径成为在线默认，旧五模型主链已退出；
2. 常见场景无需主模型即可生成完整、自然且权威绑定的回答；
3. 复杂场景最多一次查询理解和一次可选润色调用；
4. 20 组正式对话以及 §10.2 的补充多轮回归用例通过对应功能、健康、真实性和多轮门禁，20 组正式对话同时通过性能门禁；
5. 首 Token、单轮端到端和多轮平均均达到竞赛合格线；
6. 目标模块的单元、契约、集成、真实 API、SSE 和前端测试通过；
7. 性能测试逐用例证据和最终交付报告已生成；
8. 文档与代码一致，在线嵌入和重排固定为 SiliconFlow API，不再描述本地 BGE 模型或五模型在线主链为当前事实；
9. 未对固定数据、健康关系和 ready build 执行未授权重建或覆盖。
