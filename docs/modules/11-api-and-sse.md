# V2 API与SSE模块设计

- 状态：`APPROVED`
- 日期：2026-08-09
- 适用项目：`program_v2`
- 上游依据：[系统总体设计](../00-system-overview.md)、[模块边界](../contracts/module-boundaries.md)、[Agent工作流与编排模块](09-agent-workflow.md)、[上下文与记忆模块](10-memory-and-context.md)

## 0. 2026-08-09 批准的实现基线

- 创建请求必须在启动工作流前完成 Pydantic 校验、规范化、参与者引用解析、幂等冲突检查和隐私字段过滤；无效载荷不能产生后台任务。
- D1 只调用 Application 公开接口，禁止直接访问内存状态、领域私有实现或构建文件；真实 MySQL/Qdrant/Redis 适配器缺失时启动失败。
- `answer_ready` 和 `result_committed` 均来自已提交 transactional outbox；前者可以先展示正文，但只有后者或状态 `completed` 才代表最终完成。
- SSE 使用持久化稳定 `event_id`、`Last-Event-ID` 游标和幂等重放；断线不重跑请求，重复事件不重复写入前端状态。
- 取消、失败和事务回滚永不发布成功事件；未知事件类型和非法状态转换 fail-closed 并保留可诊断错误。
- 当前实现中的 SSE 异常路径和未绑定变量必须在红测试保护下修复，不能用捕获所有异常后返回成功流掩盖。

## 1. 模块目的

D1 是推荐系统的对外 HTTP 接口层。它把外部的 REST 请求适配为 C3 工作流的创建、状态查询和取消调用，把内部的结构化阶段事件序列化为 SSE 流，并确保响应中不泄漏内部健康档案、营养数值或模型隐藏思维过程。

D1 是适配层，不做业务判断。它不知道什么是"健康安全"，不知道菜单怎么生成——它只知道怎么把 HTTP 请求翻译成 C3 的调用，怎么把 C3 的事件翻译成客户端可消费的 JSON。

## 2. 已确认前提

- 推荐采用请求与事件分离：创建请求、查询状态、订阅事件、取消请求是独立的 HTTP 端点。
- SSE 只是事件消费者——连接断开不取消工作流、不重新调用模型、不创建新请求、不改变 WorkflowState。
- 阶段分析事件（`analysis_ready`）只在对应 Artifact 通过 C3 校验后发布。
- 最终回答正文（`answer_ready`）只在 `FinalValidationArtifact=PASS`、`ReviewArtifact=PASS` 且结果与强制审计事务已提交后发布。
- 前端不能在缺少事件时自行补写业务文本。
- 相同幂等键 + 相同规范化载荷 → 返回已有 `request_id`；相同幂等键 + 不同载荷 → `IDEMPOTENCY_KEY_REUSED`。
- API 默认在启动阶段预热 BGE-M3 与重排模型，首轮请求不再承担模型冷加载时间。
- D1 不负责健康检查（另有 `/health` 端点，属于基础设施运维接口）。
- D1 不直接连接 MySQL、Qdrant 或模型供应商——通过 C3 和基础设施适配器。

## 3. 职责

D1 负责：

1. 定义推荐请求的 REST API 路径、方法和参数 Schema；
2. 校验请求输入——参与者引用合法性、必填字段、参数格式；
3. 管理幂等键——规范化载荷、存储幂等映射、检测相同键不同载荷的冲突；
4. 创建 `request_id` 并调用 C3 启动工作流执行；
5. 提供请求状态查询接口（返回当前 `status` + 已有阶段事件游标）；
6. 提供 SSE 事件流订阅——支持 `Last-Event-ID` 续传、心跳维持连接；
7. 管理 SSE 事件格式和事件类型枚举（`analysis_ready`、`answer_ready`、`result_committed`、`error`、`heartbeat`）；
8. 处理用户取消请求——转发取消标记到 C3，返回取消确认；
9. 管理会话创建和参与者映射（通过 C4）；
10. 确保响应 JSON 不包含：内部营养数值、原始健康指标、参与者真实身份、模型隐藏思维过程、内部评分矩阵、Qdrant 元数据、数据库连接信息。

## 4. 非职责

D1 不负责：

- 执行健康审查、菜单生成、RAG 检索或任何业务逻辑；
- 决定工作流状态转换或节点流转（属于 C3）；
- 管理会话记忆压缩或角色投影（属于 C4）；
- 生成自然语言回答或分析摘要（属于模型和 C3）；
- 模型预热的具体实现（属于基础设施适配器，D1 只在启动时触发）；
- 前端路由、渲染或状态管理（属于 D2）；
- 速率限制、用户认证或 API 鉴权（属于基础设施网关层，不在本文范围）。

## 5. API 端点

### 5.1 创建推荐请求

```
POST /v1/recommendation-requests

Request Body:
{
  "idempotency_key": string (required, max 128 chars),
  "session_id": string | null (null 表示创建新会话),
  "participants": [
    {
      "participant_ref": string (required, 如 "participant_1"),
      "user_id": string (required, 固定用户档案 ID)
    }
  ],
  "message": string (required, 用户当前消息原文),
  "config": {
    "time_limit_minutes": integer | null,
    "strict_time": boolean (default false),
    "strict_ingredients": boolean (default false),
    "available_ingredients": string[] | null
  }
}

Response 202:
{
  "request_id": string,
  "session_id": string,
  "status": "accepted",
  "created_at": string (ISO 8601)
}

Response 200 (幂等命中):
{
  "request_id": string,
  "session_id": string,
  "status": "running" | "<terminal>",
  "created_at": string,
  "result_summary": { ... } | null (仅在 completed 时有值)
}

Response 409 (幂等键冲突):
{
  "error": "IDEMPOTENCY_KEY_REUSED",
  "existing_request_id": string
}

Response 422 (参数校验失败):
{
  "error": "VALIDATION_FAILED",
  "details": [
    { "field": string, "issue": string }
  ]
}
```

处理逻辑：
1. 校验 `idempotency_key` 非空且长度合法；
2. 校验 `participants` 至少一个、每个 `participant_ref` 唯一；
3. 校验 `message` 非空；
4. 规范化请求载荷（字段排序、去除空白差异）并计算载荷哈希；
5. 查询幂等键：存在且载荷哈希相同 → 返回 200；(只是状态查询，不创建新请求)；存在且载荷哈希不同 → 返回 409；
6. 通过 C4 创建或恢复 `session_id`；
7. 创建 `request_id`，调用 C3 启动工作流；
8. 返回 202。

### 5.2 查询请求状态

```
GET /v1/recommendation-requests/{request_id}

Response 200:
{
  "request_id": string,
  "session_id": string,
  "status": "accepted" | "running" | "revising" | "completed"
          | "needs_clarification" | "no_safe_menu" | "no_feasible_menu"
          | "failed" | "cancelled" | "interrupted",
  "created_at": string,
  "updated_at": string,
  "stage_events_cursor": integer (已发布的事件序号，供 SSE 续传),
  "result_summary": { ... } | null (仅在 completed 时有值),
  "error": { "code": string, "message": string } | null (仅在 failed/error 终态时)
}
```

纯查询接口，不修改任何状态。

### 5.3 订阅 SSE 事件流

```
GET /v1/recommendation-requests/{request_id}/events
Header: Last-Event-ID: <event_id> | null

Response: text/event-stream

事件格式:
id: <event_id>
event: <event_type>
data: <json_payload>

```

事件类型：

| event_type | 发布时机 | payload |
|---|---|---|
| `heartbeat` | 每 15 秒（无业务事件时） | `{}` |
| `request_accepted` | 请求创建成功 | `{ request_id, session_id }` |
| `analysis_ready` | 某阶段 Artifact 通过 C3 校验 | `{ stage, summary, evidence_refs[] }` |
| `answer_ready` | FinalValidationArtifact=PASS、ReviewArtifact=PASS 且结果事务已提交，由 outbox 发布 | `{ text, menu_ref, evidence_refs[] }` |
| `clarification_needed` | 进入 needs_clarification 终态 | `{ question, options[] }` |
| `result_committed` | 与 `answer_ready` 同一已提交 outbox 中的后续事件 | `{ request_id, menu_summary }` |
| `error` | 进入 failed 终态 | `{ error_code, message }` |
| `request_cancelled` | 进入 cancelled 终态 | `{ request_id }` |

`analysis_ready` 的 `stage` 字段标识哪个阶段完成：`query_understanding`、`health_evaluation`、`menu_planning`、`menu_decision`。

每个事件 ID 为递增序号，支持 `Last-Event-ID` 从指定位置续传。`Last-Event-ID` 为 null 时从第一个未发送事件开始。

SSE 连接断开后客户端使用 `Last-Event-ID` 重新订阅；服务端从持久事件游标继续发送。连接断开不取消工作流、不创建新请求。

### 5.4 取消请求

```
POST /v1/recommendation-requests/{request_id}/cancel

Response 200:
{
  "request_id": string,
  "status": "cancelled",
  "cancelled_at": string
}

Response 409 (无法取消——已进入终态):
{
  "error": "REQUEST_ALREADY_TERMINAL",
  "current_status": string
}
```

取消逻辑：
1. 查询当前请求状态；
2. 若已是终态（completed/failed/no_safe_menu/no_feasible_menu/cancelled）→ 返回 409；
3. 若提交事务已开始（atomic_commit 节点）→ 等待提交完成再定，不强行中断事务；
4. 其他状态 → 设置 Redis 取消标记，C3 在下一节点边界检查；
5. 返回 200。

### 5.5 会话管理

```
POST /v1/sessions

Request Body:
{
  "participants": [
    { "participant_ref": string, "user_id": string }
  ]
}

Response 201:
{
  "session_id": string,
  "created_at": string
}
```

```
GET /v1/sessions/{session_id}

Response 200:
{
  "session_id": string,
  "participant_refs": string[],
  "request_count": integer,
  "last_request_at": string,
  "current_menu": { ... } | null
}
```

## 6. 请求生命周期与 SSE 事件时序

```
POST /recommendation-requests
  → request_accepted 事件
  → status = running

[context_building 完成]
  → (内部，不发布事件)

[query_understanding 完成]
  → analysis_ready (stage=query_understanding)

[health_menu_planning 完成]
  → analysis_ready (stage=health_evaluation)
  → analysis_ready (stage=menu_planning)

[menu_decision 完成]
  → analysis_ready (stage=menu_decision)

[answer_generation + unified_review 完成]
  → answer_ready

[atomic_commit 完成]
  → result_committed
  → status = completed
```

失败路径：
- 任一阶段失败 → `error` 事件，包含 `error_code`；
- `no_safe_menu` / `no_feasible_menu` → `error` 事件，明确终态类型；
- `needs_clarification` → `clarification_needed` 事件，SSE 保持连接等待用户回复。

## 7. 响应投影与安全检查

### 7.1 禁止字段扫描

D1 在序列化响应 JSON 和 SSE event payload 之前，必须确认不包含以下禁止字段（自动化扫描，非人工审查）：

- `blood_pressure`、`blood_sugar`、`uric_acid`、`cholesterol` 等指标值
- `disease_name`、`allergy_type`、`condition_detail`
- `nutrition_value`、`energy_kcal`、`sodium_mg`、`protein_g` 等营养数值
- `per_serving`、`serving_size`、`portion_count`
- `health_rule_score`、`caution_reason`（旧系统残留字段）
- `participant_real_name`、`user_id`（只能使用 `participant_ref` 匿名引用）
- `model_thought_chain`、`internal_reasoning`
- `qdrant_collection`、`mysql_connection_string`、`redis_key`

### 7.2 回答文本校验

`answer_ready` 中的回答文本在发布前需通过：
1. 结构化 Schema 校验（不含禁止字段）；
2. 菜品 ID 引用校验（引用的菜品均在最终菜单中）；
3. 不包含模型自主声明的健康结论（健康通过结论必须引用 `FinalValidationArtifact`）。

## 8. CORS 与开发代理

开发环境：前端 `localhost:5173` 通过 Vite proxy 将 `/api` 和 `/v1` 代理至 API `localhost:8000`。CORS 在开发环境宽放，生产环境限制为前端部署域。

## 9. 启动与预热

```
API 启动流程:
1. 加载配置 (.env + config/*.toml)
2. 初始化基础设施适配器（MySQL 连接池、Redis 客户端、Qdrant 客户端）
3. 加载 BGE-M3 和 BGE-Reranker-v2-M3 模型到内存
4. 校验工具注册表和 Artifact Schema 完整性（C3 构建配置）
5. 启动 HTTP 服务
6. Application startup complete
```

若 `RAG_WARMUP_ON_STARTUP=false`，跳过步骤 3（用于不需要本地模型的部署场景）。

## 10. 错误响应格式

所有错误使用统一格式：

```
{
  "error": string (错误码),
  "message": string (面向开发者的说明，不含内部状态细节),
  "request_id": string | null
}
```

HTTP 状态码映射：

| 错误类别 | HTTP 状态码 |
|---|---|
| 参数校验失败 | 422 |
| 幂等键冲突 | 409 |
| 资源不存在 | 404 |
| 资源已终态无法修改 | 409 |
| 服务器内部错误 | 500 |

500 响应不向客户端暴露内部堆栈、数据库查询或模型提示词。

## 11. 依赖方向

允许依赖：

```
D1 API → C3 Workflow (创建请求、查询状态、取消)
D1 API → C4 ContextService (创建和查询会话)
D1 API → 基础设施适配器 (HTTP Server、JSON Schema 校验)
D1 API → C3 SSE 事件游标 (读取已发布事件)
```

禁止依赖：

```
D1 API → B2/B3/B4/B5/B6 任何领域服务
D1 API → C1/C2 任何检索或规划服务
D1 API → MySQL / Qdrant / Redis 客户端
D1 API → 模型供应商客户端
D1 API → 前端组件或类型（D2 依赖 D1 的 API Schema，不能反向）
```

## 12. 测试和验收标准

### 12.1 请求创建

- 合法请求返回 202 + `request_id`；
- 相同幂等键 + 相同载荷返回 200 + 已有 `request_id`；
- 相同幂等键 + 不同载荷返回 409；
- 缺少必填字段返回 422 + 字段级错误详情；
- `participants` 为空返回 422；
- `message` 为空返回 422；
- 非法 JSON 返回 422。

### 12.2 状态查询

- 不存在 `request_id` 返回 404；
- 存在 `request_id` 返回当前状态和 `stage_events_cursor`；
- `completed` 状态时 `result_summary` 非 null；
- 状态变更在 200ms 内反映（Redis 缓存）。

### 12.3 SSE 事件流

- 正常链路发出所有阶段事件：`request_accepted` → `analysis_ready`×4 → `answer_ready` → `result_committed`；
- `answer_ready` 与 `result_committed` 都在事务提交后按 outbox 顺序发出，前者在后者之前；
- 失败链路发出 `error` 事件且不再后续事件；
- `Last-Event-ID` 续传从指定位置恢复；
- SSE 断开后重连不丢失事件、不重复事件；
- 心跳每 15 秒在无业务事件时发送；
- 回答文本通过禁止字段扫描。

### 12.4 取消

- 运行中请求取消返回 200；
- 已终态请求取消返回 409；
- 取消后 SSE 发出 `request_cancelled` 事件；
- 取消不中断正在进行的数据库事务。

### 12.5 响应安全

- `answer_ready` payload 不含禁止字段；
- GET 状态查询响应不含禁止字段；
- POST 创建请求响应不含禁止字段；
- 500 错误不暴露内部状态。

### 12.6 跨模块

- C3 工作流启动通过 D1 触发；
- C4 会话创建/恢复通过 D1 触发；
- SSE 事件游标与 C3 `WorkflowState.stage_events[]` 一致。

## 13. 旧实现与目标实现差异

| 维度 | 旧实现 | V2 目标 |
|---|---|---|
| 请求模式 | 可能同步或未明确异步 | 明确请求-事件分离 |
| 幂等键 | 可能存在 | 严格幂等语义 + 载荷哈希 |
| SSE 事件类型 | 可能不完整 | 明确事件类型枚举 + 发布条件 |
| 响应安全 | 可能未扫描禁止字段 | 自动化禁止字段扫描 |
| 取消语义 | 可能不明确 | 节点边界取消 + 终态不可逆 |
| API 版本 | 可能在根路径 | `/v1/` 前缀 |

## 14. 迁移分类

| 旧实现 | 分类 | 处理结论 |
|---|---|---|
| `api/app.py` FastAPI 应用入口 | `REFACTOR` | 保留 FastAPI 框架和启动预热；路由按 V2 API 端点重写 |
| `api/routes/` 分域路由 | `REWRITE` | 按 V2 的请求-事件分离模式重写路由 |
| `api/schemas.py` 请求/响应 Schema | `REWRITE` | 按 V2 Schema 重写，移除禁止字段，增加幂等键和匿名引用 |
| SSE 事件发布 | `REFACTOR` | 保留事件流机制；按 V2 事件类型和发布条件重写 |
| 旧 API 测试 | `REFACTOR` | 保留 HTTP 状态码和 Schema 校验测试；增加幂等、取消、禁止字段扫描测试 |

## 15. 审查后清理项

以下内容在 D1、D2 和 C3 全部迁移并通过验收后清理：

- 旧 API 路由中与新端点冲突的路径；
- 旧响应 Schema 中的 `health_rule_score`、`caution_reasons`、`per_serving` 等字段；
- 旧 SSE 事件类型中与新枚举不一致的定义；
- 已被新 D1 API Schema 替代且确认无消费者的旧请求/响应模型。

## 16. 与全局流程和后续模块的关系

```
客户端 → D1 API (POST/GET/SSE/Cancel)
D1 → C3 (请求创建 / 状态查询 / 取消)
D1 → C4 (会话创建和查询)
D2 前端 → D1 API Schema (通过 /v1 端点消费)
```

- D1 是外部系统和内部工作流之间的唯一边界；
- D2 前端只通过 D1 的公开 API 消费数据，不绕过 D1 直接访问 C3；
- D3 测试与验收覆盖全部端点和 SSE 事件时序；
- 模型预热由基础设施适配器完成，D1 在启动时触发。
