# ADR-0006：事务提交后发布成功事件

- 状态：`CONFIRMED`
- 日期：2026-08-09
- 适用范围：Application、D1 API/SSE、D2 前端、C4 记忆和审计

## 背景

如果系统在最终结果和强制健康审计提交之前发送 `answer_ready`，前端可能收到一个随后被数据库回滚的“成功答案”。只靠调整代码顺序也无法解决进程在提交后、发事件前崩溃造成的丢事件问题。

## 决策

1. 最终菜单复核、统一审查、`AnswerArtifact`、强制健康审计、会话事实和结果记录必须在同一 Application 提交边界完成。
2. 数据库事务同时写入 `recommendation_result` 和 transactional outbox，之后才提交。
3. outbox 至少包含按序的 `answer_ready` 和 `result_committed` 事件；事件负载只引用已提交的不可变结果。
4. outbox dispatcher 在事务提交后发布事件，采用稳定 `event_id` 和幂等键；重复发布不得造成重复结果或重复会话事实。
5. `answer_ready` 可以继续先于 `result_committed` 被客户端观察，但二者都必须对应已经提交的结果，不能在事务前发送。
6. 失败、取消或回滚不得产生任何成功事件。SSE 重连只能按游标重放已持久化事件，不得重跑推荐请求。
7. 前端以 `result_committed` 或状态查询的 `completed` 作为最终完成依据；只收到 `answer_ready` 时显示“结果已生成，正在确认提交”，不得永久标记完成。

## 强制验收

- 模拟提交失败：客户端不能观察到成功事件，结果与审计均不存在。
- 模拟提交后 dispatcher 崩溃：重启后可从 outbox 补发，事件 ID 不变。
- 模拟重复投递和 SSE 重连：前端只呈现一份结果。
- 校验事件负载中的 `request_id`、`plan_id`、`menu_hash`、`answer_hash` 与已提交记录完全一致。
