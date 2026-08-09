# ADR-0002：工具桥接层伪造数据修复

- 日期：2026-08-07
- 状态：已修复并验证
- 相关：C3 runner / tool_handler / 全链路审查报告

## 背景

全链路审查（2026-08-07）发现 C3 的 `tool_handler.py` 中，模型调用工具时返回的数据是临时拼造的，而非真实领域服务产出。审查共发现 16 项文档与代码差异，根因追溯为单一的桥接层问题。

## 问题根因

三层代码写完之后，C3 到 B 模块的桥接（`tool_handler.py`）没有接上真实服务。模型在工具调用循环中收到的是虚构数据：

| 工具 | 旧行为 | 影响 |
|---|---|---|
| `get_health_constraints` | user_id 硬编码为 `enumerate(start=1)` | 请求中的参与者→用户映射被忽略 |
| `evaluate_recipe_health` | ingredient_map 编造为 `[rid, rid+100, rid+200]` | 健康审查基于不存在的食材ID |
| `generate_feasible_menus` | 无 safe_recipe_ids 时默认 `range(1,51)` | 菜单方案包含未经B4审查的菜品 |
| `get_execution_trace` | 永远返回空数组 | 审查模型无法验证工具合规性 |
| `expand_retrieval` | 未映射 | 模型调用时报 TOOL_PERMISSION_DENIED |
| `adjust_menu_plan` | 未映射 | 同上 |

此外，C3 的 `NodeValidator.pre_check/post_check` 虽然已定义，但从未被 runner 调用——必需工具漏调不会导致工作流失败。

## 修复方案

### 文件变更

| 文件 | 改动 |
|---|---|
| `b1/health_relation_builder.py` | `_ingredient_matches_pattern` 从子字符串包含改为词边界匹配，防止"蟹味菇"误命中"蟹" |
| `b3/identity_resolver.py` | 移除 `normalized_short` 模糊 fallback（2-4 字前缀匹配），只保留精确匹配 + 数量归一化 |
| `c3/tool_handler.py` | 完全重写。10 个工具全部映射到真实 B2/B3/B4/C1/C2 服务函数 |
| `c3/runner.py` | 补 NodeValidator 前后置校验，工具回执写入 WorkflowState，Artifact 保存，C4 request_id 外部传入 |
| `c4/__init__.py` | `build_shared_context` 从外部接收 request_id 而非自行生成 |

### 工具映射现状

所有 10 个工具均映射到真实函数：

| 工具 | 调用的真实服务 |
|---|---|
| `retrieve_recipes` | `C1.RetrievalService.retrieve()` — BM25 + Qdrant 混合检索 |
| `get_current_menu` | C4 上下文读取（当前为 stub，待 C4 完善） |
| `get_health_constraints` | `B2.UserHealthProfileService.derive_constraints()` — 从档案派生约束 |
| `evaluate_recipe_health` | `B3.RecipeViewBuilder.build_health_ingredient_view()` + `B4.HealthRuleEngine.evaluate_batch()` |
| `generate_feasible_menus` | `C2.MenuPlanner.plan()` — 多目标菜单规划 |
| `expand_retrieval` | `C1.RetrievalService.retrieve()` — 排除已有结果后二次检索 |
| `adjust_menu_plan` | `C2.MenuPlanner.adjust_menu()` — 替换菜品 |
| `validate_selected_menu_health` | `B4.HealthRuleEngine.validate_selected_menu()` — 重新加载 B2/B3 约束后校验 |
| `get_execution_trace` | 从 `WorkflowState.tool_receipts` 投影 |
| `get_artifact_chain` | 从 `ToolContext.previous_results` 构建引用链 |

### 运行时保障

- **NodeValidator.pre_check**：每个模型节点执行前检查前置 Artifact 是否存在
- **NodeValidator.post_check**：每个模型节点执行后检查必需工具是否全部调用
- **工具权限**：`_call_model` 在执行工具前检查允许列表，越权返回 TOOL_PERMISSION_DENIED
- **工具回执**：`ToolHandler.execute()` 同时写入 `ToolContext.tool_receipts`（请求级记录）

### 关联不变量验证

- INV-007（必需工具漏调停止）：post_check 强制执行
- INV-011（多人全员健康交集）：B4.evaluate_batch 逐参与者逐菜品评估
- INV-001（最终菜单全员健康校验）：validate_selected_menu_health 重新加载 B2 约束
- INV-005（回答不改变已验证菜单）：AnswerArtifact.validate 校验菜品⊆菜单
