# 测试边界

V2测试分为单元测试、契约测试、集成测试、跨模块场景测试和回归测试。

健康硬筛选、工具权限、State流转、重试上限和最终菜单一致性必须采用确定性断言；模型表达质量测试不能代替健康安全验证。

## 分级运行

- `uv run pytest -m "not live" -q`：普通回归（597 项），排除真实模型 live 测试。
- `uv run pytest tests/test_prompts_live.py -q`：真实模型 live 验收（7 项，需 DeepSeek API key）。
- `uv run pytest tests/e2e -q`：T23 真实全链路验收（需 H04 空 V2 环境已 data-initialize、API 服务器运行于 localhost:8001）。

## T23 真实全链路验收

`tests/e2e/test_full_chain_real.py`：成功路径（单人、多人、明确菜数、默认 5 道、软/硬时间、
多轮、幂等），跨库断言比较**同一份 plan_id/menu_hash/recipe_ids**（API result ↔ MySQL
result/audit/session/menu/outbox ↔ Redis SSE ↔ Qdrant recipe 存在性）。结果语义断言：
明确菜数精确满足、默认恰好 5 道。

`tests/e2e/test_full_chain_failure_paths.py`：失败路径（非法 ref 422、注入、取消、未知事件、
dispatcher 发布后标记前崩溃恢复、审计提交回滚、健康矩阵缺键、Redis 不可用 503、答案改菜
INV-005 拒绝、模型漏必需工具），全部 fail-closed；业务终态 `assert status == expected`。

**本地已验证（确定性、无需 API 服务器/模型）**：健康矩阵缺键、Redis 不可用 503、答案改菜、
模型漏必需工具、未知事件、dispatcher 崩溃恢复、审计回滚——7 项真实 PASS。

**需要 H04 空 V2 环境 + API 服务器 + 真实 LLM**（按 `scripts/run_full_acceptance.ps1` 执行）：
真实成功路径、业务终态精确断言、live 0 skip 报告。环境不满足时脚本返回
`BLOCKED_T23_EMPTY_ENV`。

**真实浏览器前端全链路（页面渲染/SSE 断线重连 UI）不在本仓库测试实现范围内**——需要扩大
T23 allowed_paths 时返回 `BLOCKED_T23_SCOPE`，不以 README 或单元测试冒充。

验收执行：`powershell -ExecutionPolicy Bypass -File scripts/run_full_acceptance.ps1`

