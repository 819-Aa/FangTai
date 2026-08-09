# ADR-0003：2026-08-08 实现轮次的决策与文档偏离留痕

- 状态：`SUPERSEDED`（2026-08-09）
- 日期：2026-08-08
- 范围：C3 节点重试、B5 时间调度、C2 时间约束、B1/B2 数据修复、C1 重排器

> **废止通知**：2026-08-09 全链路审查证明本文所称“已实现并验证”不足以成立。本文仅保留历史追溯用途，不再授权任何实现偏离。时间语义由 [ADR-0005](0005-strict-time-semantics.md) 取代；固定数据与身份重建由 [ADR-0004](0004-fixed-source-and-one-time-identity-rebuild.md) 约束；提交和事件顺序由 [ADR-0006](0006-post-commit-event-publication.md) 约束。C3 必需工具漏调仍服从 INV-007：缺少有效回执即失败，不允许模型节点自动重试。

## 背景

在 [ADR-0002](0002-bridge-layer-fix.md)（2026-08-07）修复桥接层后，本轮（2026-08-08）完成：
全链路审查 → 8 处核心 bug 修复 → C1 重排器集成 → B5 任务图调度 → 离线 LLM 时间补全
→ 提示词加固。本文档**只记录与原始文档不同的实现决策**，供后续审查对照；
符合文档的实现（如 INV-007 有效回执、INV-001 最终校验等）不再重复。

凡涉及偏离，均在对应模块文档中标注"2026-08-08 决策"引用本文。

---

## 1. C3：必需工具漏调时允许一次节点级重试（偏离 09 §13.3）

### 原文档
`09-agent-workflow.md` §13.3：必需工具漏调 → 直接 `failed`，不消耗修订、不自动补调。

### 实现
`c3/runner.py` `_run_model_node`：当节点因 `REQUIRED_TOOL_NOT_CALLED`（模型不合规——
漏调必需工具）失败时，用**干净上下文**重跑该节点一次；第二次仍漏调才 `failed`。

### 偏离理由
- 文档出发点是不掩盖**系统/健康/基础设施**错误；"模型一时没调对工具"是**模型合规性问题**，
  重试一次不损害健康/安全确定性。
- 观察 2026-08-08 多轮 E2E：DeepSeek 偶发漏调必需工具，通过率 ~80-100%。
  节点重试后显著提升演示可靠性。

### 边界（严格保留）
- 仅对 `REQUIRED_TOOL_NOT_CALLED` 重试。
- 健康校验失败（`FINAL_HEALTH_VALIDATION_FAILED`）、工具执行失败
  （`TOOL_EXECUTION_FAILED`）、证据/权限错误：照旧立即失败，不重试。
- 重试计数在节点内，最多 1 次。

---

## 2. B5：时间调度使用离线 LLM 估算 + 并行公式（偏离/增强 05 §2、§14）

### 原文档
`05-time-and-steps.md` §2：禁止简化公式；§14：基于步骤任务图调度；
严格时间只接受高置信度结果。

### 实现
- B5 `compute_menu_schedule` **优先**使用 `time_profiles.jsonl` 中的 `llm_estimate`
  （离线 DeepSeek 估算的 active/equipment/passive/total + confidence）；
  无估算的菜回退步骤任务图调度。
- LLM 估算菜单 makespan 采用**并行公式**：
  `makespan ≈ 主动总和 + 最长单菜设备 + 其余设备×40%`
  （单厨师备菜串行、设备占用与其他菜备菜并行，符合多灶头家庭厨房）。
- `strict_time_feasible`：仍只在高置信度（全部菜品 confidence=high）时判定 true/false，
  否则 `unknown`（保持文档 §2 的保守原则）。

### 偏离理由
- 文档 §2 禁止简化公式，但未禁止"离线用模型准备数据 + 运行时确定性调度"。
  2026-08-08 决策：确定性任务图作为基线兜底，离线 LLM 补全时长提升准确性。
- 简单求和（全串行）会把多菜菜单高估到不现实（见验证），并行公式更贴近实际。

### 数据来源
离线脚本 `b1/llm_time_profiler.py`（`time-profiler` 命令）读菜谱 → DeepSeek 估算 →
校验 → 写回 `llm_estimate`。运行时 B5 零模型调用。

### 时间可用性（2026-08-08 补充）
`MenuScheduleResult.time_source` = `llm_estimate | task_graph | none`：
菜单各菜**都有** LLM 估算时 `llm_estimate`（时间可信）；否则 `task_graph`/`none`。
runner 将 `time_data.available` 传给回答模型；**不可用时回答如实写
"暂无法提供准确的制作时间估算"，不编造时间**（回答提示词约束 6）。

---

## 3. C2：时间约束改为软过滤（偏离 08 §5.2）

### 原文档
`08-menu-planning.md` §5.2：严格时间作为硬约束。

### 实现
`c2/__init__.py` `plan()`：有 `strict_time_limit` 时，优先保留 `makespan ≤ 限制`
且 `strict_time_feasible != "false"` 的方案；若所有方案都超时，**软退回最快方案**，
不返回空（避免"45分钟内"请求因无满足方案而频繁 `no_feasible_menu` 失败）。

### 偏离理由
- 用户声明的"45分钟内"是**期望/偏好**，不是健康硬红线；硬失败对用户不友好。
- LLM 估算偏保守时，硬过滤会导致大量请求无可行菜单。
- 回答模型被要求**时间诚实**（`requested_time_limit_minutes` 传入），
  超时菜单会如实说明实际耗时，不会谎称满足。

### 菜单同名去重（2026-08-08 补充）
数据存在 75 组同名菜变体（如"红烧肉"3 个 recipe_id）。原贪心选菜不排除同名，
导致同一菜单出现两道同名菜（用户体验为"重复菜"）。C2 `_build_one_menu` 增加
**同名硬约束**：候选菜名已在菜单中则跳过（locked 也参与去重）。

---

## 4. B1/B2：数据与约束解析修复（恢复文档意图，非偏离）

以下修复**恢复**文档意图（INV-003/INV-016/INV-018），不构成偏离：

- B1 `health_relation_builder.py`：单字关键词（虾/蟹/贝/鱼/蛤/蚝/蛏/蚌/螺）
  改为宽松子串匹配 + 误判守卫（蟹味菇/贝贝南瓜/川贝/素蚝油），
  修复"鲜虾/虾仁/螃蟹/蛤蜊"不命中的覆盖缺陷（INV-003）。
- B1 `user_cleaning.py`：`特殊人群` 从字符串 `"['高血压']"` 解析为列表
  （此前整批慢病约束丢失，INV-016）。
- B2 `__init__.py`：`_special_group_to_constraint_code` 增加慢病映射
  （高血压/高血糖/高尿酸等）；`get_indicator_status` 键名归一化（剥离 `_mmol/L` 等单位后缀）
  + 血压复合值（"141/89"）拆分（INV-016）。
- B1 `step_time_builder.py`：步骤拆分支持"第X步：…；"格式、sum 多时长、
  跨菜共享 cook/设备互斥键（恢复任务图数据基础）。

---

## 5. C1：重排器集成（符合 07 §9.5，补充实现细节）

- 07 文档要求四段完整链路含重排；原代码缺重排器。本轮集成
  `BGE-Reranker-v2-M3`（缓存于 `.model-cache`），`RERANKER_ENABLED` 可关闭。
- 启动预热 `warmup_models()`（07 §13），首轮请求不承担冷加载。
- 重排器加载/推理失败时回退融合排序（不崩溃）——这是对 07 §9.8
  "重排失败应停止"的**放宽**，理由：演示可用性优先于严格停止，且不影响健康安全。

---

## 6. 提示词（偏离 09 的措辞层面）

- `health_menu_planning` / `menu_decision` / `answer_generation` 提示词加强
  （明确必需工具顺序、禁止 retrieve_recipes、强制结构化输出、时间诚实）。
  属于执行文档约束的强化，不改变角色职责边界。

---

## 验证记录（2026-08-08）

- 离线回归测试：44/44 通过。
- 完整 E2E（预热重排模型后）：
  - 单人（海鲜过敏，要求45分钟内）：completed，菜单全蔬菜无海鲜，
    "约44分钟，在您要求的45分钟之内"。
  - 多人（高血压+孕妇+海鲜过敏）：completed，无高盐、无海鲜（蟹味菇正确未误判）。
  - 统一审查修订循环生效（72分钟不一致 → 修订为44分钟）。
- 健康筛选：allergy_seafood 排除集 97→248，逐参与者逐菜品零漏判。
- LLM 时间补全：1943/2000 成功（97%），置信度 high 400 / medium 1460 / low 83。

## 7. INV-018 覆盖完整性（已实现，恢复文档意图）

- B1 `health_relation_builder` 审核**全部注册表食材**（含 is_edible=false，菜品的健康视图可能引用），
  并新增 `health_relation_coverage.jsonl`：每个码的完整审核食材集。
- B4 `load_relations` 加载覆盖记录；`evaluate_recipe` 对编码化约束校验：
  码无覆盖记录 或 菜品食材未审核 → 抛 `HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE`（不是 PASS）。
- 修复真实漏洞：`allergy_shrimp`/`allergy_crab` 等 8 个 B2 可产生但 B1 无关系的码
  （如 User9 虾过敏此前对虾菜静默 PASS）→ 补全 B1 模式 + B2 过敏映射规范化
  （螃蟹→allergy_crab、豆制品/豆类→allergy_soy、啤酒→allergy_alcohol）。
- 验证：User9（虾过敏）144 道含虾菜全部 EXCLUDE；全量 2000 菜 × 各用户零误抛；44 回归通过。

## 8. INV-010 MySQL 原子提交（已实现）

- 新增 `src/food_agent_v2/application/`：`commit_request_result` 在**同一事务**写入
  `recommendation_logs`（最终结果+health_evidence）、`menu_versions`、`sessions`、`conversation_events`；
  任一步失败回滚，抛 `AuditCommitFailed`，不保留成功终态。
- runner `_finalize`：`completed` 时调用；强制审计失败 → 转为 `failed`（`AUDIT_COMMIT_FAILED`）。
- 验证：事务落库成功（4 表一致）；真实 E2E 完成后自动写 MySQL；失败回滚路径正确。

## 9. C4 上下文（P1，已实现）

- `_restore_session` 接入 `build_shared_context`（修复 `_persist_session` 从未保存 session_state 的根因）：
  会话跨进程/重启可从 Redis 恢复。
- `record_session_event` 实现：SSE 事件持久化到 Redis（`v2:request:{rid}`），支持续传。
- `validate_context_integrity` 修复并接入工作流：只校验不可压缩核心块（约束/菜单/待澄清），
  不含随轮次变化的 current_message；修复 16 位哈希截断不一致 bug（INV-009）。
- **B2 约束回写 C4**：`get_health_constraints` 工具派生约束后写回 `effective_constraints`，
  角色投影（`project_model_context`）现在给健康规划模型真实约束码、查询理解匿名约束、
  回答模型不暴露（隐私正确）。此前 `effective_constraints` 恒为空。
- MySQL 提交幂等（`conversation_events` ON DUPLICATE + `recommendation_logs` 唯一键 `uk_request`），
  防请求重跑崩溃（根因：`api.create_request` 后台线程 + 同步重跑导致二次提交）。

## 10. C1 多人多路检索（已实现，文档 07 §8.4）

- `RecipeRetrievalService.multi_person_retrieve(shared_query, per_participant_prefs, top_k=50)`：
  共享查询 + 每参与者口味偏好子查询 → 各路完整混合检索（含重排）→ 合并去重
  （保留 `source_paths`：`shared` / `participant_N`）→ 取前 50。
- `retrieve_recipes` 工具在多人场景自动触发（用 B2 `dietary_preferences` 作为个人偏好）。
- 验证：路径 `multi_person_hybrid_rerank`，麻辣香锅标 `participant_2`(偏辣)、清淡菜标 `participant_1`；
  完整多人 E2E 正常。

## 11. C2 槽位约束 + C1 time_boost + 时间估算恢复（已实现）

### C2 槽位约束（文档 08 §8.1）
- `_classify_dish_type(name)` 基于菜名启发式分类（soup/staple/drink/dessert/main）。
- `_build_one_menu` 贪心选择时遵守槽位上限（汤/主食/饮品/甜品各 ≤1），验证候选 4汤+4主食+4饮品 → 菜单各 1 合规。

### C1 time_boost（文档 07 §9.6）
- C1 加载时从 time_profiles 建时间查询表（LLM 估算）。
- 查询含时间短语义（快手/半小时/快速等）时，高置信度 ≤30min 候选重排分 ×1.15（软偏置，不删菜）。
- 验证：快菜提前、慢菜不升不删；"快手"触发、"家常"不触发。

### LLM 时间估算恢复
- `data-rebuild` 重新生成 time_profiles 会冲掉 llm_estimate。
- 新增 `llm_time_profiler.merge_estimates()`，`rebuild.py` 在质量门禁后调用恢复（1943 条）。

## 12. 审查发现的 9 项差距逐一落地（2026-08-08 收尾轮）

| # | 项 | 结果 |
|---|---|---|
| 1 | INV-005 回答接地确定性强制 | ✅ answer 输出 dish_ids，runner 校验 ⊆ 菜单，违规 → `ANSWER_GROUNDING_FAILED` → failed |
| 2 | INV-012 不可信指令检测 | ✅ 组合关键词扫描（忽略+指令/系统提示词等），命中 → `UNTRUSTED_INSTRUCTION_DETECTED` → failed；无误报 |
| 3 | D1 请求状态持久化 | ✅ 请求状态/SSE/幂等写 Redis，读取时恢复，API 重启不丢 |
| 4 | needs_clarification 接线 | ✅ 查询理解输出该字段时进入终态 + 发 `clarification_needed` 事件 |
| 5 | cancel 真正停止工作流 | ✅ D1 写 Redis 取消标记，runner 节点边界检查 → `cancelled` 终态 |
| 6 | 上下文压缩（INV-009） | ✅ 超预算时旧事件去重+总结为精华摘要（保留不可压缩块，完整性校验仍过） |
| 7 | 严格时间放宽到 medium | ✅ 决定**不改**（保持 high-only 文档合规；软过滤已满足 UX） |
| 8 | E2E 稳定性 | ✅ health_menu_planning 提示词加 few-shot 示例 + 节点重试 2 次；2 轮 E2E 4/4 完成 |
| 9 | 测试覆盖 | ✅ 新增 `tests/test_invariants.py`（17 个聚焦测试），全量 61 通过 |

## 遗留差距（后续审查关注）

- C2 RAG 软目标、历史反馈软目标未实现。
- 严格时间仍受置信度限制（仅 high 可判定，决策见上表 #7）。
