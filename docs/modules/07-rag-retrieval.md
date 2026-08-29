# V2 RAG 检索模块设计

- 状态：`APPROVED`
- 更新日期：2026-08-23
- 适用模块：B1、C1、C3

## 1. 当前契约

C1 只负责从真实 eligible 菜品中召回与用户意图相关的候选。它不读取健康档案、不输出安全结论、不读取营养值或 `step_tasks`，也不执行时间 boost。健康权威属于 B4，预计时间属于 B5。

## 2. 离线 RAG 文档

`rag_documents` schema version 为 `2.0.0`，以 `recipe_id` 为粒度，包含：

- 菜名、完整原始 `label_tags`；
- meal/population/dish/taste/cuisine/scenario 等封闭 facet；
- required、optional 和 one-of 备选食材检索词；
- catalog eligibility 与同 build 身份。

不包含健康 verdict、营养向量、完整步骤、时间任务图、来源/置信度或解释字段。

## 3. 标签与餐次权威

原始 `recipe_source_rows.labels_raw` 含餐次时，该餐次集合必须原样精确保留，approved enrichment 不能增加或删除。只有原始餐次完全为空时，才使用 approved enrichment 的 meal 结果。

`G12_RAG_LABEL_AND_MEAL_COVERAGE` 同时核对 source rows、retrieval build views 与最终 RAG 文档；每个 eligible 菜品必须有非空 meal tags。H05 当前 eligible/RAG 数为 `1932`。

## 4. 在线 QueryPlan 与硬过滤

`QueryPlanArtifact` schema version 固定为 `2.0.0`。查询理解在每次推荐前产生封闭字段：

```text
rewritten_query
meal_types / population_tags / dish_types / taste_tags / cuisine_tags / scenario_tags
include_ingredients / exclude_ingredients
health_exclusions
nutrition_goal_codes
dish_count_requested
time_constraint_seconds / time_constraint_policy
```

同字段多值 OR、跨字段 AND、排除项 NOT。meal/population/exclusion 是硬过滤，不得因无结果静默放宽；无匹配时返回空集合，由上层按契约处理。

## 5. 混合召回

生产路径固定为 BM25 + BGE-M3/Qdrant + 加权 RRF + BGE reranker。任一必需组件不可用即整体失败，不降级为纯词法、纯向量或未重排结果。Qdrant payload 的标签数组必须与同一 build 的 `rag_documents` 逐项一致。

C1 不基于时长重排；“快手”语义进入 QueryPlan 后，由 C2/B5 在 B4 safe 集合上处理。

## 6. 存储与发布

MySQL 固定 Artifact 与 Qdrant staging 必须属于同一 build，recipe ID 集合和 payload 数量一致后才切 alias/提交。在线 C1 只识别已验证 alias，不创建或切换集合。

## 7. 验收

- 原始 label 完整投影；raw meal 非空时 enrichment 不扩展；
- meal/population/exclusion 硬过滤从不放宽；
- C1 模块不加载任务图，也没有时间查表或时间重排路径；
- payload 不含旧 step/time 字段；
- MySQL/Qdrant `1932` 个 recipe ID 精确一致。
