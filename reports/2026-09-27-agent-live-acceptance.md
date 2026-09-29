# C3 Agent 真实模型验收记录

- 日期：2026-09-27
- 范围：H5 真实在线模型行动选择；工作区当前 `.env` 加载后的 `LLMClient` 与 `LangGraphRecommendationOrchestrator`
- 最新结论：**H5 五类主要在线场景已有成功样本**。`completed` 分支完成真实模型 + H07 故障注入；再次追问的 `needs_clarification` 分支完成脚本化模型 + 真实 D1/C4/MySQL/Redis 三轮集成故障注入。两种终态在 MySQL 已提交后都不会因 Redis 消费失败而被覆盖；30 次在线行动调用仍不足以保证长期尾延迟。下文 17:08–17:25 的失败记录保留作修复前对照。

## 再次追问分支的跨存储复验

接受旧问题选项后，若 Agent 再次追问，`_finalize` 先把旧 `question_id` 与 `needs_clarification` 终态写入 MySQL `recommendation_logs.health_evidence`，再消费 Redis 中的旧 pending。MySQL 提交失败时有效终态改为 `failed`，旧 pending 不消费；MySQL 提交成功而 Redis 消费失败时保留 `needs_clarification`，下一轮依据 MySQL 标记清理旧残留。查询同时覆盖 `completed` 与 `needs_clarification`。

真实测试存储与 D1 的三轮集成：第一轮生成旧问题；第二轮“选第一个”后脚本化 Agent 再次追问，并故意让旧问题的 Redis 消费抛错，D1 仍为 `needs_clarification`，MySQL 旧问题标记为真、Redis 暂留两个问题；第三轮提出新需求后，旧残留被清理。另补充 MySQL 提交后 C4 会话/当前菜单投影抛错的故障回归，终态均保持已提交结果。这里的行动选择是**脚本化模型**，不能算真实在线模型对再次追问的验收。自动化最新结果：C3 **395 passed in 79.66s**；C4 + Application + D1 **79 passed in 27.35s**；Ruff **All checks passed**。

当前无 MySQL/Redis 原子事务。若用户只回“选第一个”，系统按**最新未提交问题**解释；当旧、新问题选项编号相同，纯文本无法表达要重放旧问题还是回答新问题。要严格绑定用户意图，应由客户端随选项回复提交 `question_id`，再由服务端校验其等于当前有效问题。

## 约 21:20–21:31 跨存储消费故障注入与延迟样本

已接受的 `question_id` 现在随成功菜单的 `health_evidence` 一起进入 MySQL 原子提交；下一次读取旧 pending 时，C4 查询 MySQL 已提交事实。若 Redis 消费失败，D1 不再把已提交的 `completed` 覆盖为 `failed`；重复选项被 `CLARIFICATION_ALREADY_APPLIED` 阻断，且在 Redis 恢复后清理残留。

H07 实测：首轮在线模型追问，问题保存在 Redis（请求 `606e8bbe-619c-4b9b-a61f-5faf321cbf8f`）；第二轮“选第一个”在**故意让 Redis 消费抛错**时仍提交 3 道菜，`completed`，MySQL 问题标记为真、Redis pending 仍为 1（请求 `42bcbf08-ebf3-40a0-aca8-e6b9bcea6519`）；第三轮重复选择约 1 秒内 `failed / CLARIFICATION_ALREADY_APPLIED`，0 次模型调用（请求 `a70b77d3-353d-4d35-9188-34d843d52e60`）；补上残留清理后第四轮仍被拦截，Redis pending 从 1 变为 0（请求 `09db4b5a-1542-43dc-83b2-a1854d77a829`）。这验证了 **completed 菜单提交后的可恢复去重**，并非 MySQL 与 Redis 存在原子事务。

已保存 PerfTrace 的 30 次在线 `menu_decision` 调用：中位数约 **9.93 秒**，样本最近秩 p95 约 **14.56 秒**，最大约 **15.22 秒**；本轮没有一次触及 60 秒单次超时。样本来自少量集中场景，不能推断长期 p99；另有一次独立短提示返回空 `content`，系统按 `MODEL_OUTPUT_EMPTY` 失败。

该阶段自动化：C3 **388 passed in 79.81s**；C4 + Application 提交验证 + D1 集成 **77 passed in 27.18s**；Ruff 通过。D1 成功 SSE 在会话锁释放后投递，集成测试已改为等待事件出现，避免读到 `completed` 后立即断言造成的时序竞争。最新计数见上节。

## 本轮修复后的在线复验（UTC+08:00）

本轮将当前有效 `known_evidence` 引用投影给模型，空列表时要求 `evidence_refs=[]`；Qwen3.8 的 `menu_decision` 使用低推理档（其他角色不变，显式配置优先）。[阿里云 Chat Completions 文档](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)列明 Qwen3.8 默认 `xhigh`，支持 `low`，并要求不要与 `thinking_budget` 同时指定。所有下表行动均来自在线模型，扩展场景只控制候选 ID，B4 审核、C2 规划、最终校验与提交仍走实际业务链。

| 场景 | 最新结果 | 请求 |
|---|---|---|
| 明确晚餐，推荐三道清淡家常菜 | `completed`，3 道，5 次在线决策，约 57 秒 | `c2f640f2-f404-4ae5-a3ad-0281ef34aaa3` |
| 同一会话具名局部替换 | `completed`，菜单 ID `[1065, 1883, 778]` → `[1065, 1883, 1192]`，仅目标菜改变，约 74 秒 | `6bd7c935-76c0-45f9-a149-31153bdd60fc` |
| 真实两版历史后恢复上一版 | `completed`，重新提交 `[1065, 1883, 778]`，5 次在线决策，约 71 秒 | `6ed2adef-19a1-438e-b8df-e695d6c58ccd` |
| 受控扩展，要求 4 道 | 初始 2 个、补充 5 个候选，模型实际检索 2 次、决策 6 次，`completed` 且 4 道，约 63 秒 | `ddadcad5-d3d1-4296-bbc2-32ea78390942` |
| 主动澄清与选项回复 | 首轮 `needs_clarification`，2 选项；第二轮“选第一个”后 `completed`，3 道，待澄清项从 1 清至 0；约 18 + 58 秒 | `00b96d3c-6a4f-4ea7-b128-34bc788d4df1`、`fdd96c37-3d82-4be7-bc83-a4d962366d64` |

另一次扩展候选探测虽完成 2 次检索，但 8 个受控候选经 B4 审核仅 3 个安全，模型合法转入 `needs_clarification`，不能算作 4 道菜失败。恢复首试出现 `SESSION_LOCK_UNAVAILABLE` 是验收脚本读到 `completed` 后过早退出、后台未及释放锁所致；随后等待锁释放重试成功。两者均保留在诊断记录中，不计作上表成功样本。

该阶段自动化证据：C3 **385 passed in 78.63s**；Application 提交验证与 D1 集成 **38 passed in 22.01s**；变更范围 Ruff **All checks passed**。故障注入覆盖 `_finalize` 将 Application 提交异常转为 `failed` 的实际路径，待澄清项没有提前消费；另有模型调用边界回归确认合法引用确实传入提示词。最新测试计数见本记录顶部。

**当时尚未覆盖、现已补测的边界**：`completed` 菜单的问题 ID 已纳入 MySQL 提交事实；再次进入 `needs_clarification` 的旧问题 ID 后续也纳入独立 MySQL 终态事实，并完成脚本化决策的真实存储故障注入。两次写入仍非原子事务；纯文本选项与问题 ID 的绑定限制见本记录顶部。
低推理档的一次独立短提示探测曾返回空 `content`（随后独立短提示返回内容，完整业务场景均取得有效动作）；当前空响应按 `MODEL_OUTPUT_EMPTY` 显式失败。尚无足够重复样本估计空响应率或尾延迟，不能把上述单次成功外推为稳定性承诺。

## 17:08–17:25 在线复测（UTC+08:00）

使用 H07 隔离配置、项目原生 `LLMClient` 与 D1 `WORKFLOW_MODE=langgraph`，模型为 `qwen3.8-max`。下表中的决策全部来自在线模型；扩展场景仅控制两次检索候选数，其余场景使用 H07 实际业务链。只记录动作、观察数量、工具次数、终态和耗时，不记录密钥或健康原文。

| 场景 | 动作与终态 | 验收判断 |
|---|---|---|
| 要求“三道清淡家常菜”，未指定餐次 | 首轮 `ask_user`，3 选项，0 工具；`needs_clarification`，约 60.72 秒 | 合法澄清，但不能作为三道菜完成验收。请求 `6522f0f8-5f81-4461-902b-6f5f19c435a9`。 |
| 明确晚餐、三道菜 | `search → audit → combine(3) → validate → finish`，4 工具、5 次模型决策；`completed`，提交恰好 3 道，约 75.45 秒 | **通过该样本**；PerfTrace 正确记录 5 次模型调用。请求 `6c45f358-cff4-47b5-9be5-8b6d6a77b229`。 |
| 主动澄清与两轮选项回复 | 未指定餐次时，首轮 `ask_user` 2 选项，选择后又追问餐次；明确晚餐时，首轮 `ask_user` 3 选项，第二轮 `search → audit → combine → validate → finish`，4 工具，`completed` 且 3 道，待澄清项清空，约 25.21 + 97.72 秒 | **明确餐次的两轮样本通过**；会话状态保存餐次与口味。请求 `9fc682c2-dbcf-4a44-878a-8131c7edb167`、`e5b4cec5-df61-4726-ab3a-8489672b002f`。 |
| 局部替换，同会话明确具名目标 | 首轮 `search_candidates` 带 2 个引用，门卫拒绝 `user_request`；`failed / INVALID_EVIDENCE_REFERENCE`，约 26.06 秒 | **失败**。用户意图虽已投影，模型仍可编造未登记的证据引用。请求 `95781afa-d13e-4a6e-bea8-3681aef8d72a`。 |
| 扩展召回，要求 4 道；首次检索 2 个候选，补充检索另 3 个 | `search → expand → audit → combine`，4 工具，第二次检索确实发生；第 5 次在线决策耗尽 60 秒，`failed / MODEL_INVOCATION_FAILED`，约 118.28 秒 | **失败**；扩展选择得到验证，完整成单未通过。请求 `5fe6447c-f88d-467e-9ff4-feefe7d05d04`。 |
| 恢复上一版菜单 | 先真实提交两版不同的 3 道菜菜单，历史数为 2；恢复首轮 `audit_recipe_health` 带 3 个引用，门卫拒绝 `restore_intent`，`failed / INVALID_EVIDENCE_REFERENCE`，约 28.26 秒 | **失败**；已具备真实历史版本前置条件，但模型引用了未登记的证据。请求 `48942f8e-555a-4bc0-a5c4-420240992333`。 |

另做了提交边界探测：在 `_node_commit` 中让 `_finalize` 抛出模拟提交错误，`consume_pending_clarification` 仍先被调用。当前代码顺序为“消费 → `_finalize`”，所以 17:05 文档所称“提交成功后才消费”**不成立**。这与模型超时前不消费是两个不同的边界。

本次新鲜自动化结果：`tests/c3/` **377 passed in 75.84s**；`test_agent_validation_regressions.py` 与 `test_langgraph_d1_full_chain.py` 合并 **26 passed in 18.71s**；Application 提交验证与 D1 全链路集成 **38 passed in 21.37s**；变更范围 Ruff **All checks passed**。

代码调用链复核：`_node_decide` 使用 `known_evidence` 对模型返回做 Gate 校验，`_decide_next_action` 构造提示词时却没有传入该允许列表。恢复意图已在 `_node_understand_intent` 登记 `history:<input_fingerprint>`，因此本次 `restore_intent` 失败不是历史证据不存在，而是提示词未提供合法引用且模型自行命名。替换首轮同理，当前有效引用列表为空，模型仍引用 `user_request`。提示词修正后 Gate 仍须 fail-closed，在线复验才可确认模型是否遵循。

提交边界还存在第二层问题：`_finalize` 捕获 Application 提交异常后会将有效终态改为 `failed` 并返回；因此仅把 `consume_pending_clarification` 移到 `_finalize` 之后，仍可能在失败返回后错误消费。需要显式传递业务提交结果，并处理业务提交成功但 C4 消费/持久化失败时的幂等重试，不能把 MySQL 与 Redis 的两次写入表述为一个原子事务。

下一步应先把门卫认可的 `available_evidence_refs` 精确投影给模型，首轮没有合法引用时明确要求 `evidence_refs=[]`；替换与恢复应分别验证合法历史/菜单引用及完整动作闭环。扩展场景须在实际 60 秒预算下完成，或调整模型响应策略并以真实延迟分布复验。待澄清项应在**确认业务提交成功后**消费；提交失败时保留可重试状态，跨存储失败场景需幂等恢复。修复后重跑上述失败样本，H5 才能勾选。

## 16:50–17:05 实现与脚本化回归记录（已被上方在线复测更新）

针对此前在线实测暴露出的六类问题，曾完成如下修改和自动化回归；其线上效果以本记录顶部的最新在线复测为准：

1. **中文数字菜数解析与 D1 端到端交付**：
   - 在 `src/food_agent_v2/c3/fast_intent.py` 中增强了 `_dish_count` 解析，支持 `"三道菜"`, `"三道清淡家常菜"`, `"两道菜"`, `"四个菜"`, `"三菜一汤"` 等中文数字规范映射。
   - 在 `tests/integration/test_langgraph_d1_full_chain.py` 中新增 `test_d1_api_langgraph_full_chain_chinese_numeral_dish_count` 用例，实测 POST `"三道清淡家常菜"` 经历 LangGraph Agent 循环，最终成功提交恰好 3 道菜，终态 `completed`，断言严格通过。

2. **Agent 决策提示词投影视角补齐**：
   - 在 `src/food_agent_v2/c3/agent_prompts.py` 中升级 `AGENT_DECISION_SYSTEM_PROMPT`，明确对 `replace`（局部替换）与 `restore`（历史恢复）的动作引导，杜绝盲目重复 `read_menu`。
   - `format_agent_prompt` 全面投影 `user_message`（用户原始需求）、`intent`（意图种类）、`locked_recipes_summary`（锁定保留菜品）、`rejected_recipes_summary`（排除替换菜品）及已有菜单清单。
   - 在 `tests/c3/test_agent_validation_regressions.py` 中加入 `test_format_agent_prompt_projection` 回归验证。

3. **`ask_user` 结构化选项契约与 Policy Gate 强校验**：
   - 在 `src/food_agent_v2/c3/agent_policy.py` 的 `validate_action_gate` 中加入对 `ASK_USER` 动作的强校验：强制选项数量必须在 2 到 3 项之间（少于 2 项或多于 3 项直接判定 `INVALID_OPTION_COUNT` 并阻断），且修改字段必须处于明确白名单（`dish_count_requested`, `time_constraint_seconds`, `time_constraint_policy`, `dish_types`, `meal_type`, `meal_types`, `taste_tags`, `expand_search`, `re_search`），否则拦截报错 `UNSUPPORTED_MODIFICATION_FIELD`。
   - `_default_agent_decision` 与 `_node_inquire` 中的 fallback options 全面对齐 2~3 项规范。
   - 在 `test_agent_validation_regressions.py` 中新增 `test_ask_user_gate_validation` 回归覆盖 1项、4项、非法修改字段和合法选项的所有边界。

4. **待澄清事项消费延迟到 `_node_commit` 入口（仅部分修复）**：
   - 将原先在 `_node_understand_intent` 中直接调用 `c4.consume_pending_clarification` 改为记录 `pending_clarification_to_consume`；当前仍在 `_node_commit` 调用 `_finalize` **之前**消费，不能称为提交成功后消费。
   - Agent 决策在进入 `_node_commit` 前因网络超时或异常中断时，C4 中待澄清项仍保存；`_finalize` 提交失败时则可能已经被消费，该边界仍需修复。
   - `_node_understand_intent` 新增对 `meal_type`, `meal_types`, `taste_tags`, `dish_count` 等选项修改字段的自动生效支持，并对 `query_plan_snapshot` 反序列化实现安全降级。
   - 在 `test_agent_validation_regressions.py` 中新增 `test_pending_clarification_consumed_only_at_commit` 回归用例。

5. **模型调用超时扩展与 PerfTrace 耗时追踪补齐**：
   - 在 `_decide_next_action` 中将模型调用超时参数设定为 60.0s（避免百炼 `qwen3.8-max` 慢思考时 30s 频繁超时中断）。
   - 在 `finally` 块中调用 `self._trace.add_model_call(role="menu_decision", ...)`，将真实的 Agent 决策耗时与调用次数如实记录进性能追踪中，解决了之前日志显示 `model_call_count=0` 的计量缺失。

6. **全量回归套件执行情况**：
   - `tests/c3/test_agent_validation_regressions.py`: 23 passed in 2.36s
   - `tests/integration/test_langgraph_d1_full_chain.py`: 3 passed in 17.53s
   - `tests/c3/` 全量套件: 377 passed in 76.15s
   - `ruff check`: All checks passed! 0 error, 0 warning

## 2026-09-27 16:20（UTC+08:00）重试

再次使用项目原生 `LLMClient.invoke(role="menu_decision", ...)` 发起真实请求，约 7.0 秒后返回 `openai.APIConnectionError`；底层仍为 `httpx.ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]`。按当前系统代理、显式指定本机 `127.0.0.1:7890` 代理、以及直连分别探测模型服务地址，三种路径均在 TLS 阶段失败；同一代理访问 `www.python.org` 返回 HTTP 200。本次仍未收到模型行动，后续六个业务场景保持 `NOT_RUN`。未输出密钥或原始模型请求内容。

## 2026-09-27 16:23（UTC+08:00）再次重试

项目原生 `LLMClient.invoke(role="menu_decision", ...)` 再次发起真实请求，约 3.39 秒后返回 `openai.APIConnectionError`，底层仍为 TLS EOF。额外用 Windows `Invoke-WebRequest` 探测相同模型服务地址，得到 `HttpRequestException: The SSL connection could not be established`；显式走本机代理的 Python HTTP 客户端同样 TLS EOF，而相同代理访问 `www.python.org` 返回 HTTP 200。故障跨 Python 和 Windows HTTP 客户端复现，仍未取得模型响应，六个业务场景保持 `NOT_RUN`。

## 环境与连通性

`load_config()` 检查显示 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL_REASONING` 均有非空配置；模型名为 `qwen3.8-max`，服务域名为 `dashscope.aliyuncs.com`。本记录仅确认配置存在，无法据此证明凭证有效。检查未输出密钥。

使用项目的真实 `LLMClient.invoke(role="menu_decision", ...)` 调用一次，得到 `openai.APIConnectionError`，底层为 `httpx.ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]`，尚未到 HTTP 响应或模型 Schema 校验阶段。为定位边界，分别检测：

| 路径 | 结果 |
|---|---|
| 目标服务域名 TCP 443 | 连接建立 |
| 目标服务域名直接 TLS 握手 | `SSLEOFError` |
| 目标服务域名经本机 HTTP 代理 `127.0.0.1:7890` | `ConnectError`，底层同为 TLS EOF |
| 阿里云官方试用域名 `trial.cn-beijing.maas.aliyuncs.com`、国际域名 `dashscope-intl.aliyuncs.com` 经代理 | 同样 TLS EOF；未向替代域名发送带密钥的模型请求 |
| 对照站点 `www.python.org` 经相同代理 | HTTP 200 |

本机 DNS 为相关域名返回 `198.18.0.0/15` 范围的代理虚拟地址；经 DoH 查询到目标域名的公开 A 记录后，直连这些地址的 TLS 握手仍返回 EOF。因此不能简单把问题归为域名拼写或本地 DNS 记录错误。上述探测只能把故障定位到当前环境通向百炼模型服务的 TLS/代理路径；无法判断密钥权限、模型可用性或服务端 HTTP 行为。[阿里云 Base URL 文档](https://help.aliyun.com/zh/model-studio/base-url)确认当前配置域名及试用域名是官方 OpenAI 兼容接入地址。

## 编排器故障路径实测

在不注入脚本模型的条件下，使用默认 `LangGraphRecommendationOrchestrator()` 和实际 `LLMClient` 执行 `_node_decide`。仅对会话锁守卫使用测试替身，以隔离此次模型决策探测；未替换模型客户端或模型返回值。

| 项目 | 实测值 |
|---|---|
| 模型调用 | 发起，连接失败 |
| 编排终态 | `failed`，`is_terminal=True` |
| 错误码 | `MODEL_INVOCATION_FAILED` |
| 选定行动 | 无 |
| 工具执行、提交 | 未进入 |

这证明当前故障路径没有静默切回确定性行动；它不等于六个在线业务场景的验收。

模型故障、非法 JSON、未知动作和额外字段的脚本化门卫回归不能替代以上在线业务场景。
