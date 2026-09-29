# LangGraph v2 唯一执行入口实施方案（已验收）

> **状态（2026-09-29）：** 本方案的入口收敛、自动化回归和真实模型 Q1→结构化选择→成单验收已通过；旧编排实现已清理。证据与范围见[综合验收报告第 9–10 节](../../../reports/2026-09-28-clarification-v2-acceptance.md)。

> **执行方式：** 按任务逐项写失败测试、确认红灯、做最小修改、复验。本文件记录已实施的范围与验收条件。

**目标：** 所有新推荐请求只进入 LangGraph + 澄清协议 v2；旧确定性编排器不再由 API 启动；普通新需求不会被误识别为旧问题的选项回复；原始载荷与幂等哈希严格解耦；旧协议会话明确结束，不假装迁移其业务上下文。

**架构：** D1 以持久化会话协议判定请求身份，只接受 `langgraph/v2`；无 `session_id` 的请求创建同协议会话。C3 只执行 LangGraph Agent，领域工具、健康硬约束、Application 原子提交及现有执行代次隔离保持原边界。旧会话的历史 GET 可以保留，新请求明确拒绝旧协议，不静默迁移其菜单或健康上下文。

**技术栈：** FastAPI、LangGraph、MySQL、Redis、Vue/Pinia、pytest、Vitest。

---

## 1. 行为约定与边界矩阵

| 输入场景 | 当前行为 | 目标优化行为 | 保护机制与核心考量 |
|---|---|---|---|
| `POST /v1/sessions`，无环境变量 | 默认创建 `langgraph/v2` | 固定创建 `langgraph/v2`，不由旧模式环境变量改写 | 彻底掐断旧模式创建入口 |
| `POST /v1/recommendation-requests`，省略 `session_id` | 部分场景回退 `fast_path` | **先查幂等接受记录**；命中同键直接复用原会话与请求；未命中才创建 v2 会话并领取代次 | 保证同键只有一个接受记录和一个执行者；并发首次请求可能留下未引用的空会话，不作为本轮阻断项 |
| 请求已有 `langgraph/v2` 会话 | 走 v2 链路 | 严格保持，不受进程级环境变量波动影响 | 保证会话生命周期内协议不可变 |
| 请求已有 `fast_path/v1` 会话 | 可能启动确定性编排器 | 返回 `409 SESSION_PROTOCOL_UNSUPPORTED`，拒绝启动工作线程 | 阻断旧会话异构数据污染 |
| 有活跃澄清 Q1，输入“选第一个”、“按第2个来” | 自动绑定 Q1 选项 | 保留明确选项表达的自动绑定，以 MySQL 当前有效 Q1 与私有快照为准 | 维持人机交互的便利性 |
| 有活跃澄清 Q1，输入“推荐一道清淡家常菜”、“换三道不辣的” | 宽松匹配误抽取“一/三”，误判为选项确认 | 只识别完整的明确选项表达或唯一匹配的选项全文；其他输入作为新需求，旧问题仅在新请求成功提交时作废 | 保护全新需求意图；失败时 Q1 仍可选择 |
| 触发自然语言自动绑定选项时 | 可能就地修改原始 `body` 字典 | **原始 HTTP body 保持只读**；`clarification_response` 仅作为内部派生参数，不改写持久化载荷 | **P0 幂等保护：防止重发时 Payload Hash 漂移** |
| 前端遇旧会话返回 `SESSION_PROTOCOL_UNSUPPORTED` | 可能持续复用旧 session | 清除本地旧 session，保留用户已输入文字并提示“请在新会话提出完整需求”；下一次发送创建 v2 会话，不自动重发 | 旧菜单和澄清上下文不会迁移，避免“换上一道菜”等相对请求在空会话中改变含义 |

---

## 2. 详细任务分解

### Task 1：统一会话与请求入口（幂等保护）

**核心文件：** `src/food_agent_v2/api_app.py`、`src/food_agent_v2/d1/__init__.py`、`src/food_agent_v2/c4/__init__.py`
**测试文件：** `tests/integration/test_clarification_recovery.py`、`tests/integration/test_clarification_v2_closure.py`

- [x] **Step 1: 编写红灯测试**
  - 测试无环境变量直接 POST、不带 `session_id` 的同键重发、已有 v2 会话、旧 `fast_path/v1` 会话和不存在的会话；
  - 断言并发同键请求只有一个 `request_acceptances` 赢家、一个执行者，输家返回赢家的 request/session ID；不要求零孤立空会话；
  - 断言旧会话返回 `409 SESSION_PROTOCOL_UNSUPPORTED` 且后台工作流零启动。
- [x] **Step 2: 固定服务端会话创建协议**
  - 将 `POST /v1/sessions` 的服务端默认写死为 `workflow_mode="langgraph"`、`clarification_protocol_version="v2"`；
  - 请求体继续禁止客户端自由覆写协议版本，环境变量 `WORKFLOW_MODE=fast_path` 不得影响新会话。
- [x] **Step 3: 重排 D1 请求入口与幂等检查顺序**
  - **原始载荷计算**：先对传入的原始 `body`（未做任何内部增补）计算原始 `payload_hash`；
  - **先查接受记录**：在生成或创建新 session 之前，先以 `idempotency_key_hash` 查询 `request_acceptances`。若命中同键，直接沿用已关联的 `session_id` 与 `request_id`；并发首次请求都查不到时，仍由接受记录唯一键决定赢家；
  - **按需创建会话**：仅在未命中接受记录且请求未提供 `session_id` 时，用新生成的 ID 调用 `ensure_session_record(..., workflow_mode="langgraph")`，由 C4 持久化为 v2；若请求提供了 `session_id`，校验其持久化协议必须为 `v2`，不存在则返回 404，旧协议返回 409。并发输家产生的未引用空会话可留待独立清理，不为它引入跨表新事务或队列。
- [x] **Step 4: 清除 API 层的旧编排器路由分流**
  - `_trigger_workflow` 移除针对 `DeterministicRecommendationOrchestrator` 与 `WorkflowRunner` 的条件分流，仅实例化 `LangGraphRecommendationOrchestrator`；
  - 后续清理已将旧编排类和线性执行体删除；LangGraph 共用的锁、证据与提交能力迁至 `c3/runtime.py`，约束合并函数迁至 `c3/intent_helpers.py`。

---

### Task 2：收紧文字选项识别，严格区分新意图与选项确认

**核心文件：** `src/food_agent_v2/c3/graph_orchestrator.py`、`src/food_agent_v2/d1/__init__.py`
**测试文件：** `tests/c3/test_clarification_lifecycle.py`、`tests/integration/test_clarification_recovery.py`

- [x] **Step 1: 编写反例与边界红灯测试**
  - 正向选项用例：`选第一个`、`按第2个来`、单独输入数字 `2`、`清淡家常`（唯一且完全等于选项文本）必须成功绑定；
  - 反向新意图用例：`推荐一道清淡家常菜`、`换成三道菜`、`重新推荐`、`不放宽时间，重新做`、`不要葱蒜` 绝对不绑定；
  - 断言反向用例在存在活跃 Q1 时，不注入 `clarification_response`；新请求成功提交时 Q1 与结果在同一事务中作废，提交失败时 Q1 保持 pending。
- [x] **Step 2: 使用完整表达匹配选项**
  - 不维护“推荐/换/不要/改”等新意图动词黑名单；选项文字本身可能包含这些词。只允许以下完整匹配：
    1. 纯数字（如 `"1"`、`"2"`）；
    2. 完整的序号选择表达（如 `选第一个`、`按第2个来`）；不得用 `finditer` 从普通句中提取任意数字；
    3. 文本与当前活跃问题**唯一一个** `option.text` 完全一致（去除首尾空白和句末标点后比较）；多个选项同文案或无法唯一确认时不自动绑定。
  - 其他输入按新需求处理。旧 Q1 的 supersede 只在新请求成功提交时生效，不在收到文字时提前修改问题状态。
- [x] **Step 3: 原始载荷只读与执行上下文派生**
  - D1 中严禁 `body["clarification_response"] = ...` 就地改写用户请求；
  - 先以原始 `body` 计算并固定 `payload_hash`，自动绑定成功后再构造浅拷贝 `execution_body = dict(body, clarification_response=resolved_response)`，交给 `_create_new` 和工作线程；私有 Redis 请求状态可保存派生选择供运行使用，但接受记录中的幂等哈希始终对应原始 HTTP 请求。相同原始 POST 重发应得到同一 request ID。

---

### Task 3：前端旧会话退出与新会话提示

**核心文件：** `frontend/src/stores/recommendation.ts`、`frontend/src/features/chat/ChatPanel.vue`
**测试文件：** `frontend/src/stores/recommendation.test.ts`、`frontend/src/features/chat/ChatPanel.test.ts`

- [x] **Step 1: 编写前端红灯测试**
  - 模拟请求返回 409 `SESSION_PROTOCOL_UNSUPPORTED`；
  - 断言 store 清空已废弃的旧 session ID，同时保留当前用户输入内容；
  - 断言当前请求不自动重发，下一次用户发送才创建 v2 会话。
- [x] **Step 2: 实现明确的新会话提示**
  - `recommendation.ts` 调用 `createRequest` 时捕获 `SESSION_PROTOCOL_UNSUPPORTED`：
    1. 清除 localStorage 中的旧 session 标记；
    2. 清除 store 中旧 session ID 与旧问题选择状态，保留用户刚输入的文字；
    3. 提示“旧会话无法继续；请在新会话提出完整需求，旧菜单不会自动带入”；
    4. 不自动重发，不把旧菜单、问题或健康上下文复制到新会话；下一次用户发送时按现有 `ensureSession()` 创建 v2 会话。
- [x] **Step 3: 验证端到端前端体验**
  - 运行 `npm test -- --run` 与 `npm run build`，确保无类型报错与单测红灯。

---

## 3. 验收门禁与停止条件

必须依次通过以下三级门禁，方可认为实施完成：

### 门禁 1：自动化契约与单元回归（100% 必须通过）
```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_clarification_recovery.py tests/integration/test_clarification_v2_closure.py tests/c3/test_clarification_lifecycle.py tests/d1 -q
.\.venv\Scripts\python.exe -m pytest tests/contracts tests/application tests/c3 tests/c4 tests/d1 tests/integration -q
.\.venv\Scripts\ruff.exe check src tests
cd frontend; npm test -- --run; npm run build
```

### 门禁 2：意图分流与幂等一致性检验
- [x] 输入 `推荐一道菜` 时，不会将 `一道` 误绑到选项 1；新请求成功提交时 Q1 才作废，失败时 Q1 仍 pending；
- [x] 输入 `选第一个` 时，准确消费 Q1，且同一请求重发时 `payload_hash` 与 `request_id` 保持完全一致；
- [x] 并发省略 `session_id` 的同键请求只有一个接受记录和一个执行者，输家返回赢家的 request/session ID；空会话不计入本轮门禁。赢家写入运行态前，输家可能收到带同一 request/session ID 的 503 并按原 POST 重试。

### 门禁 3：真实模型样本验收（网络可达时执行）
- 运行 `scripts/verify_v2_live_structured.py`，记录包含真实模型调用的 Q1 追问与选项确认完整闭环证据；
- 若遭遇外网代理网络异常（TLS EOF），记录为外部环境不可达，不作为代码逻辑缺陷判定，但必须保留重试命令。

### 本轮验收记录

- 后端全量 `pytest tests -q`、前端 `npm test -- --run`、`npm run build`、Ruff 和 `git diff --check` 的结果见综合报告第 9 节。
- 真实 `qwen3.8-max` 两轮请求先提交 `needs_clarification`，再由结构化选项提交 `completed`；MySQL 中 Q1 为 `consumed`，SSE 的澄清与结果事件均已投递。
- 这证明本方案规定的在线闭环；生产持续运行指标与无人重试自动恢复不在本方案验收范围内。
