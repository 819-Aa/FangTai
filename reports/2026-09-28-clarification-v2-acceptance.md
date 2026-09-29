# Agent 澄清协议 v2 综合验收与放行评估报告

- **日期**：2026-09-28
- **执行依据**：`docs/superpowers/plans/2026-09-28-clarification-v2-closure.md` (Task 7–8)
- **对应设计规范**：`docs/superpowers/specs/2026-09-27-agent-clarification-lifecycle-design.md` (I1–I7)
- **执行环境**：Windows 11, Python 3.13.13, MySQL 8.0 (127.0.0.1:3309), Redis 7 (127.0.0.1:6382), Qdrant (127.0.0.1:6339), Vue 3 + Vitest + Vite

> **最新裁决（2026-09-29）：LangGraph v2 唯一入口与真实 Q1→结构化选择→成单闭环通过验收；旧确定性实现清理后的全目录测试也已通过。以第 9–10 节为准。** 下文第 1–8 节保留当时的故障、修复与复验记录；其中“`fast_path` 默认”“v2 Canary 未放行”“真实澄清证据缺失”均是历史裁决，不代表当前代码状态。当前新会话固定为 `langgraph/v2`，旧协议会话不再启动推荐工作流。

> **Task 8 复验（2026-09-28）：** 协议固定、请求专属问题、v2 事务约束、接受记录租约重领与 SSE 恢复格式已修复；后端 683 项、前端 38 项通过。真实 `qwen3.8-max` 两条菜单成单成功，但时间冲突样本没有形成澄清问题，结构化 Q1→选择→终态的**真实模型证据仍缺失**。因此保持 `fast_path` 默认，v2 Canary 尚未放行。下文 Task 7 的原始数字为历史记录，以第 7 节的新证据为准。

> **2026-09-29 再排查：** 已确认上次在线时间冲突样本中模型实际选择了 `ask_user`，但 C3 正常解析路径漏传 `clarification_revision/is_v2`，提交被 `CLARIFICATION_REVISION_REQUIRED` 拒绝，原错误投影又遮蔽了提交异常；C3 最小修复及回归已完成。还复现了执行租约过期后旧线程污染 D1 状态的竞态，安全收口需要[Task 9 代次隔离](../docs/superpowers/plans/2026-09-28-clarification-v2-closure.md)，不能只缩短或续租。修复后的在线结构化样本三次均在模型连接阶段失败，尚无新的真实模型澄清成功证据。当前结论以第 8 节为准。

---

## 1. 门禁总结与放行判定

| 门禁项 | 覆盖范围 / 指标 | 结果 | 判定结论 |
|---|---|---|---|
| **门禁 1: 契约与增量迁移** | `db/migrations`, `contracts/clarification.py`, `request_acceptances` 幂等表 | **PASS** | 已有 INT 列升为 BIGINT；接受记录增加执行租约字段，重复迁移通过 |
| **门禁 2: Application 权威事务** | revision 校验, fencing watermark, option 校验, 信封幂等优先 | **PASS** | v2 无 transition、无 revision 和未处理 active 问题的 completed 均被拒绝并回滚 |
| **门禁 3: Outbox 可靠投递** | 会话锁释放后投递, 稳定 `ev_clarify_*` ID, 敏感字段扫描阻断 | **PASS** | 消除双通道直接调用，实现进程重启恢复 |
| **门禁 4: C3/C4 权威裁决源** | 以 MySQL 为准, 拒绝自由文本“选第一个”, 失锁 fail-closed | **PASS** | 公开会话协议由服务端选定；持久化会话状态读取失败直接失败，环境变量切换不改变旧会话 |
| **门禁 5: D1 权威恢复与防伪** | Redis 丢失后从 MySQL 日志/问题/outbox 恢复, 故障 503 明确报错 | **部分通过** | 读恢复与崩溃重试通过；租约到期后活旧线程仍可覆盖 D1 状态，需 Task 9 代次隔离 |
| **门禁 6: 前端结构化选择** | 绑定 `question_id`+`option_id`, 刷新拉取 active question, 防并发 | **PASS** | Vitest 38/38 全部通过，Vite 编译无异常 |
| **门禁 7: 自动化故障矩阵** | 协议、事务、竞态、崩溃、投影及 SSE | **PASS** | 新增真实 MySQL 双连接竞态、过期租约重领、启动前崩溃与 Q1→Q2 检查；竞态重复 5 次通过 |
| **门禁 8: 全量自动化回归** | 后端回归 + 前端回归 + 构建 + Ruff | **PASS** | 683 passed, 38 passed，Vite 构建、Ruff 与 `git diff --check` 通过 |
| **门禁 9: 真实在线模型长链样本** | DashScope `qwen3.8-max` 成单与澄清 | **部分通过** | 两条 `completed` 成单成功；时间冲突样本为 `constraint_conflict/failed`，未形成结构化澄清 |

> [!IMPORTANT]
> **2026-09-28 当时裁决（已被第 9 节取代）：【保持 fast_path 默认；v2 Canary 未放行】。** 当时真实在线模型的结构化澄清回复仍未取得成功样本，执行租约过期后的旧/新线程代次隔离也未完成。恢复依赖客户端同幂等键、同载荷重试；进程重启不会自主重放用户消息。

---

## 2. 自动化故障矩阵执行记录 (Task 7 Step 1)

测试文件：`tests/integration/test_clarification_recovery.py`、`tests/application/test_commit_validation.py`、`tests/c3/test_clarification_lifecycle.py`
执行命令：`.\.venv\Scripts\python.exe -m pytest tests/integration/test_clarification_recovery.py -v`
结果：**9 passed in 0.59s**

| 序号 | 故障矩阵场景 | 测试用例 | 验证机制与断言事实 | 结论 |
|---|---|---|---|---|
| 1 | 提交前失败（校验/图逻辑中断） | `test_commit_validation.py::test_commit_invalid_transition` | 事务整笔回滚，MySQL 无新行，旧问题 status 保持 pending | **PASS** |
| 2 | 提交后 Redis 写入失败 | `test_fault_matrix_post_commit_redis_failure_keeps_mysql_and_outbox_intact` | MySQL `recommendation_logs` 状态为 completed，outbox 完好，GET 200 从 MySQL 恢复 | **PASS** |
| 3 | 提交后 SSE 写入失败 / Redis 丢失 | `test_sse_recovers_dispatched_outbox_events_when_redis_lost` | SSE 从 MySQL outbox 读取已投递事实，稳定事件 ID `ev_clarify_*` 还原 | **PASS** |
| 4 | 已投递后 Redis 丢失 | `test_idempotent_post_recovers_original_request_id_after_redis_evaporation` | MySQL `request_acceptances` 命中同 payload_hash，直接返回原 request_id，不启动 Agent | **PASS** |
| 5 | 两个请求竞争同一原子声明 | `test_fault_matrix_concurrent_race_claim_acceptance` | MagicMock 直接返回 `existing`，验证 D1 对该返回值的处理；未以两个真实 MySQL 连接验证唯一键冲突/死锁后的恢复 | **局部 PASS，真实竞争待验** |
| 6 | 重放同一请求（修改载荷） | `test_idempotent_post_different_payload_conflicts` | 同一幂等键不同载荷检测到冲突，返回 HTTP 409，不污染数据库 | **PASS** |
| 7 | 失锁（Token 过期/被抢占） | `test_clarification_lifecycle.py::test_fencing_token_watermark_rejects_stale_runner` | 登记 `active_fencing_token` 发现水位落后，立即释放锁并 fail-closed，阻止提交 | **PASS** |
| 8 | 过期或已被替代的问题回复 | `test_commit_validation.py::test_commit_expired_clarification_rejected` | 数据库 `CURRENT_TIMESTAMP > expires_at` 校验回滚，返回 `CLARIFICATION_EXPIRED` | **PASS** |
| 9 | 旧客户端纯文本回复（“选第一个”） | `test_graph_orchestrator.py::test_v2_plain_text_reply_rejected` | v2 协议下拒绝纯文本猜测，返回 `CLARIFICATION_REFERENCE_REQUIRED` | **PASS** |
| 10 | 数据库迁移重复执行 | `test_clarification_migration.py::test_migrations_run_idempotently` | 连续两次运行 migration，表结构、数据行与唯一索引一致，无语法错误 | **PASS** |

---

## 3. 全量自动化回归与静态检查 (Task 7 Step 2)

### 3.1 后端自动化回归
```powershell
.\.venv\Scripts\python.exe -m pytest tests/contracts tests/application tests/c3 tests/integration -q
```
- **执行耗时**：135.59 秒
- **执行结果**：`668 passed, 1 warning in 135.59s` (100% 通过率)
- **覆盖子模块**：
  - `tests/contracts`: 契约模型与载荷验证全过
  - `tests/application`: 事务原子性、fencing watermark、幂等竞争全过
  - `tests/c3`: LangGraph Orchestrator、Agent Actions、Policy Gate 全过
  - `tests/integration`: D1 全链路、恢复机制、真实 Qdrant/Redis 全过

### 3.2 静态代码分析
```powershell
.\.venv\Scripts\ruff.exe check src/ tests/
```
- **执行结果**：`All checks passed!` (0 错误，0 警告)

### 3.3 前端单元测试与构建
```powershell
cd frontend
npm test -- --run
npm run build
```
- **Vitest 结果**：
  - `src/stores/recommendation.test.ts` (30 tests passed)
  - `src/features/participants/ParticipantContext.test.ts` (4 tests passed)
  - `src/features/chat/ChatPanel.test.ts` (4 tests passed)
  - **总计**：38 passed (38), 耗时 2.80s
- **Vite 构建结果**：
  - `dist/index.html` 0.53 kB
  - `dist/assets/index-vzJTQLsx.css` 10.82 kB
  - `dist/assets/index-DWD5BtgN.js` 89.36 kB
  - **总计**：`built in 492ms`，无任何类型或打包错误

---

## 4. 端到端样本与跨存储复验 (Task 7 Step 3)

### 4.1 脚本化模型端到端全链路样本（真实 MySQL + Redis + Qdrant 存储）
执行命令：`.\.venv\Scripts\python.exe -m pytest tests/integration/test_langgraph_d1_full_chain.py -v -s`
- **用例 1: `test_d1_api_langgraph_full_chain_happy_path`**
  - 请求 ID: `6915442f-4e29-4147-96b8-9a99298d7f4e`
  - 链路: context_building (74ms) -> query_understanding (27ms) -> retrieval (0.08ms) -> health_menu_planning (9666ms) -> answer_generation (278ms)
  - 终态: `completed`，菜品数严格为 3，MySQL `recommendation_logs` 写入成功，outbox 发布 `result_committed`。
- **用例 2: `test_followup_clarification_survives_redis_consume_failure` (三轮跨存储复验)**
  - 轮次 1: 请求 `f3b78efb-537b-44b0-81b2-1d1d7059d3c7`，触发首次追问，status=`needs_clarification`，MySQL 保存 `clarification_questions` 待消费行。
  - 轮次 2: 请求 `9da55feb-ecac-437b-9fe8-fb533029f953`，选择结构化选项，模拟 Redis 消费抛错，MySQL 依然完成提交，有效终态维持已提交事实，新追问事件稳定投递。
  - 轮次 3: 请求 `7e821bd6-25bf-4665-9b24-2be1b3971de6`，提出新需求，清理旧残留，终态正确。
- **用例 3: `test_d1_api_langgraph_full_chain_time_limit_clarification`**
  - 请求 ID: `5a0b0850-ee33-406a-b197-086577a44985`
  - 验证时间超限主动澄清及选项带出。
- **用例 4: `test_d1_api_langgraph_full_chain_chinese_numeral_dish_count`**
  - 请求 ID: `3a20516e-5580-447e-971d-4e8284068118`
  - 验证自然语言中文数字解析（“三道清淡家常菜”）-> dish_count=3 -> 终态 `completed`，GET 恢复出的 `result_summary.menu_summary.items` 长度严格为 3。

### 4.2 真实在线模型长链样本（真实 DashScope Qwen3.8-max 探测）
- **探测环境**：工作区 `.env` 配置了 DashScope API Key，默认使用 `qwen3.8-max` 模型。
- **探测结果**：
  - 脚本：`scripts/verify_v2_live_samples.py`
  - 请求 1: `24b0c906-edb5-40ff-8c25-611edb2c1b50` (耗时 7.8s)
  - 请求 2: `a1013af2-2740-4b4d-a6fb-6ffef9629a59` (耗时 5.2s)
  - **网络现状**：本机开启了 `Meta Tunnel` (TUN 虚拟网卡)，DNS 查询 `dashscope.aliyuncs.com` 被定向至 fake-IP `198.18.0.48`。由于 TUN 代理服务在上游握手时关闭连接，底层抛出 `[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1032)`。
  - **隔离性证明**：当网络出现故障时，`ContextService` 与 `d1_api` fail-closed，MySQL `recommendation_logs` 未产生脏数据（`log_status=None`），outbox 零泄露，公开 GET 返回标准 500/failed，无任何内部堆栈暴露。

---

## 5. 回滚演练与数据安全保证 (Task 7 Step 5)

1. **会话协议隔离**：
   - 默认环境变量为 `WORKFLOW_MODE=fast_path`；
   - 新建会话若未指定 `clarification_protocol_version="v2"`，默认写入 `NULL` 或遵循既有规则，继续由确定性快编排服务；
   - 既有 v2 会话由 `_trigger_workflow` 根据 MySQL 中持久化的 `workflow_mode` 和 `clarification_protocol_version` 路由，完全互不干扰。
2. **回滚操作演练**：
   - **步骤**：停止服务端为新会话分配 v2；已有 v2 会话继续由兼容代码处理或明确终止并提示重建。不得把结构化选择降级成普通文本，也不得让旧 `fast_path` 猜测 v2 问题身份；
   - **数据影响**：所有数据表改动（`sessions` 新增三列、`clarification_questions`、`request_acceptances`）均为纯增量字段/表，既有 H07 菜谱、营养素基线、健康规则数据表未受到任何修改或丢弃。
    - 回滚验证结果：原记录缺少可复现的切换/恢复命令与旧 v2 会话证据，**待重新演练**。

---

## 6. 报告签名与结论

- **执行人**：Antigravity Agent
- **Task 7 历史结论（已由第 7 节取代）**：当时后端 **668 passed**、前端 **38 passed**，但协议与恢复边界未通过，真实模型样本未完成。

---

## 7. Task 8 修复后的复验证据（取代上述历史结论）

- **服务端协议与兼容性**：公开 `POST /v1/sessions` 拒绝客户端指定 `workflow_mode`/`clarification_protocol_version`；默认仍是 `fast_path`。旧式请求隐式创建会话时固定首轮的服务端模式，三轮 LangGraph 脚本化测试恢复通过。C3/D1 对已持久化会话不再以当前进程环境变量覆盖协议；数据库读取异常 fail-closed。
- **请求与事件恢复**：真实 MySQL 中同会话 Q1 消费后生成 Q2，Q1 producer GET 无 `active_clarification`，Q2 producer GET 显示 Q2。MySQL 日志查询故障时，即使 Redis 缓存为 `completed` 也返回 503。outbox 恢复的 `clarification_needed`、`answer_ready`、`result_committed` 被规范化为 D1 公共 SSE 字段；`Last-Event-ID` 仅续传其后事件。
- **事务与迁移**：v2 `needs_clarification` 缺 transition/expected_revision 和 active 问题未处理就 `completed` 均拒绝。`active_fencing_token` 的已存在 INT 列经幂等迁移升级为 BIGINT。原有 fast_path 的无 transition 追问提交保持兼容。
- **接受记录崩溃恢复**：MySQL 双独立连接竞争同键，返回唯一 winner 与原 ID，重复 5 次通过；同一 request 的执行租约只有一个持有者。模拟接受后 `_create_new` 前崩溃，把租约置为过期后，原 POST 同键同载荷重试以**原 request ID**重新领取。租约有效而缺少运行态时返回含原 ID 的 503，避免把无人处理的 `accepted` 当成成功。租约默认 3600 秒；恢复依赖客户端重放原 POST，**没有自动持久任务队列**。这项选择避免在 MySQL 中存储原始幂等键、用户消息或健康资料；若产品要求无人重试也自动恢复，应另建加密队列与续租机制，再做 Canary。
- **自动化命令**：`python -m pytest tests/contracts tests/application tests/c3 tests/integration -q --tb=short` → **683 passed, 1 warning in 146.33s**；`npm test -- --run` → **38 passed**；`npm run build` → **built**；`ruff check src tests` → **All checks passed**；`git diff --check` → 退出码 0。唯一 warning 为 Starlette TestClient 弃用提示。
- **真实在线模型**：`scripts/verify_v2_live_samples.py` 于 2026-09-28 运行，`qwen3.8-max` 已可达。请求 `e72021c4-0c6f-40cb-8442-74212dc37e5a` 调用模型 5 次、约 64.54 秒，MySQL `completed`，outbox 有 `answer_ready`/`result_committed`；请求 `acea919b-c988-4c0c-a2e9-f4aca33dc99b` 调用模型 5 次、约 56.96 秒，`completed` 且菜单为 3 道菜。两条 GET 隐私扫描均通过。额外时间冲突请求 `8bb98ac9-a335-494a-93ed-48d4893903f9` 实际调用模型 1 次后返回 `constraint_conflict/failed`，无 MySQL 提交日志；该输入未产生 Q1，所以**不能作为真实澄清成功证据**。

**Task 8 当时判定（已由第 9 节取代）：** 核心成单链路与 Task 8 的可复现存储边界已通过；真实模型澄清→结构化选择→再次追问/成单尚未验收，v2 Canary 继续不放行。当时下一步需固定可稳定触发 Q1 的真实模型样本，完成结构化选择与多轮 GET/SSE 核对。

---

## 8. 2026-09-29 并行根因排查与复验

**真实澄清失败的根因。** 上次 `8bb98ac9-a335-494a-93ed-48d4893903f9` 的模型返回了 `ask_user`，不是没有选择澄清。`graph_orchestrator._node_understand_intent` 的正常返回路径漏传 `clarification_revision/is_v2`；后续 `_node_inquire_user` 因而提交 `expected_revision=None`，v2 事务按设计拒绝。对应 MySQL 事实为会话协议 v2、revision=0、无 recommendation log/clarification outbox，说明 Q1 未提交。`runner._finalize` 原先保留业务 `constraint_conflict` 错误，掩盖了提交异常。修复仅补齐 C3 状态传递，并在澄清提交异常时发布 `AUDIT_COMMIT_FAILED` 与原始原因；两条失败测试先复现，再转绿，C3 全套 411 项通过。

**在线重测。** 修复后首次请求 `6fb85a74-9d87-448b-967c-bf535818d79f` 及复验脚本 `scripts/verify_v2_live_structured.py` 的三次有限重试（`352614c7-710f-464b-9da1-7f2c9b6255bb`、`56ae9e63-3d98-48bf-a2a8-a29ad766cd7f`、`0b4f20a1-5f88-49df-bcd8-368b9ae2aa6b`）均在 `qwen3.8-max` 连接阶段报 `MODEL_INVOCATION_FAILED`，没有进入修复后的事务提交。脚本只对该连接错误重试，业务终态不重试；公开 GET 无私有字段，MySQL 无错误提交。**因此不能把自动化修复写成真实模型 Q1→选择验收成功。**

**租约竞态。** 另一独立排查以内存仓储复现：旧执行线程的租约到期、新 owner 领取同一 request 后，旧线程仍能调用 D1 `update_status('failed')` 覆盖公开状态；新线程若碰上旧线程尚持有的 Redis 会话锁，也可能写失败投影。当前 3600 秒租约及同键重试只覆盖进程中断，不足以处理活线程假死与过期重入。安全修复必须给 v2 执行增加 generation，并让 C3 提交、D1/Redis 投影和 SSE 核对代次；详见[Task 9](../docs/superpowers/plans/2026-09-28-clarification-v2-closure.md)。为保持 `fast_path` 和现有成单链路稳定，本轮未用只续租的局部补丁冒充完整修复。

**第 8 节当时结论（已由第 9 节取代）：** v2 澄清提交的已知代码缺口已修复，但真实在线复验受模型连接故障阻断；执行代次隔离仍未实现。`fast_path` 当时保持默认，v2 Canary 当时不放行。

---

## 9. 2026-09-29 LangGraph v2 唯一入口最终验收

**验收范围与裁决。** [唯一入口实施方案](../docs/superpowers/plans/2026-09-29-langgraph-v2-entry-convergence.md)规定的新请求入口、幂等、澄清选择、前端旧会话退出，以及真实模型 Q1→选择→成单闭环已通过。新建会话固定 `langgraph/v2`；API 只启动 LangGraph 编排器。旧 `fast_path/v1` 会话的新推荐请求返回 `409 SESSION_PROTOCOL_UNSUPPORTED`，不存在的会话返回 404；旧链路不再作为新请求的回退入口。本裁决针对本次代码与在线样本，不等同于生产环境持续运行指标或部署审批。

**修复与回归重点。** 代次隔离覆盖执行领取、C3 提交、D1/Redis 状态投影，防止过期执行者覆盖新代次；C3 澄清提交传播 `clarification_revision/is_v2`。自然语言选择只匹配完整序号表达或唯一的选项全文，普通新需求不抽取句中数字；自动绑定使用派生执行载荷，不改变原始 POST 的幂等哈希。D1 对未知 session 在领取接受记录前返回 404；接受记录已领取但 `_create_new` 启动异常时释放本代次，并带原 request ID 返回 `REQUEST_RECOVERY_PENDING`，不退回无代次保护的入口。并发同键请求在赢家状态尚未写入时可收到 503，但携带赢家 request/session ID，使用相同 POST 可重试。前端收到旧会话 409 后清除旧 session，不自动重放当前文字或复制旧上下文。

**自动化复验。** `.\.venv\Scripts\python.exe -m pytest tests --ignore=tests/e2e --ignore=tests/test_prompts_live.py -q --tb=short` → **1691 passed, 8 skipped, 1 warning**（194.46 秒）；唯一 warning 为 Starlette TestClient 弃用提示。该范围包含 v2 入口、真实 MySQL/Redis 并发接受、执行代次、澄清事务、D1 恢复与原有各层非旧 E2E 测试。`npm test -- --run` → **39 passed**；`npm run build` → **通过**；`ruff check src tests scripts/verify_v2_live_structured.py` 与 `git diff --check` → **通过**。其中并发省略 `session_id` 的同键用例断言同一 request/session ID、单次 `_create_new`、单次工作流启动及 MySQL 接受记录对应关系。

**全目录测试说明。** 曾执行 `pytest tests -q` 以扩大覆盖，得到 `3 failed, 1702 passed, 8 skipped, 15 errors`：15 个错误来自要求独立启动 `localhost:8000` 的旧确定性流程 E2E；两个失败来自直接调用在线模型的提示词实验（超时、输出格式不符）；另一个失败是新增 `session_id` 响应字段后测试预期未更新，已修正并在上述回归中转绿。扩展回归曾再暴露两个 D1 旧不变量测试使用固定幂等键、复用旧 MySQL 记录且并发启动工作流，已改为唯一键并隔离后台执行。这些事实说明裸跑整个 `tests` 目录当前不是全绿；本次 v2 交付门禁按方案规定的范围通过，不把旧确定性流程或在线提示词实验误算为 v2 回归。

**真实模型在线闭环。** 直接运行 `.\.venv\Scripts\python.exe scripts/verify_v2_live_structured.py`，使用真实 `qwen3.8-max`，无需本地代理。会话 `5bdda905-ba9`：

| 轮次 | Request ID | 公开终态 / MySQL `recommendation_logs` | 模型调用与耗时 | GET / SSE |
|---|---|---|---|---|
| 首轮时间约束冲突 | `fc8c9a45-6d21-4ba5-8396-3682ba21d218` | `needs_clarification` / `needs_clarification`，错误码 `TIME_LIMIT_EXCEEDED`；Q1=`q_fc8c9a45-6d21-4ba5-8396-3682ba21d218` | 4 次，约 92.54 秒 | GET 返回 Q1；SSE 有 `clarification_needed` |
| 结构化选择 Q1 的选项 1 | `4308b11a-2417-4ae7-a106-42ed4bbc9157` | `completed` / `completed` | 5 次，约 50.30 秒 | 第二轮 SSE 有 `answer_ready`、`result_committed`；首轮 GET 的 `active_clarification=null` |

**持久化事实核对。** 两条 `request_acceptances` 均为 `terminal`、代次 1；Q1 在 `clarification_questions` 中为 `consumed`，`accepted_request_id` 指向第二轮请求，`accepted_option_id=1`。首轮 `clarification_needed`、第二轮 `answer_ready` 与 `result_committed` 的 outbox 记录均为 `dispatched`，事件 ID 分别采用稳定的 `ev_clarify_*`、`ev_answer_*`、`ev_result_*` 格式。以上同时核对了公开 GET、SSE 和 MySQL，而非只依据模型输出文本。

**验收边界。** 执行异常后的恢复仍要求客户端用相同幂等键、相同原始载荷重试；本方案没有无人重试的持久任务队列。此次真实模型闭环是 Q1→选项→`completed`，没有声称真实模型 Q1→Q2 或长期压测、生产 p95 已验证。

---

## 10. 2026-09-29 旧编排清理与全目录复验

用户确认不再维护旧确定性推荐链路后，删除 `DeterministicRecommendationOrchestrator`、线性 `_run_locked`、旧五角色提示词配置与只验证旧入口的 E2E/提示词测试。LangGraph 仍需的会话锁、执行代次、证据构建和原子提交保留在 `c3/runtime.py` 的 `AgentRuntime`；原确定性文件中的两个约束合并函数移入 `c3/intent_helpers.py`。`D1` 不再提供旧模式环境变量回退；新请求始终选择 LangGraph v2，旧会话仍明确返回 409。

清理后执行 `.\.venv\Scripts\python.exe -m pytest tests -q --tb=short`：**1556 passed, 8 skipped, 1 warning in 120.88s**；唯一 warning 是 Starlette TestClient 弃用提示。前端 `npm test -- --run`：**39 passed**；`npm run build`、`ruff check src tests scripts/verify_v2_live_structured.py`、`git diff --check` 均通过。第 9 节“裸跑全目录测试不全绿”描述的是清理前状态，已由本节的全目录复验取代。真实模型在线闭环证据仍采用第 9 节已经核对的 Q1→结构化选择→成单样本；本轮纯代码清理未重新调用付费模型。
