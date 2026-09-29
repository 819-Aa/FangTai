# 模块详细设计

每个模块文档统一描述职责、非职责、输入、输出、数据所有权、处理流程、公开接口、依赖、异常、构建与初始化、测试、迁移分类和清理项。

模块之间只能通过已批准的契约和公开接口协作。系统总览负责说明它们如何构成完整链路，模块文档不得自行改变全局不变量。

2026-08-09 批准的整改基线已写入每个模块的“0”节，并共同服从[固定数据契约](../contracts/data-artifact-contracts.md)、[DeepSeek 执行契约](../contracts/deepseek-execution-contract.md)和 ADR-0004 至 ADR-0006。若后文章节存在历史措辞歧义，以“0”节和上游契约为准，并应在实现任务中同步消除冲突。

## 当前模块

- [01 数据工程](01-data-engineering.md)（`APPROVED`）
- [02 用户健康档案](02-user-health-profile.md)（`APPROVED`）
- [03 菜品与食材处理](03-recipe-ingredient-processing.md)（`APPROVED`）
- [04 健康规则与审查引擎](04-health-rule-engine.md)（`APPROVED`）
- [05 时间与步骤规划](05-time-and-steps.md)（`APPROVED`）
- [06 营养评分](06-nutrition-scoring.md)（`APPROVED`）
- [07 混合RAG检索](07-rag-retrieval.md)（`APPROVED`）
- [08 菜单规划](08-menu-planning.md)（`APPROVED`）
- [09 LangGraph Agent 编排](09-langgraph-agent-orchestration-redesign.md)（`IMPLEMENTED`；旧 [工作流设计](09-agent-workflow.md)仅供历史追溯）
- [10 上下文与记忆](10-memory-and-context.md)（`APPROVED`）
- [11 API与SSE](11-api-and-sse.md)（`APPROVED`）
- [12 回答与前端](12-answer-and-frontend.md)（`APPROVED`）
- [13 测试与验收](13-testing-and-acceptance.md)（`APPROVED`）
- [14 迁移与清理](14-migration-and-cleanup.md)（`APPROVED`）
