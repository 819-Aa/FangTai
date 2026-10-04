"""Agent 提示词构建器：受约束的决策提示与观察历史格式化（Section 9）。"""

from __future__ import annotations

import json
from typing import Any

from food_agent_v2.c3.agent_actions import Observation
from food_agent_v2.contracts.artifacts import QueryPlanArtifact

AGENT_DECISION_SYSTEM_PROMPT = """你是一个专业的餐饮健康推荐 Agent。
你的任务是根据用户的需求和当前会话的上下文，自主决定下一步采取什么行动。

你可以选择以下行动之一：
1. read_menu: 读取当前菜单或上一版已提交菜单
   参数: {"target": "current" | "previous_committed"}
2. search_candidates: 根据需求检索候选菜品
   参数: {"query": "检索词", "meal_type": "餐次"}
3. expand_candidates: 补充搜索候选菜品（最多执行一次）
   参数: {"query": "新检索词", "retrieval_evidence_ref": "前次检索引用"}
4. audit_recipe_health: 对候选菜谱进行严格的健康与过敏禁忌审核
   参数: {"candidate_recipe_ids": [菜品ID列表]}
5. combine_nutritional_menu: 将安全菜品规划为营养均衡、耗时合规的可行菜单方案
   参数: {"dish_count": 菜品数量, "health_receipt_ref": "健康审核引用"}
6. validate_selected_menu: 对选定的可行菜单方案执行最终健康安全校验（INV-007）
   参数: {"plan_id": "方案ID", "recipe_ids": [菜品ID列表]}
7. ask_user: 当约束冲突、信息不足或规划无解时，向用户提出澄清与协商选项
   参数: {"inquiry_category": "类别", "reason": "详尽诊断原因", "options": [{"option_id": 1, "text": "选项文字", "modifications": {}}]}
   约束: options 必须严格包含 2 至 3 个结构化选项（严禁少于 2 项或多于 3 项）。modifications 仅支持修改: dish_count_requested, time_constraint_seconds, time_constraint_policy, meal_type, taste_tags。
8. finish: 在最终健康校验通过（PASS）后，提交方案并完成推荐
   参数: {"plan_id": "方案ID", "final_validation_ref": "最终校验引用"}

系统规则与边界约束：
- 严禁静默放宽用户的硬性要求（时间限制、菜品数量、健康禁忌）。
- 严禁凭空创造菜品或伪造健康安全集合。
- 检索得到候选后，必须先调用 audit_recipe_health 获得健康审核回执，再调用 combine_nutritional_menu；检索成功不等于审核通过，严禁跳过审核直接规划。
- 必须显式调用 validate_selected_menu 并获得 PASS 证据后，才能调用 finish。缺回执 finish 会被门卫直接拒绝。
- 最终健康校验返回 EXCLUDE 后，先重新 audit_recipe_health 获取当前安全候选，再进行一次健康修订规划；不得复用旧健康审核直接规划。
- 同一参数与证据的工具调用不得重复执行，这包括 combine_nutritional_menu。规划已返回可行方案时直接选择其中一份做 validate_selected_menu；规划无解或时间不满足时提出 ask_user 澄清，不得原样重新规划。
- 遇到局部替换 (replace) 意图：已有锁定菜品和排除菜品，应针对替换目标检索候选并规划，严禁反复执行 read_menu。
- 遇到恢复历史 (restore) 意图：应直接基于历史菜品快照执行健康复核与规划，严禁重新发起无关检索。
- evidence_refs 只能引用用户消息中“当前可用证据引用”列出的原样字符串；列表为空时必须填写 []，严禁自拟 user_request、restore_intent 等名称。
- 最终回复必须是一个非空、合法的 JSON 对象，包含 action、arguments、evidence_refs、summary 四个字段。
- 仅内部分析不算回复；完成思考后必须在最终正文中输出 JSON。不得返回空正文、Markdown 或省略号。
- 格式示例：用户要求清淡晚餐，尚未检索且无可用证据时，可输出以下 JSON：
{"action":"search_candidates","arguments":{"query":"清淡晚餐","meal_type":"晚餐"},"evidence_refs":[],"summary":"检索符合晚餐与清淡口味的候选菜品"}
- 示例仅说明合法格式；实际行动、参数和证据必须依据本轮需求与已发生的观察重新选择。已有候选时先健康审核，已有可行菜单时先最终健康校验，校验 PASS 后才 finish；不得重复已完成的同参数检索。
"""


def format_agent_prompt(
    query_plan: QueryPlanArtifact | None,
    observations: list[Observation],
    current_menu_summary: str | None = None,
    user_message: str | None = None,
    intent: Any | None = None,
    locked_recipes_summary: str | None = None,
    rejected_recipes_summary: str | None = None,
    available_evidence_refs: list[str] | set[str] | None = None,
) -> str:
    """构建输入给 Agent 模型的决策上下文提示词（脱敏，无系统内部隐私数据）。"""
    parts = []

    if user_message:
        parts.append(f"### 用户原始请求：\n\"{user_message.strip()}\"\n")

    intent_type = getattr(intent, "intent", None) if intent else None
    if intent_type:
        intent_names = {
            "new_recommendation": "新餐次推荐",
            "replace": "局部菜品替换",
            "restore": "恢复历史菜单",
            "add_constraint": "追加约束条件",
            "reject_plan": "整单重新推荐",
        }
        parts.append(f"### 识别意图类型：{intent_names.get(intent_type, intent_type)}")

    parts.append("### 当前需求与约束：")
    if query_plan:
        parts.append(f"- 目标餐次: {', '.join(query_plan.meal_types) if query_plan.meal_types else '未指定'}")
        parts.append(f"- 要求菜数: {query_plan.dish_count_requested or '默认 5 道菜'}")
        if query_plan.time_constraint_seconds:
            mins = round(query_plan.time_constraint_seconds / 60)
            parts.append(f"- 时间限制: {mins} 分钟 ({query_plan.time_constraint_policy})")
        if query_plan.taste_tags:
            parts.append(f"- 口味偏好: {', '.join(query_plan.taste_tags)}")
    else:
        parts.append("需求尚未结构化")

    if locked_recipes_summary:
        parts.append(f"- 必须保留的锁定菜品: {locked_recipes_summary}")
    if rejected_recipes_summary:
        parts.append(f"- 已明确排除的目标菜品: {rejected_recipes_summary}")

    if current_menu_summary:
        parts.append(f"\n### 当前已有菜单：\n{current_menu_summary}")

    refs = sorted(set(available_evidence_refs or []))
    parts.append(f"\n### 当前可用证据引用: {json.dumps(refs, ensure_ascii=False)}")
    parts.append("evidence_refs 只能从上述列表原样选取；列表为空时必须为 []。")

    parts.append("\n### 历史行动与工具观察结果 (Observations)：")
    if not observations:
        parts.append("（尚未执行任何工具）")
    else:
        for idx, obs in enumerate(observations, 1):
            facts_str = json.dumps(obs.desensitized_facts, ensure_ascii=False)
            parts.append(
                f"{idx}. 行动: {obs.action.value} | 状态: {obs.status} | 引用: {obs.evidence_ref or '无'}\n"
                f"   观察事实: {facts_str}\n"
                f"   消息: {obs.message or '正常'}"
            )

    parts.append("\n请决定你的下一步行动（输出单个 JSON 对象）：")
    return "\n".join(parts)
