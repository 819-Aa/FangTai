"""D2 回答与前端模块 —— 回答内容规则、用户可见分析、前端 SSE 状态机。

定义系统输出给用户的最后两段：
1. 回答模型应该说什么、不能说什么（INV-005 + INV-013 落地）
2. 前端如何消费 SSE 事件并展示（不推导、不补写、不暴露）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

# ---- 禁止表述检测 ----

FORBIDDEN_PATTERNS = [
    # 营养数值类
    (re.compile(r"\d+\s*(?:毫克|mg|克|g|千卡|kcal|千焦|kj|微克|mcg)"), "nutrition_value"),
    (re.compile(r"(?:含[钠钙铁锌]|蛋白质|脂肪|碳水|纤维|胆固醇)\s*\d+"), "nutrition_statement"),
    # 健康判断类
    (re.compile(r"(?:适合|不适合|对.*安全|对.*危险)(?:高血压|糖尿病|痛风|过敏|肾病)"), "health_judgment"),
    # 疾病指标类
    (re.compile(r"(?:血压|血糖|尿酸|胆固醇)\s*(?:偏高|偏低|异常|正常|\d+)"), "disease_indicator"),
    # 隐私侵犯类
    (re.compile(r"(?:参与者|用户)\s*[A-Za-z0-9]*\s*(?:有|患有|不能吃)"), "privacy_violation"),
]


def audit_answer_text(text: str) -> list[dict]:
    """审查回答文本是否含禁止表述。返回发现的违规列表。"""
    violations = []
    for pattern, category in FORBIDDEN_PATTERNS:
        matches = pattern.findall(text)
        for match in matches:
            violations.append({
                "category": category,
                "matched": str(match)[:80],
            })
    return violations


def validate_answer_dish_set(answer_dish_ids: list[int],
                             selected_menu_ids: list[int]) -> bool:
    """验证回答中的菜品 ID ⊆ 最终菜单（INV-005）。"""
    return set(answer_dish_ids).issubset(set(selected_menu_ids))


# ---- 用户可见分析摘要 ----

@dataclass
class UserVisibleAnalysis:
    stage: str       # query_understanding | health_evaluation | menu_planning | menu_decision
    title: str
    summary: str      # ≤ 150 字，不含疾病/指标/营养/身份
    evidence_refs: list[str] = field(default_factory=list)

    def validate(self) -> bool:
        if len(self.summary) > 150:
            return False
        # 不含疾病名、指标值、营养数值、参与者身份的关键词扫描
        forbidden_words = ["血压", "血糖", "尿酸", "胆固醇", "糖尿病", "高血压", "过敏",
                          "毫克", "千卡", "克蛋白质", "钠", "参与者", "用户ID"]
        for word in forbidden_words:
            if word in self.summary:
                return False
        return True


# ---- 前端 SSE 状态机 ----

class FrontendState(StrEnum):
    IDLE = "idle"
    LOADING = "loading"
    STREAMING_ANSWER = "streaming_answer"
    COMPLETE = "complete"
    ERROR = "error"


SSE_EVENT_TO_STATE = {
    "request_accepted": FrontendState.LOADING,
    "analysis_ready": FrontendState.LOADING,
    "answer_ready": FrontendState.STREAMING_ANSWER,
    "result_committed": FrontendState.COMPLETE,
    "error": FrontendState.ERROR,
    "request_cancelled": FrontendState.ERROR,
}


# 前端禁止行为定义
FRONTEND_DO_NOT = [
    "不要根据展示的菜品食材反向推断用户健康状态",
    "不要在前端渲染营养数值、per_serving、采购量",
    "不要在 UI 中显示'安全''不安全'标签",
    "不要在 error 事件到达时生成假回答",
    "不要在 answer_ready 未到达时自行写出菜单",
    "不要在多人场景下渲染具体参与者的健康原因",
    "不要渲染 user_id 或真实姓名，只显示 participant_ref 匿名标签",
    "如果 answer_ready payload 意外包含禁止字段，前端必须过滤而非直接渲染",
]


def map_sse_to_ui_state(event_type: str, current_state: str) -> str:
    """SSE 事件 → 前端 UI 状态。"""
    if event_type in SSE_EVENT_TO_STATE:
        return SSE_EVENT_TO_STATE[event_type].value
    return current_state


# ---- AnswerArtifact 结构 ----

@dataclass
class AnswerArtifact:
    artifact_id: str
    request_id: str
    plan_id: str
    menu_ref: str
    final_validation_ref: str
    content: dict = field(default_factory=dict)
    user_visible_analysis: list[UserVisibleAnalysis] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    content_hash: str = ""

    def validate(self) -> list[str]:
        """验证回答是否符合 D2 规则。"""
        errors = []
        if not self.plan_id:
            errors.append("missing plan_id")
        if not self.menu_ref:
            errors.append("missing menu_ref")

        # 审查回答文本
        text = self.content.get("conclusion", "") + \
               self.content.get("menu_summary", "") + \
               self.content.get("reasoning_summary", "") + \
               self.content.get("health_note", "")
        violations = audit_answer_text(text)
        if violations:
            for v in violations:
                errors.append(f"forbidden: {v['category']} - {v['matched']}")

        # 验证分析摘要
        for analysis in self.user_visible_analysis:
            if not analysis.validate():
                errors.append(f"invalid analysis: {analysis.stage}")

        return errors


# 允许/禁止表述的示例对
ALLOWED_EXPRESSIONS = [
    "选择了方案A，因为它在满足健康要求的同时更符合你的口味偏好",
    "部分候选菜品因与当前健康限制冲突被排除",
    "这道菜的主要食材包括：猪肉、青椒、木耳",
    "约45分钟完成",
]
FORBIDDEN_EXPRESSIONS = [
    "这道菜含钠800mg",
    "总能量约450千卡",
    "这道菜适合高血压患者",
    "因为你的血压偏高，所以...",
    "考虑到你的高尿酸...",
    "参与者A有花生过敏，所以...",
    "张三不能吃这道菜",
    "鱼香肉丝因为花生过敏被去掉了",
]
