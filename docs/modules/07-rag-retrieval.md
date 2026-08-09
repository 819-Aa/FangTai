# V2 混合RAG检索模块设计

- 状态：`APPROVED`
- 日期：2026-08-09
- 适用项目：`program_v2`
- 上游依据：[系统总体设计](../00-system-overview.md)、[全局不变量](../contracts/global-invariants.md)、[模块边界](../contracts/module-boundaries.md)、[推荐请求生命周期](../scenarios/recommendation-lifecycle.md)、[数据工程模块](01-data-engineering.md)、[菜品与食材处理模块](03-recipe-ingredient-processing.md)

## 0. 2026-08-09 批准的实现基线

- C1 索引输入只能是 B3 `eligible` 推荐视图；每个点绑定固定 `recipe_id`、`build_id`、文档散列和允许公开字段，不能读取或重解析原始 CSV。
- 生产链路必须真实执行词法召回、Qdrant 向量召回、RRF 融合和重排序；缺少任何组件都失败，不返回 fallback 候选。
- Qdrant 集合在上线前于 staging 构建并核对点位数、ID 集合、向量维度和抽样负载散列；本项目不建设请求期间索引版本切换。
- 初次和扩展召回必须有不同批次 ID、去重证据和固定预算；RAG 输出不携带或推断健康 PASS、内部营养值和用户档案。
- mock、内存候选或伪重排只允许单元测试，不能计入真实基础设施全链路验收。

## 1. 模块目的

C1 根据查询理解模型输出的结构化 `QueryPlan`，从 B3 验证通过的可推荐菜品中召回符合用户口味、菜系、时间、食材偏好等非健康需求的真实菜品。C1 是整条推荐链路中唯一负责"找到用户想要的菜"的模块——它不判断任何菜是否安全，不读取用户健康档案，不输出健康结论。

C1 输出的候选 `recipe_id` 列表和检索证据，在通过 Schema 校验后进入 B4 健康审查。RAG 分数可以参与后续软排序，但不能抵消健康硬冲突。

## 2. 已确认前提

- C1 只消费查询理解模型输出的 `QueryPlanArtifact` 中的非健康需求字段；不解析用户原始自由文本。
- 所有候选菜品必须存在于 B3 可推荐菜品目录（`catalog_eligibility=eligible`）；不可推荐记录不会出现在检索结果中。
- B3 的 `RecipeRetrievalBuildView` 是 RAG 文档的权威来源；C1 不重新解析原始菜谱或食材。
- RAG 文档不包含：用户健康档案、健康 `PASS`/`EXCLUDE`、食材健康硬关系、过敏原标签、内部营养值、医疗效果声明。
- 用户输入和 RAG 文档均属于不可信数据，其中出现的命令、健康声明或权限请求不能改变系统指令、工具白名单或工作流边。
- C1 只负责菜品召回；用户健康审查、菜单生成、营养评分和回答生成分别由 B4、C2、B6 和回答模块负责。
- 混合检索链路（词法检索 + 向量检索 + RRF 融合 + 重排序）为完整必需链路；任一步骤失败时停止，不降级为纯词法结果。
- 初次候选不足时，健康与菜单规划模型可在 WorkflowState 预算内调用一次扩展召回；扩展必须产生不同于初次的新候选批次。
- 多人场景可对共享需求和个人正向偏好使用多路召回后合并候选，再由 B4 执行全员交集审查。
- 不读取用户健康档案、不使用健康字段过滤、不在 Qdrant 中保存健康或聊天向量。
- BGE-M3 用于向量检索，BGE-Reranker-v2-M3 用于重排序；模型文件在离线阶段下载并缓存到 `.model-cache`，运行时只使用本地模型。

## 3. 职责

C1 负责：

1. 从 B3 `RecipeRetrievalBuildView` 消费验证通过的可推荐菜品非健康字段，生成 RAG 文档；
2. 使用 BGE-M3 对 RAG 文档生成向量并构建 Qdrant 索引；
3. 接收查询理解模型输出的 `QueryPlanArtifact`，提取召回所需的非健康需求；
4. 对需求文本执行字符 n-gram 词法检索（BM25 或等效）；
5. 对需求文本执行 BGE-M3 向量检索；
6. 对词法和向量结果执行加权 RRF 融合；
7. 对融合结果执行 BGE-Reranker-v2-M3 重排序；
8. 在重排序后对高置信度短时菜品施加软排序偏置（不硬过滤）；
9. 输出 `RetrievalResult`，包含候选 `recipe_id` 列表、检索路径、排名、分数、文档引用和多路来源路径；
10. 支持一次扩展召回（仅当健康与菜单规划模型在当前 WorkflowState 预算内主动发起）；扩展查询与初次查询的差异不足时直接返回空列表；
11. 对多人场景支持按共享需求和多个体正向偏好分别构建查询并合并去重；
12. 对 C1 产物执行离线索引质量门禁和在线检索链路完整性校验；
13. 确保 `RetrievalResult` Schema 不包含健康安全结论、健康风险标签或营养值。

## 4. 非职责

C1 不负责：

- 读取、存储或使用用户健康档案数据；
- 判定任何菜品对任何用户是否健康安全；
- 在 RAG 文档中设置或推断健康 `PASS`、`EXCLUDE`、风险标签；
- 根据过敏原、疾病、异常指标过滤或降权候选菜品；
- 对严格时间限制执行可行性判断（属于 B5）；
- 执行菜单规划、营养评分或时间调度；
- 生成最终回答文本或用户可见分析摘要；
- 管理 WorkflowState 或决定工作流流转（扩展召回条件检查由工作流负责）；
- 在 Qdrant 中保存用户对话向量、健康特征或会话状态；
- 解析原始菜谱文本、建立食材身份或审核别名；
- 处理用户输入中的健康声明、指令注入或权限请求；
- 在混合检索链路失败时降级为纯词法结果或返回固定候选列表。

## 5. 上游输入

### 5.1 离线构建输入

| 输入 | 来源 | 内容 |
|---|---|---|
| 可推荐菜品构建视图 | B3 `RecipeRetrievalBuildView` | 每道 `eligible` 菜品的 `recipe_id`、菜名、标准食材显示名、审核后的非健康检索字段（餐次、口味、菜系、烹饪方式、类型、温度、口感、场景）、步骤摘要输入、时间引用 |
| RAG 配置 | B1 构建配置 | 文档字段模板、n-gram 参数、RRF 权重、重排序参数、索引构建参数 |
| 本地模型文件 | `.model-cache` | `BAAI/bge-m3`（向量）、`BAAI/bge-reranker-v2-m3`（重排序） |

### 5.2 在线查询输入

| 输入 | 来源 | 内容 |
|---|---|---|
| QueryPlan | 查询理解模型 `QueryPlanArtifact` | 结构化查询意图：口味偏好、菜系、菜品类型、烹饪方式、正向食材偏好、时间约束（非健康部分）、餐次、场景、多样性要求、数量预算、非健康排除项 |
| 扩展检索请求 | 健康与菜单规划模型（通过 `expand_retrieval` 工具） | 基于当前安全候选缺口调整后的查询需求，必须不同于初次查询 |
| 多人正向偏好 | 查询理解模型（多人场景） | 共享需求 + 每位参与者的个人正向口味偏好 |

C1 不接收：原始用户消息文本、B2 健康约束、B4 健康评估结果、B6 营养评分、完整用户健康档案。

### 5.3 C1 输入的受限模型调整

查询理解模型输出的是结构化 `QueryPlanArtifact`，C1 从中提取非健康需求字段构建检索查询。C1 本身不接受模型的自由文本输入或参数重写。扩展召回时，健康与菜单规划模型仍通过结构化工具参数描述调整后的需求，C1 据此生成与初次查询部分不同的检索表达——不使用模型的自然语言"重新查询"文本。

## 6. 下游输出

### 6.1 检索结果

```
RetrievalResult
├── retrieval_id
├── request_id
├── query_fingerprint (需求文本的规范化哈希)
├── candidates[]
│   ├── rank
│   ├── recipe_id
│   ├── lexical_score | null
│   ├── vector_score | null
│   ├── fusion_score
│   ├── rerank_score
│   ├── source_paths[] (该候选来自哪些查询路：shared | participant_1 | ...)
│   └── document_ref
├── retrieval_path: lexical + vector + fusion + rerank
├── index_version (关联的 Qdrant Collection 版本标识)
├── total_candidates
└── is_expansion (区分初次检索与扩展检索)
```

### 6.2 检索证据

```
RetrievalEvidence
├── retrieval_id
├── lexical_query
├── vector_query
├── fusion_method: weighted_rrf
├── rerank_model: BGE-Reranker-v2-M3
├── query_timestamp
└── index_status
```

### 6.3 消费者视图

- **查询理解模型**：初次检索结果供形成 `QueryPlanArtifact` 时引用；
- **B4 健康规则引擎**：通过 `retrieval_result_ref` 获取候选 `recipe_id` 列表，重新加载 B3 权威食材视图执行健康审查；
- **健康与菜单规划模型**：候选菜品及检索证据（不包含健康结论），供判断是否需要扩展召回；
- **C2 菜单规划**：安全候选菜品的 RAG 分数和 `source_paths` 作为软评分输入之一（与营养、时间、偏好等共同参与多目标排序）。

C1 的输出 Schema 不包含：`health_status`、`safety_label`、`allergen_list`、`nutrition_value`、`health_risk_score`。

## 7. 数据模型与数据所有权

### 7.1 RAG 文档

```
RagDocument
├── recipe_id
├── display_name
├── ingredient_display_names[]
├── searchable_fields
│   ├── cuisine
│   ├── dish_type
│   ├── cooking_method
│   ├── taste
│   ├── meal_time
│   ├── temperature
│   ├── texture
│   ├── occasion
│   └── keyword_tags[]
├── step_summary
├── time_reference
│   ├── total_time_minutes | null
│   └── time_confidence: high | medium | low
└── index_metadata (Qdrant payload)
```

其中 `searchable_fields` 来自 B3 审核后的非健康标签候选。`keyword_tags` 是可供词法检索使用的聚合文本字段。任何字段均不得包含：过敏原类型、疾病关联、健康风险标签、营养红线、医疗功效声明、用户健康条件。

### 7.2 Qdrant Collection

```
Collection: recipe_retrieval
├── 向量维度：1024 (BGE-M3)
├── 向量字段：dense_vector
├── Payload：RagDocument 的 index_metadata 子集
├── 索引类型：HNSW
└── 数量上限：当前可推荐菜品总数（离线构建时确定）
```

Qdrant 只服务于菜品召回。不建立用户健康档案 Collection、聊天向量 Collection 或健康规则 Collection。

### 7.3 检索配置

```
RetrievalConfig
├── lexical
│   ├── ngram_range: [2, 3]
│   ├── analyzer: standard
│   └── weight_in_fusion: 0.3
├── vector
│   ├── model: BGE-M3
│   ├── top_k: 50
│   └── weight_in_fusion: 0.7
├── fusion
│   ├── method: weighted_rrf
│   ├── rrf_k: 60
│   └── combined_top_k: 30
├── rerank
│   ├── model: BGE-Reranker-v2-M3
│   ├── top_k: 20
│   └── min_rerank_score: null (低于阈值的候选可被标记但不自动丢弃)
├── time_boost
│   ├── enabled: true
│   ├── boost_mode: soft (软偏置，不硬过滤)
│   ├── high_confidence_short_threshold_seconds: 1800 (30分钟)
│   └── boost_factor: 1.15 (重排序分乘法因子)
└── budget
    ├── initial_recall_limit: 10 (单人首轮)
    ├── multi_person_recall_limit: 50 (多人合并)
    └── expansion_recall_limit: 10
```

权重、预算和偏置参数属于可审查的固定配置，由 B1 构建配置管理。不在运行时由模型修改。词法/向量权重建议在实现阶段用实际查询数据验证后最终确定。

### 7.4 数据所有权

C1 拥有：
- `RagDocument` 的 Schema 和生成逻辑；
- `RetrievalResult` 和 `RetrievalEvidence` 的 Schema；
- 检索配置（n-gram 参数、RRF 权重、重排序参数、time_boost 参数、召回预算）；
- 混合检索链路实现。

B1 负责离线调用 C1 的文档生成和索引构建接口，执行质量门禁；B3 拥有 `RecipeRetrievalBuildView` 的事实权威，C1 通过公开端口消费。

## 8. 核心处理流程

### 8.1 离线索引构建

```
B3 RecipeRetrievalBuildView (仅 eligible 菜品)
→ 按 RagDocument Schema 生成每道菜品的检索文档
→ 校验：不存在健康字段、不存在营养值、不存在过敏原
→ 使用 BGE-M3 生成 dense_vector
→ 构建 Qdrant Collection（或增量更新）
→ 执行最小检索验证（已知菜名查询应返回自身）
→ 执行 RAG 文档与 B3 可推荐菜品集合的一致性校验
→ 生成索引质量报告
```

### 8.2 在线初次检索

```
QueryPlanArtifact 中的非健康需求
→ 提取检索查询文本（结构化需求 → 规范化查询字符串）
→ 构建词法查询（字符 n-gram）
→ 构建向量查询（BGE-M3 编码）
→ 并行执行词法检索和向量检索
→ 加权 RRF 融合（k=60，词法权重 0.3，向量权重 0.7）
→ BGE-Reranker-v2-M3 重排序（取融合 Top-30，输出 Top-20）
→ 应用 time_boost（time_confidence=high 且时长 ≤ 阈值的菜品重排分 × 1.15）
→ 取前 initial_recall_limit=10 道
→ 校验全部候选 recipe_id 存在于 B3 目录
→ 生成 RetrievalResult
```

### 8.3 扩展检索

```
健康与菜单规划模型调用 expand_retrieval
→ 工作流校验 retrieval_expansion_count = 0
→ 计算扩展查询与初次查询的规范化哈希差异
→ 若差异不足以产生不同的检索表达 → 直接返回空列表（不消耗重排推理）
→ C1 基于调整后的需求生成新查询表达
→ 执行完整混合检索链路（与初次相同）
→ 过滤掉已出现在初次结果中的 recipe_id
→ 若仍有新候选 → 产生新 RetrievalResult（is_expansion=true）
→ 若没有新候选 → 返回空列表（不产生新批次）
→ retrieval_expansion_count = 1
```

扩展检索不降低混合检索标准、不改变权重、不跳过重排序。查询差异地板值由配置管理。

### 8.4 多人多路检索

```
共享需求 + 参与者正向偏好列表
→ 共享需求构建主查询（source_path = "shared"）
→ 每位参与者的正向口味偏好构建独立子查询（source_path = "participant_N"）
→ 并行执行各路完整混合检索
→ 合并各路候选（保留 recipe_id → 最高融合分的来源路径）
→ 每个候选记录 source_paths[]（来自哪些查询路）
→ 去重
→ 取前 multi_person_recall_limit=50 道
→ 生成 RetrievalResult
```

多路查询可能来自同一参与者的多个偏好维度（如"喜欢辣"+"喜欢海鲜"），也可能来自不同参与者的个人偏好。C1 统一按多路合并处理，不区分偏好来源。

## 9. 算法与确定性规则

### 9.1 查询文本构建

从 `QueryPlanArtifact` 的非健康字段构建规范化查询字符串：

```
查询字符串 = 拼接(
    菜系偏好,
    菜品类型,
    烹饪方式,
    口味偏好,
    正向食材,
    餐次,
    场景,
    排除项(仅非健康排除)
)
```

查询字符串只描述用户想要什么，不描述用户不能吃什么（健康部分）。C1 从 QueryPlan 中跳过带有健康语义的字段（疾病、过敏、健康目标等）。这些字段不出现在检索查询中，也不作为 Qdrant 过滤条件。

非健康排除项（如口味上的"不吃香菜"）由查询理解模型在 QueryPlanArtifact 中标记为非健康排除类型。C1 接收该字段后作为词法查询的否定条件处理。排除项的健康/非健康分界由查询理解模型和 C3 负责，C1 不做自主判断。

### 9.2 词法检索

```
输入：查询字符串
→ 字符 2-gram + 3-gram 分词
→ BM25 评分（或等效词法评分函数）
→ 输出：recipe_id → lexical_score
```

中文字符 n-gram 不使用分词器，直接在字符级切分。英文和数字保留空格分词。混合文本先按语言分段再分别处理。

### 9.3 向量检索

```
输入：查询字符串
→ BGE-M3 编码为 1024 维向量
→ Qdrant HNSW 近似最近邻搜索
→ top_k = 50
→ 输出：recipe_id → vector_score
```

向量检索对查询文本做与离线索引相同的预处理。BGE-M3 模型在 API 启动时预热加载，首轮请求不承担冷加载时间。

### 9.4 加权 RRF 融合

```
RRF_score(d) = Σ (weight_s / (k + rank_s(d)))

其中:
  s ∈ {lexical, vector}
  weight_lexical = 0.3
  weight_vector = 0.7
  k = 60
  rank_s(d) = 文档 d 在检索源 s 中的排名（从 1 开始）
```

文档未出现在某一检索源中时，该源的 rank 视为 ∞（贡献为 0）。

权重可通过构建配置调整，但不能在请求期间由模型修改。当前权重初始值在实现阶段用实际查询数据验证后最终确定——如果词法侧的精确菜名匹配信号被压得过低，可调整至 0.4/0.6 甚至 0.5/0.5。

### 9.5 重排序

```
输入：RRF 融合后的 Top-30 文档
→ 每个文档：查询字符串 + 菜名 + 食材显示名 + 检索标签拼接文本
→ BGE-Reranker-v2-M3 计算相关性分数
→ 按 rerank_score 降序排列
→ 输出 Top-20
```

重排序只改变候选顺序，不引入新的过滤条件或不安全判定。

### 9.6 短时间软偏置

```
对每个重排序后的候选:
    if candidate.time_confidence = high AND candidate.total_time_minutes ≤ 30:
        rerank_score = rerank_score × 1.15
    (仅改变排序位置，不排除任何菜品，不用于严格时间判断)
```

软偏置仅在用户查询中包含时间短/快手等语义时启用。用户未提及时间偏好时不应用。偏置不改变 B5 的严格时间可行性判断——C1 只是让短时菜品在检索结果中排得更靠前。

### 9.7 候选预算

| 场景 | 初次候选上限 | 说明 |
|---|---|---|
| 单人首轮 | 10 | 重排序 Top-20 中取前 10 |
| 多人多路合并 | 50 | 各路合并去重后取 Top-50 |
| 扩展检索 | 10 | 过滤初次结果后取前 10 |

候选数量是上限，如果混合检索链路实际返回不足上限，则返回实际数量，不补入随机菜品或固定默认列表。

### 9.8 检索失败条件

以下情况停止检索链路，不返回部分结果：

- 词法检索服务不可用；
- 向量模型加载失败或编码失败；
- Qdrant 连接失败或 Collection 不存在；
- 重排序模型加载失败或推理失败；
- 检索结果中所有 `recipe_id` 均不存在于 B3 可推荐菜品目录。

## 10. 公开接口与模型工具

### 10.1 领域服务

```
RecipeRetrievalService
├── retrieve(QueryPlan query_plan) → RetrievalResult
├── expand_retrieval(QueryPlan adjusted_plan, previous_recipe_ids) → RetrievalResult
├── multi_person_retrieve(QueryPlan shared, QueryPlan[] individual_prefs) → RetrievalResult
└── get_retrieval_evidence(retrieval_id) → RetrievalEvidence
```

### 10.2 RAG 文档生成（离线接口）

```
RagDocumentBuilder
├── build_documents(RecipeRetrievalBuildView[]) → RagDocument[]
└── validate_documents(RagDocument[]) → ValidationReport
```

### 10.3 向量索引管理（离线接口）

```
VectorIndexManager
├── build_index(RagDocument[], collection_name) → IndexBuildResult
├── validate_index(collection_name) → IndexValidationReport
└── get_index_status(collection_name) → IndexStatus
```

### 10.4 Agent 工具

查询理解模型的允许工具：

```
retrieve_recipes(query_plan_ref) → RetrievalResult
```

- 工具由查询理解模型自主调用；
- `request_id`、`session_id` 和参与者范围由工作流注入；
- 模型不接收 Qdrant 连接信息、索引内部状态或完整 RAG 文档集合。

健康与菜单规划模型的允许工具：

```
expand_retrieval(adjusted_query_plan_ref) → RetrievalResult
```

- 仅当 `retrieval_expansion_count = 0` 时允许；
- 新查询必须产生不同于初次的结果批次；差异不足时 C1 直接返回空列表；
- 模型不能通过此工具访问健康档案或修改检索权重。

## 11. 依赖方向

允许依赖：

```
B1 离线构建器 → C1 RagDocumentBuilder + VectorIndexManager
C1 RecipeRetrievalService → B3 RecipeRetrievalBuildView (只读)
C1 RecipeRetrievalService → B3 RecipeCatalogService (候选存在性校验)
查询理解模型工具 → C1 retrieve_recipes()
健康与菜单规划模型工具 → C1 expand_retrieval()
B4 健康审查 → C1 RetrievalResult (仅 recipe_id 引用)
C2 菜单规划 → C1 RetrievalResult (仅 RAG 分数和 source_paths 引用，软排序输入)
```

禁止依赖：

```
C1 → 原始用户健康档案或 B2 健康约束
C1 → B4 健康关系、覆盖数据或评估结果
C1 → B6 营养评分
C1 → API Schema 或前端类型
C1 → 模型供应商客户端（通过基础设施适配器间接调用）
Agent 模型 → Qdrant 客户端或索引管理接口
RAG 文档 → 健康字段、营养值、过敏原列表
查询字符串 → 健康约束代码、疾病名称、异常指标
```

## 12. 异常、错误码与停止条件

| 错误码 | 触发条件 | 处理 |
|---|---|---|
| `RAG_DOCUMENT_BUILD_FAILED` | RAG 文档生成阶段失败（Schema 违规、健康字段泄漏、与 B3 不一致） | 离线构建停止，不生成 Qdrant 索引 |
| `VECTOR_INDEX_BUILD_FAILED` | Qdrant Collection 创建、向量编码或索引构建失败 | 离线构建停止 |
| `VECTOR_INDEX_VALIDATION_FAILED` | 最小检索验证失败（已知查询不能返回自身） | 离线构建停止 |
| `RAG_LEXICAL_SERVICE_UNAVAILABLE` | 词法检索服务不可用 | `failed`，不降级继续 |
| `RAG_VECTOR_SERVICE_UNAVAILABLE` | 向量模型加载失败、编码失败或 Qdrant 不可用 | `failed`，不降级继续 |
| `RAG_RERANK_SERVICE_UNAVAILABLE` | 重排序模型加载失败或推理失败 | `failed`，不降级继续 |
| `RAG_RESULT_EMPTY` | 完整混合链路执行成功但返回 0 条候选 | 正常结果（空列表），不视为失败 |
| `RAG_EXPANSION_NO_DIFFERENCE` | 扩展查询与初次查询差异不足以产生不同的检索表达 | 返回空列表，不消耗重排推理 |
| `RAG_EXPANSION_NO_NEW_RESULTS` | 扩展检索执行成功但未产生新候选 | 正常结果（空列表），由健康规划模型判断后续动作 |
| `RAG_EXPANSION_LIMIT_EXCEEDED` | 尝试超过一次扩展召回 | `failed` |
| `RAG_CANDIDATE_NOT_IN_CATALOG` | 检索结果中的 `recipe_id` 不在 B3 可推荐目录 | `failed`（索引与目录不一致，系统错误） |
| `RAG_HEALTH_AUTHORITY_VIOLATION` | RAG 文档、检索查询或结果中包含健康安全结论 | `failed` |
| `UNTRUSTED_INSTRUCTION_DETECTED` | 用户输入或 RAG 文档中检测到试图修改系统指令的内容 | `failed` |

检索链路任一步骤失败均停止，不降级为纯词法结果或固定候选列表。返回 0 条候选是合法的业务结果。

## 13. 构建与初始化要求

- B3 可推荐菜品全部通过质量门禁后，C1 才能开始构建 RAG 文档和 Qdrant 索引；
- RAG 文档按 B3 `RecipeRetrievalBuildView` 逐菜品生成，不合并不跳过；
- 每份 RAG 文档通过健康字段泄漏检查（自动化扫描过敏原、疾病、营养值、风险标签等禁止字段）；
- 生成的 `recipe_id` 集合与 B3 可推荐菜品集合精确相等；
- Qdrant Collection 使用 V2 独立命名空间，不修改旧项目索引；
- BGE-M3 和 BGE-Reranker-v2-M3 模型文件在离线阶段下载并缓存，运行时只读取本地文件；
- API 启动时预热加载 BGE-M3 和重排序模型，首轮请求不承担冷加载时间；
- 开发期间 RAG 文档 Schema 或检索配置变化时执行全量索引重建；
- 正式运行期间不更新 RAG 文档、Qdrant 索引或检索配置。

## 14. 测试和验收标准

### 14.1 RAG 文档质量

- 生成的 RAG 文档数量等于 B3 可推荐菜品数量；
- 每个文档的 `recipe_id` 存在于 B3 目录；
- 文档不包含：过敏原字段、疾病名称、健康风险标签、营养值、`health_status`、`safety_conclusion`；
- 菜名、食材显示名和检索标签可读且与 B3 视图一致。

### 14.2 索引验证

- Qdrant 向量数量等于 RAG 文档数量；
- 已知菜名查询的 Top-1 结果确认为自身；
- 向量维度为 1024（BGE-M3）；
- Collection payload 不包含健康相关字段。

### 14.3 混合检索链路

- 每条查询均经过词法 → 向量 → RRF 融合 → 重排序四阶段；
- 词法服务不可用时整条链路失败，不返回纯词法结果；
- 向量服务不可用时整条链路失败；
- 重排序模型不可用时整条链路失败；
- 返回的候选全部存在于 B3 可推荐菜品目录。

### 14.4 检索语义

- "辣味川菜"优先返回川菜且带辣味的菜品；
- "清淡蒸菜"优先返回蒸制且口味清淡的菜品；
- "适合夏天"优先返回凉菜、冷盘等场景匹配的菜品；
- 不含健康词的查询不被注入健康过滤条件；
- 查询中出现的疾病名称不被用于过滤候选（由 B4 在后续链路处理）；
- 用户提及"快手菜""半小时"时高置信短时菜品获得软提权。

### 14.5 扩展检索

- 扩展检索使用完整混合链路，不缩短或降级；
- 扩展查询与初次查询差异不足时直接返回空列表；
- 扩展结果过滤掉初次结果中的 `recipe_id`；
- 没有新候选时返回空列表而不伪造结果；
- `retrieval_expansion_count` 正确更新且上限为 1。

### 14.6 多人多路

- 共享需求 + 多个体偏好的各路结果正确合并；
- 同一 `recipe_id` 出现在多路时保留最高融合分；
- 每个候选的 `source_paths[]` 正确记录来路；
- 合并后总数不超过 `multi_person_recall_limit`。

### 14.7 跨模块契约

- 查询理解模型工具只接收 `query_plan_ref`，不接收自由文本；
- 检索结果不包含健康安全结论；
- B4 从 `RetrievalResult` 获取 `recipe_id` 后重新加载 B3 权威食材视图；
- C2 仅使用 RAG 分数和 `source_paths` 作为软排序输入，不将其等同于推荐结论；
- Qdrant 中不存在用户健康档案字段或聊天向量。

## 15. 旧实现与目标实现差异

| 维度 | 旧实现 | V2 目标 |
|---|---|---|
| 健康过滤 | RAG 文档可能包含过敏原、健康特征、风险标签 | 完全移除所有健康相关字段 |
| 查询输入 | 可能与健康约束混合 | 严格分离：C1 只接收非健康需求 |
| 检索链路 | 有词法、向量、融合、重排，但有降级路径 | 完整链路，不降级 |
| 扩展召回 | 存在但约束可能不严格 | 明确上限 1 次，必须产生新批次，差异不足直接返空 |
| 多人检索 | 可能单路查询 | 明确多路合并策略 + source_paths 溯源 |
| 时间偏置 | 无 | 高置信短时菜品软提权，不硬过滤 |
| 索引管理 | `build_recipe_knowledge.py` 混合健康字段 | RAG 文档构建由 C1 独立负责，健康字段零泄漏 |
| 模型工具 | 可能直接调用 Repository | 通过受控工具接口，模型不接触 Qdrant |

## 16. 迁移分类

| 旧实现 | 分类 | 处理结论 |
|---|---|---|
| `data_pipeline/build_recipe_knowledge.py` | `REFACTOR` | 保留 `recipe_id` 和检索文本生成思路；移除过敏原、健康特征、风险标签、内部营养字段；按新 RagDocument Schema 重建 |
| `rag/retrieval/` 混合检索链路 | `REFACTOR` | 保留 BM25 + BGE-M3 + RRF + BGE-Reranker 链路结构；移除降级路径；增加查询输入健康字段拦截、time_boost、source_paths |
| `rag/query_builder.py` | `REWRITE` | 改为从结构化 QueryPlan 提取非健康需求；移除健康约束混入逻辑 |
| `data_pipeline/build_vector_index.py` | `REFACTOR` | 保留 Qdrant HNSW 索引构建；使用 V2 独立 Collection；增加索引验证 |
| Qdrant Collection 定义 | `REWRITE` | V2 独立 Collection；payload 中移除所有健康字段 |
| 旧 RAG 文档中的 `allergen_types`、`health_features`、`risk_tags` | `REMOVE` | 这些字段不出现在新 RAG 文档中 |
| 旧 RAG 相关测试 | `REFACTOR` | 保留检索链路测试；增加健康字段泄漏测试、不降级测试、扩展召回上限和差异地板测试、多路合并测试、time_boost 测试 |

## 17. 审查后清理项

以下内容在 C1、B4、C2 和回答模块全部迁移并通过验收后清理：

- RAG 文档中的过敏原、健康特征、风险标签和内部营养字段；
- 检索查询构建中的健康约束混入逻辑；
- 混合检索的降级路径（纯词法、纯向量等）；
- 旧 Qdrant Collection 和对应的索引管理脚本；
- RAG 结果中声明或暗示健康安全的字段和代码路径；
- `build_recipe_knowledge.py` 中的健康判定和营养值生成逻辑；
- 已被新 C1 Schema 替代且确认无消费者的旧索引字段和兼容适配器。

## 18. 与全局流程和后续模块的关系

```
QueryPlanArtifact (仅非健康需求)
→ C1 混合检索
→ RetrievalResult (候选 recipe_id + 检索证据 + source_paths)
→ B4 健康审查 (重新加载 B3 权威食材视图)
→ 安全候选
→ C2 菜单规划 (RAG 分数和 source_paths 作为软输入之一)
```

- B3 提供可推荐菜品构建视图作为 C1 的权威数据源；
- B4 通过 `retrieval_result_ref` 获取候选 ID，重新加载 B3 完整健康食材视图执行审查；
- C2 可将 RAG 相关性分数和 `source_paths` 作为软目标之一，与营养、时间、偏好共同参与多目标优化；
- C4 上下文中 RAG 候选引用通过 `RetrievalResult` 保存，不保存完整文档内容；
- D1 API 和 D2 前端不接收 Qdrant 内部状态或完整 RAG 文档；
- D3 测试与验收必须覆盖离线索引门禁、混合链路完整性、健康字段零泄漏和扩展召回上限。

## 19. 待考量事项

以下问题不影响当前设计通过，但在进入实现阶段前需要进一步确认。在此留痕供后续回顾。

### 19.1 RRF 权重的实际验证

当前词法/向量权重设为 0.3/0.7，偏向语义。但对中餐菜名，字符 n-gram 词法检索的精确区分力很强——"水煮牛肉"和"水煮鱼"在向量空间很接近，但词法上差异明确。反之向量检索对"清爽""下饭""家常"等模糊语义覆盖面更好。0.3/0.7 可能导致精确菜名匹配信号被稀释。

**建议**：实现阶段用真实查询集跑一遍，对比不同权重配置下的召回质量。如果词法侧的精确匹配信号被压得过低，可调整至 0.4/0.6 或 0.5/0.5。权重参数作为可配置项保留，不硬编码。

### 19.2 source_paths 溯源字段

多人多路去重时，同一 `recipe_id` 保留最高融合分是正确的。但合并后下游无法区分一道菜是共享需求召回的、还是因为某个人的偏好命中的。B4 健康审查不需要这个信息，但 C2 做菜单规划时可能需要——比如解释"这道菜入选是因为满足了参与者A的口味偏好"。

**当前处理**：已在 `RetrievalResult.candidates[].source_paths[]` 中增加溯源字段，记录每道候选来自哪些查询路。实现成本低，C2 可用可不用。C2 设计时需决定是否消费此信息。

### 19.3 扩展检索查询差异地板

设计上扩展检索由健康规划模型构造调整后的查询，但如果模型传回一个和初次几乎一样的查询，C1 搜出来的还是同一批菜，过滤初次结果后就是空的，浪费一次重排序推理。

**当前处理**：已加入差异地板逻辑——扩展查询与初次查询的规范化哈希差异不足以产生不同检索表达时，C1 直接返回空列表，不消耗重排推理。差异地板的阈值由配置管理，实现阶段需结合 QueryPlan Artifact 的实际结构确定合理阈值。如果 QueryPlan 的字段是高度结构化的（枚举值为主），差异判断可以简化为字段级不等比较。

### 19.4 时间软偏置的定位

`RagDocument` 中有 `time_reference`，用户可能说"半小时能做完的菜"。严格时间可行性判断属于 B5（只有高置信度时间上界参与硬约束）。但如果 C1 完全不管时间，用户说"快手菜"结果 Top-10 全是 2 小时炖菜，后续链路就全在浪费。

**当前处理**：已在重排序后增加 `time_boost`——高置信度且时长 ≤ 30 分钟的菜品重排分乘以 1.15。这是一个软偏置，不硬过滤，不替代 B5 的严格时间判断。`boost_factor`、时长阈值和启用条件均为可配置参数。这只解决"用户希望快"的信号传递，不解决"用户要求严格 30 分钟"——后者由 B5 的 `strict_time_feasible` 负责。

### 19.5 非健康排除项与健康排除项的分界

查询文本构建时要从 QueryPlan 中"跳过健康字段"。但如果用户说"不要花生的菜"，查询理解模型需要判断这是食材过敏（健康硬约束 → B2）还是口味偏好（非健康排除 → C1）。这个判断不在 C1 的职责范围内。

**当前处理**：C1 接收 QueryPlanArtifact 中的非健康排除项字段并作为词法否定条件处理。排除项的健康/非健康分界由查询理解模型和 C3 的 QueryPlanArtifact 结构共同定义。C3 设计时需明确 QueryPlanArtifact 中排除项的 Schema——区分 `health_exclusions`（走 B2）和 `preference_exclusions`（走 C1）。C1 只消费后者，如果 C3 把两者混在一个字段里，C1 需要增加拦截逻辑。
