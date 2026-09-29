# Agent 澄清协议 v2 收口执行方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 LangGraph 澄清问题从权威提交、可靠投递、结构化回复到断线恢复的闭环，并用可重复的故障注入决定是否放行 v2。

**Architecture:** MySQL 的 `sessions`、`request_acceptances`、`clarification_questions`、`recommendation_logs` 和 `outbox` 是请求身份、终态及问题生命周期的权威事实；Redis 只保存锁与可重建的运行时投影。Application 事务裁决选择和状态转移，C3 提供 Agent 决策及私有选项解释，D1 和前端只消费公开问题视图。所有已提交事件使用稳定 outbox ID，GET/SSE 能从 MySQL 恢复。

**Tech Stack:** Python、FastAPI、Pydantic、LangGraph、MySQL、Redis、Pytest、Vue 3、Pinia、TypeScript、Vitest。

## Global Constraints

- 遵循 [澄清链路设计](../specs/2026-09-27-agent-clarification-lifecycle-design.md) 的 I1–I7；保留 Agent 自主决策和既有健康门卫，不回退为确定性主编排。
- `WORKFLOW_MODE=fast_path` 保持默认；v2 只给新建且持久化标记的 LangGraph 会话启用。迁移与回滚均不得重建 H07 固定菜谱或健康数据。
- 一次 `question_id` 最多产生一次 MySQL 业务提交效果；Redis、模型调用和 SSE 不承诺 exactly-once。未经 MySQL 提交的问题不得成为可选的公开问题。
- 公开问题只含 `question_id`、`question_text`、`options[{option_id,text}]`、`expires_at` 和状态；`modifications`、QueryPlan 快照与健康证据不得进入公开 JSON、SSE 或浏览器状态。
- 当前工作树含大量未提交的既有改动。实施时只暂存本任务文件；不要执行 `git add .`，不要覆盖其他变更。每个任务以测试和差异审查为验收边界。

## 当前基线与交付顺序

初版计划建立时，针对性测试为 `58 passed`，但仍有 outbox、fencing、事务幂等与前端回复缺口。任务 1–7 随后被标记完成；2026-09-28 再审运行后端 `668 passed`、前端 `38 passed`、Vite 构建与 Ruff 均通过，但代码审查发现下文任务 8 的未覆盖边界。**任务 1–7 的勾选只代表原步骤曾执行，不代表 v2 已通过最终放行；任务 8 完成并重新验证前，功能与 Canary 门禁均为未通过。**

| 责任边界 | 文件 | 交付结果 |
|---|---|---|
| 数据与契约 | `db/migrations/20260927_clarification_questions.sql`、`db/migrations/apply_migration.py`、`db/schema_mysql.sql`、`contracts/clarification.py` | 可重复迁移；公开选项与内部修改分离；请求身份与会话协议持久化 |
| 权威事务 | `application/commit_service.py`、`c4/mysql_repository.py` | 幂等优先、选项复核、版本与 fencing、同事务状态转移 |
| 事件投递 | `application/outbox.py`、`c3/runner.py`、`c3/graph_orchestrator.py`、`d1/__init__.py` | 只经 outbox 发布已提交澄清；稳定事件 ID；锁释放后投递 |
| 权威读取 | `c4/mysql_repository.py`、`c4/__init__.py`、`d1/__init__.py`、`api_app.py` | MySQL 故障显式报错；GET 与 SSE 可从提交事实恢复 |
| UI | `frontend/src/types.ts`、`api/client.ts`、`stores/recommendation.ts`、`features/chat/ChatPanel.vue` | 展示并绑定问题 ID，刷新恢复，安全处理旧选择 |
| 验收 | `tests/application/`、`tests/c3/`、`tests/integration/`、`frontend/src/**/*.test.ts`、`reports/` | 自动化回归、故障矩阵、真实模型样本和放行记录 |

### Task 1: 数据迁移、会话协议固定和公开/私有契约

**Files:** 修改 `db/migrations/20260927_clarification_questions.sql`、`db/migrations/apply_migration.py`、`db/schema_mysql.sql`、`src/food_agent_v2/contracts/clarification.py`、`src/food_agent_v2/c4/mysql_repository.py`、`src/food_agent_v2/c4/__init__.py`；测试 `tests/contracts/test_clarification_contracts.py`、新增 `tests/integration/test_clarification_migration.py`。

**Interfaces:** `ClarificationPublicPayload.options` 只接受 `ClarificationOptionView`；`ClarificationPrivateSnapshot` 持有按 `option_id` 索引的服务端 `option_modifications`；仓储增加 `load_clarification_state(session_id)`，返回 active ID、revision、当前/已提交 fencing token 和持久化的 `workflow_mode/protocol_version`。会话创建时持久化协议版本，既有会话保持旧版本。`request_acceptances` 用 `SHA256(idempotency_key)` 唯一绑定 payload hash、request ID、session ID、创建时间和接受状态；只对 v2 请求启用，避免 Redis 重建为空时重跑已接受请求。

- [x] **Step 1: 写失败测试。** 断言 `ClarificationPublicPayload` 拒绝带 `modifications` 的 option；迁移脚本对空库、已有部分列、全部列分别运行两次；已有数据和索引不被清空。断言旧会话不会因部署环境变量切换而变成 v2；相同幂等键 hash 只能对应一条请求身份记录。
- [x] **Step 2: 确认红灯。** 运行 `./.venv/Scripts/python.exe -m pytest tests/contracts/test_clarification_contracts.py tests/integration/test_clarification_migration.py -q`，记录缺失/失败项。
- [x] **Step 3: 实施最小变更。** 分列检查 `INFORMATION_SCHEMA`，逐列补齐缺失字段；迁移只做加法，不重复执行整段 `ALTER TABLE`。新增 `sessions.workflow_mode`、`sessions.clarification_protocol_version`、`sessions.active_fencing_token`，保留现有 `fencing_token` 作为上次已提交值。新会话由服务端确定并写入协议；已有 NULL 会话解释为旧协议。新增 `request_acceptances` 唯一键表。对已有 v2 问题及尚未派送的澄清 outbox，先统计含内部字段的公开 payload，再以事务把 `modifications` 移入问题私有快照并净化两处公开字段；失败则停止开关，不能丢弃现存问题。
- [x] **Step 4: 验证绿灯。** 重跑本任务测试；检查迁移前后 `sessions`、`clarification_questions` 行数和 `recommendation_logs` 摘要一致，并在 H07 副本演练，不直接拿生产库作首次迁移。

```python
# 契约边界示意；内部映射仅在服务端读取
class ClarificationPublicPayload(BaseModel):
    question_text: str
    options: list[ClarificationOptionView]

class ClarificationPrivateSnapshot(BaseModel):
    query_plan_snapshot: dict | None = None
    option_modifications: dict[int, dict] = Field(default_factory=dict)
```

```sql
-- v2 请求接受身份；原始幂等键不入库
CREATE TABLE IF NOT EXISTS request_acceptances (
  idempotency_key_hash CHAR(64) PRIMARY KEY,
  payload_hash CHAR(64) NOT NULL,
  request_id VARCHAR(64) NOT NULL UNIQUE,
  session_id VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'accepted',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

**完成标准：** `public_payload` 与 outbox 待发 payload 均不含内部键；两次迁移均成功；会话协议、fencing watermark 与请求身份可从 MySQL 读取。

### Task 2: 修正 Application 原子提交与幂等

**Files:** 修改 `src/food_agent_v2/application/commit_service.py`、`src/food_agent_v2/contracts/clarification.py`；扩展 `tests/application/test_commit_validation.py`。

**Interfaces:** `ClarificationTransition` 增加 `expected_revision`，并校验“消费旧问题”“替代旧问题”“创建新问题”的合法组合；`commit_request_result` 返回现有 `idempotent`/`outbox_event_ids` 结构，错误码保留 `CLARIFICATION_*` 与 `IDEMPOTENCY_CONFLICT`。

- [x] **Step 1: 写失败测试。** 同一 `request_id` 同信封再次提交必须返回 `idempotent=True`，不同信封冲突；不存在的选项、错误 active ID、过期问题、错误 revision、空/旧 fencing token 都回滚。两个请求竞争同一问题时仅一个提交，失败请求不能生成菜单、下一问题或 outbox。`needs_clarification` 的首次追问、再次追问、新需求替代也逐一覆盖。
- [x] **Step 2: 确认红灯。** 运行 `./.venv/Scripts/python.exe -m pytest tests/application/test_commit_validation.py -q -k clarification`。
- [x] **Step 3: 在 `SELECT sessions ... FOR UPDATE` 后立即做 `recommendation_logs` 信封幂等判定。** 同信封直接 rollback 并返回原结果；不同信封报冲突。随后校验 transition 组合、当前 revision、正整数 fencing token 和问题归属/状态/数据库到期时间，并从锁定的问题私有选项映射复核 `selected_option_id`。不得信任 D1/C3 的预检或客户端传入的修改字典。
- [x] **Step 4: 更新会话事实。** 对 v2 事务要求传入 token 等于 `sessions.active_fencing_token`，且大于现有 `fencing_token`（上次已提交值）；`completed` 和带澄清转移的 `needs_clarification` 成功后更新后者与 revision。失败、取消、中断不消费也不替代旧问题。`next_question_id`、公开负载、私有快照和到期时间必须完整且相互一致；禁止 `next_question_id` 携带 `{}` 负载入库。
- [x] **Step 5: 验证绿灯。** 重跑本任务和既有 Application 测试，并用只读 SQL 比对失败前后 session revision、问题行、菜单版本、日志和 outbox 计数。

```text
BEGIN → 锁 session → 按 request_id 检查提交信封 → 校验 fencing/revision
→ 锁并验证旧问题与 option_id → 消费/替代旧问题 → 建立下一问题
→ 更新 session → 写不可变日志/菜单/outbox → COMMIT
```

**完成标准：** I1–I4、I7 的事务级测试通过；重试不先碰问题状态；任何失败没有部分业务效果。

### Task 3: 只用 outbox 发布已提交澄清事件

**Files:** 修改 `src/food_agent_v2/application/outbox.py`、`src/food_agent_v2/c3/runner.py`、`src/food_agent_v2/c3/graph_orchestrator.py`、`src/food_agent_v2/d1/__init__.py`；扩展 `tests/application/test_commit_validation.py`、`tests/integration/test_langgraph_d1_full_chain.py`。

**Interfaces:** `publish_clarification_event(request_id, query_plan, *, event_id=None)` 支持 outbox 稳定 ID；`OutboxDispatcher._publish` 识别 `clarification_needed`；outbox payload 直接使用 Task 1 的公开视图。D1 `_emit_event` 使用稳定 ID 去重。

- [x] **Step 1: 写失败测试。** 首次追问提交后 outbox 可投递一次；重复派送同 ID 不增加 SSE 游标；事务提交前不出现正式澄清事件；模拟“commit 后、派送前进程退出”，恢复循环仍能发布；一次发布失败保留 pending，后续可重试。
- [x] **Step 2: 确认红灯。** 运行 `./.venv/Scripts/python.exe -m pytest tests/integration/test_langgraph_d1_full_chain.py tests/application/test_commit_validation.py -q -k 'clarification or outbox'`。
- [x] **Step 3: 接通 dispatcher。** `clarification_needed` 分支把公开 payload 和数据库 `event_id` 交给 D1；敏感字段扫描失败时抛错，dispatcher 释放 claim 并保留待重试。D1 对 outbox 事件必须保证可恢复的持久写入；Redis 写失败时不能假称成功并标记 outbox `dispatched`。
- [x] **Step 4: 删除双通道。** 去掉 `runner._finalize` 和 graph `_node_commit` 的直接 `publish_clarification_event`；`needs_clarification` 与 `completed` 都在会话锁释放后触发 `dispatch_request(request_id)`。保留启动时的 `dispatch_pending` 恢复循环。
- [x] **Step 5: 验证绿灯。** 检查同一请求只有一个稳定 `ev_clarify_<request_id>` 正式事件，且事件可见时会话锁已释放；重跑相关测试。

**完成标准：** MySQL 已提交但进程中断时，问题可从 outbox 重投；重复投递不出现第二个可执行问题。

### Task 4: C3/C4 以 MySQL 为澄清裁决源

**Files:** 修改 `src/food_agent_v2/c3/graph_orchestrator.py`、`src/food_agent_v2/c3/runner.py`、`src/food_agent_v2/c4/mysql_repository.py`、`src/food_agent_v2/c4/__init__.py`；扩展 `tests/c3/test_clarification_lifecycle.py`、`tests/c3/test_graph_orchestrator.py`。

**Interfaces:** C3 读取 `load_clarification_state` 的 active ID/revision 与私有 option 映射，向 Application 传 `expected_revision`；Redis pending 只能用于执行缓存，不能在 v2 下决定选择对象。

- [x] **Step 1: 写失败测试。** Redis 有过期/已消费的旧问题、MySQL 指向新问题时只能选择新问题；MySQL 查询异常时请求明确失败，不回退 Redis 猜测；结构化 `question_id/option_id` 只应用 MySQL 的私有修改；失锁后 `needs_clarification` 不写新问题；纯文本“选第一个”在 v2 下拒绝。
- [x] **Step 2: 确认红灯。** 运行 `./.venv/Scripts/python.exe -m pytest tests/c3/test_clarification_lifecycle.py tests/c3/test_graph_orchestrator.py -q`。
- [x] **Step 3: 调整读取与提交前检查。** v2 下删去 `load_active_clarification` 失败时的 `pass` 和 Redis pending 兜底；根据 public `option_id` 从 private snapshot 取修改，绝不从客户端或 public payload 获取。新 token 取得后，在启动 Agent 前以 `FOR UPDATE` 比较并登记 `sessions.active_fencing_token`；若 token 不大于已登记值则释放 Redis 锁并失败。提交前再次确认 Redis 锁归属，Task 2 的事务再要求 token 与 active watermark 相等并校验 revision；旧 token 不得在新持有者登记之后提交。
- [x] **Step 4: 验证绿灯。** 跑 C3 和集成测试；故障注入时确认旧问题仍 pending、新问题未生成、D1 不发可执行澄清。

**完成标准：** MySQL 不可用时 v2 停止裁决；Redis 残留或缺失均不能改变选择语义。

### Task 5: D1 权威状态、幂等与断线恢复

**Files:** 修改 `src/food_agent_v2/d1/__init__.py`、`src/food_agent_v2/api_app.py`、`src/food_agent_v2/c4/mysql_repository.py`、`src/food_agent_v2/c4/__init__.py`；扩展 `tests/integration/test_langgraph_d1_full_chain.py`，新增 `tests/integration/test_clarification_recovery.py`。

**Interfaces:** 仓储提供 v2 请求身份原子声明/读取、按 `request_id` 读取已提交日志和 outbox 公共事件的方法；`GET /v1/recommendation-requests/{id}` 与 `GET /v1/sessions/{id}` 输出同一 `ClarificationView`。Redis 丢失且 MySQL 无可确认事实时返回 `503 REQUEST_STATE_UNAVAILABLE`，而非误报 404。

- [x] **Step 1: 写失败测试。** 同一幂等键重发已提交的选择应返回原 `request_id`，包括 Redis 被清空后；同键不同载荷返回冲突，不能启动第二次 Agent。MySQL 查询异常时 POST/GET 返回 503；Redis 请求状态及事件丢失后，GET 从日志/问题表恢复 `needs_clarification` 和同一问题 ID；已投递后 Redis 再丢失，SSE 从 outbox 恢复相同稳定 ID。会话 GET 不再输出旧 `pending_clarifications` 数组。
- [x] **Step 2: 确认红灯。** 运行 `./.venv/Scripts/python.exe -m pytest tests/integration/test_clarification_recovery.py -q`。
- [x] **Step 3: 重排 POST 判定。** 格式校验后，v2 先查 MySQL `request_acceptances`；命中同载荷则恢复原请求，不重新预检问题也不启动 Agent，命中不同载荷返回 409。未命中才做 MySQL active/过期预检；通过后以唯一键原子声明，若并发输掉声明则读取赢家并按载荷判定，不启动第二个 Agent。数据库异常返回 503，不吞异常。Redis 幂等记录仅作缓存。
- [x] **Step 4: 实现权威 GET/SSE 与固定路由。** 已提交请求以 MySQL `recommendation_logs` 与 outbox/问题表为准，Redis 可补充未提交的进度；SSE 只从 `dispatched` outbox 恢复事件，按 `event_id` 合并并去重，避免锁释放前从 pending 行提前公开。会话 GET 只返回公共 `active_clarification`，不透传 Redis pending；MySQL 查询失败不能显示 `active_clarification: null`。`api_app.py` 必须将 D1 返回的所有 `>=400` 状态原样转为 HTTP 响应。`_trigger_workflow` 按持久化会话 `workflow_mode/protocol_version` 选 runner，不随当前进程环境变量切换；其总异常兜底若发现 MySQL 已有终态，不得覆盖成 `WORKFLOW_ERROR`。
- [x] **Step 5: 验证绿灯。** 重跑 D1/集成测试，扫描 GET 与 SSE JSON 中的 `modifications`、`private_snapshot`、`query_plan_snapshot`、健康内部键。

**完成标准：** 已提交事实在 Redis 丢失、API 重启、SSE 断线后仍可恢复；不可确认事实以 503 明确呈现。

### Task 6: 前端结构化选择与刷新恢复

**Files:** 修改 `frontend/src/types.ts`、`frontend/src/api/client.ts`、`frontend/src/stores/recommendation.ts`、`frontend/src/features/chat/ChatPanel.vue`；扩展 `frontend/src/stores/recommendation.test.ts`、`frontend/src/features/chat/ChatPanel.test.ts`。

**Interfaces:** `ClarificationView` 与后端同名字段；`createRequest` 可选 `clarification_response: {question_id: string; option_id: number}`；store 保存一个当前公开问题，按钮按所显示的 ID 发送，不把任意自由文本自动绑定到当前问题。

- [x] **Step 1: 写失败测试。** SSE 收到问题后渲染编号/文字；点击选项发送准确的 ID/编号且发送期间禁用按钮；刷新后从会话或请求 GET 恢复同一问题；`CLARIFICATION_ALREADY_APPLIED`、`CLARIFICATION_STALE`、`CLARIFICATION_EXPIRED` 触发刷新并显示最新问题，不自动重试；普通输入“选第一个”不生成结构化选择。
- [x] **Step 2: 确认红灯。** 在 `frontend` 运行 `npm test -- --run src/stores/recommendation.test.ts src/features/chat/ChatPanel.test.ts`。
- [x] **Step 3: 最小实现。** 定义公开类型；SSE 和 GET 共用一个公开问题解析/更新函数；渲染明确的选择按钮，点击时连同 `question_id`、`option_id` 创建新请求；保留自由文本输入用于完整的新需求。浏览器状态、日志和本地存储均不保存内部 modifications。
- [x] **Step 4: 验证绿灯。** 运行 `npm test -- --run`、`npm run build`，再在真实后端进行一次追问→选择→再次追问/完成的手动检查。

**完成标准：** 用户无需手写编号即可完成澄清；刷新、断线和冲突后的界面与 MySQL active question 一致。

### Task 7: 故障矩阵、真实验收、上线与回滚门禁

**Files:** 扩展 `tests/integration/test_langgraph_d1_full_chain.py`、`tests/integration/test_clarification_recovery.py`；更新 `reports/2026-09-27-agent-live-acceptance.md` 或新增 `reports/2026-09-28-clarification-v2-acceptance.md`；按结果更新 `docs/superpowers/plans/2026-09-27-agent-refactor-handoff.md`。

- [x] **Step 1: 执行自动化矩阵。** 覆盖提交前失败、提交后 Redis 写失败、提交后 SSE 写失败、已投递后 Redis 丢失、两个请求竞争同一问题、重放同一请求、失锁、过期/替代问题、旧客户端文本回复、迁移重复运行。每例记录 MySQL 行数/状态、outbox 状态与公开响应，不能只断言 HTTP 状态。
- [x] **Step 2: 跑完整回归。** 在仓库根目录运行 `./.venv/Scripts/python.exe -m pytest tests/contracts tests/application tests/c3 tests/integration -q`；在 `frontend` 运行 `npm test -- --run` 与 `npm run build`；运行已配置的 Ruff 检查。失败项逐项修复后重跑受影响测试。
- [x] **Step 3: 做真实端到端样本。** 在独立新会话、`WORKFLOW_MODE=langgraph`、持久化 v2 协议下，用真实模型覆盖首次追问、选项完成、选项再次追问，以及设计文档原有五类 Agent 场景。记录 request/session/question ID、模型调用次数与耗时、MySQL/outbox 事实、SSE/GET 一致性；真实模型与脚本化故障样本分栏，不混为同一次验收。
- [x] **Step 4: 放行判定。** 只有设计 I1–I7、故障矩阵、后端/前端回归和真实样本全部通过，才允许在少量新会话 canary；保留 `fast_path` 默认。统计澄清率、提交失败率、outbox pending 积压与修复时长、模型总耗时 p50/p95/p99 及样本量。阈值未确定或证据不足时记录“功能通过、默认切换未放行”。
- [x] **Step 5: 回滚演练。** 停止新会话进入 v2；已存在 v2 会话继续由兼容代码处理或明确终止并提示重建，不能交给旧 `fast_path` 解释选项。核对回滚后菜单/健康数据未受影响。

**完成标准：** 报告包含每个门禁的 PASS/FAIL、命令与时间、关键数据库/事件证据和明确结论；没有空泛的“已完成验收”。

### Task 8: 再审发现的协议与恢复缺口（放行前必做）

**Files:** 修改 `src/food_agent_v2/api_app.py`、`src/food_agent_v2/d1/__init__.py`、`src/food_agent_v2/c3/graph_orchestrator.py`、`src/food_agent_v2/c4/mysql_repository.py`、`src/food_agent_v2/c4/__init__.py`、`db/schema_mysql.sql`、`db/migrations/apply_migration.py`；扩展 `tests/integration/test_clarification_recovery.py`、`tests/c3/test_clarification_lifecycle.py`、`tests/integration/test_clarification_migration.py`；更新本任务验收报告。

**再审证据：** `api_app.py` 的会话创建接口直接接受请求体里的 `workflow_mode` 与 `clarification_protocol_version`；`d1/__init__.py::_trigger_workflow` 在数据库读取异常后退回环境变量；`graph_orchestrator.py` 用环境变量与会话协议作 OR。`get_request_status` 对任意 `needs_clarification` 请求读取会话**当前** active 问题，不按 `producer_request_id` 关联。`commit_request_result` 的 v2 `needs_clarification` 在没有 transition 时仍可写终态，带 transition 时 `expected_revision=None` 会跳过版本检查。`request_acceptances` 先提交，再由 D1 创建内存/Redis 请求并启动线程，崩溃窗口没有恢复所需的请求负载和领取机制；仓储的唯一键并发插入异常仅 rollback/重抛，现有“输家读赢家”的测试使用 MagicMock，没有真实 MySQL 竞争。MySQL 查询异常但 Redis 中还有请求时，状态 GET 仍返回缓存状态。正常 SSE 经 D1 重建公开字段，而 Redis 丢失后的 SSE 直接透传 outbox 原始 payload，可能多出 `inquiry_category` 且字段名不同。以上路径均未被 668 项回归覆盖。

- [ ] **Step 1: 写针对性失败测试并确认红灯。** (a) POST `/v1/sessions` 传入 `workflow_mode=langgraph, clarification_protocol_version=v2`，未获服务端开关授权时不得创建 v2；输入两者不一致也应拒绝。(b) 已固定 v2 的会话在 MySQL 查询异常、或进程环境变量切换时，不能改由 `fast_path`/旧协议解释。(c) 同一会话先产生问题 Q1，消费后再产生 Q2；GET Q1 的 producer request 不得返回 Q2 的 `active_clarification`。(d) MySQL 接受记录提交后、`_create_new` 前模拟进程中断；同幂等键重试不能永远返回无人处理的 `accepted`。(e) Redis 有 `completed`、MySQL 查询失败时 GET 不得凭缓存宣称权威完成。(f) v2 `needs_clarification` 不带 transition、或 transition 不带 `expected_revision` 时事务必须拒绝；有 active 问题的 `completed` 不带消费/替代声明时也不得留下旧 active。
- [ ] **Step 2: 收紧会话协议入口。** 公开会话请求体只接收参与者；`workflow_mode/protocol_version` 由服务端受控 rollout 配置决定并在创建时一起持久化，禁止客户端任意指定。若必须提供内部 Canary 入口，应使用服务器端鉴权/配置且显式拒绝 `fast_path+v2` 等不合法组合。D1 与 C3 只读取持久化会话协议；已存在会话读取失败返回 503，不通过当前环境变量猜测。
- [ ] **Step 3: 修正请求专属投影。** 仓储按 `producer_request_id` 读取该请求生成的问题（含已消费/替代状态），`GET /v1/recommendation-requests/{id}` 只投影本请求问题。`GET /v1/sessions/{id}` 才读取会话当前可选择的问题。旧请求不再显示后续 Q2；过期/已消费的 Q1 不得作为可点击选项。
- [ ] **Step 4: 封闭接受记录崩溃与并发窗口。** 给 v2 接受记录定义 `accepted → running → terminal/recovery_required` 状态与有界领取期限，并持久化恢复工作流所需的最小请求载荷或采用等价的持久任务队列；原始幂等键和健康档案不得明文落库。重试同键必须返回原 request ID，并能安全地领取尚未启动的任务或返回明确可恢复错误；不得凭一条永久 `accepted` 记录假称任务仍在运行。用两个独立 MySQL 连接和同步屏障实际竞争同一个新幂等键；唯一键冲突/死锁输家须在 rollback 后读取赢家并返回原 ID，同键不同载荷则 409，不能把正常竞争映射成 503。并发领取只能有一个赢家。
- [ ] **Step 5: 收紧事务与 fail-closed GET/SSE。** v2 `needs_clarification` 必须带合法 transition 和 `expected_revision`；有活跃问题的 `completed` 必须明确消费或替代，不能保留旧 active 指针。对 v2，MySQL 查询失败时即使 Redis 显示 `completed`/`needs_clarification` 也返回 503；恢复的 SSE 事件必须与正常 D1 发布事件使用相同公共契约。尤其不能把 outbox 原始 `inquiry_category` 传给前端；测试 `clarification_needed` 和 `result_committed` 两类事件的 JSON 及 `Last-Event-ID` 续传。将 `sessions.active_fencing_token` 从 `INT` 调整为与现有 `fencing_token`/Redis 自增计数一致的 `BIGINT`，并以已存在 INT 列的增量迁移测试验证。
- [ ] **Step 6: 重新执行门禁。** 运行本任务新增测试、后端完整回归、前端完整回归、构建、Ruff 与 `git diff --check`。重新做真实 MySQL/Redis 的 Q1→Q2 请求专属 GET、协议切换、接受记录崩溃及恢复场景，并在报告中分别记录自动化、真实存储、真实模型证据；在线模型仍不可达时不能声称真实模型验收通过。

**完成标准：** 服务端独占 Canary 协议选择；旧请求不展示新问题；任何持久化接受记录都有可判定的运行/恢复路径；数据库未知时不返回缓存中的权威终态；所有新增测试及原有回归通过后，才可重新评估 Canary。

**2026-09-28 Task 8 执行状态：** Step 1、2、5 已完成；Step 3 的公开投影已改为按 producer request 且仅显示当前 pending 问题，历史 consumed/superseded 仍保存在问题账本，尚未提供历史问题查询接口。Step 4 已实现唯一接受记录、执行租约及同键同载荷 POST 重领，真实 MySQL 双连接竞态与启动前崩溃模拟通过；没有持久化用户消息或自动任务队列，恢复需要客户端保留并重放原 POST。Step 6 后端 683 项、前端 38 项、构建和静态检查通过；两条真实模型成单通过，但强制时间冲突样本返回 `constraint_conflict/failed`，真实模型澄清 Q1→结构化选择尚无成功证据，Canary 不放行。详见[验收报告](../../../reports/2026-09-28-clarification-v2-acceptance.md)第 7 节。

### Task 9: 执行租约与会话锁的代次隔离（v2 Canary 前）

**复现证据：** 当前 `request_acceptances` 的 `running` 租约到期后，新 owner 可以重领同一 request ID；旧线程即使不再拥有租约，仍可调用 D1 `update_status('failed')`。内存仓储复现了 MySQL 接受记录显示新 owner/running、D1 投影却被旧线程写成 failed。新线程若撞上旧线程尚持有的 Redis 会话锁，也会经 `_finalize_lock_failure` 把同一请求投影为 failed。单纯缩短 3600 秒租约或加 D1 心跳不能解决：模型调用卡住时心跳可能仍活跃；租约过期也不会自动停止旧 C3 的会话锁心跳、提交和 SSE。

- [ ] **定义 v2 执行代次。** `request_acceptances` 增 `execution_generation BIGINT NOT NULL DEFAULT 0`。领取必须在单个 MySQL 事务内锁住接受记录，先检查该 request 没有终态 `recommendation_logs`，再 CAS 增代次并返回 `{owner,generation,lease_until}`；续租和释放均核对 request、owner、generation、未过期租约，使用数据库时间。原始幂等键、消息与健康资料不明文落库。
- [ ] **把代次传到 C3 边界。** D1 对 LangGraph v2 启动显式执行代次；模型调用与工作流有绝对超时。会话锁心跳同时检查 DB 租约，丢失代次后停止续 Redis 锁，在节点及最终提交前中止。新代次撞上旧会话锁时保持可重试状态，不能写 failed 终态或失败 SSE。
- [ ] **统一终态事务。** v2 的 completed、needs_clarification、failed/cancelled/interrupted 在同一事务内按固定顺序锁 session 与 acceptance，核对代次、租约及 fencing，再写唯一 `recommendation_logs`/outbox，并把 acceptance 置 terminal。旧代次无权提交，即使仍持有过期前获得的 Redis 锁。领取与提交通过 acceptance 行串行化。
- [ ] **隔离投影。** v2 的 D1 状态、临时 SSE、Redis 快照带 generation，写入使用原子 compare-generation；GET/SSE 按 MySQL 当前代次过滤，旧线程迟到写入不可覆盖新代次。已提交的成功/追问事件仍只由 outbox 投递。
- [ ] **确定性竞态验收。** (A) 旧线程停在提交前，租约过期、新代次领取后放行旧线程：旧提交、状态和 SSE 全被拒，只有新代次能提交；(B) 新线程遇旧 Redis 锁：无 failed/terminal 污染，可继续重试；(C) 提交先于重领、重领先于提交两种锁序均只产生一个终态日志和 outbox；(D) 旧代次在新代次投影后迟到写 Redis，GET/SSE 不显示旧事件。再跑 v2 真实 MySQL/Redis 故障矩阵和在线模型样本。

**2026-09-29 范围调整：** 用户明确不再维护默认确定性旧流程。本任务的修复与验收以 LangGraph v2 为准；旧流程兼容性失败只记录，不作为 v2 放行门禁，也不为此扩大修改范围。

**执行边界：** 这不是 D1/C4 局部补丁，需要改 C3 提交接口；在这套代次隔离验收前，不把租约重领当作已经安全支持活线程假死，也不放行 v2 Canary。

## 执行注意事项

1. 任务 2 与任务 4 必须一起评估失锁语义：仅比较“上次提交 token”不足以识别刚失锁、而新 worker 尚未提交的旧实例；获取新锁时登记 `active_fencing_token`，提交要求相等且检查 Redis 锁归属，并通过受控竞态测试证明边界。Redis 与 MySQL 不能原子双写，测试须包含登记失败后释放锁及锁在登记前后失效的窗口；如无法满足 I7，则不放行 v2。
2. 任务 3 与任务 5 必须一起评估事件恢复：outbox 标记 `dispatched` 不代表 Redis 事件永存。已提交的公开事件需能直接从 MySQL outbox 还原，且 D1 不能在持久写入失败时向 dispatcher 假称成功。
3. 任何看似“修复”但需要从 Redis pending 推断 v2 问题身份的实现都不满足设计；Redis 只可在 MySQL active ID 与 revision 校验后重建投影。
4. 完成每个任务后检查 `git diff --check` 与对应测试，只暂存本任务文件。所有代码与报告完成后再做一次整体审查；668 个测试通过也不能替代任务 8 的场景证据。
