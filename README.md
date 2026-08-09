# 健康菜品推荐系统 V2

`program_v2`是健康菜品推荐系统的新一代模块化实现，与旧项目`../program`并列存在。

当前仓库已包含后端、前端、数据库和测试的候选实现，但 2026-08-09 审查确认它尚未满足 V2 准则，也未打通真实基础设施的前后端全链路。旧项目在 V2 通过最终验收前保持独立可运行，不修改、移动或删除。

已批准的整改准则从[文档索引](docs/README.md)进入；实现问题见[代码审查报告](reports/2026-08-09-v2-code-review.md)，DeepSeek 的执行边界见[执行契约](docs/contracts/deepseek-execution-contract.md)，逐任务方案见[全链路整改计划](docs/superpowers/plans/2026-08-09-v2-full-chain-remediation.md)。在计划最终阶段通过前，不得把当前实现标记为 production-ready。

交给 DeepSeek V4 Flash 时只使用根目录的 [DEEPSEEK_START_HERE.md](DEEPSEEK_START_HERE.md) 作为启动入口，并在 `remediation/deepseek-v4-flash` 分支执行。

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
