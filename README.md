# 个性化健康膳食推荐 Agent

一个面向多人用餐场景的健康膳食推荐应用。用户用自然语言提出需求，Agent 调用菜谱检索、健康审查和菜单规划工具，生成符合参与者约束的菜单；条件冲突时通过结构化选项与用户澄清。项目包含 FastAPI 服务和 Vue 对话界面。

## 功能

- **多人推荐**：综合参与者的过敏、疾病、忌口、偏好和本轮要求，筛选共享菜单。
- **Agent 编排**：基于 LangGraph 选择检索、审查、规划、扩展检索、澄清等行动，并根据工具反馈继续决策。
- **健康校验**：以标准食材和健康规则审查候选菜品，在提交菜单前再次校验；模型回答不能替代健康结论。
- **多轮调整**：支持追加约束、替换菜品、重新推荐和恢复菜单版本。
- **可追踪请求**：提供幂等提交、请求状态查询、取消请求和 SSE 事件订阅。

## 快速开始

需要 Python 3.11–3.13、[uv](https://docs.astral.sh/uv/)、Node.js 20+、MySQL 8、Redis 和 Qdrant。推荐服务还需要 OpenAI 兼容的 LLM API，以及 SiliconFlow 的嵌入与重排 API。以下命令以 PowerShell 为例。

```powershell
Copy-Item .env.example .env
# 编辑 .env，填写 MySQL 连接信息、LLM_API_KEY 和 SILICONFLOW_API_KEY
uv sync

Set-Location frontend
npm ci
Set-Location ..
```

启动 MySQL、Redis 和 Qdrant，在 MySQL 中创建 `.env` 指定的数据库，并执行 [`db/schema_mysql.sql`](db/schema_mysql.sql)。首次运行还需要生成并导入菜谱、食材和健康规则数据。`data-rebuild` 会在指定目录生成 Build Manifest；通过质量校验后，将其导入空的 MySQL 固定数据表和 Qdrant Collection。构建时 Git 工作区须干净，输出目录须为空。数据来源和审核规则见[数据工程文档](docs/modules/01-data-engineering.md)。

```powershell
uv run food-agent-v2 data-rebuild --staging-dir .\build
uv run food-agent-v2 data-verify --manifest .\build\build_manifest.json
uv run food-agent-v2 data-initialize --manifest .\build\build_manifest.json --confirm-empty-v2
```

分别在两个终端启动服务：

```powershell
# 终端 1：在项目根目录
uv run food-agent-v2 api-start

# 终端 2：在项目根目录
Set-Location frontend
npm run dev
```

打开 <http://127.0.0.1:5174> 使用对话界面；API 文档位于 <http://127.0.0.1:8003/docs>。`.env.example` 配置的本地服务端口为 MySQL `3309`、Redis `6382`、Qdrant `6339/6340`。

## API 示例

向 `POST /v1/recommendation-requests` 提交消息和匿名参与者引用：

```json
{
  "idempotency_key": "dinner-001",
  "message": "给 p1 推荐三道不含花生、40 分钟内完成的晚餐",
  "participants": [{"participant_ref": "p1"}]
}
```

接口返回的 `request_id` 可用于查询 `GET /v1/recommendation-requests/{request_id}`，或订阅 `GET /v1/recommendation-requests/{request_id}/events`。若请求需要澄清，客户端从 `active_clarification` 读取 `question_id` 和选项 ID，并在后续请求的 `clarification_response` 中提交选择。同一请求重试时保持幂等键和请求体一致。完整字段与响应格式见运行中的 [OpenAPI 文档](http://127.0.0.1:8003/docs)。

## 系统组成

```text
Vue 对话界面 ──> FastAPI ──> LangGraph Agent ──> 检索 / 健康审查 / 菜单规划
                         │                                  │
                         └── MySQL、Redis、SSE <── 校验与结果提交
```

`src/food_agent_v2/b1`–`b6` 负责固定数据与领域能力，`c1`–`c2` 负责检索和菜单规划，`c3` 实现 Agent 编排，`c4` 与 `application` 管理会话和提交，`d1` 提供 HTTP 接口，`frontend` 提供 Web 界面。健康硬约束、工具权限及最终提交由服务端校验。

## 开发与验证

```powershell
uv run pytest tests -m "not live" -q
uv run ruff check src tests scripts/verify_v2_live_structured.py

Set-Location frontend
npm test -- --run
npm run build
npm run test:e2e
```

真实浏览器验收使用独立 5176 服务，保留用户正在使用的 5174 页面。当前 LLM 配置为 DeepSeek 官方 `deepseek-v4-pro`，由 `.env` 中的 `LLM_*` 字段指定；检索使用 SiliconFlow 嵌入与重排。旧提供商别名和工作流模式开关已移除。最新验收记录见 [复测说明](verification/README.md)。

领域约束与数据契约见[全局不变量](docs/contracts/global-invariants.md)和[固定数据契约](docs/contracts/data-artifact-contracts.md)。本项目提供膳食推荐，不提供医学诊断或治疗建议。
