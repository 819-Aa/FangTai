# V2 菜单规划模块设计

- 状态：`APPROVED`
- 更新日期：2026-08-23
- 权威时间语义：[ADR-0007](../decisions/0007-estimated-task-graph-time-semantics.md)

## 1. 当前契约

C2 只在 B4 输出的 `safe_recipe_ids` 内构造菜单。B5 计算预计时间，B6 计算显式营养目标软分；两者都不能接触未通过 B4 的菜品。

## 2. 硬约束

- 菜数、汤/主食/饮品/甜点结构；
- 锁定菜必须属于 safe 集合，拒绝菜不得出现；
- 仅现有食材等请求级封闭约束；
- `max_estimated_time_seconds`：只接受 B5 返回 `estimated_time_feasible=true` 的组合。

固定 build 缺时间图或 B5 仓储失败属于系统失败，不产生 `unknown` 或自动放宽。无组合满足硬约束时返回 `no_feasible_menu`。

## 3. 软目标

RAG 相关性、明确口味、显式 nutrition goal、多样性和历史反馈只在 safe 集合内比较。任一软维度 `unavailable` 时禁用该维度并对其余权重重归一化，不使用默认 `0.5`、零值或模型猜测。

## 4. 时间

B5 对候选组合执行默认厨房容量下的 CP-SAT，返回：

```text
active_seconds
estimated_makespan_seconds
estimated_time_feasible: bool
schedule_hash
```

C2 不重新解析步骤、不简单求和、不读取 cache，也不把预计值表述为保证完成。

## 5. 输出与复核

每个 `FeasibleMenuArtifact` 绑定 `plan_id`、recipe IDs、menu hash、预计 makespan、布尔可行性和软评分分解。C3 只能从已发布方案中选择，终选后由 B4 使用同一菜品集合重新健康复核。

## 6. 验收

- 空或不足的 safe 集合不能产生菜单；
- 锁定菜不在 safe 集合时 fail-closed；
- B5/B6 调用参数精确等于 B4 safe 集合；
- estimated hard limit 只接受布尔 `true`；
- nutrition unavailable 时剩余软目标重归一化；
- 相同输入生成稳定 `plan_id` 和 menu hash。
