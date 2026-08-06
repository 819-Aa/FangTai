# 模块详细设计

每个模块文档统一描述职责、非职责、输入、输出、数据所有权、处理流程、公开接口、依赖、异常、构建与初始化、测试、迁移分类和清理项。

模块之间只能通过已批准的契约和公开接口协作。系统总览负责说明它们如何构成完整链路，模块文档不得自行改变全局不变量。

## 当前模块

- [01 数据工程](01-data-engineering.md)（`APPROVED`）
- [02 用户健康档案](02-user-health-profile.md)（`APPROVED`）
- [03 菜品与食材处理](03-recipe-ingredient-processing.md)（`APPROVED`）
- [04 健康规则与审查引擎](04-health-rule-engine.md)（`APPROVED`）
