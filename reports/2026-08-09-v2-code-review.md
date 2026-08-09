# Program V2 代码与开发文档审查报告

> 审查日期：2026-08-09
> 审查对象：`program_v2` 当前工作区
> 审查基准：审查时依据系统概览、原 18 条全局不变量、模块边界、模块设计、场景文档与 ADR；审查后正式基线已补充为 INV-001..INV-024
> 配套整改方案（已获项目所有者批准）：[`2026-08-09-v2-remediation-proposal-draft.md`](2026-08-09-v2-remediation-proposal-draft.md)；正式执行见[全链路整改计划](../docs/superpowers/plans/2026-08-09-v2-full-chain-remediation.md)

## 1. 结论

V2 文档具备较完整的架构、模块职责、数据流、全局不变量和验收目标，经过少量治理后，可以成为整个系统的开发准则。当前实现还不能被判定为符合该准则，也不建议现阶段替换 `program`。

阻止替换的核心原因不是代码风格，而是固定事实层尚未按 V2 文档建立，且多条健康安全与提交一致性链路仍然 `fail-open`：输入事实不完整时可能得到健康 `PASS`，B4 安全集可能被绕过，模型输出和工具回执缺少确定性校验，回答可能在数据库原子提交之前发布。上述问题均有可实施的修复路径，见已批准整改方案、正式 V2 契约与全链路整改计划。

当前建议状态：

- 文档：`APPROVED_TARGET_BASELINE`，审查要求的文档漂移、执行约束和验收声明已于 2026-08-09 修订；代码仍需按计划实现。
- 实现：`NOT_READY_FOR_REPLACEMENT`。
- 上线/迁移：`BLOCKED_BY_P0`。
- 整改策略：健康事实完整性 → 健康关系证据 → 安全集与菜单身份 → 工作流强校验 → 原子提交/SSE → 会话与基础设施 → 文档和质量门禁。

## 2. 审查范围与方法

### 2.1 已审查文档

- `README.md`、`docs/00-system-overview.md`、`docs/documentation-roadmap.md`；
- `docs/contracts/global-invariants.md` 中 INV-001 至 INV-018；
- `docs/contracts/module-boundaries.md`；
- `docs/modules/01-data-engineering.md` 至 `14-migration-and-cleanup.md`；
- `docs/scenarios/recommendation-lifecycle.md`；
- ADR-0001、ADR-0002、ADR-0003；
- `reports/FINAL_REVIEW.md`。

### 2.2 已审查实现

- B1 数据构建与健康关系、B2 用户约束、B3 食材身份与健康视图、B4 健康规则；
- B5 时间、B6 营养、C1 检索、C2 菜单规划；
- C3 模型编排、工具桥接、状态机和提交；
- C4 Redis 会话和上下文；
- D1 FastAPI/SSE、D2 回答审计；
- MySQL Schema、Docker Compose、前端 Vue/Vite、现有测试。

### 2.3 证据方法

- 静态追踪关键函数和跨模块调用路径；
- 将文档不变量逐条映射到运行时代码；
- 针对 fail-open 路径执行最小复现；
- 执行非付费测试、测试收集、Ruff 和前端生产构建；
- 检查报告声称的测试数量与真实测试内容。

### 2.4 限制

- 仓库根目录的 `.git` 元数据不完整，无法核对提交历史和变更来源；
- MySQL、Redis 相关用例在依赖服务不可用时会跳过；
- 7 个在线/付费 LLM 用例没有纳入默认非付费验证；
- 本报告不对营养或医疗规则本身作专业医学背书，只审查工程证据链是否符合项目文档。

## 3. 文档质量评估

### 3.1 可作为系统准则的部分

以下内容结构清晰，适合作为系统级规范：

- INV-001 至 INV-018 将健康安全、证据、隐私、原子提交和运行时边界提升为全局约束；
- B1-B6、C1-C4、D1-D2 的模块所有权和依赖方向基本明确；
- 推荐生命周期描述了检索、健康审查、菜单规划、最终复核、回答与提交的完整链路；
- 文档明确区分硬约束、软评分、用户可见数据和内部审计数据；
- 测试文档给出了多数不变量的目标测试类型。

### 3.2 成为唯一开发准则前必须治理的部分

- 统一端口：隔离方案是 API `8001`、前端 `5174`，`docs/modules/11-api-and-sse.md` 仍写 `8000/5173`；
- 统一模型供应商和配置表述：DeepSeek、Qwen/DashScope 的描述并存；
- 删除或标记过期的 README 描述，例如“尚无可运行业务代码”；
- 将 `reports/FINAL_REVIEW.md` 的历史结论标记为未验证快照，不能继续作为当前通过证明；
- 为每条不变量补充机器可执行的验收用例、失败码和证据字段；
- ADR 偏离必须同时回写模块文档，避免“ADR 允许、模块文档禁止”的双重标准。

因此，文档可以作为目标开发准则，但需要建立“契约文档为权威、ADR 解释偏离、报告只记录证据”的优先级。

## 4. 问题总览

| 编号 | 级别 | 问题 | 违反或威胁的准则 | 当前后果 |
|---|---:|---|---|---|
| R-001 | P0 | B3 食材事实缺失、B4 缺映射时按空集合评估 | INV-001、INV-003、INV-018 | 不完整菜品可能得到 PASS |
| R-002 | P0 | 对话健康信号没有进入 B2/C4/B4，参与者输入不完整也可受理 | INV-001、INV-004、INV-016 | 用户本轮约束被忽略或后台崩溃 |
| R-003 | P0 | C2 在安全候选为空时回退到原始检索候选 | INV-001、INV-002 | 绕过 B4 安全集 |
| R-004 | P0 | 关键词匹配自动标记 approved，覆盖记录自动声明 complete | INV-003、INV-018 | “审核证据”由算法自我认证 |
| R-005 | P0 | C3 后置校验不验证 Schema、权限、回执身份和证据链 | INV-006、INV-007、INV-011 | 非法模型行为仍可能继续 |
| R-006 | P0 | 缺失/未知审查结论默认 PASS，回答审计未接线 | INV-005、INV-013 | 幻觉菜品或敏感表述可能发布 |
| R-007 | P1 | `answer_ready/result_committed` 早于 MySQL 原子提交 | INV-010 | 客户端先看到成功，数据库随后失败 |
| R-008 | P1 | SSE 游标闭包错误，analysis payload 被覆盖为空列表 | API/SSE 契约 | SSE 首读异常或内容丢失 |
| R-009 | P1 | 菜数/槽位/严格食材未硬执行，plan_id/menu_hash 非内容寻址 | INV-014、菜单规划契约 | 不可行菜单被接受，身份可碰撞 |
| R-010 | P1 | 会话、当前菜单、临时约束、锁和取消链路不完整 | INV-009、INV-016 | 多轮恢复和并发状态不可信 |
| R-011 | P1 | 数据库重复提交产生副作用，审计字段不足 | INV-010、INV-011 | 会话计数/菜单版本重复，证据不可追溯 |
| R-012 | P1 | 报告与真实测试不一致，E2E/不变量测试存在空壳 | 测试与验收准则 | 无法证明系统满足文档 |
| R-013 | P2 | README、端口、模型、报告状态漂移 | 文档治理 | 执行者会依据错误信息修改系统 |
| R-014 | P2 | `/v1/users` 暴露详细健康资料 | INV-013、隐私边界 | 前端和调用者获取超出所需的数据 |
| R-015 | P2 | Ruff 基线较差，Qdrant 使用 `latest`，Git 历史不可用 | 可运维性/可复现性 | 构建和回归难以稳定复现 |
| R-016 | P0 | RecipeCatalog 缺失，现有食材注册表被分组/数量/形态污染 | INV-008、INV-017、INV-018 | 全部下游事实建立在错误身份上 |
| R-017 | P0 | 用户清洗确定性丢失身高、体重、单位和孕周期，未知特殊状态被静默忽略 | INV-001、INV-016 | 固定健康事实不完整且无法追溯 |
| R-018 | P1 | B5 让 LLM 自报 high 参与严格时间；B6 2,000 份画像覆盖均为 0 | INV-004、时间/营养契约 | 时间承诺无权威，营养软评分实际失效 |
| R-019 | P1 | 在线模块继续拆食材字符串，数据库初始化逐表提交且不清除陈旧事实 | INV-008、初始化契约 | B1 单入口和完整发布边界未成立 |
| R-020 | P1 | 前端不复用 session，并直接展示用户编号、过敏和特殊人群 | INV-009、INV-013 | 多轮是假会话，隐私边界被破坏 |

## 5. 详细发现

### R-001 [P0] 健康食材集合不完整时仍可 PASS

证据：

- `src/food_agent_v2/b3/recipe_views.py:73-114` 会统计 `unresolved_occurrence_count`，但把 `composition_expansion_status` 固定为 `atomic`，没有阻止下游健康评估；
- `src/food_agent_v2/b4/__init__.py:200-229` 使用 `recipe_ingredient_map.get(rid, [])`；缺少菜品映射时用空集合继续评估；
- `src/food_agent_v2/c3/tool_handler.py:137-156` 仅在构建成功时加入 `ing_map`，未将缺失视图作为系统错误。

最小复现结果：存在一条健康约束时，缺少食材映射的菜品仍返回 `PASS`。

影响：任何解析失败、菜品 ID 错误、复合食材未展开，都可能被误判为没有命中风险。健康判断必须 fail-closed。

修复方向：B3 输出完整性状态；B4 要求 `recipe_ids == recipe_ingredient_map.keys()`，且每个视图 `unresolved=0`、组成已验证、证据路径完整；否则抛确定性错误，不产生 PASS/EXCLUDE 业务结论。

### R-002 [P0] 本轮健康信号和参与者身份没有形成闭环

证据：

- `health_signals` 只出现在提示词和在线测试，工作流未消费该字段；
- `src/food_agent_v2/b2/__init__.py:262-300` 已提供 `validate_temporary_signal`，但没有调用方；
- `src/food_agent_v2/c4/__init__.py:590-605` 已提供临时约束存储，但没有接入推荐主链；
- `src/food_agent_v2/d1/schemas.py:56-63` 不要求每个参与者具有 `participant_ref` 和 `user_id`；
- `src/food_agent_v2/c3/runner.py:43-45` 后台线程直接索引这两个字段，因此请求可先返回 202，再在后台失败；
- `src/food_agent_v2/c3/tool_handler.py:99-124` 每次只从固定用户档案重新派生约束。

影响：用户在本轮明确声明的过敏/禁忌可能完全不进入健康审查；畸形请求会产生异步失败，破坏 API 语义。

修复方向：入口严格校验参与者；QueryPlan 中健康信号必须经过 B2 验证；已验证的 turn/session 临时约束写入 C4，并与永久约束合并后传给 B4；未知用户和未知信号 fail-closed 或进入澄清终态。

### R-003 [P0] 安全候选为空时绕过 B4

证据：`src/food_agent_v2/c3/tool_handler.py:169-181` 在参数和 `ctx.safe_recipe_ids` 均为空时，从 `retrieval.candidates` 恢复候选。

最小复现结果：显式传入空 `safe_recipe_ids` 时仍生成了菜单方案。

影响：`safe_recipe_ids=[]` 的含义从“没有安全候选”变成“重新使用未经确认的候选”，直接违反 B4 是唯一健康裁决者的边界。

修复方向：用 `None` 表示“尚未执行健康评估”，用空列表表示“已评估但无安全候选”；C2 只能消费具有有效 B4 回执和输入指纹的安全集合。

### R-004 [P0] 健康关系审核和覆盖由关键词算法自我认证

证据：

- `src/food_agent_v2/b1/health_relation_builder.py:151-176` 对关键词命中的关系直接写 `review_status=approved`、`hard_filter=true`；
- 同文件 `183-194` 将注册表所有食材写入 `covered_ingredient_ids` 并标记 `complete`；
- 没有为每个 `constraint_code × ingredient_id` 保存明确的正向或负向人工/权威审核决定。

影响：INV-003/INV-018 要求的“已审核关系”和“完整审核矩阵”被简化为模式匹配结果。关键词漏匹配时会生成可信外观的完整覆盖证明。

修复方向：关键词只能生成 `pending` 候选；建立全量审核矩阵，每一对必须是 `hard_exclude`、`no_hard_relation` 或 `rejected`，并包含 reviewer、reviewed_at、evidence_refs、规则集版本；覆盖文件必须由矩阵集合等价校验派生，不能独立宣称完整。

### R-005 [P0] C3 工作流校验器无法强制执行文档不变量

证据：

- `src/food_agent_v2/c3/__init__.py:312-345` 只检查必需工具名、成功标志和非空输出；
- 未验证输出 Artifact Schema、工具参数 Schema、回执哈希/身份、证据引用存在性、菜单哈希和跨 Artifact 一致性；
- `src/food_agent_v2/c3/runner.py:608-626` 对未授权工具只向模型返回错误文本，不记录权限违规并终止节点；
- `ToolReceipt` 的 `parameter_hash`、`result_hash` 在 `runner.py:562-572` 被写为空字符串。

最小复现结果：统一审查节点调用禁用健康工具后，只要也调用必需工具，`post_check` 仍可返回无错误。

影响：角色权限和证据链停留在提示词约束，模型偏离时不会被确定性拦截。

修复方向：引入 Pydantic Artifact/Receipt Schema；所有工具调用（包括拒绝调用）进入不可变账本；节点后置校验执行权限、调用预算、回执身份、证据引用和跨 Artifact 不变量。

### R-006 [P0] 回答与审查默认放行

证据：

- `src/food_agent_v2/c3/runner.py:327-329` 对缺失或未知 verdict 默认改为 `PASS`；
- `runner.py:305-316` 仅在模型提供 `dish_ids` 时校验，缺失时不构成失败；
- `src/food_agent_v2/d2/__init__.py:31-47` 已实现文本审计和菜品集合验证，但没有接入 runner/D1；
- `src/food_agent_v2/d1/__init__.py:244-261` 只扫描禁止字段名，不扫描回答正文。

最小复现结果：`这道菜含钠800mg，适合高血压患者` 可被发布为 `answer_ready`。

影响：回答可引用未验证菜品、包含健康判断或营养数值，违反 INV-005/INV-013。

修复方向：所有未知/缺失 verdict 必须失败；AnswerArtifact 强制包含与最终菜单完全一致的 `dish_ids`、`plan_id`、`menu_hash`；D2 文本审计必须在统一审查之前和 SSE 发布之前各执行一次。

### R-007 [P1] 成功事件早于原子提交

证据：

- `src/food_agent_v2/c3/runner.py:384-394` 先发布 `answer_ready` 和 `result_committed`；
- `runner.py:743-770` 后执行 MySQL `commit_request_result`；
- 提交失败只把内部状态改为 failed，不能撤回客户端已经收到的成功事件。

影响：`result_committed` 的名称和 API 契约不真实，客户端与数据库产生不可补偿的不一致。

修复方向：构建并审计 AnswerArtifact → MySQL 原子提交 → C4 提交会话状态 → 更新 D1 状态 → 发布 `answer_ready` → 发布 `result_committed`。任何提交失败只能发布 `error`，不能发布成功事件。

### R-008 [P1] SSE 游标和分析事件实现错误

证据：

- `src/food_agent_v2/api_app.py:89-101` 内部生成器给外层 `last_event_id` 赋值，Python 将其视为局部变量，首次读取会触发 `UnboundLocalError`；
- `src/food_agent_v2/d1/__init__.py:232-242` 将 payload 变量赋值为 `scan_forbidden_fields(...)` 的返回值，正常分析事件变成 `[]`。

影响：SSE 首次订阅或重连续传不可依赖，阶段分析数据丢失。

修复方向：生成器使用独立 `cursor` 局部变量；D1 保留原 payload，只对 violations 做判断；补充 Last-Event-ID、断线重连、心跳和终态关闭测试。

### R-009 [P1] 菜单硬约束和内容身份不成立

证据：

- `src/food_agent_v2/c2/__init__.py:118-122` 候选不足时自动减少默认菜数；
- `_build_one_menu` 只要求至少 2 道，不要求恰好等于请求菜数；
- strict ingredients 依赖默认值为 true 的 `has_available_ingredients`，没有集合包含校验；
- `require_soup/require_staple/require_drink` 未形成最终硬断言；
- `src/food_agent_v2/c2/__init__.py:300-302` 的 plan_id 仅由策略和菜数构成，不同菜单会碰撞；
- `src/food_agent_v2/c3/tool_handler.py:239-242` 使用 `hash_{plan_id}` 伪造 menu_hash，并接受模型提供的任意 recipe_ids。

最小复现结果：请求 5 道、仅 2 个安全候选时仍得到 2 道“可行”菜单；两组不相交菜单可获得相同 `plan_balanced_4`。

影响：不可行菜单可被标记 feasible，最终校验身份不能证明“校验的就是提交的菜单”。

修复方向：使用规范 JSON 的 SHA-256 生成 menu_hash 和 plan_id；所有硬约束在选前过滤和选后断言；最终验证只能按已注册的 FeasibleMenuArtifact 查找，禁止模型重新提供菜品集合。

### R-010 [P1] 多轮会话、锁和取消仍是骨架

证据：

- `src/food_agent_v2/api_app.py:152-168` 创建会话时丢弃参与者，查询会话固定返回空状态；
- `src/food_agent_v2/c3/tool_handler.py:94-96` 的 `get_current_menu` 固定返回空；
- `src/food_agent_v2/c4/redis_store.py:142-156` 定义了会话锁，但工作流不调用；Redis 不可用时锁直接放行；
- `src/food_agent_v2/c3/runner.py:772-777` 调用 `commit_session_state` 时不传 menu artifact ref，因此菜单历史不会追加；
- D1 取消接口立即标记 cancelled，而 runner 仅检查 Redis marker；Redis 不可用时工作流仍可能继续。

影响：替换/恢复、跨进程会话、同会话并发和取消语义不能满足文档。

修复方向：会话元数据由 C4/Application 真正持久化；worker 领取请求和 session lock；取消状态存储必须与工作流读取一致，并在外部调用前后检查；只有工作流确认停止后才进入 cancelled 终态。

### R-011 [P1] 数据库提交并非完整幂等，审计字段不足

证据：

- `src/food_agent_v2/application/__init__.py:51-76` 重复提交会增加 `sessions.request_count` 并重复插入 `menu_versions`；
- 现有双提交测试只验证 `recommendation_logs` 行数；
- `menu_versions` 没有唯一约束；
- application 提交前不验证 `final_validation_verdict=PASS`、review PASS、menu_hash 和证据引用；
- recommendation log 只保存松散 JSON，无法可靠查询完整约束/回执/Artifact 引用。

影响：重试会产生重复副作用，数据库无法独立证明最终结果满足健康和审查门禁。

修复方向：以 request_id 建立提交幂等边界；菜单版本增加 request_id/唯一键；会话计数只在首次插入 recommendation log 时增加；提交函数接收强类型 CommitBundle 并在事务开始前验证。

### R-012 [P1] 测试和最终报告不能证明系统通过

审查时证据：

- `pytest --collect-only` 收集 68 个用例，其中 7 个是在线/付费 LLM 测试；
- `uv run pytest tests --ignore=tests/test_prompts_live.py -q -ra`：`56 passed, 1 failed, 4 skipped`；
- 失败：`TestC4Compression.test_restore_session`；Redis 2 个、MySQL 2 个用例跳过；
- 单独重跑恢复用例仍失败；
- `tests/test_e2e_cases.py` 没有 pytest 测试函数，收集数为 0；
- 3 个在线 LLM 用例只打印 WARN，没有断言；
- `tests/test_b2_b4_health.py::test_inv003_no_pending_relations` 的测试体是 `pass`；
- 无前端测试；
- `reports/FINAL_REVIEW.md` 声称 68 passed、真实 E2E、MySQL 幂等和 Redis 恢复，与上述事实不一致。

影响：测试数字包含空壳、在线依赖和被跳过基础设施，不能作为退出 V1 的证据。

修复方向：测试分层并显式报告 collected/passed/failed/skipped；将 18 条不变量建立一对一自动化矩阵；CI 必须启动 MySQL/Redis/Qdrant；E2E 必须被 pytest 收集并断言终态和证据链。

### R-013 [P2] 文档存在多处漂移

证据：

- 根 README 仍称没有可运行业务代码；
- API/SSE 文档使用 5173/8000，而 V2 隔离和实际脚本使用 5174/8001；
- DeepSeek 与 Qwen/DashScope 描述并存；
- 历史最终报告没有标记验证环境和证据时间。

影响：后续执行者可能修错端口、模型配置或误以为系统已验收。

修复方向：增加文档权威层级和“最后验证日期/命令/环境”；所有运行参数从单一配置源生成文档片段。

### R-014 [P2] 用户列表接口暴露超出选择参与者所需的数据

证据：`src/food_agent_v2/api_app.py:129-147` 返回用户 ID、年龄、性别、过敏、疾病和特殊人群，前端可直接展示。

影响：多人和非本地部署时违反最小披露原则，也与回答隐私约束冲突。

修复方向：公开接口只返回不可逆公开标识和显示标签；健康详情只在服务端 B2 内部使用；如需管理员视图，建立鉴权和独立权限域。

### R-015 [P2] 可复现性和静态质量基线不足

证据：

- `uv run ruff check src tests` 报告 144 个错误；
- 定向致命检查发现 `api_app.py` 的 F823，以及两个 B905；
- 前端生产构建成功，但没有前端测试；
- Docker Compose 中 Qdrant 使用 `latest`；
- 当前工作区没有完整 Git 历史。

影响：依赖变化和静态错误可能让同一版本在不同时间得到不同结果。

修复方向：固定镜像 digest/版本；先清除 F/E/B，再逐步收紧全量 Ruff；建立可重放 CI 和版本化迁移脚本；恢复完整 Git 仓库元数据。

### R-016 [P0] 固定菜品事实层和食材身份未按 V2 建立

补充审查证据：

- 固定 CSV 为 GBK、2,000 行、1,138,083 字节，SHA-256 为 `B2177DC6CDCAE24FC5671C8DADA44295228F4301E3CE620ED11D51B1ABFE4371`，但当前构建没有先验证机器可读 SourceManifest；
- `recipe_cleaning.py` 基本复制原始字段，没有文档要求的 `RecipeCatalog`、`record_type`、事实完整性、推荐资格、原因和来源引用；
- 当前 `ingredient_registry.jsonl` 有 3,326 条记录，其中 2,131 条只出现一次，别名、类别和食材族均为空；
- 标准身份中出现 `A料：T55面粉`、`A料：乌鸡半只`、`A料：冷冻龙利鱼` 等分组和形态污染；
- 当前 ID 由出现频率和名称排序产生，无法支持经过审核的一次性稳定身份；
- B3 在线视图再次解析清洗文件中的原始食材字符串，组成状态固定为 `atomic`。

影响：B4 健康全集、B5 步骤绑定、B6 营养匹配和 C1 检索都继承了错误事实；只修 B4 的空映射不足以恢复可信链路。

修复方向：先锁定固定 SourceManifest，对 2,000 条记录全部显式分类；废弃当前食材 ID，完整重建标准食材、别名、出现、选择组和组成，生成旧→新审计映射；B1 成为唯一离线解析入口，所有在线消费者只读 B3 权威视图。

### R-017 [P0] 用户档案清洗丢失固定事实

补充审查证据：

- 原始数据字段为 `身高_cm` 和 `体重_kg`，实现读取 `身高` 和 `体重`，因此 50 份清洗档案的身高、体重全部为 null；
- 指标单位包含在原始键名中，但清洗后每个指标的 `unit` 均为 null；
- 原始 `孕周期` 未进入清洗产物；
- 6 份 `备孕` 特殊状态未映射到允许约束码，也未形成待澄清或数据错误；
- 未找到用户时 `derive_constraints` 返回空约束集，而文档要求 `HEALTH_PROFILE_NOT_FOUND`；
- 未知过敏/疾病文本会动态拼接新约束码，允许代码注册表不是封闭集合。

当前 50 位用户实际派生的约束码碰巧均存在于现有 26 个覆盖码中，但这不能证明 B2/B4 契约成立；潜在允许码和运行时动态码仍可超出覆盖。

修复方向：按原始 Schema 重建类型化档案，保存值、单位、状态和来源；建立封闭 B2 允许码注册表；未知值进入澄清/数据审核；明确禁忌必须经 B3 解析成 ID；用户不存在或档案损坏必须失败。

### R-018 [P1] 时间与营养辅助链路产生“有文件、无权威”的假可用状态

补充审查证据：

- `time_profiles.jsonl` 有 13,784 个步骤，只有 3,209 个步骤包含显式时长；
- 1,943 道菜写入了 LLM 估算，206 道被模型自报为 high；B5 在菜单全部有估算时优先使用经验公式，而不是文档规定的任务图；
- `step_time_builder.py` 把无法判别的步骤默认设为 active，并未绑定 B3 食材；
- `nutrition_profiles.jsonl` 虽有 2,000 行，但 2,000 行全部为 low 且覆盖率为 0；
- 营养构建直接拆原始食材字符串，读取的参考字段名与真实 `*_per_100g` Schema 不一致，并用后一个匹配食材覆盖前一个食材的营养值；
- B6 忽略调用者提供的 goal weights，缺数据时仍生成中性维度分。

影响：严格时间可能依赖模型自信声明，营养方案看似存在但实际没有任何有效输入，质量门禁只按行数仍宣布通过。

修复方向：LLM 时间只能作为低/中置信软特征；严格时限只使用显式高置信任务图；营养从 B3 视图和真实参考 Schema 重建，零覆盖返回 unavailable 并确定性重分配软权重；门禁检查语义覆盖而非文件行数。

### R-019 [P1] B1 单入口、MySQL 初始化和 Qdrant 索引边界未成立

补充审查证据：

- B5、B6、C1 的构建器均直接消费 `clean_recipes` 内的原始字符串，而不是 B3 消费者视图；
- 在线 B2/B3/B4/C1 服务直接读取 JSONL，未通过文档规定的 MySQL 领域 Repository；
- `database_loader.py` 每张表单独 commit，任一后续表失败会保留部分正式数据；
- loader 不初始化 RecipeCatalog、别名、出现、选择组、组成、允许约束码、完整健康决定和覆盖数据；
- upsert 只更新少数字段，不删除已经不存在的旧食材/关系；一次性 ID 重建后会残留陈旧事实；
- Qdrant 客户端在读取路径自动创建 collection，未校验文档集合、向量数量、payload Schema 或构建哈希，也不会清除陈旧点。

影响：离线文件“passed”并不能保证正式数据库和索引是同一个完整快照，运行时还可绕过 Repository 边界。

修复方向：所有派生产物从 B3 视图构建；先在 staging 完成全量门禁，再向空的 V2 正式库一次性初始化并统一 commit；Qdrant 使用独立 staging collection 验证后在服务启动前配置；在线只读 Repository，不直接读取生成文件。

### R-020 [P1] 前端、会话和最小披露没有闭环

补充审查证据：

- 前端每次 `send` 都只提交新请求，不保存并复用返回的 `session_id`；
- `/v1/sessions` 和 `GET /v1/sessions/{id}` 返回固定骨架，`get_current_menu` 也固定返回空；
- `/v1/users` 返回 user_id、年龄、性别、过敏、疾病和特殊状态；
- 前端用 `用户N` 展示用户编号，并直接展示过敏和特殊人群；
- 前端没有处理 clarification 内容、取消、interrupted，也没有禁止字段兜底过滤；
- 前端无测试，SSE 断线和事务失败后已显示回答的状态未被验证。

影响：界面虽能构建，但不是文档描述的匿名多轮系统，且直接突破 INV-013 的最小披露边界。

修复方向：服务端提供最小匿名参与者目录；前端只显示参与者 A/B；保存并复用 session_id；实现澄清、取消和所有终态；在 result_committed 后标完成；补齐隐私和 SSE 状态机自动测试。

## 6. 验证快照

| 检查项 | 命令 | 审查时结果 | 判定 |
|---|---|---|---|
| 测试收集 | `uv run pytest --collect-only -q` | 68 collected，含 7 live LLM | 不能等同 68 passed |
| 非在线测试 | `uv run pytest tests --ignore=tests/test_prompts_live.py -q -ra` | 56 passed / 1 failed / 4 skipped | 未通过 |
| 恢复回归 | `uv run pytest tests/test_invariants.py::TestC4Compression::test_restore_session -q` | 1 failed | 未通过 |
| E2E 收集 | `uv run pytest tests/test_e2e_cases.py --collect-only -q` | 0 collected | 未建立自动 E2E |
| Ruff 全量 | `uv run ruff check src tests` | 144 errors | 未通过 |
| Ruff 致命类 | `uv run ruff check src --select F821,F823,E902,B --output-format concise` | 3 errors | 未通过 |
| 前端构建 | `npm run build` | exit 0，1801 modules | 构建通过，不代表行为验收 |

## 7. 退出 P0 的硬门槛

以下条件必须同时满足，才能开始讨论 V2 替换 V1：

1. 固定菜品 SourceManifest 精确通过，2,000 条 RecipeCatalog 全部显式分类；
2. 当前食材 ID 完整重建，合格菜品健康视图零 unresolved、零悬空引用；
3. 缺菜品、缺食材映射、未解析食材、未验证复合组成全部 fail-closed；
4. 每个生效 constraint code 对健康食材全集具有可审计的逐项正/负决定；
5. C2 只能消费与本请求和候选指纹绑定的 B4 安全集；
6. 最终菜单是某个已生成 FeasibleMenuArtifact 的精确内容，plan_id/menu_hash 可重算；
7. 所有节点输出、工具调用、回执和证据引用由确定性 Schema 校验；
8. 回答 dish_ids 与最终菜单一致，正文通过确定性 D2 审计；
9. MySQL 原子提交成功后才允许发布成功事件；
10. INV-001 至 INV-018 均有被 pytest 实际收集的自动化正/负测试；
11. 默认 CI 中测试、Ruff、前端测试和前端构建均为零失败、零意外跳过；
12. 报告由命令生成，不再手写“全部通过”数字。

## 8. 建议执行顺序

| 阶段 | 内容 | 进入下一阶段的条件 |
|---|---|---|
| Phase 0 | 可恢复基线、文档权威和执行纪律 | Git/备份可恢复，冲突文档已消解 |
| Phase 1 | SourceManifest、RecipeCatalog、食材身份和 B3 视图 | 固定事实全量门禁通过 |
| Phase 2 | 用户档案、B2 允许码和健康决定矩阵 | 覆盖集合等价且独立审核通过 |
| Phase 3 | B4 fail-closed、批次/最终回执 | 所有不完整输入均无法 PASS |
| Phase 4 | B5/B6/C1 辅助产物和检索索引 | 软特征可用性真实、索引身份一致 |
| Phase 5 | C2 安全集、硬约束和菜单身份 | 模型不能伪造/改写菜单 |
| Phase 6 | C3 Artifact、回执、回答强校验 | 未知/缺失内容一律失败 |
| Phase 7 | C4、Application、MySQL、SSE 与幂等 | commit 失败时无成功事件 |
| Phase 8 | API、前端、隐私、多轮、取消 | 会话/并发/终态测试通过 |
| Phase 9 | 数据初始化、E2E、文档和 CI | 18 条不变量及全链路通过 |

具体策略、任务顺序、停止条件和 DeepSeek 校准协议见配套整改方案草案。

## 9. 给执行模型的边界

- 不允许通过删除断言、扩大跳过、捕获异常后返回 PASS 来“修复”测试；
- 不允许让 LLM 代替 B2/B4/C2/Application 的确定性裁决；
- 不允许在安全候选为空、证据缺失或身份不一致时回退到原始候选；
- 不允许手工修改测试报告数字；
- 每个任务必须经历红灯测试、最小实现、任务级回归和独立提交；
- 任一 P0 失败时停止后续阶段并保留失败证据。

## 10. 最终意见

V2 的方向和文档骨架是正确的，问题主要集中在“文档宣称了强不变量，但实现使用了宽松回退或自我认证”。整改时应保留当前模块边界，把所有安全相关的默认值从放行改为拒绝，把所有身份从名称/计数改为内容哈希，把所有报告从人工结论改为可重复命令证据。

在配套计划全部执行并通过最终门禁之前，`program_v2` 应保持为候选重构版本，不应替换生产或竞赛提交中的 `program`。
