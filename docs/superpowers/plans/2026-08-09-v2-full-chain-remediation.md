# Program V2 Full-Chain Remediation Implementation Plan

> **状态：`APPROVED_FOR_EXECUTION`（2026-08-09）**
>
> **For DeepSeek V4 Flash:** REQUIRED: execute this plan task by task under [`deepseek-execution-contract.md`](../../contracts/deepseek-execution-contract.md). Do not batch stages, alter locked tests, approve health relations, or advance your own gate.
>
> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` in a separate execution session. At each task boundary, use fresh context for read-only review. The execution controller, not the implementation model, unlocks the next task.

**Goal:** 从固定 2,000 条菜品源开始，重建唯一可信的数据事实和健康关系，修复 B2—D2 的确定性边界、五模型工作流、事务提交、SSE 与匿名前端，并用真实 MySQL/Qdrant/Redis 和前端证明成功/失败全链路均符合 INV-001..INV-024。

**Architecture:** 保持 V2 模块化单体。B1 独占原始文本解析，B3/B4/B5/B6/C1 只消费带 `build_id/source_manifest_hash` 的结构化 Artifact；在线只经 Repository 访问已初始化存储。模型只产生受 Schema 约束的 Artifact 和主动工具调用，确定性服务执行健康、时间、规划、状态转换与提交。Application 使用 MySQL transactional outbox，使最终结果、强制健康审计、会话事实和成功事件拥有同一提交事实。

**Tech Stack:** Python 3.11、Pydantic 2、pytest、Ruff、FastAPI、MySQL 8、Qdrant、Redis 7、LangGraph/LangChain、Vue 3、TypeScript、Pinia、Vitest、Vue Test Utils、Vite。

---

## 0. 执行总则

### 0.1 权威输入

- 系统总览：`docs/00-system-overview.md`
- 不变量：`docs/contracts/global-invariants.md`
- 数据契约：`docs/contracts/data-artifact-contracts.md`
- 执行契约：`docs/contracts/deepseek-execution-contract.md`
- 模块详设：`docs/modules/01-data-engineering.md` 至 `14-migration-and-cleanup.md`
- ADR：0004、0005、0006
- 审查问题：`reports/2026-08-09-v2-code-review.md` 的 R-001..R-020

历史 `reports/FINAL_REVIEW.md` 和 ADR-0003 均为 `SUPERSEDED`，不得作为实现理由。

### 0.2 固定数据常量

```python
SOURCE_ID = "fixed-recipes-2000"
SOURCE_ENCODING = "GBK"
SOURCE_BYTE_SIZE = 1_138_083
SOURCE_SHA256 = "B2177DC6CDCAE24FC5671C8DADA44295228F4301E3CE620ED11D51B1ABFE4371"
SOURCE_ROW_COUNT = 2_000
SOURCE_HEADERS = ("名称", "食材清单", "烹饪步骤", "label")
```

任何不匹配都停止，不允许“兼容新文件”。

### 0.3 每任务固定流程

1. 读取 `TaskEnvelope` 并核对 `spec_hash/base_commit/allowed_paths`；
2. 只添加任务列出的测试，运行并保存预期红灯；
3. 写最小实现；
4. 运行任务局部命令；
5. 运行任务列出的受影响回归；
6. 生成证据包和 diff 自审；
7. 新上下文只读复核，通过后由外部控制者解锁下一任务；
8. Git 可用时，每任务单独提交。不得把多个任务压成一次大提交。

所有命令默认工作目录为 `program_v2/`。Windows PowerShell 命令必须保持同一 shell；禁止重建 Git 或执行宽范围删除。

---

## Stage S0：合法基线与执行护栏

### Task T00：修复前置 Git 闸门

**Files:** 本任务不授权修改项目文件。

**Step 1: 核验 Git 元数据**

Run:

```powershell
git rev-parse --show-toplevel
git rev-parse --verify HEAD
git rev-parse "v2-remediation-baseline-2026-08-09^{commit}"
git branch --show-current
git status --short
```

Expected: 命令均成功，顶层目录严格等于 `program_v2`，`HEAD` 与基线标签指向同一 commit，分支为 `remediation/deepseek-v4-flash`，工作区无修改。外层 `竞赛/.git` 不属于 V2 仓库，不作为本计划的 Git 根。

**Step 2: 停机规则**

若在 `program_v2/` 工作目录执行时任一命令失败，输出 `BLOCKED_GIT_METADATA` 与原始退出码，停止整个计划。禁止运行 `git init`、复制别处 `.git`、删除任一 `.git` 或制造空提交。由项目所有者恢复正确元数据后，从 T00 重来。

**Acceptance:** 基线标签对应的合法 `base_commit` 被写入所有后续 TaskEnvelope；交接前全部已接受文件已进入该基线，工作区为 clean，后续每项 diff 均可归属。

### Task T01：建立不可变任务与证据护栏

**Files:**

- Create: `execution/README.md`
- Create: `execution/task-envelope.schema.json`
- Create: `execution/tasks/T01.yaml`（后续每任务各一份，由控制者写入）
- Create: `scripts/execution/verify_task_envelope.py`
- Create: `scripts/execution/collect_evidence.py`
- Create: `scripts/execution/check_changed_paths.py`
- Test: `tests/execution/test_execution_guards.py`

**Step 1: 写红测试**

在 `tests/execution/test_execution_guards.py` 实现四个独立用例：越界文件被拒绝、`spec_hash` 不匹配被拒绝、缺退出码不能通过、锁定测试被修改时拒绝。每个用例必须断言具体错误码和进程非零退出码，不能只断言 Python 抛出任意异常。

Run: `uv run pytest tests/execution/test_execution_guards.py -q`
Expected: FAIL，因为护栏脚本尚不存在。

**Step 2: 实现最小接口**

```python
def verify_envelope(path: Path, *, expected_spec_hash: str, head_sha: str) -> None: ...
def check_changed_paths(changed: Sequence[Path], allowed: Sequence[str], forbidden: Sequence[str]) -> None: ...
def record_command(command: list[str], cwd: Path, output_dir: Path) -> CommandEvidence: ...
```

`CommandEvidence` 至少含命令、cwd、UTC 时间、退出码、stdout/stderr 路径与 SHA-256。脚本只收集证据，不替模型修改测试结果。

**Step 3: 验证**

Run:

```powershell
uv run pytest tests/execution/test_execution_guards.py -q
uv run ruff check scripts/execution tests/execution
```

Expected: PASS；越界路径和缺证据的负例必须失败。

**Commit:** `chore: add immutable remediation execution guards`

**Gate S0:** T00/T01 通过；所有后续 `allowed_paths`、锁定测试和证据目录由外部控制者生成，DeepSeek 无写权限。

---

## Stage S1：先锁契约，再动实现

### Task T02：建立固定源和构建清单 Schema

**Files:**

- Create: `src/food_agent_v2/contracts/__init__.py`
- Create: `src/food_agent_v2/contracts/build.py`
- Modify: `src/food_agent_v2/b1/schemas.py`
- Test: `tests/contracts/test_build_manifest.py`

**Required interfaces:**

```python
class SourceManifest(BaseModel):
    schema_version: Literal["1.0.0"]
    source_id: Literal["fixed-recipes-2000"]
    relative_path: Literal["data/raw/recipes_sample_2000.csv"]
    encoding: Literal["GBK"]
    byte_size: Literal[1138083]
    sha256: Literal["B2177DC6CDCAE24FC5671C8DADA44295228F4301E3CE620ED11D51B1ABFE4371"]
    row_count: Literal[2000]
    headers: tuple[str, str, str, str]

class ArtifactEntry(BaseModel):
    relative_path: str
    row_count: int
    sha256: str

class BuildManifest(BaseModel):
    build_id: UUID
    source_manifest_hash: str
    builder_version: str
    schema_versions: dict[str, str]
    artifacts: dict[str, ArtifactEntry]
    quality_gate_report: ArtifactEntry
```

**Red tests:** wrong encoding/hash/header/row count; absolute path; `..` path; missing Git SHA; mixed source hash.

Run red then green:

```powershell
uv run pytest tests/contracts/test_build_manifest.py -q
uv run ruff check src/food_agent_v2/contracts src/food_agent_v2/b1/schemas.py tests/contracts/test_build_manifest.py
```

**Acceptance:** canonical JSON hashing has golden vectors; a one-byte source change fails `SOURCE_MANIFEST_MISMATCH` before CSV parsing.

**Commit:** `feat: define fixed source and build manifest contracts`

### Task T03：锁定在线 Artifact、ToolReceipt 与终态 Schema

**Files:**

- Create: `src/food_agent_v2/contracts/artifacts.py`
- Create: `src/food_agent_v2/contracts/receipts.py`
- Create: `src/food_agent_v2/contracts/status.py`
- Test: `tests/contracts/test_artifact_schemas.py`
- Test: `tests/contracts/test_receipt_binding.py`

**Required contracts:**

```python
StrictTimeFeasible = Literal[True, False, "unknown"]
RequestStatus = Literal[
    "accepted", "running", "revising", "completed", "needs_clarification",
    "no_safe_menu", "no_feasible_menu", "strict_time_indeterminate",
    "failed", "cancelled", "interrupted",
]

class ToolReceipt(BaseModel):
    request_id: UUID
    node_id: str
    tool_call_id: str
    tool_name: str
    input_hash: str
    output_hash: str
    build_id: UUID
    success: bool
    error_code: str | None
```

为 `QueryPlanArtifact`、`HealthEvaluationArtifact`、`FeasibleMenuArtifact`、`MenuDecisionArtifact`、`FinalValidationArtifact`、`AnswerArtifact`、`ReviewArtifact` 定义严格 Schema，禁止 extra fields。严格时间、软维度 availability、证据引用和所有 hash 均为显式字段。

**Red tests:** receipt 复用于不同 request/node/input；未知 verdict；答案菜单散列与最终校验不一致；`unknown` 被解析成 truthy；额外字段被接受。

Run:

```powershell
uv run pytest tests/contracts/test_artifact_schemas.py tests/contracts/test_receipt_binding.py -q
uv run ruff check src/food_agent_v2/contracts tests/contracts
```

**Commit:** `feat: lock workflow artifact and receipt schemas`

### Task T04：建立不变量追踪和在线/离线静态边界

**Files:**

- Create: `tests/contracts/invariant_traceability.yaml`
- Create: `tests/architecture/test_online_offline_boundary.py`
- Create: `tests/architecture/test_module_imports.py`
- Create: `scripts/verify_invariant_traceability.py`

**Red tests:** 在线模块导入 `b1.recipe_cleaning`、`b1.ingredient_identity` 或引用 `data/raw|data/processed|*.jsonl`；追踪矩阵漏掉 INV-019..024；不变量测试指向不存在 node ID。

**Acceptance:** INV-001..024 每条至少关联一个确定性校验点和测试 ID；架构扫描忽略测试夹具但不忽略运行代码。

Run:

```powershell
uv run pytest tests/architecture -q
uv run python scripts/verify_invariant_traceability.py
```

**Commit:** `test: enforce invariants and online offline boundary`

**Gate S1:** 所有契约测试通过；Schema 与锁定测试进入只读集合；后续任务不得改变语义，只能按新 ADR 单独申请。

---

## Stage S2：固定 2,000 条数据的一次性重建

### Task T05：源守恒、逐行解析与 2,000 条显式分类

**Files:**

- Modify: `src/food_agent_v2/b1/recipe_cleaning.py`
- Modify: `src/food_agent_v2/b1/schemas.py`
- Create: `src/food_agent_v2/b1/source_manifest.py`
- Create: `src/food_agent_v2/b1/recipe_classifier.py`
- Create: `data/review/recipe_classification_overrides.csv`
- Test: `tests/b1/test_fixed_recipe_source.py`
- Test: `tests/b1/test_recipe_classification.py`

**Implementation order:**

1. 校验 byte size/SHA-256/GBK/header/2,000 行；
2. `recipe_id = row_index`，禁止名称排序和变体重编号；
3. 解析所有行但不在 B1 丢弃；
4. 确定性分类器产生候选和 rule evidence；
5. 争议项只从人工 override 读取；
6. 发布门禁要求 2,000 个唯一分类、`pending=0`。

```python
def load_verified_recipe_source(path: Path, manifest: SourceManifest) -> list[SourceRecipeRow]: ...
def classify_recipe(row: SourceRecipeRow, overrides: ClassificationOverrides) -> RecipeClassification: ...
```

**Tests:** ID 连续；全部 2,000 守恒；封闭类型；旧 V1 候选分布只生成 diff 不强制相等；任何未决阻断。

Run:

```powershell
uv run pytest tests/b1/test_fixed_recipe_source.py tests/b1/test_recipe_classification.py -q
uv run food-agent-v2 data-rebuild --stage source-and-classification --staging-dir .staging/T05
```

Expected artifact: 分类差异报告与 2,000 行输出；不得初始化数据库。

**Human gate H01:** 仅审查争议分类；签名 override 后才能通过 T05。DeepSeek 不得填审核人。

**Commit:** `feat: preserve and classify all fixed recipe rows`

### Task T06：重建食材身份、出现和 crosswalk

**Files:**

- Modify: `src/food_agent_v2/b1/ingredient_identity.py`
- Move/refactor parser logic from: `src/food_agent_v2/b3/ingredient_parser.py`
- Create: `src/food_agent_v2/b1/ingredient_parser.py`
- Create: `src/food_agent_v2/b1/ingredient_crosswalk.py`
- Create: `data/review/ingredient_identity_overrides.csv`
- Test: `tests/b1/test_ingredient_parser_golden.py`
- Test: `tests/b1/test_ingredient_identity_rebuild.py`

**Required output:** `ingredient_occurrences`、`ingredient_registry`、`ingredient_aliases`、`ingredient_crosswalk`、`recipe_ingredient_relations`。

```python
class CrosswalkDecision(BaseModel):
    source_key: str
    operation: Literal["merge", "split", "discard"]
    target_ingredient_ids: tuple[int, ...]
    reason_code: str
    review_status: Literal["pending", "approved", "rejected"]
```

**Golden cases:** 组合项、可选项、替代项、数量单位、切法/泡发/去皮等处理语、调料组、非食材说明，以及旧系统已暴露的“海鲜菇/红酒醋/素蚝油”名称边界。

**Hard gates:** 推荐出现零未解析；同一 occurrence 恰好一个目标（组合拆分后每个子 occurrence 恰好一个）；别名精确唯一；类别/食材族不为空的覆盖阈值由审核清单明确；无模型自动批准。

**Human gate H02:** 审核所有 pending merge/split/discard；批准后冻结注册表。当前 3,326 身份只作输入差异，不作为目标数量。

Run:

```powershell
uv run pytest tests/b1/test_ingredient_parser_golden.py tests/b1/test_ingredient_identity_rebuild.py -q
uv run food-agent-v2 data-rebuild --stage ingredients --staging-dir .staging/T06
```

**Commit:** `feat: rebuild and freeze canonical ingredient identities`

### Task T07：从统一事实生成 B3/B5/B6/C1 离线视图

**Files:**

- Modify: `src/food_agent_v2/b1/step_time_builder.py`
- Modify: `src/food_agent_v2/b1/nutrition_feature_builder.py`
- Modify: `src/food_agent_v2/b1/rag_document_builder.py`
- Create: `src/food_agent_v2/b1/consumer_views.py`
- Test: `tests/b1/test_consumer_view_consistency.py`
- Test: `tests/b1/test_nutrition_availability.py`
- Test: `tests/b1/test_step_authority.py`

**Rules:**

- 三个构建器只收结构化 recipe/occurrence/identity，不接 raw strings；原始步骤的解析只在 B1 内完成一次。
- 营养使用固定参考文件的明确 `*_per_100g` 字段、来源和单位；没有可靠映射即 `unavailable`。
- `llm_estimate` 标记为低权威，不能产生严格时间 true/false。
- RAG 文档只含 `record_type=dish` 且 eligible 的菜，不能含健康结论或内部营养值。

**Red tests:** 同一 recipe 在健康/时间/营养/RAG 视图使用不同食材 ID；缺营养映射得到 0.5；model estimate 获 high；非 dish 进入索引。

Run: `uv run pytest tests/b1/test_consumer_view_consistency.py tests/b1/test_nutrition_availability.py tests/b1/test_step_authority.py -q`

**Commit:** `feat: publish consistent downstream build views`

### Task T08：生成完整健康关系候选并接收独立人工批准

**Files:**

- Rewrite: `src/food_agent_v2/b1/health_relation_builder.py`
- Create: `src/food_agent_v2/b1/health_relation_review.py`
- Create: `data/review/health_relation_decisions.csv`
- Test: `tests/b1/test_health_relation_matrix.py`
- Test: `tests/b1/test_health_relation_approval_separation.py`

**Required behavior:**

```python
expected_keys = set(product(allowed_constraint_codes, health_ingredient_ids))
assert set(approved_decisions) == expected_keys
```

候选器可提供 `suggested_decision/reason/evidence`，但导出时必须是 pending。审核导入器要求独立 reviewer 标识、时间、决定和证据；同一构建进程不能自签。

**Human gate H03 (mandatory):** 人工完成全部矩阵决定。任何 pending、重复键、未知代码、未知食材、缺证据或 reviewer 缺失，T08 失败。DeepSeek 只能汇总差异，不能代签。

Run:

```powershell
uv run pytest tests/b1/test_health_relation_matrix.py tests/b1/test_health_relation_approval_separation.py -q
uv run food-agent-v2 data-rebuild --stage health-relations --staging-dir .staging/T08
```

**Commit:** `feat: require independently approved health relation matrix`

### Task T09：全量质量门禁与 staging 原子初始化

**Files:**

- Rewrite: `src/food_agent_v2/b1/quality_gates.py`
- Rewrite: `src/food_agent_v2/b1/rebuild.py`
- Rewrite: `src/food_agent_v2/b1/database_loader.py`
- Modify: `src/food_agent_v2/c1/index_builder.py`
- Modify: `src/food_agent_v2/c1/qdrant_client.py`
- Modify: `db/schema_mysql.sql`
- Test: `tests/integration/test_staging_initialization.py`
- Test: `tests/integration/test_mysql_qdrant_parity.py`

**Required command interface:**

```text
food-agent-v2 data-rebuild --staging-dir <new-empty-dir>
food-agent-v2 data-verify --manifest <BuildManifest>
food-agent-v2 data-initialize --manifest <BuildManifest> --confirm-empty-v2
```

`data-initialize` 必须确认 V2 目标为空、验证 Git SHA 与全部门禁、在 MySQL 事务写入固定事实、构建隔离 Qdrant staging collection、核对 ID/计数/散列后才切成配置中唯一 collection。失败时回滚 MySQL 并删除本次明确命名的 staging collection；不得接触 V1。

**Integration tests:** 中途 DB 失败无部分数据；Qdrant 点位缺 1 个阻断；跨 build Artifact 阻断；二次初始化非空目标阻断；在线配置只含单 collection 名。

Run:

```powershell
docker compose up -d mysql qdrant redis
uv run pytest tests/integration/test_staging_initialization.py tests/integration/test_mysql_qdrant_parity.py -q
```

**Human gate H04:** 项目所有者确认目标是空 V2 环境后才执行实际初始化。

**Commit:** `feat: atomically initialize verified fixed data`

**Gate S2:** 2,000 行守恒、零分类未决、零身份未决、完整人工健康矩阵、B3/B5/B6/C1 同构建、MySQL/Qdrant 一致。任一不满足不得进入在线实现验收。

---

## Stage S3：确定性领域服务

### Task T10：修复 B2 封闭健康档案语义

**Files:**

- Refactor: `src/food_agent_v2/b2/__init__.py`
- Create: `src/food_agent_v2/b2/schemas.py`
- Create: `src/food_agent_v2/b2/constraint_registry.py`
- Create: `src/food_agent_v2/b2/service.py`
- Test: `tests/b2/test_fixed_profiles.py`
- Test: `tests/b2/test_constraint_registry.py`

将 50 份档案全部校验；未知映射 fail-closed；`备孕` 等特殊阶段使用显式规则；临时禁忌绑定标准 ingredient ID；对模型仅投影匿名引用。

Run: `uv run pytest tests/b2 tests/test_b2_b4_health.py -q`

**Commit:** `refactor: enforce closed health profile constraints`

### Task T11：把 B3 改为纯 Repository 和消费者视图

**Files:**

- Rewrite: `src/food_agent_v2/b3/recipe_views.py`
- Rewrite: `src/food_agent_v2/b3/identity_resolver.py`
- Remove runtime usage from: `src/food_agent_v2/b3/ingredient_parser.py`
- Create: `src/food_agent_v2/b3/repository.py`
- Test: `tests/b3/test_repository_views.py`
- Test: `tests/architecture/test_b3_never_parses_raw.py`

```python
class RecipeCatalogRepository(Protocol):
    def get_health_view(self, recipe_ids: Sequence[int], build_id: UUID) -> Sequence[RecipeHealthIngredientView]: ...
    def get_retrieval_view(self, recipe_ids: Sequence[int], build_id: UUID) -> Sequence[RecipeRetrievalView]: ...
    def get_time_view(self, recipe_ids: Sequence[int], build_id: UUID) -> Sequence[RecipeTimeView]: ...
```

Unknown ID fails; no fuzzy identity creation; all views verify build identity.

**Commit:** `refactor: serve canonical recipe views from repositories`

### Task T12：重写 B4 fail-closed 健康引擎

**Files:**

- Refactor: `src/food_agent_v2/b4/__init__.py`
- Create: `src/food_agent_v2/b4/schemas.py`
- Create: `src/food_agent_v2/b4/repository.py`
- Create: `src/food_agent_v2/b4/engine.py`
- Test: `tests/b4/test_health_engine_matrix.py`
- Test: `tests/b4/test_final_revalidation.py`

评估前验证关系全集和作用域；PASS/EXCLUDE 只在系统数据完整时产生；最终复核重新执行同一核心并绑定 menu hash。删除从关键词、类别、模型或营养分推断硬命中的路径。

Run: `uv run pytest tests/b4 tests/test_b2_b4_health.py -q`

**Commit:** `refactor: make health evaluation complete and fail closed`

### Task T13：修复 B5 三值时间与 B6 unavailable 软评分

**Files:**

- Refactor: `src/food_agent_v2/b5/__init__.py`
- Refactor: `src/food_agent_v2/b6/__init__.py`
- Test: `tests/b5/test_strict_time_semantics.py`
- Test: `tests/b6/test_soft_evidence_availability.py`

```python
class MenuScheduleResult(BaseModel):
    strict_time_feasible: bool | Literal["unknown"]
    authority: Literal["deterministic_high", "deterministic_partial", "model_estimate"]
    missing_facts: tuple[str, ...]
    schedule_hash: str
```

移除 LLM/40% 公式的严格权限。B6 输出 `available: bool` 与 reason；不可用不返回数值分。

Run: `uv run pytest tests/b5 tests/b6 -q`

**Commit:** `fix: enforce strict time and soft evidence semantics`

### Task T14：完成无降级混合 C1

**Files:**

- Refactor: `src/food_agent_v2/c1/__init__.py`
- Refactor: `src/food_agent_v2/c1/qdrant_client.py`
- Refactor: `src/food_agent_v2/c1/index_builder.py`
- Test: `tests/c1/test_full_hybrid_retrieval.py`
- Test: `tests/integration/test_real_qdrant_retrieval.py`

保证词法、向量、RRF、重排均真实执行；任一不可用明确失败。验证候选全部 eligible 且 build 匹配。内存和伪 reranker 只能通过显式 test fixture 注入。

Run:

```powershell
uv run pytest tests/c1/test_full_hybrid_retrieval.py -q
uv run pytest tests/integration/test_real_qdrant_retrieval.py -q
```

**Commit:** `refactor: require complete hybrid retrieval in production`

### Task T15：重写 C2 确定性菜单规划器

**Files:**

- Refactor: `src/food_agent_v2/c2/__init__.py`
- Create: `src/food_agent_v2/c2/schemas.py`
- Create: `src/food_agent_v2/c2/planner.py`
- Test: `tests/c2/test_menu_hard_constraints.py`
- Test: `tests/c2/test_deterministic_plans.py`

**Hard rules:** 只收有效 B4 receipt；默认/明确菜数精确执行；严格时间只收 true；不改菜；软维度 unavailable 时重归一化；相同输入 plan ID/hash 稳定；输出 3—5 个实际不同方案或准确无可行终态。

Run: `uv run pytest tests/c2 tests/test_invariants.py::TestC2 -q`

**Commit:** `refactor: generate deterministic feasible menu plans`

**Gate S3:** B2—C2 领域契约与真实 Repository 集成通过；健康和严格时间负例均 fail-closed；不得使用模型/内存 fallback 计入通过。

---

## Stage S4：工作流、上下文和提交

### Task T16：统一 C3 State、Reducer、工具策略和回执

**Files:**

- Refactor: `src/food_agent_v2/c3/__init__.py`
- Refactor: `src/food_agent_v2/c3/tool_handler.py`
- Create: `src/food_agent_v2/c3/state.py`
- Create: `src/food_agent_v2/c3/receipts.py`
- Test: `tests/c3/test_state_reducer.py`
- Test: `tests/c3/test_tool_permissions.py`

所有状态更新经纯 reducer；五角色白名单静态；工具回执验证 request/node/input/build；模型不能写 state。必需工具失败或漏调立即失败，不自动节点重试。

Run: `uv run pytest tests/c3/test_state_reducer.py tests/c3/test_tool_permissions.py -q`

**Commit:** `refactor: enforce workflow state and tool receipt boundaries`

### Task T17：重构 C3 Runner 的有界状态机

**Files:**

- Refactor: `src/food_agent_v2/c3/runner.py`
- Modify: `src/food_agent_v2/c3/llm_client.py`
- Modify: `src/food_agent_v2/c3/prompts.py`
- Modify: `config/prompts.json`
- Test: `tests/c3/test_workflow_transitions.py`
- Test: `tests/c3/test_required_tool_failure.py`
- Test: `tests/c3/test_artifact_grounding.py`

删除 `REQUIRED_TOOL_NOT_CALLED` 自动重试；每节点只接类型化 Artifact；未知 verdict fail；循环预算固定；Answer 与最终菜单 hash 接地；模型异常不切换、不模板回答。

Run: `uv run pytest tests/c3 tests/test_d2_answer.py -q`

**Commit:** `refactor: make agent workflow bounded and fail closed`

### Task T18：完成 C4 约束先行、锁和提交记忆

**Files:**

- Refactor: `src/food_agent_v2/c4/__init__.py`
- Refactor: `src/food_agent_v2/c4/redis_store.py`
- Create: `src/food_agent_v2/c4/mysql_repository.py`
- Test: `tests/c4/test_context_manifest.py`
- Test: `tests/integration/test_session_lock_and_restore.py`

在 ContextManifest 前加载完整约束；Redis 锁使用 fencing token；恢复从 MySQL 已提交边界与 Redis 可恢复状态组合；压缩核对核心块 hash；失败请求不留下成功记忆。

Run: `uv run pytest tests/c4 tests/integration/test_session_lock_and_restore.py -q`

**Commit:** `refactor: preserve complete session context and locks`

### Task T19：实现结果/审计/会话/outbox 原子提交

**Files:**

- Rewrite: `src/food_agent_v2/application/__init__.py`
- Create: `src/food_agent_v2/application/commit_service.py`
- Create: `src/food_agent_v2/application/outbox.py`
- Modify: `db/schema_mysql.sql`
- Test: `tests/application/test_commit_validation.py`
- Test: `tests/integration/test_transactional_outbox.py`

**Required transaction:**

```text
validate artifact chain and hashes
BEGIN
insert immutable result
insert mandatory health audit
insert committed session facts
insert ordered answer_ready/result_committed outbox rows
mark request completed
COMMIT
```

dispatcher 在 commit 后投递，使用稳定 event ID/幂等键。模拟 audit 失败无结果/事件；commit 后 dispatcher 崩溃可补发；重复投递不重复事实。

Run: `uv run pytest tests/application tests/integration/test_transactional_outbox.py -q`

**Commit:** `feat: commit results and success events atomically`

**Gate S4:** 模型节点权限、状态机、上下文完整性、会话锁、最终 hash 和 outbox 崩溃恢复全通过。

---

## Stage S5：API、SSE 与匿名前端

### Task T20：修复 D1 API/SSE 真实 Application 边界

**Files:**

- Refactor: `src/food_agent_v2/d1/schemas.py`
- Refactor: `src/food_agent_v2/d1/__init__.py`
- Refactor: `src/food_agent_v2/api_app.py`
- Test: `tests/d1/test_request_validation.py`
- Test: `tests/d1/test_sse_replay.py`
- Test: `tests/integration/test_api_application.py`

创建请求前完成 Schema/幂等/participant_ref 校验；无效请求不启动任务。SSE 读取持久化事件，支持 Last-Event-ID；修复异常路径未绑定变量；取消/失败不发成功事件。移除公开 `/users` 中的敏感/真实用户依赖，若内部需要则仅管理员离线使用且不进入前端。

Run: `uv run pytest tests/d1 tests/integration/test_api_application.py -q`

**Commit:** `refactor: expose validated post-commit api events`

### Task T21：完成 D2 匿名多轮前端和终态 Reducer

**Files:**

- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/stores/recommendation.ts`
- Modify: `frontend/src/features/participants/ParticipantContext.vue`
- Modify: `frontend/src/features/chat/ChatPanel.vue`
- Modify: `frontend/src/App.vue`
- Create: `frontend/src/stores/recommendation.test.ts`
- Create: `frontend/src/features/participants/ParticipantContext.test.ts`
- Create: `frontend/vitest.config.ts`

先增加 `test` 脚本、Vitest、Vue Test Utils、jsdom。参与者 UI 只生成匿名槽位/participant_ref，不收真实 user ID/健康详情；session_id 持续复用。Reducer 去重 event ID，`answer_ready` 显示待确认，`result_committed` 才 completed；分别展示 no-safe/no-feasible/strict-time-indeterminate/failed/cancelled/reconnect。

Run:

```powershell
npm test -- --run
npm run build
```

**Commit:** `feat: complete anonymous multi-turn recommendation ui`

### Task T22：清零静态错误并整理旧测试

**Files:** 仅允许修改 Ruff 报告中的受影响 `src/` 文件和已在计划中列出的测试；不得批量重写批准文档或锁定契约测试。

**Required checks:**

```powershell
uv run ruff check src tests scripts
uv run pytest --collect-only -q
uv run pytest -m "not live" -q
npm test -- --run
npm run build
```

修复所有 E/F/I/UP/B；删除或改写只验证旧错误语义的测试；基础设施测试的 skip 必须保留为 NOT_RUN，不能把 skip 变 pass。特别确认 SSE 未绑定变量、C4 restore、测试收集数与 markers。

**Commit:** `test: align regression suite with approved v2 contracts`

**Gate S5:** 后端静态检查零错误、非 live 测试零失败、前端单测和构建通过；API 契约不暴露真实身份和内部健康/营养字段。

---

## Stage S6：真实全链路验收

### Task T23：从空 V2 staging 跑真实固定数据验收

**Files:**

- Create: `tests/e2e/test_full_chain_real.py`
- Create: `tests/e2e/test_full_chain_failure_paths.py`
- Create: `scripts/run_full_acceptance.ps1`
- Modify: `tests/README.md`

**Preconditions:** T00—T22 全部 gate 通过；H01—H04 已签；目标是获授权的空 V2 环境；必要模型 API key 和本地模型可用。

**Run sequence:**

```powershell
docker compose up -d mysql qdrant redis
uv run food-agent-v2 data-rebuild --staging-dir .staging/final
uv run food-agent-v2 data-verify --manifest .staging/final/build-manifest.json
uv run food-agent-v2 data-initialize --manifest .staging/final/build-manifest.json --confirm-empty-v2
uv run pytest tests/integration tests/e2e -q
uv run pytest tests/test_prompts_live.py -q
uv run ruff check src tests scripts
npm test -- --run
npm run build
```

**Required success cases:** 单人、多人全员交集、明确菜数、默认 5 道、软“尽量快”、硬截止可证明、多轮替换/恢复、SSE 断线重连、幂等重放、页面刷新继续同 session。

**Required failure cases:** 无安全菜单、有安全但无可行菜单、严格时间 unknown、健康矩阵缺 1 键、Qdrant/Redis/MySQL 不可用、模型漏调必需工具、提交审计失败、dispatcher 崩溃恢复、取消、恶意提示注入、未知事件、答案改菜。

**Cross-store assertions:** API completed、MySQL result/audit/session/outbox、Redis 终态、Qdrant recipe IDs、SSE event hashes 和前端菜单完全一致。任何 live skip 使最终状态为 `NOT_ACCEPTED`。

**Commit:** `test: prove v2 real full-chain acceptance`

### Task T24：最终独立审查和交付

**Files:**

- Create: `reports/<execution-date>-v2-remediation-verification.md`
- Update only if implementation changed them: affected module docs/contracts

**Steps:**

1. 新上下文按 R-001..R-020、INV-001..024、T00..T23 三张矩阵逐项复核；
2. 核对所有命令原始退出码、skip、stdout/stderr hash、Git diff 和 TaskEnvelope；
3. 重新运行关键最终命令，不能复用 DeepSeek 自报结果；
4. 报告明确区分 `PASS / FAIL / NOT_RUN / BLOCKED`；
5. 确认 V1 未修改，固定源未修改，无未授权破坏性操作；
6. 只有 P0/P1 为零且 live 全链路通过，才写 `READY_FOR_OWNER_ACCEPTANCE`。

**Commit:** `docs: record independently verified v2 remediation`

**Gate S6 / Definition of Done:**

- 固定源和 2,000 行身份完全守恒；
- 食材身份和健康矩阵完成真实人工审核；
- B1—D2 只通过批准契约协作，在线不读离线文件；
- 健康、严格时间、软证据、模型权限和提交事件都 fail-closed；
- 后端、真实基础设施、模型 live、API/SSE 和前端全链路同时通过；
- 所有证据机器可核验，独立复核通过；
- 项目所有者最终验收。DeepSeek 无权跳过最后一项。

---

## 1. 推荐的任务信封拆分

外部控制者应为 T01—T24 各生成一个信封；每个信封的 `allowed_paths` 只包含任务 `Files` 段。`docs/contracts/**`、ADR-0004/5/6、固定源 CSV、已签 review CSV、前序锁定测试和 `execution/tasks/**` 始终放入 `forbidden_paths`，除非某任务明确拥有其中一个新文件且尚未锁定。

不要让 DeepSeek 一次接收“修好整个系统”的开放任务。允许它看到完整准则以理解上下文，但每次只给一个已解锁任务的写权限。这样不能阻止模型产生自由想法，却能保证不符合策略的代码没有权限、没有证据、没有阶段通行证，也就不能成为最终系统的一部分。
