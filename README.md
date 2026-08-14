# 健康菜品推荐系统 V2

`program_v2`是健康菜品推荐系统的新一代模块化实现，与旧项目`../program`并列存在。

当前仓库已包含后端、前端、数据库和测试的完整实现。2026-08-14 已通过 20 组竞赛对话用例的功能与性能验收（单轮 e2e 7–11s、多轮平均 <12s、零意外失败），默认走确定性快速路径（`WORKFLOW_MODE=fast_path`），legacy 五模型链作为回滚开关保留。

验收报告见 [reports/2026-08-14-fast-path-performance-acceptance.md](reports/2026-08-14-fast-path-performance-acceptance.md)；性能快速路径设计见 [设计文档](docs/superpowers/specs/2026-08-14-performance-fast-path-design.md)，整改计划见 [整改计划](docs/superpowers/plans/2026-08-14-performance-fast-path-remediation.md)。

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
