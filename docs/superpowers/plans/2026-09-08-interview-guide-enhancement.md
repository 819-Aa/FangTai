# Interview Guide Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有方太健康膳食推荐 Agent 面试题库增强为兼顾快速介绍、实现深挖、异常防守和事实证据的可直接备考文档。

**Architecture:** 只修改 `docs/interview-guide.md`，保留现有 30 道主体问题，前置分层介绍和端到端案例，在关键问题内补充可验证的实现细节，后置异常场景及证据速查表。所有表述以当前 README、配置和代码为边界，不增加通用技术八股或未经验证的性能结论。

**Tech Stack:** Markdown、Git、PowerShell、ripgrep

## Global Constraints

- `fast_path` 必须描述为“千问完成一次语义理解，后续由 Python 确定性执行”，不能描述为纯规则模式。
- Qdrant 只负责向量召回与 Payload 过滤，健康裁决由规则引擎完成。
- OR-Tools 只用于烹饪任务调度和时间可行性计算，不能描述为主菜单选择器。
- Redis 保存运行时短期状态，MySQL 保存已提交的持久事实。
- 项目只能描述为具有可靠性和一致性设计的完整原型，不能描述为生产级高可用或医疗诊断系统。
- 不加入未经实际测量的准确率、延迟收益、吞吐量或业务增长数据。
- 不运行后端、前端或 Docker 测试；本次只做 Markdown 内容与格式检查。

---

### Task 1: 增加分层介绍与端到端案例

**Files:**
- Modify: `docs/interview-guide.md`

**Interfaces:**
- Consumes: `docs/superpowers/specs/2026-09-08-interview-guide-enhancement-design.md` 中确认的文档结构
- Produces: 位于题库正文前的“如何使用”“30 秒介绍”“90 秒介绍”和“请求执行案例”四个部分

- [ ] **Step 1: 调整文档开头的使用说明**

在主标题后说明三种阅读方式：面试前只看统一口径和优先题；一面使用 30 秒介绍；技术面使用 90 秒介绍并准备端到端案例。保留“模型处理语义不确定性、程序控制业务确定性”的统一主线。

- [ ] **Step 2: 写入 30 秒项目介绍**

介绍中必须包含：多人健康膳食场景、千问语义理解、混合检索、双重健康校验、多轮菜单调整；控制为一个自然段，不展开技术参数。

- [ ] **Step 3: 将现有长介绍整理为 90 秒版本**

介绍顺序固定为：业务问题 → 离线食材与健康关系建模 → 在线工作流 → Redis/MySQL 多轮状态 → 模型与规则边界。避免重复列举技术栈。

- [ ] **Step 4: 增加“不要花生并换掉第二道菜”的端到端案例**

使用八步编号流程，依次写明读取当前菜单、识别操作、生成 QueryPlan、合并约束、检索替代候选、健康审核、整单终检、版本化原子提交。案例中明确只替换目标位置，且不能绕过最终健康校验。

- [ ] **Step 5: 检查新增内容的标题和流程完整性**

Run:

```powershell
rg -n "30 秒|90 秒|不要花生|QueryPlan|最终健康校验|版本化提交" docs/interview-guide.md
```

Expected: 六个关键词均能命中新增的前置章节，且案例步骤顺序完整。

- [ ] **Step 6: 提交 Task 1**

```powershell
git add -- docs/interview-guide.md
git commit -m "docs: add layered interview introductions"
```

### Task 2: 补强实现深挖与架构取舍

**Files:**
- Modify: `docs/interview-guide.md`

**Interfaces:**
- Consumes: Task 1 保留的 30 道问题编号与栏目结构
- Produces: 带实际执行边界和防守回答的语义、检索、健康、上下文、一致性问题

- [ ] **Step 1: 补强 QueryPlan 与模型异常回答**

写明 Pydantic 使用 `extra="forbid"`，枚举值受控，模型结果必须能从当前消息或上一轮计划找到依据；语义重写单次调用默认超时为 3 秒，超时、非法 JSON 或校验失败时使用确定性解析结果兜底。

- [ ] **Step 2: 补强混合检索回答**

写明 BM25 和向量检索各取前 100 个候选，再通过 RRF 融合和 BGE Reranker 精排；Qdrant Payload 包含食材、标签和 `build_id` 等字段。说明这些是实现参数，不宣称它们是经过线上实验得到的最优值。

- [ ] **Step 3: 补强 Artifact、Context Manifest 与提交校验回答**

写明 Artifact 绑定 `request_id`、输入指纹、内容哈希和证据引用；Context Manifest 校验不可变数据块哈希；提交阶段继续核对参与者、菜单哈希、健康结论和 fencing token，说明 Manifest 解决的是身份与一致性问题，而不是简单记录日志。

- [ ] **Step 4: 补强 Outbox 与故障恢复回答**

写明 Outbox 使用稳定事件 ID、领取令牌和 30 秒租约；发布失败时释放领取状态并停止后续同请求事件，启动后重新扫描未完成事件。用“至少一次发布配合消费幂等”描述效果，不使用严格 exactly-once 表述。

- [ ] **Step 5: 增加三道架构反质疑问题**

在现有题库末尾增加以下问题及完整回答：

1. “只有约两千道菜，为什么还需要向量数据库？”回答规模不是唯一理由，重点是语义检索、Payload 过滤和构建版本隔离，同时承认当前规模用其他方案也可实现。
2. “Artifact、Manifest 和 Outbox 是否过度设计？”回答它们分别约束节点结果、数据版本和跨存储提交，复杂度集中在高风险边界；对于普通菜谱 Demo 确实不需要，但健康约束和多轮修改需要可追踪性。
3. “如果去掉大模型还能运行吗？”回答确定性回退可处理明确表达，但对口语、省略和上下文指代的理解能力会下降，因此模型是语义增强层而非安全依赖。

- [ ] **Step 6: 检查关键实现口径**

Run:

```powershell
rg -n "extra=.forbid|3 秒|前 100|30 秒租约|至少一次|约两千道菜|过度设计|语义增强层" docs/interview-guide.md
```

Expected: 每个实现事实和三道反质疑问题均至少命中一次。

- [ ] **Step 7: 提交 Task 2**

```powershell
git add -- docs/interview-guide.md
git commit -m "docs: deepen interview implementation answers"
```

### Task 3: 增加异常、证据和最终复核

**Files:**
- Modify: `docs/interview-guide.md`

**Interfaces:**
- Consumes: Task 1 和 Task 2 完成后的完整题库
- Produces: 异常场景速查表、数据证据分级、最终统一口径和通过格式检查的 Markdown 文档

- [ ] **Step 1: 增加异常场景速查表**

表格列为“场景、当前处理、保护目标、没有做到的部分”，覆盖模型超时或输出非法、Qdrant 不可用、无安全候选、重复请求、锁过期后旧任务继续执行、事务提交后事件发布失败、MySQL/Qdrant 版本不一致、用户取消请求八种情况。

- [ ] **Step 2: 增加项目规模与验证证据**

写入固定数据基线：2,000 条原始菜谱、1,932 条可推荐菜品、1,770 个标准食材、17,493 条菜谱—食材关系、65,588 条健康约束—食材矩阵、1,932 个 Qdrant 向量点。测试部分只描述验收记录及覆盖范围，不声称本次重新执行了测试。

- [ ] **Step 3: 增加证据使用分级**

使用三组清单：

- 可主动说：数据建模规模、工作流和核心边界；
- 被问再说：具体超时、召回数量、Outbox 租约和测试数量；
- 不要说：未经测量的准确率、QPS、延迟提升和商业收益。

- [ ] **Step 4: 合并并校正统一口径**

保留现有统一口径，补充“当前实现没有 SDK 自动重试，失败由工作流按安全边界处理”和“Qdrant 故障时不提交新菜单，而不是绕过检索编造结果”。删除与新增前置章节重复的大段表述。

- [ ] **Step 5: 执行 Markdown 与事实口径检查**

Run:

```powershell
git diff --check -- docs/interview-guide.md
rg -n "2000|2,000|1,932|1,770|17,493|65,588|QPS|准确率|生产级高可用|医疗诊断" docs/interview-guide.md
```

Expected: `git diff --check` 无输出；六项数据规模均存在；“QPS”和“准确率”只出现在禁止夸大或无数据不应声称的语境；“生产级高可用”和“医疗诊断”只出现在项目边界说明中。

- [ ] **Step 6: 确认只修改计划内文档**

Run:

```powershell
git status --short
```

Expected: 除已知的设计和计划文档外，不出现源代码、配置、数据或运行时文件变更。

- [ ] **Step 7: 提交 Task 3**

```powershell
git add -- docs/interview-guide.md
git commit -m "docs: complete interview defense guide"
```
