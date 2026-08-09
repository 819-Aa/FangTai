# H04 全新隔离卷初始化与固定产物读取设计

- 状态：`REVIEW_REQUIRED`
- 日期：2026-08-10
- 适用范围：T09/H04、T11–T14 固定数据 Repository
- 批准方向：项目所有者已批准“保留旧卷、新建隔离卷初始化；T09 保存不可变产物，T11 起由类型化 Repository 读取”

## 1. 背景与现状

T07–T09 已在提交 `4eae6acda5b950d5f98ed9841b3653a80b9b241b` 完成，最终构建位于 `.staging/final`。该构建包含 19 个同 `build_id` 产物，并通过 13 道全量门禁。

当前 Docker 主机仍保存 2026-08-07 创建的旧资源：

- 容器：`food_agent_v2_mysql`、`food_agent_v2_qdrant`、`food_agent_v2_redis`，均处于停止状态；
- 数据卷：`program_v2_mysql_v2_data`、`program_v2_qdrant_v2_data`、`program_v2_redis_v2_data`。

旧卷是否为空未经证明，因此不满足 H04。启动、清空、复用或删除旧卷都不属于本次授权。

## 2. 方案比较与选择

### 方案 A：复用旧卷

优点是启动命令最少；缺点是无法先验保证为空，且可能混入旧构建事实。违反 H04，拒绝。

### 方案 B：新建 Compose project 与全新卷（采用）

保留旧容器和旧卷不动，通过独立 Compose project、独立容器名和自动生成的新卷启动 T09 环境。新卷在本次执行中首次创建，可机器证明初始化前为空，符合 H04，且不会销毁既有数据。

### 方案 C：删除旧容器和旧卷后重建

环境最整洁，但会不可恢复地删除旧数据，竞赛项目没有必要承担该风险，拒绝。

## 3. 隔离环境设计

`docker-compose.yml` 的三个 `container_name` 改为可由环境变量覆盖，同时保留现有默认值：

```text
MYSQL_CONTAINER_NAME      默认 food_agent_v2_mysql
QDRANT_CONTAINER_NAME     默认 food_agent_v2_qdrant
REDIS_CONTAINER_NAME      默认 food_agent_v2_redis
```

H04 使用固定的新 Compose project 名 `food_agent_v2_h04`，并使用：

```text
food_agent_v2_h04_mysql
food_agent_v2_h04_qdrant
food_agent_v2_h04_redis
```

由 Compose 首次创建的新卷必须为：

```text
food_agent_v2_h04_mysql_v2_data
food_agent_v2_h04_qdrant_v2_data
food_agent_v2_h04_redis_v2_data
```

仍使用项目配置中的 V2 端口 `3307/6335/6336/6380`。旧容器保持停止，避免端口冲突。执行前后都记录旧资源状态，禁止对旧容器或旧卷执行 `rm`、`down -v`、清空或覆盖。

## 4. T09 MySQL 固定事实存储

T09 只维护两个权威固定数据表：

### `data_builds`

一行代表一个构建，保存：

- `build_id`；
- `source_manifest_hash`；
- `builder_version`；
- BuildManifest 与质量报告 SHA-256；
- 19 项 Artifact 计数；
- `initializing | ready` 状态和初始化时间。

在线系统只接受恰好一个 `ready` 构建。零个或多个 `ready` 构建都必须 fail-closed。

### `fixed_artifact_records`

按 `build_id + artifact_name + record_index` 无损保存 BuildManifest 已验证的 19 项 JSON 记录。所有记录必须携带与 `data_builds` 一致的 `build_id` 和 `source_manifest_hash`。

T09 不再把同一事实双写进 `recipes`、`ingredients` 等旧领域表。双写会产生两套权威来源和跨表漂移，不适合固定数据、一次初始化的竞赛范围。现有旧领域表保留但不作为 T11–T14 固定事实来源；删除它们不属于本变更。

## 5. T11–T14 类型化 Repository 边界

从 T11 开始，领域 Repository 必须：

1. 从 `data_builds` 解析唯一 `ready build_id`；
2. 按 `build_id + artifact_name` 一次读取需要的固定记录；
3. 使用领域 Pydantic Schema 反序列化并验证主键、引用和构建身份；
4. 在进程内构建只读索引，供 B3/B4/B5/B6 查询；
5. 未知 ID、记录缺失、Schema 错误、构建身份不一致时 fail-closed；
6. 禁止回退到 JSONL、原始 CSV、旧 `program` 或未声明的领域表。

固定数据只有 2,000 条菜品、1,781 个食材身份和 65,854 条健康决定。Repository 可在启动时按 Artifact 批量读取并建立内存只读映射，无需增加事件流、缓存失效、增量同步或运行时版本切换。

Qdrant 在线配置仍只暴露 `recipe_retrieval_v2`。该名称是发布别名，指向本次构建的物理 staging collection；在线客户端不得自动创建缺失集合。

## 6. H04 初始化顺序

1. 记录当前 Git HEAD、BuildManifest SHA-256、旧容器与旧卷清单。
2. 校验工作树干净，且 `builder_version == HEAD`。
3. 执行 `data-verify`，重新验证固定源、19 项 Artifact、散列、计数、构建身份和 13 道门禁。
4. 创建并启动 `food_agent_v2_h04` 的 MySQL/Qdrant/Redis 与三个全新卷。
5. 等待三个服务健康；记录新容器、新卷和创建时间。
6. 执行 `data-initialize --confirm-empty-v2`。初始化器必须再次确认 MySQL 固定表为空且 Qdrant 最终名称不存在。
7. MySQL 在一个事务内写入构建登记与全部固定记录；事务保持未提交。
8. Qdrant 创建带 `build_id` 的物理 staging collection，写入 1,914 个 RAG 点位并核对完整 recipe ID 集合。
9. 发布唯一别名 `recipe_retrieval_v2`，随后提交 MySQL。
10. 写出初始化报告，并从真实 MySQL/Qdrant 重新核验构建身份、19 项计数、1,914 个菜品 ID 和别名目标。

## 7. 失败与回滚

- 清单、Git SHA、空环境检查或服务健康失败：不开始写入。
- MySQL 写入或计数失败：回滚事务，不创建/发布在线集合。
- Qdrant 写入、点位或 ID 对账失败：回滚 MySQL，删除本次明确命名的 staging collection。
- 别名发布后 MySQL 提交失败：删除本次别名和物理 staging collection，再回滚 MySQL。
- 回滚动作本身失败：返回 `INITIALIZATION_ROLLBACK_INCOMPLETE` 并保留诊断证据；禁止宣称 H04 通过。
- 任一失败都不得删除或修改 2026-08-07 的旧容器和旧卷。

自动回滚只处理本次 `build_id` 明确命名的资源。失败环境保留供诊断，是否删除新隔离卷需要新的显式授权。

## 8. 验收标准

H04 通过必须同时满足：

- 旧容器和旧卷的标识及状态未改变；
- 新 Compose project、容器和卷名称与本设计一致；
- `data_builds` 恰好一行且状态为 `ready`；
- MySQL 19 项 Artifact 计数与 BuildManifest 精确一致；
- MySQL `rag_documents.recipe_id` 集合与 Qdrant 点位 ID 集合精确相等，均为 1,914 个；
- Qdrant 只以配置名 `recipe_retrieval_v2` 对外，别名指向本次 `build_id` 物理集合；
- 初始化报告记录 Git SHA、BuildManifest/质量报告散列、MySQL 计数、Qdrant 点位数和真实资源名称；
- 第二次执行初始化被 `V2_TARGET_NOT_EMPTY` 拒绝；
- T09 集成测试、B1 测试及受影响的契约/架构测试通过。

## 9. 文档同步范围

书面复核本设计后，同一实施任务必须同步：

- `docs/modules/01-data-engineering.md`：固定事实存储、Repository 读取和 H04 隔离环境；
- `docs/contracts/data-artifact-contracts.md`：19 项实际 Artifact、MySQL 构建登记与在线读取契约；
- `docker-compose.yml`：仅参数化容器名，不改变默认开发环境；
- 必要测试与 H04 机器证据。

## 10. 非目标

- 不删除、迁移、启动或覆盖旧 Docker 资源；
- 不建设多版本发布、蓝绿切换、增量同步或自动回滚平台；
- 不在 T09 双写领域表；
- 不提前实现 T11–T14 的 Repository 业务逻辑；
- 不修复与数据链路无关的 C4 会话恢复基线问题。
