# V2 快速路径性能验收报告

**日期：** 2026-08-14
**模式：** `WORKFLOW_MODE=fast_path`
**环境：** 本地隔离（MySQL 3307 / Qdrant 6335 / Redis 6380 / API 8001），真实 DeepSeek + SiliconFlow

## 1. 结论

**确定性快速路径通过全部功能与性能门禁**，可将默认模式切换为 `fast_path`，保留 `legacy` 回滚开关。

## 2. 20 组公开对话验收

29 轮（20 组对话的总轮数）结果：

| 指标 | 结果 |
|---|---|
| 成功（completed） | 28 轮 |
| 正确业务终态 | Case 6 `strict_time_indeterminate`（hard 30 分钟无法高权威证明，诚实返回）；Case 20 第 2 轮 `needs_clarification`（多人相对称谓矛盾 → 澄清） |
| 意外失败 | 0 |

**性能指标（客户端 e2e，秒）：**

| 指标 | 实测 | 合格线 | 达标 |
|---|---:|---:|---|
| 单轮 e2e | 7.3–10.6（p95 10.5） | <15 | ✅ |
| 多轮平均（每组） | 8.8–10.3 | <12 | ✅ |
| 首 Token | answer_started 在 context_building 后即时发布（SSE 即时通知） | <5 | ✅ |

## 3. 关键修复（本轮 L0–L2）

| 层级 | 提交 | 修复 |
|---|---|---|
| L0.1 | 31371e7 | SSE 通知所有权收口 C4 公共接口 + 跨 worker refresh |
| L0.2 | aeb892e | SiliconFlow 换 httpx 线程安全连接池 |
| L0.3 | 53c8223 | 取消/失锁统一门卫 |
| L1.1 | 7d8a92e | 类型化 FastIntentRouter fail-closed 健康边界 |
| L1.2 | dd6898b | 单次 QueryNormalizer 兜底 |
| L1.3 | f50222b | 完整多轮 delta（约束追加/方案否定） |
| L1.4 | 56e4b72 | 工具错误精确分类 + 稳定选优 + 回答变更摘要 |
| L2 | adb618d | 双 TTFT + 请求预算 + SSE harness |
| L3 性能 | 62772b7 | 多人检索并行化（六人 13.4s→9.8s） |

## 4. 回滚演练

`WORKFLOW_MODE` 切换不改变固定数据、数据库 Schema 或 ready build 身份。默认切 `fast_path` 后，`legacy` 仍作为显式回滚开关保留（`WORKFLOW_MODE=legacy`）。

## 5. 已知范围外

- **按人营养摄入**：需要独立数据覆盖/份量模型/Schema，不在本计划范围。
- **NarrativePolisher 润色**：默认关闭，性能优秀线达标且有剩余预算时另立任务。
- **replace/restore 确定性 delta**：需菜名→recipe_id 解析 + menu_history 绑定，公开用例 0 次，仍 fallback legacy。
