# 数据门禁与发布输入闭包实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让固定数据构建从独立标签权威、完整依赖产物和 manifest-bound readiness 三个方向具备正式 clean rebuild 前所需的闭包。

**Architecture:** G12 直接从固定 source artifact 与批准 enrichment 重算期望；新增第 20 类 `recipe_dependencies` artifact 保存全部批准关系；固定产物名称移入共享 build contract，readiness 从 ready build 的 `artifact_counts` 读取期望并与实际记录核对。

**Tech Stack:** Python 3.11、Pydantic、pytest、Ruff、JSONL BuildManifest、MySQL fixed-artifact metadata。

## Global Constraints

- 只在 `C:\Users\zhiyo\Desktop\竞赛\program_v2\.worktrees\recipe-rag-nutrition-time-v2` 工作，分支为 `feature/recipe-rag-nutrition-time-v2`。
- 保留已有 dirty worktree 和所有不相关 owner/prior-task 改动；禁止 destructive Git 命令。
- 不 commit、不 stage；通过任务级 before/after 快照、SHA-256、RED/GREEN 输出和 review package 留痕。
- 严格 TDD：每项生产行为必须先有真实失败测试并记录 RED，再修改生产代码。
- 不写现有 `.staging`，不运行 Docker、数据库、Qdrant、Redis、API、initialization 或 publication 命令。
- 所有 fresh rebuild 只能使用系统临时目录，并在验证后自动清理。
- 不改变 2,000 条 source、50 个用户、1,932 个 eligible dish、营养/时间/健康业务决定或 RAG 检索策略。
- 新固定产物总数必须为 20；`recipe_dependencies` 必须有 167 行，其中 `bundle_contains` 12 行。
- readiness 必须使用 manifest-bound `artifact_counts`，不得保留手写的数据集计数字典。
- 每个任务实现后必须经过独立 task reviewer；全部任务完成后必须经过独立 final reviewer。

---

### Task 1: 关闭 G12 标签与餐次自证

**Files:**

- Modify: `src/food_agent_v2/b1/consumer_views.py`
- Modify: `src/food_agent_v2/b1/quality_gates.py`
- Modify: `tests/b1/test_quality_gate_authority.py`

**Interfaces:**

- Consumes: `recipe_source_rows.labels_raw` 与 `load_recipe_profile_enrichments(path, known_recipe_ids=...)`。
- Produces: `label_tags_from_raw_labels(labels_raw: str) -> tuple[str, ...]` 和独立计算的 G12 expected label/meal tags。

- [x] **Step 1: 保存 before 快照与哈希**

  将三个任务文件复制到本计划 workspace 的 `task-1-before/`，保存路径、字节数和 SHA-256；记录当前 HEAD 和 `git status --short`。

- [x] **Step 2: 写 paired-corruption 失败测试**

  在 `test_quality_gate_authority.py` 增加两个真实行为测试：

  ```python
  def test_g12_rejects_paired_meal_corruption_when_raw_meal_is_empty(...):
      artifacts = _valid_artifacts(labels_raw="")
      approved_enrichment = {"recipe_id": 1, "meal_tags": ["午餐"], "review_status": "approved"}
      artifacts["recipe_retrieval_build_views"][0]["meal_tags"] = ["晚餐"]
      artifacts["rag_documents"][0]["meal_tags"] = ["晚餐"]
      with pytest.raises(DataQualityError) as caught:
          _evaluate(..., enrichment=approved_enrichment)
      assert caught.value.code == "G12_RAG_LABEL_AND_MEAL_COVERAGE"
  ```

  ```python
  def test_g12_rejects_paired_raw_label_loss(...):
      artifacts = _valid_artifacts(labels_raw="晚餐,清淡")
      artifacts["recipe_retrieval_build_views"][0]["label_tags"] = ["晚餐"]
      artifacts["rag_documents"][0]["label_tags"] = ["晚餐"]
      with pytest.raises(DataQualityError) as caught:
          _evaluate(...)
      assert caught.value.code == "G12_RAG_LABEL_AND_MEAL_COVERAGE"
  ```

- [x] **Step 3: 验证 RED**

  Run:

  ```powershell
  uv run pytest tests/b1/test_quality_gate_authority.py -k "paired" -q
  ```

  Expected: 两个测试都因旧 G12 接受 paired corruption 而 FAIL；不是 fixture/导入错误。

- [x] **Step 4: 实现独立权威计算**

  在 `consumer_views.py` 暴露：

  ```python
  def label_tags_from_raw_labels(labels_raw: str) -> tuple[str, ...]:
      return _split_label_tags(labels_raw)
  ```

  在 `quality_gates.py` 从 `PROJECT_ROOT/data/review/recipe_profile_enrichment.jsonl` 加载批准补全。对每个 eligible recipe 计算：

  ```python
  expected_label_tags = label_tags_from_raw_labels(labels_raw)
  raw_meal_tags = meal_tags_from_raw_labels(labels_raw)
  enrichment = enrichments.get(recipe_id)
  expected_meal_tags = raw_meal_tags or (
      tuple(enrichment.meal_tags) if enrichment is not None else ()
  )
  ```

  retrieval 与 RAG 的两类字段必须分别等于 expected；expected meal 为空也必须失败。

- [x] **Step 5: 验证 GREEN 与回归**

  Run:

  ```powershell
  uv run pytest tests/b1/test_quality_gate_authority.py -q
  uv run pytest tests/b1/test_recipe_profile_enrichment.py tests/b1/test_consumer_view_consistency.py -q
  uv run ruff check src/food_agent_v2/b1/consumer_views.py src/food_agent_v2/b1/quality_gates.py tests/b1/test_quality_gate_authority.py
  ```

  Expected: 全部 exit 0，输出无错误或警告。

- [x] **Step 6: 写任务报告并自审**

  报告必须含 before hashes、RED/GREEN 原文摘要、任务级 diff、测试、Ruff、自审和未决问题。

---

### Task 2: 发布全部 recipe dependency 固定事实

**Files:**

- Modify: `src/food_agent_v2/contracts/build.py`
- Modify: `src/food_agent_v2/b1/quality_gates.py`
- Modify: `src/food_agent_v2/b1/rebuild.py`
- Modify: `src/food_agent_v2/b1/source_manifest.py`
- Modify: `src/food_agent_v2/b1/database_loader.py`
- Modify: `tests/b1/test_recipe_dependencies.py`
- Modify: `tests/b1/test_fixed_recipe_source.py`
- Modify: `tests/contracts/test_artifact_schemas.py`
- Modify: `tests/integration/test_staging_initialization.py`
- Modify: `docs/contracts/data-artifact-contracts.md`

**Interfaces:**

- Produces: `FIXED_ARTIFACT_NAMES: tuple[str, ...]` in `contracts.build`, containing exactly 20 names and `recipe_dependencies`.
- Produces: `T07/recipe_dependencies.jsonl`, 167 build-bound rows.
- Produces: `G18_RECIPE_DEPENDENCY_CLOSURE` exact source/artifact parity gate.
- Preserves: `quality_gates.REQUIRED_ARTIFACTS` as an alias to the shared tuple for existing imports.

- [x] **Step 1: 保存 before 快照与哈希**

  保存全部任务文件 before 副本、HEAD、status 与 SHA-256；记录当前 source-manifest hash。

- [x] **Step 2: 写 artifact 与 gate 失败测试**

  在 `test_recipe_dependencies.py` 增加真实 2,000-source 构建测试，断言：

  ```python
  assert len(dependency_records) == 167
  assert sum(row["relation_type"] == "bundle_contains" for row in dependency_records) == 12
  assert {row["review_status"] for row in dependency_records} == {"approved"}
  assert all(row["build_id"] == expected_build_id for row in dependency_records)
  assert all(row["source_manifest_hash"] == expected_manifest_hash for row in dependency_records)
  ```

  增加 quality gate tamper 测试：从 artifact 删除一个 meal-bundle `bundle_contains` 行，期望 `G18_RECIPE_DEPENDENCY_CLOSURE`。

  在 contracts/integration 测试中断言 `FIXED_ARTIFACT_NAMES` 共 20 项且 manifest 缺 `recipe_dependencies` 时验证失败。

- [x] **Step 3: 验证 RED**

  Run:

  ```powershell
  uv run pytest tests/b1/test_recipe_dependencies.py tests/contracts/test_artifact_schemas.py tests/integration/test_staging_initialization.py -q
  ```

  Expected: 因共享 catalog、artifact 和 G18 尚不存在而 FAIL。

- [x] **Step 4: 实现共享 catalog 与 dependency artifact**

  在 `contracts/build.py` 定义包含现有 19 项和 `recipe_dependencies` 的 `FIXED_ARTIFACT_NAMES`。在 `quality_gates.py` 使用：

  ```python
  REQUIRED_ARTIFACTS = FIXED_ARTIFACT_NAMES
  ```

  `prepare_reviewed_consumer_inputs()` 返回 `(facts, reviewed_occurrences, dependencies)`；`build_fixed_data_staging()` 将 dependencies 作为 build-bound JSONL 写入 T07，并加入 `artifact_paths`。

- [x] **Step 5: 实现 G18 精确闭包**

  使用 source artifact 的 recipe IDs/classifications 加载正式 `RECIPE_DEPENDENCIES`，分别构造 expected 与 actual 四元组。门禁必须同时满足：

  ```text
  actual == expected
  rows == unique_keys == 167
  bundle_contains == 12
  ```

  任一不满足抛 `G18_RECIPE_DEPENDENCY_CLOSURE`。

- [x] **Step 6: 更新初始化和契约文档**

  initializer 从共享 catalog 逐类加载 20 个 artifact；`recipe_dependencies` 只写入 `fixed_artifact_records`，本轮不新增 runtime table、不改变 Qdrant 行为。更新 data-artifact contract 的产物数量和字段。

- [x] **Step 7: 验证 GREEN 与回归**

  Run:

  ```powershell
  uv run pytest tests/b1/test_recipe_dependencies.py tests/b1/test_fixed_recipe_source.py tests/contracts/test_artifact_schemas.py tests/integration/test_staging_initialization.py -q
  uv run pytest tests/b1 -q
  uv run ruff check src/food_agent_v2/contracts/build.py src/food_agent_v2/b1/quality_gates.py src/food_agent_v2/b1/rebuild.py src/food_agent_v2/b1/source_manifest.py src/food_agent_v2/b1/database_loader.py tests/b1/test_recipe_dependencies.py tests/b1/test_fixed_recipe_source.py tests/contracts/test_artifact_schemas.py tests/integration/test_staging_initialization.py
  ```

  Expected: 全部 exit 0；B1 不出现新增 skip/warning/failure。

- [x] **Step 8: 写任务报告并自审**

  报告记录 before/after hashes、RED/GREEN、20 类 catalog、167/12 计数、diff、测试与 concerns。

---

### Task 3: 让 readiness 只信任 manifest-bound counts

**Files:**

- Modify: `src/food_agent_v2/application/readiness.py`
- Modify: `tests/application/test_readiness.py`
- Modify: `tests/integration/test_api_readiness.py`
- Modify: `docs/runbooks/h06-publication.md`

**Interfaces:**

- Consumes: `FIXED_ARTIFACT_NAMES` 和 ready `data_builds.artifact_counts`。
- Produces: `mysql_fixed_data_probe()` 返回 manifest-bound `artifact_count` 与 `recipe_count`；`check_readiness()` 用 recipe count 校验 Qdrant。
- Removes: `EXPECTED_FIXED_ARTIFACT_COUNTS` 手写数据集计数。

- [x] **Step 1: 保存 before 快照与哈希**

  保存四个文件的 before 副本、HEAD、status 和 SHA-256。

- [x] **Step 2: 写 fail-closed 失败测试**

  新测试必须通过真实 probe 行为证明：

  1. manifest counts 使用当前 20 类目录和新数据计数时通过；
  2. DB 实际计数与 `artifact_counts` 任一不同即失败；
  3. `artifact_counts` 缺 `recipe_dependencies` 即失败；
  4. `artifact_counts` 为 null、非法 JSON 或非对象即失败；
  5. `check_readiness()` 将 MySQL 返回的 `recipe_count` 原样传给 Qdrant，不读取静态 1,932 计数字典。

- [x] **Step 3: 验证 RED**

  Run:

  ```powershell
  uv run pytest tests/application/test_readiness.py tests/integration/test_api_readiness.py -q
  ```

  Expected: 新测试因 query 未读取 `artifact_counts`、缺共享 catalog 或仍依赖静态字典而 FAIL。

- [x] **Step 4: 实现 manifest-bound readiness**

  查询：

  ```sql
  SELECT build_id, schema_versions, artifact_counts
  FROM data_builds
  WHERE status='ready'
  ```

  统一解码 JSON object；要求 expected keys 等于 `set(FIXED_ARTIFACT_NAMES)`，actual group-by counts 严格等于 expected。recipe count 从 `expected_counts["recipe_retrieval_build_views"]` 取得且必须为正。

  `check_readiness()` 只要求 build ID 非空、artifact count 等于共享 catalog 长度、recipe count 为正，并以该 recipe count 调用 Qdrant probe。

- [x] **Step 5: 更新 runbook**

  明确 `/ready` 不再持有 H05/H06 手写行数；它校验唯一 ready build 的 manifest-bound counts 与实际 MySQL rows，再核对 Qdrant point count。

- [x] **Step 6: 验证 GREEN 与回归**

  Run:

  ```powershell
  uv run pytest tests/application/test_readiness.py tests/integration/test_api_readiness.py -q
  uv run pytest tests/infrastructure/test_h06_publication_contract.py -q
  uv run ruff check src/food_agent_v2/application/readiness.py tests/application/test_readiness.py tests/integration/test_api_readiness.py
  ```

  Expected: 全部 exit 0，输出无错误或警告。

- [x] **Step 7: 写任务报告并自审**

  报告记录 query/contract 变化、RED/GREEN、diff、测试、Ruff 和 concerns。

---

### Task 4: 全量临时重建、证据归档和最终复审

**Files:**

- Create: `.superpowers/sdd/2026-08-29-data-gate-publication-closure/final-verification.md`
- Create: `.superpowers/sdd/2026-08-29-data-gate-publication-closure/final-review-package.md`
- Modify: `reports/2026-08-28-final-data-quality-audit.md`

**Interfaces:**

- Consumes: Tasks 1–3 的最终工作树、task reports 和 reviews。
- Produces: 可复验的最终证据与对原审计报告的 superseding addendum；不产生正式 staging 或发布状态。

- [x] **Step 1: 运行 fresh temporary rebuild**

  使用系统临时目录、固定 audit UUID、显式 `deepseek-chat` model IDs 调用 `build_fixed_data_staging()`；验证后自动删除临时目录。

- [x] **Step 2: 记录构建与 manifest 证据**

  记录 source-manifest hash、builder identity 的审计性质、20 类 artifact 的 row count/SHA-256、manifest SHA-256、G01–G18 结果、`recipe_dependencies=167`、`bundle_contains=12`、`.staging` pre/post digest。

- [x] **Step 3: 运行完整允许范围验证**

  Run:

  ```powershell
  uv run pytest tests/b1 tests/application/test_readiness.py tests/integration/test_api_readiness.py tests/contracts/test_artifact_schemas.py tests/integration/test_staging_initialization.py -q
  uv run ruff check src/food_agent_v2/contracts/build.py src/food_agent_v2/b1/consumer_views.py src/food_agent_v2/b1/quality_gates.py src/food_agent_v2/b1/rebuild.py src/food_agent_v2/b1/source_manifest.py src/food_agent_v2/b1/database_loader.py src/food_agent_v2/application/readiness.py tests/b1/test_quality_gate_authority.py tests/b1/test_recipe_dependencies.py tests/b1/test_fixed_recipe_source.py tests/contracts/test_artifact_schemas.py tests/integration/test_staging_initialization.py tests/application/test_readiness.py tests/integration/test_api_readiness.py
  git diff --check
  ```

  Expected: exit 0；skip 只允许已有外部基础设施相关 skip，不得新增失败或 warning。

- [x] **Step 4: 更新审计报告**

  在原报告增加 2026-08-29 addendum：说明 G12、dependency closure、readiness 已修；明确当前仍是 dirty overlay、不得冒充 HEAD；正式 clean rebuild、新库初始化和 RAG 优化仍需后续授权。

- [x] **Step 5: 独立最终代码审查**

  最终 reviewer 对本计划全部 task-scoped diff、ledger、task reports 和 final verification 做只读审查；Critical/Important 必须修复并复审后才能完成。

- [x] **Step 6: 完成 ledger**

  写入每个任务的完成状态、review 结论、所有 deferred/parked finding 及裁决；保留 workspace 作为用户要求的实施留痕，不按默认流程删除。
