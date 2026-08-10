# 固定数据与离线 Artifact 契约

- 状态：`APPROVED`
- 日期：2026-08-09
- 权威决策：[ADR-0004](../decisions/0004-fixed-source-and-one-time-identity-rebuild.md)

## 1. 目的

本文定义固定 2,000 条菜品从原始 CSV 到 MySQL、Qdrant 和各领域只读视图的唯一交付协议。字段的最终可执行权威是代码中的 Pydantic/JSON Schema；任何实现字段变更必须在同一任务内同步本文、Schema、消费者契约和测试。

## 2. 构建身份

### 2.1 `SourceManifest`

```json
{
  "schema_version": "1.0.0",
  "source_id": "fixed-recipes-2000",
  "relative_path": "data/raw/recipes_sample_2000.csv",
  "encoding": "GBK",
  "byte_size": 1138083,
  "sha256": "B2177DC6CDCAE24FC5671C8DADA44295228F4301E3CE620ED11D51B1ABFE4371",
  "row_count": 2000,
  "headers": ["名称", "食材清单", "烹饪步骤", "label"]
}
```

`source_manifest_hash` 是上述对象按批准的 canonical JSON 规则序列化后的 SHA-256。构建命令必须先校验源清单，再读取任何业务行。

### 2.2 `BuildManifest`

```text
build_id: UUID
source_manifest_hash: sha256
builder_version: Git commit SHA
schema_versions: artifact_name -> semver
artifacts: artifact_name -> {relative_path, row_count, sha256}
quality_gate_report: {relative_path, sha256, passed}
created_at: UTC timestamp
```

- `builder_version` 缺失时构建不得进入可发布状态。
- 所有 Artifact 必须携带同一 `build_id` 和 `source_manifest_hash`。
- 清单中的路径必须位于本次 staging 根目录，禁止绝对路径和目录穿越。

## 3. 必需 Artifact

| Artifact | 主键/粒度 | 强制内容 | 主要消费者 |
|---|---|---|---|
| `recipe_source_rows` | `recipe_id=1..2000` | 源行号、源字段散列、原始字段引用 | 审计、B3 |
| `recipe_classifications` | `recipe_id` | 唯一 `record_type`、规则、置信度、审核状态 | B3、C1 |
| `ingredient_occurrences` | `occurrence_id` | `recipe_id`、源片段、位置、解析结构、证据 | B3 |
| `ingredient_registry` | `ingredient_id` | 规范名、批准别名、类别、食材族、审核状态 | B3、B4、B6 |
| `ingredient_crosswalk` | 源词条/旧 ID | `merge/split/discard`、目标 ID、理由、审核者 | 迁移、审计 |
| `recipe_ingredient_relations` | `recipe_id + occurrence_id` | 目标食材、角色、可选/替代/组合语义 | B3、B4 |
| `step_tasks` | `recipe_id + task_id` | 步骤、依赖、主动/设备/等待时长及权威级别 | B5 |
| `nutrition_features` | `ingredient_id` | 参考数据映射、每 100g 字段、来源、置信度 | B6 |
| `health_relation_decisions` | `constraint_code + ingredient_id` | 独立批准决定、`hard_filter`、理由、证据 | B4 |
| `rag_documents` | `recipe_id` | 仅推荐菜品、结构化检索字段、源散列 | C1 |

Artifact 可以拆成多个物理文件，但逻辑主键、计数和散列不能改变。不得用空列表、默认分数或占位字符串伪装“已生成”。

实际构建共 **19 项已验证 Artifact**，以 `BuildManifest.artifacts` 为权威清单：

```text
recipe_source_rows(2000)  recipe_classifications(2000)  ingredient_occurrences(17521)
ingredient_registry(1781)  ingredient_aliases(21)        ingredient_forms(381)
ingredient_crosswalk(431)  recipe_ingredient_relations(17505)
step_tasks(1914)           nutrition_features(1914)      recipe_health_views(1914)
recipe_nutrition_input_views(1914)  recipe_retrieval_build_views(1914)
recipe_step_binding_views(1914)     rag_documents(1914)
health_relations(985)      health_relation_coverage(38)  health_relation_decisions(65854)
user_profiles(50)
```

计数以该构建的 `BuildManifest.artifacts[*].row_count` 为准；任何与 Manifest 计数不一致的初始化不得发布。

## 4. 分类契约

每个 `recipe_id` 恰好有一个封闭枚举分类：

```text
dish | preparation | meal_bundle | cooking_program | test_record
```

- `dish` 才能进入普通推荐候选。
- 其他类型保留为可追溯事实，但不能被 C1/C2 当成一道菜。
- 分类器必须输出规则证据；人工审查完成前状态为 `pending`，构建门禁要求 `pending=0`。
- 旧 `program` 的数量只能作为差异对照，不能作为 V2 强行达到的配额。

## 5. 食材身份契约

- 每个进入推荐视图的 `ingredient_occurrence` 必须解析为恰好一个已批准 `ingredient_id`；组合项必须显式拆成有序子项，不得把整段文本注册为食材。
- 数量、单位、处理方式、可选和替代语义不是食材身份的一部分。
- `merge` 表示多个源词条归一到同一身份；`split` 表示源组合词条拆成多个出现；`discard` 只允许非食材文本并必须有批准原因。
- 名称相似、字符串包含和模型输出都只能生成候选，不得直接批准身份或健康关系。
- 身份冻结后，运行时未知名称返回 `UNKNOWN_INGREDIENT_IDENTITY`，不得动态创建 ID。

## 6. 健康关系矩阵

对封闭的允许健康约束代码集合 `C` 和已批准健康食材全集 `I`，构建产物必须满足：

```text
keys(health_relation_decisions) = C × I
```

每对关系都有且只有一个独立的人工批准决定，值为 `hard_exclude` 或 `no_hard_relation`。候选生成器、模型、旧系统或类别规则不能把 `review_status` 设为 `approved`。缺键、重复键、未知代码、未知食材或证据缺失都使构建失败。

## 7. 营养与时间的不可用语义

- 营养参考映射缺失时输出显式 `unavailable` 和缺失原因；禁止用 `0.5`、零值或伪造均值替代。
- 某软评分维度不可用时，C2 对剩余可用维度重新归一化权重，并在评分分解中记录禁用维度。
- 时间数据必须区分确定性任务图证据与 `model_estimate`；严格时间只服从 [ADR-0005](../decisions/0005-strict-time-semantics.md)。

## 8. 发布与在线边界

构建阶段：

```text
固定源 -> 清单校验 -> B1 解析/归一 -> 领域 Artifact -> 全局门禁
       -> staging MySQL/Qdrant 验证 -> 空 V2 存储的一次性原子初始化
```

在线阶段：

```text
API -> Application/Workflow -> 领域服务 -> Repository -> MySQL/Qdrant/Redis
```

在线导入 `data/raw`、`data/processed`、JSONL reader 或解析器视为 `ONLINE_OFFLINE_BOUNDARY_VIOLATION`。Qdrant 集合在上线前固定并验证，不支持请求期间选择索引版本。

## 9. 质量门禁报告

报告至少包含：

- 源清单核验结果；
- 2,000 行连续性、唯一性和分类分布；
- 食材出现数、标准身份数、merge/split/discard 数和零未决证明；
- 孤儿引用、重复主键、未知枚举、空必需字段；
- 健康关系矩阵期望/实际键数和集合差；
- 营养、步骤时间与 RAG 覆盖率，不可用原因分布；
- MySQL 表计数、Qdrant 点位计数和抽样内容散列；
- 每个门禁的命令、退出码、stdout/stderr 文件散列。

只有报告 `passed=true` 且所有 P0 门禁为零，才允许初始化。

## 10. MySQL 构建登记与在线读取契约

### 10.1 `data_builds`

每个构建一行，保存：

- `build_id`；
- `source_manifest_hash`；
- `builder_version`（Git commit SHA）；
- BuildManifest 与质量报告 SHA-256；
- 19 项 Artifact 计数；
- `initializing | ready` 状态与初始化时间。

在线系统只接受**恰好一个 `ready` 构建**；零个或多个 `ready` 构建都必须 fail-closed。

### 10.2 `fixed_artifact_records`

按 `build_id + artifact_name + record_index` 无损保存 BuildManifest 已验证的 19 项 JSON 记录。
所有记录必须携带与 `data_builds` 一致的 `build_id` 与 `source_manifest_hash`。

T09 不在旧领域表（`recipes`/`ingredients` 等）双写固定事实；旧领域表保留但不作为 T11–T14
固定事实来源。

### 10.3 在线读取契约

- T11–T14 Repository 从 `data_builds` 解析唯一 `ready build_id`，按 `build_id + artifact_name`
  读取固定记录，用领域 Pydantic Schema 反序列化并验证主键、引用与构建身份；未知 ID、记录缺失、
  Schema 错误、构建身份不一致时 fail-closed；禁止回退到 JSONL、原始 CSV 或旧领域表。
- Qdrant 在线配置只暴露 `recipe_retrieval_v2`（发布别名，指向本次构建的物理 staging collection）；
  在线客户端不得自动创建缺失集合。
- 固定数据只有 2,000 条菜品、1,781 个食材身份和 65,854 条健康决定，Repository 可在启动时按
  Artifact 批量读取并建立内存只读映射，无需增量同步或运行时版本切换。
