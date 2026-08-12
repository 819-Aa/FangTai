# 测试边界

V2测试分为单元测试、契约测试、集成测试、跨模块场景测试和回归测试。

健康硬筛选、工具权限、State流转、重试上限和最终菜单一致性必须采用确定性断言；模型表达质量测试不能代替健康安全验证。

## 分级运行

- `uv run pytest -m "not live" -q`：普通回归（597 项），排除真实模型 live 测试。
- `uv run pytest tests/test_prompts_live.py -q`：真实模型 live 验收（7 项，需 DeepSeek API key）。
- `uv run pytest tests/e2e -q`：T23 真实全链路验收（需 H04 空 V2 环境已 data-initialize、API 服务器运行于 localhost:8001）。

## T23 真实全链路验收

`tests/e2e/test_full_chain_real.py`：成功路径（单人、多人全员交集、明确菜数、默认 5 道、
软"尽量快"、硬截止可证明、多轮替换/恢复、SSE 断线重连、幂等重放、刷新继续同 session），
并做跨库一致断言（API completed ↔ MySQL result/audit/session/outbox ↔ Redis SSE 终态）。

`tests/e2e/test_full_chain_failure_paths.py`：失败路径（无安全/无可行/严格时间独立终态、
健康矩阵缺键、Qdrant/Redis/MySQL 不可用、模型漏调必需工具、提交审计失败、dispatcher 崩溃
恢复、取消、恶意提示注入、未知事件、答案改菜），全部 fail-closed，不伪装成普通 failed。

验收执行：`powershell -ExecutionPolicy Bypass -File scripts/run_full_acceptance.ps1`

