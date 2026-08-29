# ADR-0007：source-authoritative 任务图与预计单值时间语义

- 状态：`CONFIRMED`
- 日期：2026-08-23
- 适用范围：B1、C1、B4、B5、B6、C2、C3、回答与前端
- 取代：[ADR-0005](0005-strict-time-semantics.md) 的在线三值、高/低置信度和 `time_source` 语义

## 背景

ADR-0005 允许在线时间结果为 `true/false/unknown`，并把来源和置信度带入运行时。当前固定数据链路已经改为：离线为每个 eligible 菜品构建完整任务图，构建缺图或图非法即失败；在线只读取通过门禁的任务图并做确定性资源调度。因此，缺失数据不再伪装成用户时间语义的第三种状态。

## 决策

### 1. 原始步骤与批准决定是 atom 权威

- `recipe_source_rows.steps_raw` 是步骤文本权威；生产 `split_steps` 与 `atomize_recipe_steps` 生成稳定 atom。
- `data/review/recipe_time_graph_decisions.csv` 只应用 `approved/modified` 决定，可锁定显式时长、任务类型、资源和依赖。
- 模型只在离线补齐未锁定 atom 的单值任务元数据，不能覆盖显式时长。
- ignored `data/cache/recipe_time_graphs.jsonl` 只是可再生缓存，不进入 source manifest，也不能成为离线质量门禁的事实来源。

### 2. 完整性由程序独立验证

发布前必须从 `recipe_source_rows` 与批准决定重新生成真实 atom，并核对：

- atom 一一覆盖、ID 和文本不变；
- 锁定的显式时长不变；
- 任务类型与资源组合合法；
- 依赖存在、无自依赖、无重复且为 DAG；
- 默认厨房容量下可由 CP-SAT 调度。

任一 eligible 菜品不满足即由 `G16_TIME_GRAPH_COMPLETE_AND_ACYCLIC` 阻止整个 build。

### 3. 运行时只有预计单值

`step_tasks` 运行时记录只含 atom ID、文本、单个 `duration_seconds`、任务类型、资源和依赖，不含 `source`、`confidence`、证据、范围或解释。

B5 返回：

- `active_seconds`：manual 与 attended equipment 时长之和；
- `estimated_makespan_seconds`：默认厨房资源下的 CP-SAT 预计完工时间；
- `estimated_time_feasible`：未给上限时为 `true`，给上限时比较预计 makespan 得到布尔值；
- `schedule_hash`：绑定本次菜品集合和任务图调度结果。

固定 build 缺任务图或仓储不可用属于系统失败，不产生 `unknown` 或 `STRICT_TIME_INDETERMINATE`。

### 4. 消费者边界

- C1 只读取 `rag_documents`，不读取 `step_tasks`，不执行 `time_boost`。
- B4 不读取营养或时间值。
- B5/B6 只消费 B4 输出的 `safe_recipe_ids`。
- 用户可见文案使用“预计”，不得承诺保证在时限内完成。

## 影响

- ADR-0005 保留为历史记录，但不再授权当前实现。
- 旧 `strict_time_feasible` 三值、`time_source`、运行时 confidence/range 和 C1 `time_boost` 测试与文档必须替换。
- MySQL/Qdrant 只发布同一 ready build；时间 Artifact schema 固定为 `2.0.0`。
