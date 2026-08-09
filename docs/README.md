# V2文档索引

V2文档采用“系统总览、模块详设、共享契约、跨模块场景、架构决策、执行计划”六层结构。2026-08-09 用户已批准的整改基线已经进入正式文档；当前代码仍是待整改实现，不能用代码现状反向修改已批准准则。

```text
docs/
├── 00-system-overview.md   # 系统总设计与全局架构入口
├── modules/                # 各业务与工程模块的详细设计
├── contracts/              # WorkflowState、Artifact、工具、错误码和证据契约
├── scenarios/              # 关键端到端场景与节点时序
├── decisions/              # 已确认的架构选择及其理由
└── superpowers/plans/      # 可逐任务执行和验收的整改计划
```

## 文档职责

- 系统总设计只描述全局目标、边界、数据流、依赖关系和不变量。
- 模块文档描述职责、非职责、输入输出、算法、异常、依赖和验收标准。
- 契约文档维护跨模块共用的权威协议，模块文档只引用，不重复定义。
- 场景文档检查模块组合后的完整行为。
- 决策文档记录选择、替代方案、影响和后续约束。
- 执行计划把批准设计拆成红测试、实现、验证和提交任务，不拥有修改架构语义的权限。

## 权威顺序

发生冲突时依次服从：用户确认的 ADR → `contracts/` 和全局不变量 → 模块详设与场景 → 批准执行计划 → 当前 V2 实现 → 旧 `program`。发现无法消解的冲突必须停止，不允许执行模型自行选择。

## 阶段A总览基线

- [系统总体设计](00-system-overview.md)
- [全局不变量](contracts/global-invariants.md)
- [模块边界与数据所有权](contracts/module-boundaries.md)
- [固定数据与离线 Artifact 契约](contracts/data-artifact-contracts.md)
- [DeepSeek V4 Flash 整改执行契约](contracts/deepseek-execution-contract.md)
- [推荐请求生命周期](scenarios/recommendation-lifecycle.md)
- [架构决策登记](decisions/README.md)

当前固定数据、严格时间和提交事件的批准决策分别见 [ADR-0004](decisions/0004-fixed-source-and-one-time-identity-rebuild.md)、[ADR-0005](decisions/0005-strict-time-semantics.md) 和 [ADR-0006](decisions/0006-post-commit-event-publication.md)。ADR-0003 已废止，仅供历史追溯。

全部已确认、明确延期和明确排除的选择见[V2架构决策登记](decisions/README.md)。

文档的建设顺序、依赖关系和审查状态见[V2文档建设路线图](documentation-roadmap.md)。

已批准的逐任务交付方案见[2026-08-09 V2 全链路整改执行计划](superpowers/plans/2026-08-09-v2-full-chain-remediation.md)。执行者必须同时遵守 DeepSeek 执行契约，不能只读取计划而忽略阶段闸门。

实际交接从仓库根目录的 [DEEPSEEK_START_HERE.md](../DEEPSEEK_START_HERE.md) 开始；该入口固定工作目录、基线标签、执行分支和 T00 校验命令。
