# V2文档索引

V2文档采用“系统总览、模块详设、共享契约、跨模块场景、架构决策”五层结构。

```text
docs/
├── 00-system-overview.md   # 系统总设计，完成全维度讨论后编写
├── modules/                # 各业务与工程模块的详细设计
├── contracts/              # WorkflowState、Artifact、工具、错误码和版本契约
├── scenarios/              # 关键端到端场景与节点时序
└── decisions/              # 已确认的架构选择及其理由
```

## 文档职责

- 系统总设计只描述全局目标、边界、数据流、依赖关系和不变量。
- 模块文档描述职责、非职责、输入输出、算法、异常、依赖和验收标准。
- 契约文档维护跨模块共用的权威协议，模块文档只引用，不重复定义。
- 场景文档检查模块组合后的完整行为。
- 决策文档记录选择、替代方案、影响和后续约束。

当前已确认的工作区决策见[0001-v2-workspace-and-migration.md](decisions/0001-v2-workspace-and-migration.md)。

全部已确认、明确延期和明确排除的选择见[V2架构决策登记](decisions/README.md)。

文档的建设顺序、依赖关系和审查状态见[V2文档建设路线图](documentation-roadmap.md)。
