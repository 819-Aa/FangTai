# V2 System Documentation Implementation Plan

> 历史说明：本计划记录阶段A文档建立时的执行假设，其中关于`release_id`和运行时数据发布的内容已被当前系统总览及DEC-C019、DEC-C020修正，不再作为V2要求。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立V2系统总设计及其全局配套文档，形成后续各模块详细设计共同遵守的架构基线。

**Architecture:** 文档采用“一份系统总览 + 全局不变量 + 模块边界契约 + 端到端场景 + 决策登记 + 模块文档路线图”的结构。总览只定义全局边界和协作关系，不重复模块内部算法；模块详设在总览获批后分别规划和编写。

**Tech Stack:** Markdown、Mermaid、Git、PowerShell、现有V2文档结构。

## Global Constraints

- 只修改 `<repo-root>`，不得修改、移动或删除旧 `program`。
- 当前阶段只编写文档，不迁移业务代码，不初始化数据库、Qdrant、Redis或前端工程。
- 系统范围仅覆盖项目已有的健康档案、菜品、食材、步骤、时间、营养和对话数据，不扩展到真实世界人群代表性或临床诊疗。
- 任何菜品只要标准食材命中任一参与者的过敏、疾病、异常指标派生硬约束或明确健康禁忌，整道菜必须排除。
- 多人共享菜单采用全员健康约束的交集，每道最终菜品必须对所有参与者通过健康审查。
- RAG只负责召回符合用户意图的真实菜品，不负责健康安全判断，也不得输出安全结论。
- 营养数据只参与内部软排序；低置信度估算不得触碰健康硬筛选，不在正常回答或前端中展示营养数值。
- 不计算烹饪营养损耗；内部营养依据原始食材理论值。项目不处理分量、`per_serving`、人数份量和采购量。
- 制作时间基于步骤任务、依赖、主动操作、设备占用和被动等待建模；严格时间约束只使用高置信度时间结果。
- 系统使用五个职责模型：查询理解、健康与菜单规划、菜单决策、回答、统一审查；不增加管理模型。
- 工作流负责角色权限、必需工具校验、State更新和节点流转；模型只能在自己的工具白名单内判断和调用。
- 模型不能直接修改WorkflowState、扩大数据范围、增加工具权限或把自己的输出标记为最终通过。
- 模型间通过SharedWorkflowContext、类型化Artifact和HandoffMessage通信，不共享隐藏思维过程。
- 不设置Agent回答兜底、固定模板降级、自动模型切换或SDK技术重试；必需工具漏调和关键校验失败必须暴露并停止。
- 允许的业务闭环有固定上限：召回扩展最多一次，健康失败重新规划最多一次，统一审查定向修订和复审各最多一次。
- 核心健康约束、当前消息、当前菜单、待澄清事项和版本信息不得在上下文压缩中丢失。
- 一次请求全程固定数据版本标识（离线构建时确定，不在请求期变更），MySQL、Qdrant、规则和缓存使用同一发布身份；运行期只做轻量版本一致性校验。
- 最终回答只能引用通过校验的菜单事实，不增加菜品、不替换食材、不输出医疗结论或其他参与者的具体健康信息。
- LangSmith、自建Trace详情页和完整指标平台登记为明确延期决定，不进入当前核心实施范围。
- 文档使用中文，术语、字段名、Artifact名、错误码和代码标识保留英文。
- 每个提交只包含一个可独立审查和回退的文档逻辑块；提交前执行占位符、链接、格式和Git差异检查。

---

## File Map

本计划创建或修改以下文件：

| 文件 | 职责 |
|---|---|
| `docs/documentation-roadmap.md` | 记录总文档、模块文档和共享契约的编写顺序、依赖与交付状态 |
| `docs/README.md` | 提供已存在文档的入口，不链接尚未创建的文件 |
| `docs/00-system-overview.md` | V2系统总设计和全局架构入口 |
| `docs/contracts/global-invariants.md` | 维护不可被任何模块破坏的全局不变量 |
| `docs/contracts/module-boundaries.md` | 维护模块职责、数据所有权、依赖方向和公开协作边界 |
| `docs/scenarios/recommendation-lifecycle.md` | 描述正常链路、失败链路和有界修订链路 |
| `docs/decisions/README.md` | 登记已确认、延期和明确排除的架构决策 |

后续模块详设不属于本计划的文件创建范围。它们的目标文件名和顺序由`docs/documentation-roadmap.md`确定。

### Task 1: 建立文档路线图

**Files:**
- Create: `docs/documentation-roadmap.md`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes: `docs/decisions/0001-v2-workspace-and-migration.md`中的目录边界和实施顺序。
- Produces: 后续总览、契约、场景和模块详设共同使用的文档交付顺序。

- [ ] **Step 1: 创建路线图并写明四个阶段**

在`docs/documentation-roadmap.md`中写入以下明确阶段：

```text
阶段A：系统总览基线
  00-system-overview.md
  contracts/global-invariants.md
  contracts/module-boundaries.md
  scenarios/recommendation-lifecycle.md
  decisions/README.md

阶段B：数据与确定性能力详设
  modules/01-data-engineering.md
  modules/02-user-health-profile.md
  modules/03-recipe-ingredient-processing.md
  modules/04-health-rule-engine.md
  modules/05-time-and-steps.md
  modules/06-nutrition-scoring.md

阶段C：检索、规划与Agent详设
  modules/07-rag-retrieval.md
  modules/08-menu-planning.md
  modules/09-agent-workflow.md
  modules/10-memory-and-context.md

阶段D：交付与验证详设
  modules/11-api-and-sse.md
  modules/12-answer-and-frontend.md
  modules/13-testing-and-acceptance.md
  modules/14-release-migration-and-cleanup.md
```

为每个文档记录固定状态枚举：`PLANNED`、`IN_REVIEW`、`APPROVED`、`SUPERSEDED`。初始状态中ADR-0001为`APPROVED`，本计划内待创建文件为`PLANNED`；不得使用含义不清的“差不多完成”或百分比。

- [ ] **Step 2: 写明模块详设统一模板**

路线图必须规定每个模块详设依次包含：职责、非职责、上游输入、下游输出、数据所有权、核心流程、算法与规则、公开接口或工具、依赖、异常、版本、测试验收、旧实现差异、迁移分类、清理项。

- [ ] **Step 3: 更新文档入口**

修改`docs/README.md`，只增加已经创建的`documentation-roadmap.md`链接。尚未创建的总览和模块文档使用代码路径列举，不建立失效Markdown链接。

- [ ] **Step 4: 验证路线图内容**

Run:

```powershell
rg -n "阶段A|阶段B|阶段C|阶段D|PLANNED|IN_REVIEW|APPROVED|SUPERSEDED" docs/documentation-roadmap.md
rg -n "documentation-roadmap.md" docs/README.md
git diff --check
```

Expected: 第一条命令命中四个阶段和四个状态；第二条命中文档入口；`git diff --check`无错误。

- [ ] **Step 5: 提交路线图**

```powershell
git add docs/README.md docs/documentation-roadmap.md
git commit -m "docs(roadmap): define v2 documentation delivery order"
```

### Task 2: 编写系统总设计

**Files:**
- Create: `docs/00-system-overview.md`
- Modify: `docs/documentation-roadmap.md`

**Interfaces:**
- Consumes: 本计划Global Constraints、ADR-0001、旧项目设计文档和已确认讨论结论。
- Produces: 所有模块详设必须遵守的系统目标、端到端链路、模块关系和全局失败原则。

- [ ] **Step 1: 写入文档元信息和边界**

文档开头必须包含状态`IN_REVIEW`、日期`2026-08-04`、适用项目`program_v2`、替代范围“V2目标架构，不覆盖旧项目运行说明”。目标和非目标必须明确排除医疗诊断、真实世界扩展、采购量、人数份量、烹饪营养损耗和当前延期的可观测平台。

- [ ] **Step 2: 写入总体架构和依赖方向**

使用一张Mermaid图表达以下完整链路：

```text
请求与健康档案
→ 上下文构建
→ 查询理解模型
→ 混合RAG
→ 健康与菜单规划模型及工具
→ 多个可行菜单
→ 菜单决策模型及最终健康校验
→ 回答模型
→ 统一审查模型
→ 原子提交
→ SSE和前端展示
```

同时表达健康规则、营养软评分、时间步骤规划、WorkflowState和发布版本对链路的支撑关系。

- [ ] **Step 3: 写入系统分层与模块协作**

总览必须区分：领域与共享契约、数据工程、确定性业务模块、模型与工具、工作流编排、上下文记忆、基础设施、API与前端、测试验收。写明API依赖工作流、工作流依赖模型节点和领域服务、模型依赖工具接口、工具依赖领域服务、基础设施实现Repository接口；禁止反向依赖。

- [ ] **Step 4: 写入五模型职责和权限总表**

表格必须覆盖查询理解、健康与菜单规划、菜单决策、回答、统一审查五个模型，并为每个模型说明输入Artifact、输出Artifact、允许工具类别、禁止行为。明确没有管理模型，工作流负责分配权限和节点流转。

- [ ] **Step 5: 写入关键全局边界**

使用独立小节概述：健康硬筛选、多人全员交集、RAG边界、营养软排序、时间与步骤、上下文压缩、模型通信、失败与有界修订、版本发布、API/SSE、回答表达和数据隐私。每节只写全局规则并链接计划中的契约或场景路径，不展开表结构和算法细节。

- [ ] **Step 6: 写入端到端正常流程和终态**

正常流程列出每个节点的主要输入、输出和进入下一节点的条件。终态必须包含`completed`、`needs_clarification`、`no_safe_menu`、`no_feasible_menu`、`failed`、`cancelled`和`interrupted`，并解释`no_safe_menu`与`no_feasible_menu`的区别。

- [ ] **Step 7: 写入建设顺序和文档导航**

建设顺序固定为：总设计、共享契约、数据工程、确定性工具、State与Artifact、五模型工作流、上下文记忆、API/SSE、前端、全链路验收、旧项目清理。模块详设路径以代码格式列出，未创建前不使用Markdown链接。

- [ ] **Step 8: 更新路线图状态并验证**

将`docs/00-system-overview.md`状态更新为`IN_REVIEW`。执行：

```powershell
rg -n "查询理解模型|健康与菜单规划模型|菜单决策模型|回答模型|统一审查模型" docs/00-system-overview.md
rg -n "no_safe_menu|no_feasible_menu|REQUIRED_TOOL_NOT_CALLED|release_id" docs/00-system-overview.md
rg -n "采购量|per_serving|烹饪营养损耗|管理模型" docs/00-system-overview.md
git diff --check
```

Expected: 前两条命中规定内容；第三条只命中非目标或禁止边界，不得将这些能力描述为目标实现；格式检查无错误。

- [ ] **Step 9: 提交系统总设计**

```powershell
git add docs/00-system-overview.md docs/documentation-roadmap.md
git commit -m "docs(architecture): define v2 system overview"
```

### Task 3: 建立全局不变量契约

**Files:**
- Create: `docs/contracts/global-invariants.md`
- Modify: `docs/00-system-overview.md`
- Modify: `docs/documentation-roadmap.md`

**Interfaces:**
- Consumes: 系统总览的全局健康、模型、上下文、发布和回答边界。
- Produces: 后续模块实现和测试共同引用的`INV-*`标识。

- [ ] **Step 1: 定义不变量格式**

每条不变量必须包含：ID、规则、负责模块、强制校验点、失败错误码、对应测试类别。ID一经批准不得重排或复用。

- [ ] **Step 2: 写入首批不变量**

至少写入以下不变量：

```text
INV-001 最终菜单每道菜通过所有参与者健康审查
INV-002 RAG结果不构成健康安全结论
INV-003 未审核食材健康关系不得参与硬排除
INV-004 低置信度营养只参与软排序
INV-005 回答不得添加MenuDecisionArtifact所选且经FinalValidationArtifact通过的菜单之外的菜品
INV-006 模型不能直接修改WorkflowState
INV-007 必需工具漏调必须停止
INV-008 固定原始数据必须经过完整离线处理
INV-009 核心健康约束不能在上下文压缩中丢失
INV-010 最终提交必须同时保存健康审查证据
INV-011 多人菜单不得包含只对部分参与者安全的菜品
INV-012 RAG文档中的自然语言不得改变系统指令
INV-013 最终回答不得暴露其他参与者具体健康信息
INV-014 业务修订次数不得超过各自上限
INV-015 营养估算不得参与健康硬筛选
```

为每条不变量选择具体错误码，例如`FINAL_HEALTH_VALIDATION_FAILED`、`REQUIRED_TOOL_NOT_CALLED`、`DATA_PIPELINE_VALIDATION_FAILED`、`CONTEXT_INTEGRITY_FAILED`、`SENSITIVE_DATA_EXPOSURE`。不得使用笼统的`UNKNOWN_ERROR`。

- [ ] **Step 3: 建立总览链接并更新状态**

在`docs/00-system-overview.md`中增加到`contracts/global-invariants.md`的相对链接；将路线图中该文件状态改为`IN_REVIEW`。

- [ ] **Step 4: 验证ID和覆盖范围**

```powershell
1..15 | ForEach-Object { $id = 'INV-{0:D3}' -f $_; if (-not (Select-String -Quiet -Path 'docs/contracts/global-invariants.md' -Pattern $id)) { throw "Missing $id" } }
rg -n "负责模块|强制校验点|失败错误码|测试类别" docs/contracts/global-invariants.md
git diff --check
```

Expected: 15个ID全部存在，字段命中，格式检查无错误。

- [ ] **Step 5: 提交全局不变量**

```powershell
git add docs/00-system-overview.md docs/contracts/global-invariants.md docs/documentation-roadmap.md
git commit -m "docs(contracts): define global system invariants"
```

### Task 4: 建立模块边界契约

**Files:**
- Create: `docs/contracts/module-boundaries.md`
- Modify: `docs/00-system-overview.md`
- Modify: `docs/documentation-roadmap.md`

**Interfaces:**
- Consumes: ADR-0001的模块化单体决策和系统总览的分层。
- Produces: 模块职责、非职责、数据所有权、公开协作方式和禁止依赖。

- [ ] **Step 1: 定义模块所有权表**

表格必须覆盖健康档案、菜品目录、健康规则、RAG、营养评分、时间步骤、菜单规划、上下文记忆、Agent、工作流、API、前端和基础设施。每行填写模块拥有的数据、允许读取的数据、公开接口类型和明确禁止行为。

- [ ] **Step 2: 定义单向依赖规则**

写入以下强制方向：

```text
API/前端适配 → Application/Workflow
Workflow → Agent节点与领域服务接口
Agent节点 → Tool接口
Tool接口 → 领域服务
领域服务 → Repository接口
Infrastructure → Repository接口实现
```

明确禁止数据管道依赖Agent、RAG依赖健康规划模型、Repository依赖API Schema、优化器导入RAG内部词法函数、模型节点直接访问数据库和前端重建业务结论。

- [ ] **Step 3: 定义跨模块写入规则**

规定模块只能写自己拥有的数据；跨模块事务由Application层协调；最终结果和强制健康审计在同一提交边界完成；任何模型不得直接写业务表或State。

- [ ] **Step 4: 建立总览链接并验证**

在系统总览中增加相对链接，并将路线图状态改为`IN_REVIEW`。执行：

```powershell
rg -n "健康档案|菜品目录|健康规则|RAG|营养评分|时间步骤|菜单规划|上下文记忆|Agent|工作流|API|前端|基础设施" docs/contracts/module-boundaries.md
rg -n "禁止|只能写|公开接口|数据所有权" docs/contracts/module-boundaries.md
git diff --check
```

Expected: 所有模块和边界词均命中，格式检查无错误。

- [ ] **Step 5: 提交模块边界**

```powershell
git add docs/00-system-overview.md docs/contracts/module-boundaries.md docs/documentation-roadmap.md
git commit -m "docs(contracts): define module ownership and dependencies"
```

### Task 5: 编写推荐生命周期场景

**Files:**
- Create: `docs/scenarios/recommendation-lifecycle.md`
- Modify: `docs/00-system-overview.md`
- Modify: `docs/documentation-roadmap.md`

**Interfaces:**
- Consumes: 系统总览、全局不变量和模块边界。
- Produces: 后续WorkflowState、Artifact、API/SSE和端到端测试详设使用的场景基线。

- [ ] **Step 1: 编写正常链路时序图**

使用Mermaid sequence diagram展示客户端、API、Workflow、五个模型、RAG、健康工具、菜单规划、MySQL/Redis和统一审查之间的调用。RAG必须发生在健康审查之前，最终答案只能在最终健康校验和统一审查均通过后提交。

- [ ] **Step 2: 编写健康冲突和无菜单场景**

分别描述：候选菜命中健康硬约束后排除、全部候选均不安全进入`no_safe_menu`、存在安全菜但无法满足菜数或严格时间进入`no_feasible_menu`。三个场景不得混用终态。

- [ ] **Step 3: 编写有界修订场景**

明确：召回扩展最多一次且必须产生新候选批次；最终健康校验失败后重新规划最多一次；统一审查只允许一次定向修订和一次复审；再次失败进入`failed`。所有计数来自WorkflowState，不由模型自行声明。

- [ ] **Step 4: 编写工具、上下文和取消失败场景**

覆盖`REQUIRED_TOOL_NOT_CALLED`、`TOOL_PERMISSION_DENIED`、`CONTEXT_INTEGRITY_FAILED`、`DATA_PIPELINE_VALIDATION_FAILED`、用户显式取消、SSE断开和重复`request_id`。SSE断开不得取消或重跑请求，相同幂等键不同载荷必须失败。

- [ ] **Step 5: 建立总览链接并验证**

更新总览和路线图后执行：

```powershell
rg -n "no_safe_menu|no_feasible_menu|REQUIRED_TOOL_NOT_CALLED|TOOL_PERMISSION_DENIED|CONTEXT_INTEGRITY_FAILED|DATA_PIPELINE_VALIDATION_FAILED" docs/scenarios/recommendation-lifecycle.md
rg -n "最多一次|一次定向修订|一次复审|SSE断开|幂等" docs/scenarios/recommendation-lifecycle.md
git diff --check
```

Expected: 规定终态、错误码和上限全部命中，格式检查无错误。

- [ ] **Step 6: 提交生命周期场景**

```powershell
git add docs/00-system-overview.md docs/scenarios/recommendation-lifecycle.md docs/documentation-roadmap.md
git commit -m "docs(scenarios): define recommendation lifecycle"
```

### Task 6: 建立架构决策登记

**Files:**
- Create: `docs/decisions/README.md`
- Modify: `docs/README.md`
- Modify: `docs/documentation-roadmap.md`

**Interfaces:**
- Consumes: ADR-0001、系统总览和本计划Global Constraints。
- Produces: 已确认、明确延期和明确排除的决策索引。

- [ ] **Step 1: 建立三类决策表**

`docs/decisions/README.md`必须区分：

```text
CONFIRMED：已经进入系统目标架构的决策
DEFERRED：明确不阻塞当前实现、在指定阶段重新评估的决策
REJECTED：已经讨论并明确不采用的路径
```

- [ ] **Step 2: 登记确认决策**

至少登记：并列V2工作区、模块化单体、多人全员健康交集、五模型职责、无管理模型、RAG不负责健康、营养仅软排序、无Agent兜底、无技术自动重试、模型通过受控共享上下文通信、运行期保留轻量版本校验。

- [ ] **Step 3: 登记延期和排除决策**

`DEFERRED`登记LangSmith、自建Trace详情页和完整指标平台，重新评估时点为“核心工作流、数据规则和模型职责稳定并通过基础验收后”。`REJECTED`登记原地清空旧项目、在`program/next`嵌套新工程、当前拆微服务、用管理模型分配权限、RAG直接判定健康安全和用回答模板继续失败流程。

- [ ] **Step 4: 更新入口和路线图并验证**

```powershell
rg -n "CONFIRMED|DEFERRED|REJECTED|LangSmith|管理模型|微服务" docs/decisions/README.md
rg -n "decisions/README.md" docs/README.md
git diff --check
```

Expected: 三类状态和关键决策全部命中，文档入口存在，格式检查无错误。

- [ ] **Step 5: 提交决策登记**

```powershell
git add docs/README.md docs/decisions/README.md docs/documentation-roadmap.md
git commit -m "docs(decisions): register confirmed and deferred choices"
```

### Task 7: 完成跨文档一致性审查并提交修订

**Files:**
- Modify: `docs/00-system-overview.md`
- Modify: `docs/documentation-roadmap.md`
- Modify: `docs/contracts/global-invariants.md`
- Modify: `docs/contracts/module-boundaries.md`
- Modify: `docs/scenarios/recommendation-lifecycle.md`
- Modify: `docs/decisions/README.md`

**Interfaces:**
- Consumes: Task 1至Task 6的全部文档。
- Produces: 可以提交用户审查的系统总文档基线。

- [ ] **Step 1: 扫描禁止占位内容**

```powershell
$hits = rg -n "TBD|TODO|待补充|以后再说|视情况而定|UNKNOWN_ERROR" docs/00-system-overview.md docs/documentation-roadmap.md docs/contracts docs/scenarios docs/decisions
if ($LASTEXITCODE -eq 0) { $hits; throw 'Placeholder or vague requirement found' }
```

Expected: 无命中。延期事项必须具有明确状态、范围和重新评估时点，不使用占位语句。

- [ ] **Step 2: 检查核心术语一致性**

逐项确认所有文档统一使用：`WorkflowState`、`SharedWorkflowContext`、`HandoffMessage`、`QueryPlanArtifact`、`HealthEvaluationArtifact`、`FeasibleMenuArtifact`、`MenuDecisionArtifact`、`FinalValidationArtifact`、`AnswerArtifact`、`ReviewArtifact`、`constraint_code`、`plan_id`。发现拼写或职责冲突时直接修改对应文档。

- [ ] **Step 3: 检查设计矛盾**

确认不存在以下表述：

```text
RAG负责健康审查
只要至少一人可食用即可进入共享菜单
低置信度营养参与硬筛选
模型直接修改State
管理模型分配权限
回答模型重新生成或改写MenuDecisionArtifact所选菜单
缺少工具时继续生成固定回答
无限重试或无限审查
烹饪损耗用于当前营养计算
当前系统计算采购量或人数份量
```

- [ ] **Step 4: 检查相对链接**

运行以下PowerShell脚本检查本计划范围内Markdown相对链接是否存在：

```powershell
$files = Get-ChildItem docs -Recurse -Filter *.md
foreach ($file in $files) {
    $content = Get-Content -Raw -Encoding utf8 $file.FullName
    [regex]::Matches($content, '\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)') | ForEach-Object {
        $target = $_.Groups[1].Value
        if ($target -notmatch '^(https?:|[A-Za-z]:)') {
            $resolved = Join-Path $file.DirectoryName $target
            if (-not (Test-Path $resolved)) { throw "Broken link in $($file.FullName): $target" }
        }
    }
}
```

Expected: 脚本正常结束，无断链。

- [ ] **Step 5: 检查路线图状态和Git差异**

将本计划内六份交付文档保持为`IN_REVIEW`，等待用户审查后再改为`APPROVED`。执行：

```powershell
git diff --check
git status --short
```

Expected: 无格式错误；状态只包含本任务的文档修订。

- [ ] **Step 6: 提交一致性修订**

如果自审产生修改：

```powershell
git add docs
git commit -m "docs(architecture): align v2 documentation baseline"
```

如果没有修改，不创建空提交。

- [ ] **Step 7: 交付用户审查**

向用户提供以下可点击文件：

```text
docs/00-system-overview.md
docs/documentation-roadmap.md
docs/contracts/global-invariants.md
docs/contracts/module-boundaries.md
docs/scenarios/recommendation-lifecycle.md
docs/decisions/README.md
```

用户确认后，将对应路线图状态从`IN_REVIEW`改为`APPROVED`并单独提交：

```powershell
git add docs/documentation-roadmap.md docs/00-system-overview.md
git commit -m "docs(architecture): approve v2 system baseline"
```

系统总设计获批后，再为阶段B至阶段D的每个模块或紧密相关模块组分别编写实施计划；不得直接用本计划开始业务代码迁移。

