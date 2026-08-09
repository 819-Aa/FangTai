# 共享契约

本目录维护WorkflowState、Artifact、工具、错误码、证据引用和请求幂等等跨模块协议。

契约最终以可执行Schema作为唯一权威定义，文档解释字段语义、约束和兼容策略，不手工维护与代码冲突的第二套结构。

## 当前权威契约

- [全局不变量](global-invariants.md)
- [模块边界与数据所有权](module-boundaries.md)
- [固定数据与离线 Artifact 契约](data-artifact-contracts.md)
- [DeepSeek V4 Flash 整改执行契约](deepseek-execution-contract.md)

当现有代码、历史报告或 ADR-0003 与上述契约冲突时，现有实现必须整改，不能用“已经实现”作为保留偏离的理由。
