# V2 数据工程人工审阅清单

生成日期：2026-08-21
数据范围：仅 `program_v2/data/raw/recipes_sample_2000.csv` 与中国疾控、USDA FoodData Central 官方参考；未读取或搬用 `datas`。
状态规则：除项目所有者已明确批准的 2 条克重修正、1 条菜谱分类和 21 道时间图决定外，其余模型候选全部保持 `pending`；程序自动批准数为 0。

## 已落地的项目所有者决定

| 范围 | 决定 | 生效结果 |
|---|---|---|
| recipe 147「碗蒸小酥肉」 | `葱 5h` 修改为 `5g` | `147-7` 写入 `ingredient_quantity_decisions.csv`，状态 `modified` |
| recipe 1502「芝心鸡腿」 | `鸡腿 400个` 修改为 `400g` | `1502-1` 写入 `ingredient_quantity_decisions.csv`，状态 `modified` |
| recipe 1579「停刀烧煮」 | 分类为 `cooking_program` | 不再进入 RAG、健康、营养或时长 eligible dish 集合 |
| 21 道时间图冲突菜 | P1–P5 全部批准 | 44 条原子决定生效，1,913 道 eligible dish 时间图全部通过 |

当前 eligible dish 为 1,913 道；剩余模糊克重候选为 4,372 条。上述决定均已从待审候选中消失。

## 已完成的确定性与官方数据处理

- 继续保留括号内逗号不拆食材、历史食材 ID 不复用等既有修复。
- 导入 USDA FoodData Central Foundation Foods 2026-04 共 469 条、SR Legacy 2018-04 共 7,793 条；连同中国疾控 1,356 条，正式离线参考共 9,618 条。
- USDA 原始归档下载 URL、版本、SHA-256、记录数和导入异常写入 `data/reference/usda_fooddata_central_manifest.json`。
- Foundation 中 10 条肉/鱼记录的差值碳水为轻微负数；未改成 0，按缺失值保留，并在 manifest 中逐条记录。
- USDA fallback 使用“受限英文身份检索词 → BGE-M3 Top-20 → 封闭模型选择或 null”，模型不能创造 USDA ID 或营养值。
- 时长生成器显式携带完整 JSON Schema；确定性处理结束/享用/说明文字，并统一发酵、冷藏、冷冻、浸泡等 passive 资源语义。每道失败菜最多再生成一次。

## 当前候选覆盖

| 范围 | 结果 |
|---|---:|
| 菜品画像候选 | 2,000 道，全部 pending |
| 餐次标签非空 | 2,000 / 2,000 |
| 模糊克重候选 | 4,372 项，覆盖 1,501 道菜，全部 pending |
| 中国疾控名称/别名 crosswalk 键 | 1,790 个食材/形态键 |
| 有中国疾控候选的键 | 398 |
| 中国疾控无候选、进入 USDA fallback 的键 | 1,392 |
| USDA fallback 选出待审候选 | 736 |
| USDA fallback 明确无精确匹配 | 656 |
| fallback 后仍无候选 | 656 |
| `food_origin` 待审候选 | 904 个唯一参考，全部 pending |
| 时间任务图已通过 | 1,913 / 1,913 道 eligible dish |
| 时间任务图剩余冲突 | 0 道 |

## 仍需项目所有者决定

### 1. 菜品画像

2,000 条画像均为 `pending`。餐次全部非空，模型没有新增源 label 之外的敏感人口标签。需要批量批准或逐条修改后，才能写入正式画像审阅文件。

### 2. 剩余模糊克重

4,372 个单值候选仍需批准或修改，才可参与营养计算。所有候选均为正数、无重复 occurrence、无超过 5,000g 的异常值；已批准的 `5h` 与 `400个` 不再计入未知单位或超限异常。

### 3. 营养 crosswalk 与食物来源

- 中国疾控名称/别名候选共 2,014 行；101 个键存在多个参考候选，必须选定唯一食材身份与形态。
- USDA fallback 共 1,392 行，其中 736 行给出一个待审 FDC 候选，656 行保持空；不允许用近似食材替代。
- 904 个 `food_origin` 候选分布为 plant 606、animal 244、mixed 49、unknown 5，全部 pending。
- 正式参考中的 9,618 个 `food_origin` 仍全部是 `unknown`，因此尚未应用“植物胆固醇=0”或“动物膳食纤维=0”。
- 只有 crosswalk 与 `food_origin` 得到批准后，结构性零值和菜品 all-or-nothing 原始营养才可进入正式构建。

### 4. 时间任务图（本轮已完成）

原 21 道、36 个冲突已按项目所有者批准的 P1–P5 定向处理；结构修复只作用于该批菜谱，既有非目标缓存保持兼容。`time_graph_conflicts.jsonl` 当前为 0 行。无米“炒饭”已锁定为前段烤 210 秒、翻拌 30 秒、后段烤 210 秒，平底锅任务可与无人值守烤箱任务并行但仍占用唯一 `cook`。

## 当前门禁结论

1. `automatic_approvals=0`，所有新生成候选仍为 `pending`。
2. 2 条克重修正、recipe 1579 分类和 21 道时间图决定已生效。
3. USDA 参考事实可用，但 crosswalk 与 `food_origin` 尚未获批，不能触发正式营养计算。
4. 时间图门禁已通过；正式 `data-rebuild`/发布仍应因画像、克重、营养 crosswalk 与食物来源待审而失败。
5. 未获得生产发布授权前，不切换当前 active MySQL/Qdrant build。
