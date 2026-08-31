# Qwen 语义重写与 RAG 过滤设计

- 状态：`APPROVED_FOR_IMPLEMENTATION`
- 日期：2026-08-31
- 基线：`3701461fbbe1633d45aedb4eea0abfec51116983`
- 评测证据：`recipe-rag-nutrition-time-v2/.superpowers/sdd/2026-08-30-h07-semantic-rag-evaluation/semantic-rag-evaluation-report.md`

## 1. 目标

在不重建 H07 数据、MySQL、Qdrant 或 Redis 的前提下，将每次推荐前的语义理解调整为“千问一次结构化重写，失败走确定性 fallback”，并保证模型结果不能删除用户原话中已被规则明确识别的约束。语义计划进入 RAG 时，只有 ready build 实际支持的软 facet 才成为硬过滤条件，避免合法的菜单结构或口味偏好把召回压成 0。

## 2. 已确认边界

- 查询理解模型仍为现有 `model_reasoning`，当前部署值为 `qwen3.8-max`；仅 `query_understanding` 显式关闭思考模式。
- 一次模型调用；传输失败、超时、JSON/Schema 失败、受控 facet 非法或正向检索词污染时，立即使用确定性 fallback，不盲目重试。
- 显式菜数、硬时间、餐次、人群、包含/排除食材、健康信号和营养目标不得被模型空值删除。
- `retrieval_query` 只含正向找菜语义；被排除食材、疾病、过敏、健康指标及否定词不得进入正向 query。
- `meal_types`、`population_tags`、包含食材和排除食材继续是硬过滤；不能因无结果放宽。
- `dish_types`、`taste_tags`、`cuisine_tags`、`scenario_tags` 属于菜单结构/偏好 facet。只有值在当前 ready build 对应 payload 字段中真实出现时才投影为 RAG 硬过滤；未覆盖值仍保留在 QueryPlan，供 C2/偏好逻辑使用。
- “三菜一汤”必须得到 `dish_count=4` 和 `dish_types` 含“汤”；“家常”保留为偏好；“45分钟内”仍是 2700 秒硬约束。本次不放宽时间，也不修改 C2/B5。
- B4 健康安全边界、H07 数据和向量不变。

## 3. 组件设计

### 3.1 角色级模型参数

`LLMConfig` 新增可选的 `query_extra_body`，由 `LLM_MODEL_QUERY_EXTRA_BODY` 加载。`extra_body_for_role("query_understanding")` 优先使用它；回答角色继续使用 `answer_extra_body`，其他推理角色继续使用 `reasoning_extra_body`。示例配置写明千问非思考参数 `{"enable_thinking": false}`，但不写任何密钥。

### 3.2 语义归一化

`QueryNormalizer` 使用评测通过的封闭 Prompt，最多调用模型一次。模型输出先经 Pydantic schema、受控 facet 和正向 query 污染检查。有效输出按字段与确定性 fallback 合并：集合去重合并；规则明确的 `max_time_minutes`、`dish_count` 优先；模型只能补充没有被规则识别的语义。

确定性 fallback 补齐已验证场景：识别“家常”，将“N菜一汤”投影成“汤”结构，保留菜数和硬时间；排除/健康词不进入 `retrieval_query`。fallback 是模型不可用时的正常可用路径，不调用其他模型。

### 3.3 路由结果合并

`_apply_semantic_rewrite` 不再整对象覆盖 `FastIntentRouter` 的结果。集合字段去重合并；路由器已明确的菜数、硬时间和健康排除优先。重写后的正向 query 仍作为唯一 RAG query，避免原始负向句子进入向量检索。

### 3.4 ready build 软 facet 投影

`RecipeRetrievalService` 在 load 后从内存中的同 build RAG documents 建立各 facet 的可用值集合，并提供纯投影方法。C3 在调用 `retrieve` / `multi_person_retrieve` / 扩展检索前调用该方法：只裁剪 dish/taste/cuisine/scenario；meal/population/include/exclude 原样保留。该方法不重试检索、不触碰 Qdrant alias，也不改变 QueryPlan。

## 4. 数据流与失败处理

```text
用户原话
  ├─ FastIntentRouter（确定性显式约束）
  └─ QueryNormalizer
       ├─ Qwen 一次结构化调用成功且有效 → 字段级合并
       └─ 失败/非法/污染 → 确定性 fallback
             ↓
       与 FastIntentRouter 字段级合并
             ↓
       QueryPlan（完整语义留存）
             ↓
       ready-build 软 facet 投影
             ↓
       BM25 + Qdrant + reranker → B4 → C2/B5
```

千问失败不是请求失败；只有后续既有检索组件不可用时才沿用原有 fail-closed 行为。模型输出非法时不进行第二、第三次语义调用。

## 5. 验收标准

- 配置测试证明只对 `query_understanding` 使用 query 专属 extra body。
- 单元测试证明一次调用失败即 fallback，且 fallback/合并保留显式菜数、汤、家常、45 分钟、包含/排除和健康信息。
- 单元测试证明 `dinner`、`三菜一汤` 等非法 facet 不会作为硬过滤，负向/健康词不会留在正向 query。
- 检索测试证明不受当前 build 支持的 meal/population/include/exclude 仍为硬过滤，而未覆盖的 dish/taste/cuisine/scenario 被从 RetrievalFilters 裁剪且仍留在 QueryPlan。
- H07 实测中 E02 重写保真、RAG 候选非 0；最终仍可因 2700 秒硬时间无可行菜单而返回既有确定性终态。
- 目标测试和相关回归测试全部通过；工作树无密钥或无关文件进入提交。

## 6. 非目标

- 不重建或迁移 MySQL、Qdrant、Redis。
- 不修改嵌入/重排模型、B4、C2/B5 或回答生成。
- 不把 45 分钟改成软目标，不为 E02 特判配方。
- 不增加第二个语义模型或复杂代理链。
