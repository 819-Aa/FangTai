# V2 时间与步骤规划模块设计

- 状态：`APPROVED`
- 更新日期：2026-08-23
- 权威决策：[ADR-0007](../decisions/0007-estimated-task-graph-time-semantics.md)

## 1. 当前契约

B1 离线生成并验证 `step_tasks`，B5 在线只读取同一 ready build 的任务图并执行确定性 CP-SAT 排程。运行时没有来源、置信度、范围或 `unknown` 时间状态；所有时长都以“预计”表达。

B5 不判断健康安全，也不解析原始步骤。B4 先输出 `safe_recipe_ids`，C2 只能把该集合交给 B5。

## 2. 离线任务图

步骤权威链固定为：

```text
recipe_source_rows.steps_raw
  -> split_steps
  -> atomize_recipe_steps
  -> approved/modified recipe_time_graph_decisions
  -> validated step_tasks
```

`StepAtom` 是离线中间态：

```text
atom_id
source_step_index
text
explicit_duration_seconds
duration_locked
reviewed_task_type/resources/depends_on
```

模型只补充未锁定 atom 的单值元数据。ignored time cache 是可再生缓存，不是事实输入，也不进入 source manifest。

最终 `StepTask` 固定为：

```text
atom_id
text
duration_seconds
task_type: manual | attended_equipment | unattended_equipment | passive | non_task
resources
depends_on
```

## 3. 构建门禁

`G16_TIME_GRAPH_COMPLETE_AND_ACYCLIC` 必须从 `recipe_source_rows` 和 committed time decisions 独立重建真实 atom，不能从 published tasks 反造，也不能依赖 ignored cache。逐菜检查：

- atom 一一覆盖，ID 与文本不变；
- 显式锁定时长不变；
- 资源属于 `cook/burner/oven/steamer/microwave/blender/fridge/counter`；
- task type 与资源组合合法；
- 依赖存在、无自依赖、无重复且无环；
- 默认厨房容量下可调度。

缺 atom、等数量替换、显式时长篡改、非法资源或环都会阻止整个 build。

## 4. 在线排程

默认厨房容量：一名 cook、两个 burner，以及各一个 oven/steamer/microwave/blender；fridge/counter 不设 cumulative 容量。

- manual 占用 cook；
- attended equipment 占用 cook 与设备；
- unattended equipment 只占设备；
- passive 只受依赖约束；
- non-task 时长必须为 0。

B5 输出：

```text
recipe_ids
active_seconds
estimated_makespan_seconds
estimated_time_feasible: bool
schedule_hash
```

`active_seconds` 是 manual 与 attended equipment 时长之和。未给上限时 `estimated_time_feasible=true`；给定上限时比较预计 makespan。缺任务图、仓储错误或非法图属于系统失败，不能转成三值时间语义。

## 5. 消费者与展示边界

- C1 不读取 `step_tasks`，不做基于时长的检索重排。
- B4 不读取时间值。
- B5 只消费 B4 safe 集合。
- C2 的硬时间条件使用 `estimated_makespan_seconds <= max_estimated_time_seconds`。
- 回答与前端使用“预计 X 分钟”，不得承诺保证完成。

## 6. 验收

- eligible/RAG/time recipe ID 集合精确一致，H05 当前均为 `1932`；
- `step_tasks` schema version 为 `2.0.0`；
- 408 的 source step 6 任务依赖 source step 5；
- G16 的 atom substitution 与 locked-duration tamper 回归失败；
- 单菜与多菜均由 CP-SAT 产生稳定 `schedule_hash`。
