# 健康菜品推荐系统 V2

`program_v2`是健康菜品推荐系统的新一代模块化实现，与旧项目`../program`并列存在。

当前主链为：Qwen 语义重写 → 确定性约束解析与失败兜底 → H07 RAG → B4 健康校验 → 菜单规划 → Redis 会话版本记忆。嵌入与重排通过 SiliconFlow API 调用 BGE-M3/BGE-Reranker，不需要本地模型文件。默认走确定性快速路径（`WORKFLOW_MODE=fast_path`）。

当前验收与集成记录见 [H07 Agent 链路集成验证](reports/2026-09-01-h07-agent-chain-integration-verification.md)。一键验收：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_full_acceptance.ps1
```

## 建设原则

- 采用模块化单体，不拆分微服务。
- 系统总设计统一全局边界，模块文档描述各自详细实现。
- 共享契约只有一个权威定义，模型、工具、工作流、API和前端共同遵守。
- 新实现按模块审查旧代码，分别标记为复用、重构、重写或移除。
- 健康硬筛选、状态流转和最终提交由可验证的系统约束保证。
- 新旧系统使用隔离的数据派生产物、服务命名空间和发布标识。

## 当前目录

```text
program_v2/
├── docs/           # 总设计、模块设计、共享契约、场景和架构决策
├── src/            # 后端模块化单体实现
├── frontend/       # 前端应用
├── db/             # 数据库Schema和迁移
├── data/           # V2输入、处理产物、发布产物和评测数据
├── tests/          # 单元、契约、集成、场景和回归测试
└── reports/        # 验收和质量报告
```

文档入口见[docs/README.md](docs/README.md)。目录建立和迁移决策见[0001-v2-workspace-and-migration.md](docs/decisions/0001-v2-workspace-and-migration.md)。
