# Task 6 数量、用途与可食比例审阅门禁

日期：2026-08-24  
状态：候选生成完成；正式规则/决定未写入；等待架构口径与关键数值审阅。

## 结论先行

Task 6 目前不能直接进入“批量批准”。CLI 和候选工程已经修好，但数据审计暴露出两个不同问题：

1. 15 个统计稳定的数量规则中，只有 2 个适合保留工程近似；旧模型把毫升数直接复制成克数的迹象明显，不能把 `is_stable=true` 当作物理参数可信。
2. `usage_code` 与 `retained_in_dish` 被同一 schema/runtime 门禁耦合。10,417 个 occurrence 因此同时为空，其中 7,993 个本来不属于数量异常，却被用途分类挡住。营养求和真正依赖的是 retention；main/supporting 只在模糊量规则匹配时影响克重。

建议先采用“用途与留存解耦”方案，再生成独立的 pending usage/retention 候选。禁止为了通过门禁而给 10,417 行随意填 `main/supporting + retained=true`。

## 已完成与验证证据

- 新增并修复离线命令：`quantity-rules`、`edible-fractions`。
- 实现提交：`7735d75`、`9633251`、`2408730`。
- 主控回归：`291 passed, 1 skipped`；Ruff 通过。
- 独立最终复审：PASS，0 Critical / 0 Important。
- 权威候选：453 个 quantity rule 键；16,820 个 edible occurrence；全部 `pending`。
- `candidate_edible_fraction` 全空；未由模型发明比例。
- 五个正式文件运行前后 SHA-256 完全一致。

| 权威报告 | 行数 | SHA-256 |
|---|---:|---|
| `quantity_rule_candidates.csv` | 453 | `3DE96BF656798DCBA5A41615CE965EED3EFD12901626B850392F463C6B9D3BF8` |
| `quantity_rule_exceptions.csv` | 158 | `4BC8035C5F2F1C2F8A7BA45A1E965A08FD9DE3CAD94ED6C670B957343BDAA8BA` |
| `edible_fraction_candidates.csv` | 16,820 | `96791D383EDBCE3E158CA0B59F8AF30FB6129FFA0611590D865CE06492FABA9C` |
| `edible_fraction_exceptions.csv` | 48,557 | `EADA1F8455BA8374F272F18D831E0F30CA3734A2B232E8593DDC4D42F573F881` |

## 数据质量画像

### 数量规则

- 453 行对应 453 个唯一规则键，重复键为 0。
- 15 行 `is_stable=true`，134 行至少有一个异常码，异常展开后为 158 行。
- 规则类型：88 个 density、312 个 unit weight、53 个 fuzzy single value。
- 17 个盐、糖、酱油等高影响模糊规则全部保留在异常队列，未批准。
- 11 个稳定 density 中有 9 个精确等于 `1.000 g/mL`，合计 106 个旧候选样本，而且来源均为 `whole_recipe_context_model`。这是内部一致性，不是实测证据。

### 可食比例

- 16,820 行对应 16,820 个唯一 occurrence，覆盖 1,932 道菜和 1,727 个 ingredient。
- 聚合后为 2,065 个 `ingredient_id + source_form` 键；原计划中的 1,790 已过时，实际多 275 个（+15.36%）。
- 空 form：14,516 行 / 1,628 键（86.30% occurrence）。
- usage/retention 未决：10,417 行 / 1,834 个含未决键（61.93% occurrence）。
- multi-form：6,804 行 / 522 键 / 184 个 ingredient。
- 所有 16,820 行都缺有效 edible rule/decision；10,417 行还同时缺 usage/retention。

`片/末/丝/段/碎/熟/干/新鲜` 等 form 只描述切形或状态，不能推出可食比例；`鱼段、排骨块、鸡腿块` 仍可能带骨。只有“去皮/去骨/去壳/去核”或身份本身明确表示净肉、仁、汁、粉、泥、蓉等，才可进入 1.0 的高置信候选池，而且仍需先通过 retention 门禁。

## 15 个稳定数量规则的处置建议

`is_stable` 只表示旧模型输出的组内离散较小。外部事实使用 [NIST 水密度](https://www.nist.gov/system/files/documents/pml/wmd/labmetrology/NISTIR_7383_20130424_20160123Rev.pdf)、[FDA 体积单位定义](https://www.ecfr.gov/current/title-21/chapter-I/subchapter-B/part-101/subpart-A/section-101.9)、USDA FoodData Central、[Codex 食用植物油标准](https://www.fao.org/4/Y2774E/y2774e04.htm)、[Codex 橄榄油标准](https://www.fao.org/fao-who-codexalimentarius/sh-proxy/en/?lnk=1&url=https%253A%252F%252Fworkspace.fao.org%252Fsites%252Fcodex%252FStandards%252FCXS%2B33-1981%252FCXS_033e.pdf) 和国家/厂商官方资料。外部值仍只作为待审建议。

| 食材与规则 | 当前候选 | 建议 | 处置 |
|---|---:|---:|---|
| 纯净水·mL | 1.000 | 1.000 | 可保留工程近似 |
| 普通醋·mL | 1.000 | 1.000 | 可保留工程近似；不覆盖甜醋/浓缩醋 |
| 水淀粉·mL | 1.000 | 无通用值 | 拒绝；缺少淀粉:水比例 |
| 牛奶·mL | 1.000 | 1.02 | 外部值替换；[USDA FDC 171265](https://api.nal.usda.gov/fdc/v1/food/171265?api_key=DEMO_KEY) |
| 柠檬汁·mL | 1.000 | 1.02 | 外部值替换；[USDA FDC 167747](https://api.nal.usda.gov/fdc/v1/food/167747?api_key=DEMO_KEY) |
| 色拉油·mL | 0.95918 | 0.92 | 外部值替换；通用混合油仍有品种风险 |
| 橄榄油·mL | 0.96015 | 0.913 | 外部值替换；Codex 20°C 区间中点 |
| 烧烤汁·mL | 1.000 | 1.13 | 外部值替换；[USDA FDC 174523](https://api.nal.usda.gov/fdc/v1/food/174523?api_key=DEMO_KEY) 的 17g/tbsp |
| 黄酒·mL | 1.000 | 待定 | 必须人工；[GB/T 13662-2018](https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=6087AD6AC018BA858B6EFE0953197037) 无统一密度常数 |
| 辣椒油·mL | 1.000 | 澄清油可约 0.92 | 必须人工；名称可能指含水/固形物产品 |
| 香醋·mL | 1.000 | 待定 | 必须人工；固形物与陈酿差异大 |
| 鸡精·勺 | 5g | 条件 5g | 必须人工；厂商量勺不等于通用“勺” |
| 蛋清·个 | 31.17g | 条件 33g/大号蛋 | 必须人工；[USDA FDC 172183](https://api.nal.usda.gov/fdc/v1/food/172183?api_key=DEMO_KEY) |
| 香蕉·根 | 100g | 条件 118g/中等可食部 | 必须人工；[USDA FDC 173944](https://api.nal.usda.gov/fdc/v1/food/173944?api_key=DEMO_KEY)，并防止后续重复扣皮 |
| 皮蛋·个 | 57.5g | 55–60g 参考 | 必须人工；品牌规格与含壳口径不同 |

若所有外部替换也需要 owner 签字，最小人工审阅集合为 12 条：上表 7 条“必须人工”加牛奶、柠檬汁、色拉油、橄榄油、烧烤汁。

## 必须先解决的 usage/retention 架构冲突

### 当前行为

- 正式 CSV 强制一行同时给出非空 `usage_code` 与 `retained_in_dish`。
- `NutritionUsageResolution` 只有一个合并的 `requires_review`。
- 营养计算器在求和前要求 usage 和 retention 都已定，但真正决定是否进入总量的只有 retention。
- quantity normalizer 在尝试明确的克/千克换算之前也要求 usage；实际上 usage 只在 `fuzzy_single_value` 规则匹配时需要。
- `retained=false` 的 occurrence 仍被迫选择一个不会使用的 usage code。

这使 10,417 条 usage/retention 空值变成系统级阻塞。对其中 10,417 条分层：909 条受控油身份、1,107 条液体 marker、1,312 条 source group 主料、298 条 source group 辅料、6,791 条没有可审计的 mechanical usage 证据。只有 2,424 条也在 quantity candidates 中，另 7,993 条本不应被用途 taxonomy 阻塞。

### 推荐方案：解耦 retention 与 usage

1. 新增独立正式 retention decision（或等价的独立字段生命周期）。
2. 分别表示 `usage_requires_review` 与 `retention_requires_review`。
3. 营养计算只因 retention 未决 fail closed。
4. 确定性质量/计数/体积解析不要求 usage；只有进入 fuzzy 规则分支时才要求 usage。
5. 新增离线 `data-review --kind nutrition-usage`，输出 pending-only `nutrition_usage_candidates.csv`，携带 source group、category、quantity、绑定步骤、完整风险上下文、候选依据和异常码。
6. 不在正式文件中复制 pending，也不自动批准 main/supporting。

保留现有耦合 schema 的代价，是必须给 6,791 条无证据记录猜 main/supporting；这违反当前 fail-closed 设计，不建议采用。

## 解耦后的最省人工批次

1. 校验当前 occurrence ID、元数据和 optional/one_of/process-material 上游排除。
2. 生成 pending usage 候选：909 条油、1,312 条主料、298 条辅料。
3. 生成 retention 候选：4,857 条有 step binding 且无丢弃/抽滤风险的固体作为高置信 `retained=true` 待审批次。
4. 单独审阅 1,107 条液体和 909 条油的 retention，优先炸制、刷油、焯水、倒掉场景。
5. 审阅 1,873 条风险固体和 1,671 条无 binding 记录。
6. 对当前 6,403 条 seasoning 做 exception-only 审计，优先 350 条直接 discard/filter 风险。
7. usage/retention 收敛后重生 edible 候选；先审核明确净料 form，再补 175 个“空+非空 form”ingredient，最后查约 252 个整料/带骨/带壳/带皮风险键的权威可食部资料。
8. 每批后重新生成候选并运行重复键、pending 正式行、unknown occurrence 和闭环门禁审计。

## 本轮需要 owner 决定

1. 是否批准“retention 与 usage 解耦”的架构修正。建议批准。
2. 是否按上表处理 15 条稳定数量规则：2 条保留、1 条拒绝、5 条外部替换、7 条逐项人工决定。

在这两个决定前，不写任何 approved/modified 正式行，不运行 Task 6 dry-run，也不开始下一份营养 crosswalk 计划。
