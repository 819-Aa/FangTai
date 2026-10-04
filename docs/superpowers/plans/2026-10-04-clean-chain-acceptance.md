# 当前调用链清理与完整验收实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 完成当前官方 DeepSeek 配置的全部验收，并去除可干扰当前入口的旧模型配置和旧分支。

**Architecture:** 线上请求唯一进入 LangGraphRecommendationOrchestrator，通用 LLM 客户端只传递当前角色显式配置。检索仍使用 SiliconFlow 嵌入与重排；真实浏览器验收统一使用默认 Playwright 配置，独立 5176 服务。

**Tech Stack:** Python、FastAPI、LangGraph、MySQL、Redis、Qdrant、Vue、Playwright。

## Global Constraints

- 使用现有工作区继续用户已授权的修改，保留其他未提交工作和历史失败证据。
- 当前模型 deepseek-v4-pro，base URL https://api.deepseek.com；不输出任何密钥。
- 线上 API 8003，用户前端 5174；测试前端 5176，不初始化或删除已发布存储。
- 旧协议的历史数据读取兼容和 USDA SR Legacy 营养数据不属于废弃调用链，不按关键词删除。
- 测试失败先检查实际请求与错误；禁止用放宽 completed/健康/结构断言替代修复。

### Task 1: 清理模型入口与环境

**Files:** `src/food_agent_v2/c3/llm_client.py`、`core/config.py`、`c3/graph_orchestrator.py`、`.env`、`.env.example`、`tests/c3/test_agent_validation_regressions.py`。

- [x] 将旧 Qwen 隐式推理档测试改为显式角色配置透传测试：DeepSeek enabled/low、disabled 和空配置；验证没有按旧模型名自动注入参数。
- [x] 运行 `.venv/Scripts/python.exe -m pytest tests/c3/test_agent_validation_regressions.py -q`，观察旧逻辑导致的失败。
- [x] 删除 qwen3.8 模型名触发的分支、未使用的 LLM_MAX_RETRIES 选项、旧模型默认观测名。
- [x] 从本地 `.env` 移除 DASHSCOPE_API_KEY、DASHSCOPE_BASE_URL、DASHSCOPE_PREVIOUS_API_KEY、DEEPSEEK_API_KEY、DEEPSEEK_BASE_URL、BGE_MODEL_PATH、RERANKER_MODEL_PATH，保留有效 LLM_* 与 SILICONFLOW_*。
- [x] 同一测试绿灯后执行验收 preflight；仅记录是否配置和模型名，不输出密钥。

### Task 2: 统一验收入口

**Files:** `frontend/playwright.config.ts`、`frontend/package.json`、`frontend/verification/playwright.*.config.ts`、`scripts/verify_v2_live_samples.py`、README。

- [x] 默认真实浏览器配置使用 5176 与 `/api`，报告由 PLAYWRIGHT_REPORT_NAME 指定，默认按时间生成唯一名称。
- [x] 删除 live/completion/deepseek/free-model/free-clarification 五个重复真实配置；保留 mock API 的 audit 配置。
- [x] 添加 `test:e2e` 命令；清除脚本中无效 WORKFLOW_MODE 设置和旧固定模型说明。
- [x] 运行 `npm run test:e2e -- --list`，确认收集到 10 个真实场景。

### Task 3: 清理后全链路验收

**Files:** `verification/artifacts/final-clean-*`。

- [x] 运行后端完整非 live 测试和前端完整单元测试、构建，读取退出码与实际计数。
- [x] 查数据库确认无有效运行中执行租约，重启 8003 API，检查 `/ready`。
- [x] 执行默认 Playwright 全部 10 项，覆盖新对话/成员选择/提交/历史/取消/SSE/恢复/澄清/四菜一汤。
- [x] 运行 `python -m verification.live_chain_audit`，输出名通过 LIVE_AUDIT_OUTPUT 指定，补覆盖真实换菜、并发菜单版本、幂等和存储一致性。
- [x] 用本轮已提交请求，重启后先直接 SSE 再 GET/幂等 POST，只读核对历史菜单和事件，不再调用模型。

### Task 4: 更新交付记录

**Files:** `verification/README.md`、`verification/artifacts/completion-summary-2026-10-04.json`、交互设计和实施文档。

- [x] 最新章节写明清理范围、真实测试计数及报告文件；历史测试只作为历史证据。
- [x] 检查 diff 与当前配置、唯一入口、活动服务，完成条件按实际结果填写。
- [x] 不创建提交或推送；用户可直接审阅工作区修改。

## 执行记录

- 初始检查：当前生产入口已无线性编排选择；旧 WORKFLOW_MODE 环境变量无读取者。BGE_MODEL_PATH/RERANKER_MODEL_PATH 同样无运行时代码读取者。模型客户端仍有 Qwen 自动注入分支和无效 max_retries 配置，需清理。

- 第一轮真实浏览器 7 通过 / 3 失败，报告保留。页面曾在执行中回到空白对话，具体刷新触发源未捕获；改为先构建再用 preview 运行验收，消除开发热刷新影响。另两项为供应商空正文和重复规划拒绝，已按官方建议补真实 JSON 示例与明确阶段推进要求。
- 最终完整后端：1591 通过、3 跳过、5 deselected。浏览器最终轮运行中，页面使用稳定构建产物。

- 逐道核对真实源菜谱发现 recipe 1740 麻婆蛋羹的步骤是蒸蛋，撤回 stable-browser 的严格结构判定。新增 5 个分类样例，初跑 3 失败 / 6 通过，修复后槽位与结构回归 26 通过。完整基线暴露一条旧“番茄蛋羹=soup”预期，已修正为 main 并保留豆腐羹汤分类检查。
- HTTP 全链路 43/43 通过，无 blocked；该脚本三道菜推荐/替换无 require_soup，不受蒸蛋分类修正影响。最终代码的完整后端和 10 场景浏览器仍在运行。

- 最终交付：独占后端 1596 通过（3 个 Windows symlink 权限跳过）、前端 80、构建 310ms、真实浏览器 10、HTTP 43、冷重启 16 全部通过。API 5804/8003 与用户前端 16580/5174 均 HTTP 200，临时服务退出。新增更正均有原失败和最终报告，验收完成。
