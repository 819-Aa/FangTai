# 前后端全链路验收（2026-10-04）

## GitHub 发布范围

仓库包含完整源码、固定输入、数据库结构、依赖锁文件、测试、验证脚本、实施文档及 JSON 验收报告。关键验收日志随仓库发布；浏览器临时目录、运行日志、数据库存储和 `.env` 密钥配置保留在本机。复现时复制 `.env.example` 并填写自己的凭据。源码中的旧 MySQL 默认密码已移除，数据库密码须显式配置。

发布准备阶段执行配置与健康关系审核回归：`pytest tests/test_config_defaults.py tests/b1/test_health_relation_audit.py -q`，12 项通过。此结果补充下方既有全链路验收记录。

## 当前唯一运行配置

- API：`http://127.0.0.1:8003`；用户前端：`http://127.0.0.1:5174`。
- 编排：LangGraph + 持久化 v2 协议；当前官方 DeepSeek 模型 `deepseek-v4-pro`。
- MySQL 3309 / food_agent_v2_h07、Redis 6382 / v2:h07、Qdrant 6339 / recipe_retrieval_v2_h07。
- 检索：SiliconFlow BAAI/bge-m3 嵌入及 BAAI/bge-reranker-v2-m3 重排。

## 本轮清理与已验证结果

已删除旧 Qwen 按模型名自动注入推理参数的分支、旧默认模型观测名、无效 LLM_MAX_RETRIES 配置。`.env` 中百炼密钥/旧密钥备份、重复 DeepSeek 别名、未使用本地模型路径均已去除，Windows 用户级旧 DASHSCOPE_API_KEY 也已删除。API 子进程清除继承的旧提供商变量后启动，仅使用当前 LLM_* 配置。

五份重复的 live/completion/deepseek/free-model/free-clarification 浏览器配置已删除。真实浏览器唯一入口为 `frontend/playwright.config.ts`，先构建再使用独立 5176 preview 服务，避免开发热刷新干扰。模拟接口 audit 配置单独保留。重启验证也合并到 `restart_audit.py`，要求显式指定本轮请求与报告路径。

| 验证 | 本轮结果 | artifacts 下的证据 |
| --- | --- | --- |
| 模型参数回归 | 旧分支移除前 1 失败 / 41 通过；清理后模型、配置与 preflight 55 通过 | `final-clean-model-red-2026-10-04.log` |
| 完整非 live 后端 | 1596 通过、0 失败；3 项 Windows 符号链接权限跳过、5 deselected，1 条既有弃用警告 | `final-clean-exclusive-backend-2026-10-04.log` |
| 前端基础 / 构建 | 80 通过；最终构建成功 310ms | `final-clean-final-frontend-2026-10-04.log`、`final-clean-verified-browser-2026-10-04.log` |
| 真实 HTTP 全链路 | 43/43 通过，无 blocked | `final-clean-http-2026-10-04.json` |
| 最终浏览器 10 场景 | 10/10 通过，0 失败/跳过/flaky，约 6 分钟；严格一汤四主菜核对真实源步骤 | `final-clean-verified-browser-2026-10-04.json`、`final-clean-actual-soup-source-2026-10-04.json` |
| 重启、冷 SSE 与结果保留 | 两个本轮成单请求共 16/16 通过；先 SSE 再 GET，事件/续传/菜单/代次一致，无重执行 | `final-clean-restart-after-2026-10-04.json` |

## 当前验收命令

在项目根目录执行，所有新测量使用新报告名：

```powershell
.venv/Scripts/python.exe -m pytest tests -m "not live" -q
scripts/run_full_acceptance.ps1 -PreflightOnly
$env:NO_PROXY = "127.0.0.1,localhost"
$env:LIVE_AUDIT_DATE = "2026-10-04"
$env:LIVE_AUDIT_OUTPUT = "verification/artifacts/<本轮唯一名称>.json"
.venv/Scripts/python.exe -m verification.live_chain_audit
```

前端目录：

```powershell
npm test -- --run
npm run build
$env:NO_PROXY = "127.0.0.1,localhost"
$env:PLAYWRIGHT_REPORT_NAME = "<本轮唯一名称>"
npm run test:e2e
```

报告名只能包含字母、数字、下划线和连字符。真实测试创建独立会话，模型及存储均为真实服务；仅测试 SSE 断线和锁竞争时注入传输/锁故障。不要在存在有效执行租约时停止 API。

## 历史证据

此前免费模型、百炼欠费、旧菜单结构误判及断线错误记录只用于追溯，不作为当前配置或完整验收结论。详情保存在交互设计文档第 7—14 节及以下原报告：

- `official-deepseek-browser-2026-10-04.json`：官方 Pro 两项场景。
- `official-deepseek-pro-fixed-soup-2026-10-04.json`：一次模型空输出按失败保存。
- `http-commit-fault-2026-10-04.json`、`http-commit-fault-reconciled-2026-10-04.json`：真实 SQL 写入后异常、事务回滚和原请求恢复，共 17 检查。
- `completion-cold-restart-after-2026-10-04.log`：此前冷 SSE 恢复及菜单保留。
- `free-qwen-explicit-audit-soup-2026-10-04.json`：历史免费 Qwen 成单。
- `completion-billing-block-2026-10-04.json`：旧百炼欠费阻塞。

实施与清理步骤：`docs/superpowers/plans/2026-10-04-clean-chain-acceptance.md`；交互设计：`docs/superpowers/specs/2026-09-30-mainstream-frontend-backend-interaction-design.md`。

## 本轮发现和更正

第一轮浏览器 7 通过 / 3 失败，保留 `final-clean-browser-2026-10-04.json`。页面意外回到新对话的具体刷新源未捕获，后续改为稳定构建预览；模型空正文与重复规划均按失败保存，已补真实 JSON 示例和明确阶段推进规则。稳定预览复跑虽 10 项 UI 检查通过，但逐道检查发现麻婆蛋羹是蒸蛋而非汤，撤回其严格四菜一汤结论。源步骤见 `final-clean-custard-source-2026-10-04.json`。分类修正与回归红/绿记录完整保留，最终验收使用 verified-browser 报告。

官方说明 JSON 输出可能偶发为空，调整提示词是缓解措施；仍保留空输出拒绝和不提交假菜单：[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)。

最终完整后端使用独占验收窗口（确认无活动请求后暂停 API），避免共享 MySQL 上的后台 Outbox 线程争抢测试行。此前一次并行运行的死锁报告 `final-clean-verified-backend-2026-10-04.log` 保留，不计入通过。最终 API 已重启（8003/PID 5804）、用户前端 5174/PID 16580 正常，`/ready` 与页面均 HTTP 200，临时 5176/8004 服务已退出。完整当前汇总为 `artifacts/completion-summary-2026-10-04.json`；早期汇总另存 before-cleanup 文件。
