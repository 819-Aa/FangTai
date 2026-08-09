# DeepSeek V4 Flash 执行入口

- 状态：`READY_FOR_HANDOFF`
- 仓库根：`C:\Users\zhiyo\Desktop\竞赛\program_v2`
- 基线标签：`v2-remediation-baseline-2026-08-09`
- 执行分支：`remediation/deepseek-v4-flash`

## 唯一任务

严格执行 [V2 全链路整改计划](docs/superpowers/plans/2026-08-09-v2-full-chain-remediation.md)，使当前候选实现满足正式 V2 准则并通过真实前后端全链路验收。不得重新设计需求，不得修改固定 2,000 条源数据，不得替人批准健康关系。

## 开始前必须完整读取

按顺序读取：

1. [DeepSeek 整改执行契约](docs/contracts/deepseek-execution-contract.md)
2. [全链路整改计划](docs/superpowers/plans/2026-08-09-v2-full-chain-remediation.md)
3. [V2 文档权威入口](docs/README.md)
4. [代码审查报告](reports/2026-08-09-v2-code-review.md)
5. [整改前交接基线](reports/2026-08-09-v2-handoff-baseline.md)

不得只读取本入口的摘要后直接改代码。

## 第一动作：只执行 T00

在本文件声明的仓库根运行：

```powershell
git rev-parse --show-toplevel
git rev-parse --verify HEAD
git rev-parse "v2-remediation-baseline-2026-08-09^{commit}"
git branch --show-current
git status --short
```

允许开始 T01 的条件：

- repository root 严格等于 `program_v2`；
- `HEAD` 与基线标签指向同一 commit；
- 当前分支为 `remediation/deepseek-v4-flash`；
- `git status --short` 无输出。

任一条件不满足，返回 `BLOCKED_BASELINE_MISMATCH` 并停止。禁止 `git init`、`reset --hard`、删除 `.git`、移动固定数据或自行修改基线标签。

## 执行纪律

- 按 T01 → T24 顺序执行，不合并任务，不越过 Gate S0—S6。
- 每个任务先红测试，再最小实现，再局部验证、全局校准、diff 自审和独立只读复核。
- 每个任务只修改其 `Files` 白名单；锁定契约、测试、计划和任务信封不得由实现任务修改。
- 每个任务单独提交，提交信息使用计划给定文本；不得把失败或未验证工作提交为通过。
- H01—H04、医疗/健康关系批准、固定清单变化、破坏性操作或规范冲突必须停机等待人工决定。
- 只有 T24 独立复核得到 `READY_FOR_OWNER_ACCEPTANCE`，才可声称整改完成。

## 必须返回的阶段结果

每个任务返回：`task_id`、修改文件、红测试证据、验证命令及退出码、不变量映射、风险、commit SHA、下一 Gate 状态。模型的文字“已通过”不能替代原始命令证据。
