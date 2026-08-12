# MC-03A 已提交结果投影设计

## 目标

把 Application 已原子提交的最终菜单事实稳定投影到 D1 请求状态，使 SSE 中断后的轮询仍能恢复回答与菜单；同时保证 outbox 暂时投递失败不会把已经提交成功的业务结果反写成 `failed`。

本阶段只收口后端成功边界，不增加数据库表、不重建固定数据、不修改 B1/B2 数据产物，也不实现前端菜单卡片视觉层。

## 当前断点

`commit_request_result` 已在一个 MySQL 事务中写入：

- `recommendation_logs` 的完成状态与健康审计；
- `sessions.current_menu_plan_id`；
- `menu_versions` 的 `plan_id/menu_hash/recipe_ids`；
- `answer_ready` 与 `result_committed` 两条有序 outbox 记录。

但 `WorkflowRunner._finalize` 随后只向 D1 写入 `{"status": "completed"}`。因此：

1. SSE 正常时前端能读到回答，但 SSE 失败后轮询只能知道完成，不能恢复回答或菜单；
2. 请求状态中的 `result_summary` 无法与 MySQL、outbox、会话菜单做同一身份校验；
3. 提交成功后的 dispatcher 异常与提交异常共用一个 `try`，存在把已提交成功结果误标为 `failed` 的风险。

## 设计

### 1. 唯一事实与投影边界

MySQL Application 提交仍是唯一业务完成边界。D1 的 `result_summary` 只是面向 API/前端的公开投影，不是第二份可独立修改的菜单事实。

只有 `commit_request_result` 成功返回后，Runner 才能构造并写入以下公开投影：

```json
{
  "status": "completed",
  "answer": {
    "text": "用户可见回答",
    "menu_ref": "已提交菜单 Artifact 引用",
    "evidence_refs": ["公开证据引用"]
  },
  "menu_summary": {
    "plan_id": "已提交 plan_id",
    "menu_hash": "64 位 SHA-256",
    "recipe_ids": [1, 2, 3]
  }
}
```

字段值必须直接来自本次已通过 FinalValidation/Review 且已提交的 Artifact，不允许从模型文本、截断工具摘要或 Redis 事件反推。

### 2. 提交与投递分离

Runner 的完成路径拆成两个边界：

1. **原子提交**：失败时请求转为 `failed/AUDIT_COMMIT_FAILED`，不得生成成功投影；
2. **outbox 尽力即时投递**：提交成功后调用 dispatcher。若即时投递异常或只投递部分事件，请求仍为 `completed`，公开投影仍可查询，未完成 outbox 保持 `pending/dispatching`，由后续 dispatcher 重试。

不得因为 SSE 传输层暂时不可用而否定已经完成的 MySQL 业务事务，也不得额外发布 `request_terminal: failed`。

### 3. D1 状态查询与隐私

`RecommendationAPI.update_status` 继续把投影持久化到其现有 Redis request state；`GET /v1/recommendation-requests/{request_id}` 继续经 `strip_forbidden_fields` 返回投影。

允许公开：`plan_id`、`menu_hash`、`recipe_ids`、回答正文、菜单引用、公开证据引用。

禁止公开：`user_id`、疾病名、健康指标、永久约束详情、参与者到用户档案的映射、内部异常与模型隐藏推理。

本阶段不增加 D1 直连 MySQL 的回退路径；Redis 不可用仍按既有运行时存储策略处理，避免 API 越过 Application/C4 边界。

### 4. 会话一致性

C4 `GET /v1/sessions/{session_id}` 继续从 MySQL `menu_versions` 返回 `current_menu`。MC-03A 不复制或覆盖此数据，只新增契约测试证明：

- 请求 `result_summary.menu_summary`；
- outbox `result_committed.menu_summary`；
- C4 `current_menu`

共享同一 `plan_id/menu_hash/recipe_ids`。

## 失败语义

| 场景 | 请求状态 | result_summary | outbox |
|---|---|---|---|
| Application 提交失败 | `failed` | 不得有成功投影 | 不得留下成功提交 |
| 提交成功、即时投递成功 | `completed` | 完整投影 | `dispatched` |
| 提交成功、即时投递异常 | `completed` | 完整投影 | 保持可重试，不改业务结果 |
| 非成功业务终态 | 保持各自精确终态 | 不伪造菜单投影 | 不产生成功 outbox |

## 测试策略

1. Runner `_finalize` 单元测试：提交成功且 dispatcher 抛错，断言状态仍为 `completed`、投影完整且不发布失败终态。
2. Runner `_finalize` 单元测试：提交失败，断言 `failed/AUDIT_COMMIT_FAILED` 且无成功菜单投影。
3. D1 API 测试：轮询得到回答与 `plan_id/menu_hash/recipe_ids`，禁止字段仍被剥离。
4. Application/会话集成测试：同一提交的状态投影、outbox 与 `current_menu` 身份一致。
5. 回归：C3、D1、Application、C4 与前端现有 Reducer 测试；T23 固定数据只读，禁止初始化。

## 明确不在本阶段

- `/health` 或 `/ready` 的基础设施 readiness 聚合；
- 前端结构化菜单卡片、菜品详情与视觉调整；
- 真实模型 live/Playwright 总验收；
- 新数据字段、新迁移、新固定数据构建或向量重建。

这些工作必须在 MC-03A 提交并复审后以独立任务推进。
