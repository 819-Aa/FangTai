# V2 原始投料营养与软评分模块设计

- 状态：`APPROVED`
- 更新日期：2026-08-23
- 适用模块：B1、B6、C2

## 1. 当前契约

营养只用于 B4 安全候选之间的软比较，不参与健康硬筛选，不计算烹饪损耗、得率、份数、人均或个人摄入。运行时 `nutrition_features` 不含来源、confidence、match method、coverage ratio 或解释字段；这些审计信息只存在于离线 reference/review 文件。

## 2. 权威输入

每个进入基础营养的食材 occurrence 必须同时具备：

- required 或已批准的默认 one-of 选择；optional/process material 不进入基础营养；
- `approved/modified` 单值克重；
- 已批准可食比例；
- 唯一已批准 nutrition crosswalk；
- 完整九维营养参考。

九维字段固定为能量、蛋白质、脂肪、碳水、膳食纤维、钠、钾、钙、铁；结构性零值必须来自批准参考，不能由缺失值推断。

## 3. All-or-nothing

任一前置事实缺失时，该菜必须发布：

```text
available = false
reason = <closed build reason>
raw_edible_input_weight_g = null
raw_nutrition_total = null
raw_nutrition_per_100g = null
```

全部前置事实完整时才发布 `available=true` 的完整九维总量和每 100g 向量。禁止部分向量、默认 `0.5`、均值或模型补值。`G14_NUTRITION_ALL_OR_NOTHING` 对此 fail-closed。

## 4. B6 与 C2

- B6 只能消费 B4 的 `safe_recipe_ids`。
- 没有显式 nutrition goal 时，不生成营养总分。
- 有显式目标时，B6 只在当前 safe 集合内计算目标方向和分位比较。
- 单菜 `available=false` 时不返回伪造数值分；C2 禁用该维度并对其余可用软目标重归一化。
- 营养结果不能生成、触发或抵消 B4 健康关系命中。

## 5. 运行时 Artifact

`nutrition_features` 以 `recipe_id` 为粒度，schema version 为 `2.0.0`。H05 当前覆盖 `1932` 个 eligible 菜品，允许 `available=false`，但 recipe ID 集合必须与 RAG/time/consumer views 精确一致。

## 6. 验收

- 四类缺失前置事实都产生完整 null 向量；
- available 记录恰有九个非负字段；
- runtime payload 不含 source/confidence/range；
- B4 输入不含营养值，B6 只消费 safe 集合；
- MySQL 固定记录与 manifest 逐项一致。
