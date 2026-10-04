# 交互优化剩余项实施计划

> 按既有已更新方案在当前会话逐项执行，沿用当前工作区。

**Goal:** 落实公开进度投影和执行身份，补齐四菜一汤及 HTTP 提交失败后的恢复证据。
**Architecture:** D1 集中投影进度事件；C3 在真实工具调用前分配身份，回执使用同一身份。混合菜单的菜型要求交由 C2 强制执行，不将全体候选限制为汤。故障注入仅存在于独立验证服务。
**Tech Stack:** Python / FastAPI / LangGraph / MySQL / Redis / Vue / Playwright。

## 约束

- 保留固定菜数、时间上限、全员健康审查和菜单版本保护。
- 保留字段白名单和禁止词拦截；公开摘要仅来自固定模板。
- 同一事实重播去重，新调用及新执行代次拥有不同 ID。
- 不改固定数据；不覆盖此前失败日志；不向生产 API 增加故障入口。

## 1. 公开事件与调用身份

Files: `src/food_agent_v2/d1/progress.py`, `d1/__init__.py`, `c3/tool_handler.py`, `c3/graph_orchestrator.py`, `tests/d1/test_progress_projection.py`, `tests/d1/test_sse_cold_restore.py`, `frontend/src/stores/recommendation.ts`, `frontend/src/types.ts`, `frontend/src/features/chat/ChatPanel.vue`, `frontend/src/stores/progress.test.ts`。

- [x] 先验证任意动态文本不会公开：摘要必须为固定模板。
- [x] 验证 running/done 使用同一 invocation_id；执行代次改变后 ID 不碰撞；显式重播不新增事实。
- [x] 工具回执的 tool_call_id 使用调用前分配的身份，验证与 SSE 对齐。
- [x] 实施投影并执行 D1/C3 和既有隐私审计回归。

## 2. 混合菜单结构

Files: `c3/fast_intent.py`, `c3/tools.py`, `c3/tool_handler.py`, `tests/c3/test_mixed_menu_structure.py`, `c2/planner.py`, `tests/c2/test_soup_classification.py`, `frontend/e2e/structure.spec.ts`。

- [x] 验证四菜一汤解析为 5 道菜及汤要求。
- [x] 验证 mixed menu 不对所有候选施加仅汤过滤，单独汤请求仍按菜型过滤。
- [x] 修复后检查 C2 严格包含一汤并生成真实五道菜，保留 90 分钟不可行证据。切换免费 Qwen/Qwen3-8B、补齐提示词审核前置条件后，HTTP 8 项通过，见 free-qwen-explicit-audit-soup-2026-10-04.json。

## 3. 真实 HTTP 提交故障

Files: `verification/http_commit_fault_app.py`, `verification/http_commit_fault_audit.py`。

- [x] 独立 8004 测试服务仅对测试控制文件指定的新请求注入一次 SQL 写入后异常。
- [x] 真实换菜请求进入 recovery_required，原菜单不变；审计、结果、outbox 均无残留。
- [x] 同键同体恢复到 generation 2，成功提交后菜单、MySQL、SSE 一致且续传准确。
- [x] 关闭测试服务，更新实施文档及证据索引。

## 最终验证

- [x] 全后端非 live 基线、前端基础及构建。
- [x] 原请求恢复浏览器与冷重启已验证；免费 Qwen 下最新四菜一汤、执行身份/刷新恢复及澄清续接均已通过，分别保留原失败与补测报告。
- [x] 汇总实际通过/失败数量及证据范围，历史结果保留。

## 本轮结果

完整后端 1581 项、前端 80 项通过，构建成功。HTTP 真实 SQL 故障回滚及原请求恢复最终 17 项通过；主 API 冷重启直接 SSE 恢复已验证。浏览器整套 10 项前 8 项完成，后续澄清和最新四菜一汤遭账户欠费，整套已停止。完整证据见实施文档第 12 节，未完成项保持未勾选。


## 免费模型切换补测

已切换硅基流动免费 Qwen/Qwen3-8B；百炼新模型仍受账号欠费阻断。HTTP 四菜一汤 8 项与定向回归 94 项通过；浏览器结构 1 项通过；澄清首次超时后改为显式选择界面取消时间选项，补测 1 项通过。详见实施文档第 13 节。用户后续指定 DeepSeek 官方新密钥，当前正在验证官方 Pro，见第 14 节。


## 用户指定官方 DeepSeek 的结果

已切换 deepseek-v4-pro。修复主食挤占菜槽位与验收脚本固定 Qwen 型号；新 Pro 浏览器 2/2、原浏览器成单只读核对 8/8、定向回归 35/35 通过。完整基线初跑 1583 通过 / 2 验收配置失败，配置门卫修复后只定向验证；另有一次模型空输出，已 fail-closed 保留记录。完整范围见实施文档第 14 节，不宣称新模型完整 10 项已重跑。

## 当前最终收尾（旧记录之后的有效结论）

旧配置清理和最终复验已完成：后端 1596、前端 80、真实浏览器 10、HTTP 43、冷重启 16 项通过；3 个 Windows 符号链接权限测试跳过。严格汤位进一步纠正蒸蛋误分类，并核对真实西红柿豆腐羹源步骤。详见 `2026-10-04-clean-chain-acceptance.md`、交互设计第 15 节和 verification 当前汇总。五份重复真实浏览器配置及旧提供商密钥别名已删除，默认 Playwright 入口为唯一真实验收入口。
