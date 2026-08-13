# 测试边界

V2测试分为单元测试、契约测试、集成测试、跨模块场景测试和回归测试。

健康硬筛选、工具权限、State流转、重试上限和最终菜单一致性必须采用确定性断言；模型表达质量测试不能代替健康安全验证。

## 分级运行

- `uv run pytest -m "not live" -q`：普通回归（717 项），排除真实模型 live 测试。
- `uv run pytest tests/test_prompts_live.py -q`：真实模型 live 验收（7 项，需 DeepSeek API key）。
- `uv run pytest tests/e2e -q`：T23 真实全链路验收（需所有者授权的 T23 隔离环境已
  data-initialize、隔离 API 运行于 localhost:38001）。
- `frontend/e2e/**`：Playwright 浏览器跨端验收（同 T23 隔离环境），不走 Vitest。

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

**真实浏览器跨端**：`frontend/e2e/browser.spec.ts`（Playwright）——成功路径只接受
completed/result_committed、刷新继续同 session（localStorage 比较 + 请求载荷证明）、
SSE 首次连接中断后原生 Last-Event-ID 重连无丢失重复、菜单跨库一致。Vite 经
`VITE_API_BASE_URL=http://localhost:38001` 访问 T23 隔离 API（绝不访问 8001）。

验收执行分为两个互斥入口：

- 复验当前所有者授权且已初始化的 T23 隔离环境：
  `powershell -ExecutionPolicy Bypass -File scripts/run_full_acceptance.ps1 -UseExistingAuthorizedT23`
- 仅在所有者再次明确授权全新空 T23 环境时首次初始化：
  `powershell -ExecutionPolicy Bypass -File scripts/run_full_acceptance.ps1 -InitializeAuthorizedEmptyT23`

现有环境模式严格核对精确容器、卷、端口、manifest/build 和 `/ready` 三存储
身份；禁止 `docker compose up`、`data-rebuild`、`data-initialize` 与卷删除。

脚本严格锁定 H04 BuildManifest 批准值（SHA-256 / build_id / builder commit），
不 data-rebuild；任何资源身份不一致或数据策略不一致均返回精确 `BLOCKED_*`；
live 0 skip 使用 JUnit 机器可解析报告。

