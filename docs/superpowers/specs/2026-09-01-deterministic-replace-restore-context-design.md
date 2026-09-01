# 确定性 Replace/Restore 记忆链路设计

## 目标

把 `replace` 与 `restore` 从 legacy 模型工作流接入现有确定性链路，复用 C4 已提交菜单、最近菜单版本和 QueryPlan 快照。`reject_plan` 已在确定性增量链中，只补回归，不重写。

不保存完整聊天历史，不新增数据库表，不增加模型调用次数，不修改 H07、Qdrant 或 Redis 数据。

## 设计原则

1. 当前菜单和菜单历史是确定事实，不交给模型猜测。
2. 千问只理解本轮替换偏好，不决定目标 recipe ID 或历史版本。
3. 任何恢复菜单都必须按当前健康档案重新经过 B2/B4 和最终校验。
4. 无法唯一定位目标时澄清，不静默选择。
5. 只有最终成功提交才形成新的当前菜单和 QueryPlan 记忆。

## C4 会话投影

`ContextService.get_session_state()` 在现有 `current_menu`、`query_plan` 基础上增加最近 5 个已提交菜单版本。版本顺序以 MySQL `menu_versions` 为权威，每个版本至少包含：

- `plan_id`
- `recipe_ids`
- `menu_hash`
- `committed_at`
- 可用时对应的 `query_plan`

旧版本没有 QueryPlan 快照时仍可读取 recipe IDs，但恢复后使用当前已提交 QueryPlan 的硬约束作为兼容基线。

## Replace 流程

输入示例：`把红烧肉换掉`、`把红烧肉换成清蒸鱼`。

1. 从 C4 读取当前菜单的 `items` 与上一轮 QueryPlan。
2. 使用当前菜单菜名在本轮原文中做精确包含匹配：
   - 恰好一个匹配：绑定该 `recipe_id`；
   - 零个或多个匹配：返回 `needs_clarification`；
   - `不要这道` 等无菜名指代不得猜测。
3. 千问最多调用一次，只重写替换后的正向偏好或食材。
4. 继承上一轮的全局硬约束：餐次、人群、排除食材、健康约束、营养目标、硬时间和菜单菜数。
5. 替换检索不继承上一轮 `rewritten_query` 和正向 `include_ingredients`，避免旧目标把新候选过滤掉；当前消息明确的新口味、菜型、菜系、场景和包含食材用于 RAG。
6. 将目标 ID 传给 `DeltaPlanner(intent="replace")`：其他仍安全的当前菜锁定，目标菜进入 rejected IDs。
7. RAG 补充候选，B2/B4 审查当前菜与候选，C2 生成同菜数的新菜单，最终校验后提交。

千问超时或输出非法时使用现有确定性 fallback；仍必须排除目标 recipe ID。

## Reject 流程

`重新推荐一批`、`换一批` 等继续走现有 `reject_plan` 增量链：拒绝当前全部 recipe IDs，保留参与者及全局硬约束，重新 RAG、健康审查、生成和提交。本次只补端到端回归，避免回退 legacy。

## Restore 流程

输入示例：`回到之前那个方案`、`恢复上一版`。

1. 从 C4 最近 5 个成功版本中定位当前版本紧邻的上一版。
2. 没有上一版时返回 `needs_clarification`。
3. 本次不调用千问、不做 RAG，因为目标 recipe IDs 已确定。
4. 用当前 build 解析历史 recipe IDs；任一菜不存在则返回明确的 `RESTORE_VERSION_UNAVAILABLE`，不得静默替换。
5. 使用当前 B2 永久约束和会话约束重新执行 B4 健康审查。
6. 全部安全后，以历史 recipe IDs 全量锁定生成可行菜单，并执行最终健康校验。
7. 成功后作为一个新的菜单版本提交；其 QueryPlan 使用历史快照，旧版本无快照时使用当前 QueryPlan 硬约束兼容基线。

本次只支持紧邻上一版，不支持“第三版”等任意版本选择，也不解析完整聊天指代。

## 错误与安全边界

- 无当前菜单：`replace` / `restore` 返回澄清，不降级为首次推荐。
- 目标菜无法唯一识别：澄清。
- 无上一版本：澄清。
- 历史 recipe ID 在当前 build 不存在：`RESTORE_VERSION_UNAVAILABLE`。
- 当前健康档案判定不安全：沿用 `no_safe_menu` / 最终校验失败，不提交。
- 存储、上下文完整性或会话锁失败：沿用现有 fail-closed 行为。
- 失败、取消、澄清均不更新当前菜单或 QueryPlan。

## 测试与验收

采用 TDD，至少覆盖：

1. replace 精确匹配当前菜，只替换目标 ID，其他安全菜锁定。
2. replace 无目标或多目标时澄清。
3. replace 千问失败时仍使用 fallback，并拒绝目标 ID。
4. replace RAG 保留上一轮硬排除与健康约束，但不继承旧正向查询和旧 include。
5. reject_plan 拒绝当前全部 IDs，确认不进入 legacy。
6. restore 恢复紧邻上一版的精确 recipe IDs，并重新经过 B4 和最终校验。
7. restore 无历史、历史 ID 不可用、当前健康不安全时不提交。
8. 成功 replace/restore 后，新菜单和 QueryPlan 可由下一轮 C4 恢复。
9. C1/C3/C4 相关回归、Ruff、diff check 和密钥扫描通过。

## 不在范围内

- 完整聊天历史或向量化对话记忆。
- 任意历史版本编号选择。
- 模型推断“这道、第二道、刚才说的那个”等无明确菜名指代。
- 数据库、Qdrant、Redis 或 H07 重建。
