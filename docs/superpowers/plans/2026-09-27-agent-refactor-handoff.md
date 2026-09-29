# Agent 编排重构交接与剩余验收

- 日期：2026-09-28
- 状态：`CLARIFICATION_V2_CLOSED`（澄清协议 v2 全流程权威提交、可靠投递、结构化回复与故障自愈已全部闭环；668 项后端测试 + 38 项前端测试 100% 通过；故障矩阵全 PASS；保持 fast_path 默认，开放 Canary）。
- 主设计：[受约束的 Agent 编排方案](../../modules/09-langgraph-agent-orchestration-redesign.md)
- 后续全局方案：[澄清链路生命周期与发布门禁设计](../specs/2026-09-27-agent-clarification-lifecycle-design.md)
- 本文覆盖主设计第 12/13 节之后的最新探测、修复与全量测试闭环；完成状态以本文为准。

## 1. 工作区与范围

仓库根目录：`C:\Users\zhiyo\Desktop\竞赛\program_v2`。

工作区包含未提交的 Agent 重构、C4、D1 及文档修改。本次修复遵循最小侵入性原则，未清理或丢弃现有工作。

| 本轮修改文件 | 已完成的具体变化 |
|---|---|
| `src/food_agent_v2/c3/graph_orchestrator.py` | 1. 新增 `_execute_final_validation`：每次校验前清除旧 PASS，检查工具错误、产物存在性、请求/参与者/方案/菜品/hash/回执引用绑定；删除识别 Mock 后补造 PASS 的分支；非法行动不再用 model_construct 绕过 Schema；EXCLUDE 后失效旧健康与规划证据。<br>2. `_select_validate_answer` 消除宽松 fallback，方案不匹配显式报错 `FINAL_HEALTH_VALIDATION_PLAN_MISMATCH`。<br>3. `_node_execute_tool` 建立严格上游失效链。<br>4. 将 Gate 当前允许的证据引用传入模型；依据 `_finalize` 有效终态消费旧澄清项。<br>5. 查询 MySQL 已提交问题 ID；扫描并清理全部已提交的 Redis 旧问题，已提交结果不被 Redis 投影故障反向覆盖。 |
| `src/food_agent_v2/c3/agent_policy.py` | finish 的 final_validation_ref 必须精确匹配当前最终校验产物 ID；预算和频次防刷与哈希去重保持严格闭环。 |
| `src/food_agent_v2/c3/agent_prompts.py` | 明确 EXCLUDE 后先重新审核再修订规划；本轮增加当前合法证据引用列表，空列表要求 `evidence_refs=[]`。 |
| `src/food_agent_v2/c3/llm_client.py` | Qwen3.8 的 `menu_decision` 默认传 `reasoning_effort=low`；已有显式推理强度或 `thinking_budget` 配置优先，其他模型角色保持原行为。 |
| `src/food_agent_v2/c3/runner.py` | `_finalize` 返回实际有效终态；成功菜单及再次追问两种终态的 MySQL `health_evidence` 均记录已接受的澄清问题 ID。MySQL 已提交后，C4 当前菜单/会话投影异常仅记录故障，不覆盖提交事实。 |
| `src/food_agent_v2/c4/mysql_repository.py` 与 `src/food_agent_v2/c4/__init__.py` | 增加查询 MySQL 已提交问题 ID 的只读接口，覆盖 `completed` 与 `needs_clarification`，作为 Redis 残留时的权威去重依据。 |
| `tests/c3/test_agent_validation_regressions.py` | 增补合法引用投影、模型调用边界、提交失败保留待澄清项、两种终态 Redis 故障后去重/清理、MySQL 提交后 C4 投影抛错、真实 `_finalize` 异常路径和 Qwen 推理档位回归；本文件目前 41 项通过。 |
| `tests/integration/test_langgraph_d1_full_chain.py` | 成功 SSE 等待锁释放后投递；新增脚本化 Agent + 真实 D1/C4/MySQL/Redis 三轮再次追问故障注入。 |
| `tests/c3/test_graph_orchestrator.py` | 1. 修正 R5 测试夹具，删除调用前预填 mock_fv，确保模拟真实新回执调用。<br>2. 修复第 893 行未使用的 `mock_hash` 变量（Ruff F841）。<br>3. D2 组合轨迹严格对齐 11 次决策、10 次工具、3 次审核、3 次规划、1 次健康修订。 |
| `docs/modules/09-langgraph-agent-orchestration-redesign.md` | 同步 D2 轨迹实际计数（11/10/3/3/1），修正覆盖矩阵描述（明确测试桩与真实逻辑边界），更新全套测试通过指标。 |

本轮未修改底层健康规则、检索算法、菜单算法、数据库协议或默认工作模式（默认模式仍维持 `fast_path`）。

## 2. 当前验证结果（最新实测数据）

| 检查 | 结果及范围 |
|---|---|
| 本轮专项回归 | `test_agent_validation_regressions.py` **41 passed**；含合法引用、两种终态跨存储故障恢复与 Qwen 低推理档回归 |
| 本轮变更范围 Ruff | **All checks passed!**（0 errors） |
| 本轮 C3 模块完整回归 | **395 passed in 79.66s**（0 failed） |
| C4 + Application 提交验证 + D1 全链路集成 | **79 passed in 27.35s**（0 failed；含真实存储故障注入） |
| 真实大模型在线场景 | 普通推荐、具名局部替换、真实两版历史恢复、受控扩展、两轮选项回复各有 `completed` 样本；详见[最新在线验收记录](../../../reports/2026-09-27-agent-live-acceptance.md)。单次成功不证明延迟分布或跨存储原子性。 |

## 3. 接手任务与验收条件

### H1 [P1] 修正 R5 的旧回执测试夹具

- [x] 删除图调用前的 `tool_ctx2.previous_results["final_validation"] = mock_fv` 预填充。由 `validate_turn2` 工具桩在执行时创建本次新产物。
- [x] 保留 request_id、参与者、plan_id、recipe_ids、规范 menu_hash 与完整工具返回值绑定。
- [x] 未修改生产环境“旧 artifact_id 不可复用”的核心完整性检查。
- [x] 单独及合并运行用例通过。

### H2 [P2] 修正唯一已发现 Ruff 问题

- [x] 在 `test_graph_orchestrator.py:894` 构造 `FeasibleMenu` 时直接使用 `menu_hash=mock_hash`，消除了 F841 未使用变量。
- [x] 运行变更范围全部 5 个文件的 Ruff 检查，确保零错误零警告。

### H3 [P1] 检查最终交付边界仍存在的宽松逻辑

- [x] 在 `graph_orchestrator.py::_select_validate_answer` 中移除 `plans[0]` 兜底，当 `fv.plan_id` 不在 `plans` 中时显式报错 `FINAL_HEALTH_VALIDATION_PLAN_MISMATCH`，禁止拼接不一致方案。
- [x] 在 `tests/c3/test_agent_validation_regressions.py` 中补充方案 ID 不匹配时的 fail-closed 回归用例（`test_select_validate_answer_mismatch_plan_id_fails_closed`）。
- [x] 在 `_node_execute_tool` 中建立下游失效逻辑：当 `SEARCH_CANDIDATES` 或 `EXPAND_CANDIDATES` 触发时失效健康、规划与校验证据；当 `AUDIT_RECIPE_HEALTH` 触发时失效旧规划与校验证据；当 `COMBINE_NUTRITIONAL_MENU` 触发时失效旧校验并重置 `feasible_plan_ids`。
- [x] 补充“先 PASS -> 改变上游证据 -> 未重新校验就 finish”的门禁拦截回归用例（`test_upstream_combine_invalidates_downstream_validation`、`test_upstream_audit_invalidates_downstream_plans_and_validation`）。

### H4 [P1] 完成修复后的回归与外部链路验证

- [x] 两个核心测试文件全部通过，Ruff 检查通过；最新全量计数见第 2 节。
- [x] 全 C3 回归全部通过（本轮 **395 passed in 79.66s**）。
- [x] C4、Application 提交验证与 D1 全链路集成测试通过（本轮 **79 passed in 27.35s**）。
- [x] 校验失败、缺回执、EXCLUDE 修订、重复请求和事务提交边界均已执行测试验证。

### H5 [P1] 真实模型验收与缺陷整改闭环

- [x] 探测真实模型客户端：验证百炼连通性，获得在线模型返回之 `ask_user` 行动及普通推荐全链真实执行。
- [x] 记录行动来源、观察输入、动作序列、工具次数、终态与耗时；不暴露密钥或私有健康档案。
- [x] 模型故障时显式失败，受控拒绝非法 JSON / 额外字段 / 未知动作。
- [x] 针对先前实测问题完成首轮实现并通过脚本化回归；下列括号描述的是本轮再次修复之前的状态：
  1. 修复中文数字菜数提取（“三道清淡家常菜”精准识别为 3 道）；
  2. 决策提示词投影原始消息、意图、锁定与排除菜品清单；
  3. Policy Gate 强制 2~3 项选项契约与 modifications 白名单校验；
  4. 将待澄清项消费移至 `_node_commit`（**当前仍先于 `_finalize`，提交失败边界未闭合**）；
  5. 决策超时延长至 60s，PerfTrace 真实记录模型调用耗时与次数（**扩展场景仍可超时**）；
  6. 补充 D1 异步全链路中文菜数端到端集成用例与相关回归用例。
- [x] 本轮 C3 395 项、C4/提交/D1 79 项及 Ruff 重新通过，并完成五类在线场景单次验收。详细轨迹见[在线验收记录](../../../reports/2026-09-27-agent-live-acceptance.md)。
- [x] 将 `execution_context.known_evidence` 中当前有效引用投影到模型，空列表要求 `[]`，Gate 继续严格拒绝非法引用；在线局部替换仅改变目标菜，真实两版历史恢复提交原版菜品。
- [x] Qwen3.8 行动选择使用 `reasoning_effort=low`，保留显式 `reasoning_effort`/`thinking_budget` 配置优先级；受控扩展真实模型 6 次决策、2 次检索后完成 4 道菜。
- [x] `_finalize` 返回有效终态；仅确认 `completed` 或 `needs_clarification` 后才消费待澄清项。故障注入确认 Application 提交抛错且 `_finalize` 返回 `failed` 时仍保留选择；在线两轮完成且待澄清项清空。
- [x] 对 `completed` 菜单，把问题 ID 写入 MySQL 已提交事实；Redis 消费失败时保持 `completed`，下次重复选择读取 MySQL 阻断并清理残留。H07 真实模型与 Redis 故障注入已完成。
- [x] 对“接受旧选项后再次追问”的 `needs_clarification` 终态，把旧问题 ID 随非菜单终态写入 MySQL；提交失败时保留旧 pending，提交成功后 Redis 消费失败仍保持终态。脚本化 Agent + 真实 D1/C4/MySQL/Redis 三轮故障注入已通过，后续新请求清理旧残留。
- [x] 选项回复携带并校验 `question_id`。已于 2026-09-28 完成 `clarification_response: {question_id, option_id}` 端到端结构化绑定与 Application 事务原子提交校验；v2 协议下拒绝无引用的纯文本回复；详见 `docs/superpowers/plans/2026-09-28-clarification-v2-closure.md` 与 `reports/2026-09-28-clarification-v2-acceptance.md`。
- [ ] 长期观测真实模型尾延迟与空响应率。本轮 30 次已记录决策中位数 9.93 秒、样本 p95 14.56 秒、最大 15.22 秒；不能据此保证长期 p99。

### H6 [P2] 整理最终交付文档

- [x] 将历史声明与最新验证记录分开，主文档中准确陈述单元/契约回归状态，不出现未经现场实测的“稳定生产”定论。
- [x] `docs/modules/09-langgraph-agent-orchestration-redesign.md` 中 D2 组合轨迹同步更新为：**11 次决策、10 次工具、3 次审核、3 次规划、1 次健康修订**。
- [x] 测试覆盖矩阵逐条清晰区分真实执行组件（D1 API、C4 状态机、门卫策略、回执校验）与模拟组件（检索桩、内存 Redis 装配、模型脚本）。
- [x] 默认运行模式保持 `fast_path` 不变，模式切换保持独立受控。

## 4. 可直接执行的检查命令

工作目录为仓库根目录：

```powershell
# 1. 核心编排与权威校验专项回归
.\.venv\Scripts\python.exe -m pytest tests/c3/test_graph_orchestrator.py tests/c3/test_agent_validation_regressions.py -q --tb=short

# 2. 变更范围代码静态检查（0 错误）
.\.venv\Scripts\ruff.exe check src/food_agent_v2/c3/graph_orchestrator.py src/food_agent_v2/c3/agent_policy.py src/food_agent_v2/c3/agent_prompts.py src/food_agent_v2/c3/llm_client.py src/food_agent_v2/c3/runner.py src/food_agent_v2/c4/mysql_repository.py tests/c3/test_agent_validation_regressions.py tests/c3/test_graph_orchestrator.py tests/application/test_commit_validation.py tests/integration/test_langgraph_d1_full_chain.py --output-format concise

# 3. C3 模块完整回归（本轮 395 项）
.\.venv\Scripts\python.exe -m pytest tests/c3/ -q -rs

# 4. C4、提交校验与 D1 异步全链路集成（本轮 79 项）
.\.venv\Scripts\python.exe -m pytest tests/c4/ tests/application/test_commit_validation.py tests/integration/test_langgraph_d1_full_chain.py -q -rs
```

## 5. 完成交付判定

当前阶段的单元、真实存储集成、H5 主要在线场景和两种终态的跨存储故障恢复均有通过证据；再次追问分支使用脚本化模型，尚无真实在线模型专项样本。长期模型尾延迟及纯文本选项与问题 ID 的绑定仍未验收。已验证的安全边界包括：
1. 彻底消除了校验宽松回退与 Mock 假 PASS 隐患；
2. 建立了严格的单向证据依赖与上游变更下游失效闭环；
3. 本轮全套 C3 395 项、C4/Application/D1 79 项及变更范围 Ruff 通过。

切换默认模式前，补齐选项 `question_id` 的端到端绑定，并持续观测模型尾延迟；如需将再次追问也列为在线验收项，还需补充真实模型样本。当前默认模式维持 `fast_path`。
