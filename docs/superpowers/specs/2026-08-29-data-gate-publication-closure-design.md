# 数据门禁与发布输入闭包设计

## 1. 目标

在不建立新数据库、不启动 MySQL/Qdrant/Redis/API、也不优化 RAG 策略的前提下，关闭 2026-08-28 数据审计暴露的三项发布前风险：

1. G12 必须从固定原始标签和正式批准的画像补全独立重算期望值，禁止 retrieval 与 RAG 相互自证；
2. 全部 167 条批准菜谱依赖必须进入 BuildManifest 管理的固定产物，18 条非 dish 父依赖不能在发布边界消失；
3. readiness 必须使用已验证 BuildManifest 绑定的产物计数，禁止继续维护会随数据更新漂移的手写计数字典。

本变更只收紧构建、初始化输入和 readiness 契约。正式 clean rebuild、新库初始化和 RAG 优化均不属于本轮实施。

## 2. 架构

### 2.1 G12 权威链

`recipe_source_rows.labels_raw` 是 `label_tags` 的唯一权威。餐次期望按以下顺序独立计算：

- 原始 label 含餐次：期望餐次严格等于原始餐次；画像补全不得扩展；
- 原始 label 完全缺餐次：期望餐次严格等于 `data/review/recipe_profile_enrichment.jsonl` 中该 recipe 的 `approved` 或 `modified` 记录；
- 两者都没有：构建失败。

G12 分别将 retrieval 和 RAG 的 `label_tags`、`meal_tags` 与独立期望比较。即使两份下游产物以相同方式损坏，也必须失败。

### 2.2 固定依赖产物

新增固定产物 `recipe_dependencies`，每行包含：

- `build_id`
- `source_manifest_hash`
- `parent_recipe_id`
- `dependency_recipe_id`
- `relation_type`
- `review_status`

该产物保存 `recipe_dependencies.jsonl` 中全部 167 条 `approved/modified` 关系，包括 149 条 dish 父关系、16 条 meal_bundle 父关系和 2 条 preparation 父关系。现有消费者视图仍只为 1,932 个 eligible dish 投影其依赖；非 dish 不进入 RAG，但其固定事实通过独立产物保留。

新增 `G18_RECIPE_DEPENDENCY_CLOSURE`：从正式 review 输入与已发布 artifact 分别构造 `(parent_recipe_id, dependency_recipe_id, relation_type, review_status)` 集合，要求精确相等、无重复、共 167 条，且 12 条 `bundle_contains` 全部保留。

### 2.3 单一固定产物目录

在共享构建契约中定义 `FIXED_ARTIFACT_NAMES`。B1 quality gate 保留 `REQUIRED_ARTIFACTS` 兼容别名，但 rebuild、manifest verification、initializer 与 readiness 都消费同一目录。加入 `recipe_dependencies` 后固定产物总数为 20。

### 2.4 Manifest 驱动 readiness

初始化流程已经在写入 ready 状态前将 MySQL 实际计数与 BuildManifest 计数逐项比较，并把结果存入 `data_builds.artifact_counts`。readiness 改为：

1. 读取唯一 ready build 的 `schema_versions` 和 `artifact_counts`；
2. 要求 `artifact_counts` 的 key 集严格等于 `FIXED_ARTIFACT_NAMES`；
3. 查询 `fixed_artifact_records` 的实际计数并与 `artifact_counts` 精确比较；
4. 从 `recipe_retrieval_build_views` 的 manifest-bound 计数取得 recipe count，并用它校验 Qdrant point count；
5. 保留运行时 schema version 的代码契约检查。

删除数据集特定的 `EXPECTED_FIXED_ARTIFACT_COUNTS`，避免下一次合法数据更新再次造成 `/ready` 误报。

## 3. 错误处理

- G12 任一权威不一致抛 `DataQualityError(code="G12_RAG_LABEL_AND_MEAL_COVERAGE")`；
- dependency source/artifact 不一致抛 `DataQualityError(code="G18_RECIPE_DEPENDENCY_CLOSURE")`；
- readiness 遇到缺失、非对象或 key 不闭合的 `artifact_counts`，以及实际计数不一致，均 fail closed 为 `SERVICE_NOT_READY`；
- 本轮不提供兼容旧 19-artifact manifest 的发布路径。旧 build 可继续作为既有回滚单元，但新代码只初始化新的 20-artifact manifest。

## 4. 测试与证据

所有生产改动严格执行 TDD，并在 `.superpowers/sdd/2026-08-29-data-gate-publication-closure/` 保存：

- 任务 brief；
- 修改前文件 SHA-256；
- RED 命令与预期失败；
- GREEN 命令与通过摘要；
- task-scoped diff/review package；
- 实现报告、独立审查和修复轮次；
- 最终测试、Ruff、临时目录全量 rebuild 与 manifest verification 证据。

当前 worktree 原本存在大量未提交工作，本轮不执行 Git commit，不改 `.staging`、数据库、容器或服务；任务归属通过 before/after 文件快照与哈希确定。

## 5. 验收标准

- 两个 paired-corruption G12 回归测试在旧实现上 RED、修复后 GREEN；
- `recipe_dependencies` 正式产物 167 行，12 条 `bundle_contains`，全部 build identity 正确；
- BuildManifest 恰含 20 类产物，G01–G18 全部通过；
- readiness 对 manifest-bound 当前计数通过，对计数/产物 key 篡改 fail closed；
- 完整 B1 测试和相关 application/integration 测试通过；
- Ruff 对本轮触及的 Python 文件无错误；
- fresh temporary rebuild 和 `verify_build_manifest()` 通过，仓库 `.staging` 与外部运行状态不变。

