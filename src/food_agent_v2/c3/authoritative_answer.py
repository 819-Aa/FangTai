"""C3 权威回答构建器（P3）—— 确定性回答，替代 answer_generation 模型。

只消费已通过最终校验的菜单身份（MenuDecisionArtifact + FinalValidationArtifact）
与同一 ready build 的公开菜品视图，生成结构化 AnswerArtifact。不输出疾病、指标、
参与者身份、营养数值等禁止字段；不依赖模型润色（润色为可选 P6，独立于本模块）。
"""

from __future__ import annotations

import uuid

from food_agent_v2.application.menu_projection import build_public_menu
from food_agent_v2.contracts.artifacts import (
    AnswerArtifact,
    AnswerContent,
    FinalValidationArtifact,
    MenuDecisionArtifact,
)
from food_agent_v2.contracts.build import canonical_json_hash


def _content_hash(answer: AnswerArtifact) -> str:
    """对语义内容做规范 hash（排除身份/指纹字段，同 runner._content_hash 约定）。"""
    payload = answer.model_dump(
        exclude={"content_hash", "artifact_id", "request_id"})
    return canonical_json_hash(payload)


def compute_changes(previous, current):
    """计算菜单变更的精确 recipe_id 差异，返回 (removed, added)。"""
    prev = set(int(r) for r in (previous or ()))
    curr = set(int(r) for r in (current or ()))
    removed = tuple(sorted(prev - curr))
    added = tuple(sorted(curr - prev))
    return removed, added


class AuthoritativeAnswerBuilder:
    """确定性回答构建器：直接消费已通过最终校验的菜单身份。"""

    @staticmethod
    def build(
        decision: MenuDecisionArtifact,
        final: FinalValidationArtifact,
        build_id: str,
        time_note: str = "",
        previous_recipe_ids: tuple[int, ...] | None = None,
    ) -> AnswerArtifact:
        menu_items = build_public_menu(list(final.recipe_ids), build_id)
        names = [it["name"] for it in menu_items]

        conclusion = f"为您推荐以下 {len(names)} 道菜：{'、'.join(names)}。"
        menu_summary = "、".join(names)
        reasoning_summary = (
            "菜品已结合当前参与者的饮食约束与健康审查结果筛选，"
            "并兼顾口味与菜式多样化。")
        if previous_recipe_ids is not None:
            removed, added = compute_changes(previous_recipe_ids, final.recipe_ids)
            if removed or added:
                parts = []
                if removed:
                    parts.append(f"移除 {len(removed)} 道不再满足约束的菜")
                if added:
                    parts.append(f"新增 {len(added)} 道符合约束的菜")
                reasoning_summary = (
                    "已按新增约束最小调整：" + "，".join(parts) + "，其余保持不变。")
        content = AnswerContent(
            conclusion=conclusion,
            menu_summary=menu_summary,
            reasoning_summary=reasoning_summary,
            health_note="",
            time_note=time_note,
        )

        answer = AnswerArtifact(
            artifact_id=uuid.uuid4(),
            request_id=final.request_id,
            plan_id=final.plan_id,
            menu_ref=decision.feasible_menu_artifact_ref,
            final_validation_ref=str(final.artifact_id),
            recipe_ids=tuple(final.recipe_ids),
            menu_hash=final.menu_hash,
            content=content,
            evidence_refs=(str(final.artifact_id),),
            content_hash="0" * 64,
        )
        return answer.model_copy(update={"content_hash": _content_hash(answer)})
