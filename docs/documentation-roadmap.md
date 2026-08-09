# V2文档建设路线图

- 状态：`APPROVED`
- 适用项目：`program_v2`
- 更新日期：2026-08-09

## 状态定义

| 状态 | 含义 |
|---|---|
| `PLANNED` | 已确定目标、边界和编写顺序，尚未进入审查 |
| `IN_REVIEW` | 已形成完整文档，正在进行一致性检查或用户审查 |
| `APPROVED` | 已通过审查，是后续设计与实现的有效依据 |
| `SUPERSEDED` | 已被明确的新版本替代，仅保留历史追溯用途 |

状态只表示文档生命周期，不表示对应业务代码已经实现。

## 阶段A：系统总览基线

| 顺序 | 文档 | 状态 | 依赖 |
|---:|---|---|---|
| A1 | `00-system-overview.md` | `APPROVED` | ADR-0001及已确认的全局讨论结论 |
| A2 | `contracts/global-invariants.md` | `APPROVED` | A1 |
| A3 | `contracts/module-boundaries.md` | `APPROVED` | A1、A2 |
| A4 | `scenarios/recommendation-lifecycle.md` | `APPROVED` | A1、A2、A3 |
| A5 | `decisions/README.md` | `APPROVED` | A1至A4 |
| A6 | `contracts/data-artifact-contracts.md` | `APPROVED` | A1、A2、ADR-0004 |
| A7 | `contracts/deepseek-execution-contract.md` | `APPROVED` | A1至A6、批准的整改方案 |

阶段A通过用户审查后，才能进入各模块详细设计。

## 阶段B：数据与确定性能力详设

| 顺序 | 文档 | 状态 | 主要依赖 |
|---:|---|---|---|
| B1 | `modules/01-data-engineering.md` | `APPROVED` | 阶段A |
| B2 | `modules/02-user-health-profile.md` | `APPROVED` | B1 |
| B3 | `modules/03-recipe-ingredient-processing.md` | `APPROVED` | B1 |
| B4 | `modules/04-health-rule-engine.md` | `APPROVED` | B2、B3 |
| B5 | `modules/05-time-and-steps.md` | `APPROVED` | B3 |
| B6 | `modules/06-nutrition-scoring.md` | `APPROVED` | B3 |

## 阶段C：检索、规划与Agent详设

| 顺序 | 文档 | 状态 | 主要依赖 |
|---:|---|---|---|
| C1 | `modules/07-rag-retrieval.md` | `APPROVED` | B1、B3 |
| C2 | `modules/08-menu-planning.md` | `APPROVED` | B4、B5、B6、C1 |
| C3 | `modules/09-agent-workflow.md` | `APPROVED` | B4、C1、C2 |
| C4 | `modules/10-memory-and-context.md` | `APPROVED` | C3 |

## 阶段D：交付与验证详设

| 顺序 | 文档 | 状态 | 主要依赖 |
|---:|---|---|---|
| D1 | `modules/11-api-and-sse.md` | `APPROVED` | C3、C4 |
| D2 | `modules/12-answer-and-frontend.md` | `APPROVED` | C3、D1 |
| D3 | `modules/13-testing-and-acceptance.md` | `APPROVED` | 阶段B、阶段C、D1、D2 |
| D4 | `modules/14-migration-and-cleanup.md` | `APPROVED` | D3 |

## 已批准的架构记录

| 文档 | 状态 | 说明 |
|---|---|---|
| `decisions/0001-v2-workspace-and-migration.md` | `APPROVED` | 确定并列V2工作区、模块化单体、逐模块迁移和Git提交规则 |
| `decisions/0002-bridge-layer-fix.md` | `APPROVED` | 2026-08-07 桥接层伪造数据修复，工具全部映射真实服务 |
| `decisions/0003-implementation-round-2-deviations.md` | `SUPERSEDED` | 仅保留 2026-08-08 偏离历史，不再授权实现 |
| `decisions/0004-fixed-source-and-one-time-identity-rebuild.md` | `APPROVED` | 固定 2,000 条菜品、B1 单一解析和一次性食材身份重建 |
| `decisions/0005-strict-time-semantics.md` | `APPROVED` | 严格时间三值证据与软时间偏好分离 |
| `decisions/0006-post-commit-event-publication.md` | `APPROVED` | 结果与审计原子提交，成功事件通过事务 outbox 在提交后发布 |

## 2026-08-09 整改执行阶段

| 顺序 | 交付 | 状态 | 说明 |
|---:|---|---|---|
| R1 | `reports/2026-08-09-v2-code-review.md` | `APPROVED_INPUT` | 20 项实现审查发现和证据 |
| R2 | `reports/2026-08-09-v2-remediation-proposal-draft.md` | `OWNER_APPROVED` | 已获用户确认的整改策略来源，正式准则以 ADR/契约为准 |
| R3 | `superpowers/plans/2026-08-09-v2-full-chain-remediation.md` | `APPROVED_FOR_EXECUTION` | DeepSeek 逐任务执行计划；必须服从执行契约和阶段闸门 |

## 模块详细设计统一模板

每份模块详细设计必须依次包含以下内容：

1. 模块目的；
2. 职责；
3. 非职责；
4. 上游输入；
5. 下游输出；
6. 数据模型与数据所有权；
7. 核心处理流程；
8. 算法与确定性规则；
9. 公开接口或模型工具；
10. 依赖方向；
11. 异常、错误码与停止条件；
12. 构建与初始化要求；
13. 测试和验收标准；
14. 旧实现与目标实现的差异；
15. `REUSE`、`REFACTOR`、`REWRITE`、`REMOVE`或`PENDING`迁移分类；
16. 确认无消费者后才能执行的清理项。

模块文档只定义本模块内部细节。跨模块共享字段、状态、Artifact、错误码和证据格式必须引用`contracts/`中的权威契约，不在各模块重复定义。

## 审查与提交规则

- 每份文档形成完整逻辑块并通过自审后单独提交。
- 文档进入用户审查时标记为`IN_REVIEW`。
- 用户明确批准后标记为`APPROVED`。
- 修改已经批准的全局边界时，必须同步检查全部下游模块文档。
- 不使用百分比或“基本完成”等模糊状态。
