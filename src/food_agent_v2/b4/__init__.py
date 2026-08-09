"""B4 健康规则与审查引擎 —— 二元 PASS/EXCLUDE 评估。

基于 B1 离线构建的约束—食材关系表，
对候选菜品执行逐参与者、逐菜品的确定性健康审查。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from food_agent_v2.core.paths import CLEANED_DIR
from food_agent_v2.b2 import (
    ParticipantHealthConstraintSet,
    CodedHealthConstraint,
    ExplicitFoodTabooConstraint,
    ConstraintScope,
)


# ---- 错误码 ----
class NUTRITION_HEALTH_BOUNDARY_VIOLATION(Exception):
    """营养值进入 B4 输入。"""


class HEALTH_INGREDIENT_SET_INCOMPLETE(Exception):
    """health_ingredient_ids 少于 B3 RecipeHealthIngredientView 的完整并集。"""


class HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE(Exception):
    """某个 constraint_code 的覆盖审核未完成。"""


# ---- 评估结果 ----

@dataclass
class RecipeHealthResult:
    """单道菜品对单个参与者的健康评估结果。"""
    recipe_id: int
    participant_ref: str
    verdict: str                    # PASS | EXCLUDE
    hitting_constraints: list[dict] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class HealthEvaluationReceipt:
    """候选批次健康审查回执。"""
    evaluation_id: str
    request_id: str | None
    retrieval_result_ref: str | None
    constraint_set_refs: list[str]
    participant_recipe_results: list[RecipeHealthResult]  # 逐参与者逐菜品
    safe_recipe_ids: list[int]
    excluded_recipe_ids: list[int]
    input_fingerprint: str
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class FinalValidationResult:
    """最终健康校验结果。"""
    request_id: str
    plan_id: str
    verdict: str                   # PASS | EXCLUDE
    menu_hash: str
    participant_recipe_results: list[RecipeHealthResult]
    evidence_refs: list[str] = field(default_factory=list)


class HealthRuleEngine:
    """健康规则引擎 —— 确定性二元评估。

    关系表结构（从 B1 离线构建）：
    constraint_code × ingredient_id → approved/rejected
    """

    def __init__(self):
        self._relations: dict[str, set[int]] = {}       # constraint_code → set of excluded ingredient_ids
        self._coverage: dict[str, int] = {}             # constraint_code → expected coverage count
        self._loaded = False

    def load_relations(self, path: Optional[Path] = None) -> None:
        """加载约束—食材关系表 + INV-018 覆盖记录。"""
        if path is None:
            path = CLEANED_DIR / "health_relations.jsonl"

        self._relations.clear()
        self._coverage.clear()
        if not path.exists():
            raise HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE(
                f"Health relations file not found: {path}"
            )

        loaded = 0
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                code = rec["constraint_code"]
                iid = rec["ingredient_id"]
                if rec.get("review_status") == "approved" and rec.get("hard_filter", True):
                    if code not in self._relations:
                        self._relations[code] = set()
                    self._relations[code].add(iid)
                    loaded += 1

        if loaded == 0:
            raise HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE(
                "No approved health relations loaded"
            )

        # INV-018：加载覆盖记录（每码针对全部可食用标准食材的完整审核集）
        coverage_path = CLEANED_DIR / "health_relation_coverage.jsonl"
        if not coverage_path.exists():
            raise HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE(
                f"Health relation coverage file not found: {coverage_path}"
            )
        with coverage_path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                code = rec["constraint_code"]
                if rec.get("review_status") == "complete":
                    self._coverage[code] = set(rec.get("covered_ingredient_ids", []))
        if not self._coverage:
            raise HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE(
                "No health relation coverage records loaded"
            )
        self._loaded = True

    def evaluate_recipe(
        self,
        recipe_id: int,
        ingredient_ids: list[int],
        constraints: list[CodedHealthConstraint | ExplicitFoodTabooConstraint],
        participant_ref: str,
    ) -> RecipeHealthResult:
        """单道菜对单个参与者的健康评估。"""
        if not self._loaded:
            raise HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE("Relations not loaded")

        hitting: list[dict] = []

        for constraint in constraints:
            # 显式食材禁忌——通过 B3 解析为 ingredient_id 后精确比对
            if isinstance(constraint, ExplicitFoodTabooConstraint):
                taboo_name = constraint.taboo_ingredient_name
                if taboo_name:
                    from food_agent_v2.b3.identity_resolver import get_resolver
                    resolver = get_resolver()
                    identity = resolver.resolve(taboo_name)
                    if identity.identity == "resolved" and identity.ingredient_id:
                        taboo_iid = identity.ingredient_id
                        if taboo_iid in ingredient_ids:
                            hitting.append({
                                "constraint_code": f"taboo:{taboo_name}",
                                "ingredient_id": taboo_iid,
                                "source_refs": constraint.source_refs,
                            })
                continue

            # 编码化约束
            if isinstance(constraint, CodedHealthConstraint):
                code = constraint.constraint_code
                # INV-018：该码必须有完整覆盖记录，否则是系统失败（不是 PASS）
                if code not in self._coverage:
                    raise HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE(
                        f"constraint_code 无覆盖记录: {code}"
                    )
                # 菜品的每个食材都必须已审核；未审核食材 → 覆盖缺失，不是 PASS
                covered = self._coverage[code]
                for iid in ingredient_ids:
                    if iid not in covered:
                        raise HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE(
                            f"constraint_code {code} 未覆盖食材 id={iid}"
                        )
                excluded_ids = self._relations.get(code, set())
                for iid in ingredient_ids:
                    if iid in excluded_ids:
                        hitting.append({
                            "constraint_code": code,
                            "ingredient_id": iid,
                            "source_refs": constraint.source_refs,
                        })

        if hitting:
            return RecipeHealthResult(
                recipe_id=recipe_id,
                participant_ref=participant_ref,
                verdict="EXCLUDE",
                hitting_constraints=hitting,
                evidence_refs=[f"constraint_hit:{h['constraint_code']}:{h['ingredient_id']}"
                              for h in hitting],
            )
        else:
            return RecipeHealthResult(
                recipe_id=recipe_id,
                participant_ref=participant_ref,
                verdict="PASS",
                evidence_refs=[f"recipe_pass:{recipe_id}"],
            )

    def evaluate_batch(
        self,
        recipe_ids: list[int],
        recipe_ingredient_map: dict[int, list[int]],
        constraint_sets: dict[str, list[CodedHealthConstraint | ExplicitFoodTabooConstraint]],
    ) -> HealthEvaluationReceipt:
        """候选批次健康审查。"""
        results: list[RecipeHealthResult] = []
        safe: set[int] = set(recipe_ids)
        excluded: set[int] = set()

        for rid in recipe_ids:
            ingredient_ids = recipe_ingredient_map.get(rid, [])
            for participant_ref, constraints in constraint_sets.items():
                result = self.evaluate_recipe(rid, ingredient_ids, constraints, participant_ref)
                results.append(result)
                if result.verdict == "EXCLUDE":
                    safe.discard(rid)
                    excluded.add(rid)

        return HealthEvaluationReceipt(
            evaluation_id=f"eval_{len(results)}",
            request_id=None,
            retrieval_result_ref=None,
            constraint_set_refs=list(constraint_sets.keys()),
            participant_recipe_results=results,
            safe_recipe_ids=sorted(safe),
            excluded_recipe_ids=sorted(excluded),
            input_fingerprint=f"recipes:{len(recipe_ids)}_constraints:{sum(len(c) for c in constraint_sets.values())}",
        )

    def validate_selected_menu(
        self,
        selected_recipe_ids: list[int],
        recipe_ingredient_map: dict[int, list[int]],
        constraint_sets: dict[str, list[CodedHealthConstraint | ExplicitFoodTabooConstraint]],
        plan_id: str,
        menu_hash: str,
    ) -> FinalValidationResult:
        """最终健康校验（原子提交前）。"""
        batch = self.evaluate_batch(selected_recipe_ids, recipe_ingredient_map, constraint_sets)

        all_pass = all(r.verdict == "PASS" for r in batch.participant_recipe_results)

        return FinalValidationResult(
            request_id="",
            plan_id=plan_id,
            verdict="PASS" if all_pass else "EXCLUDE",
            menu_hash=menu_hash,
            participant_recipe_results=batch.participant_recipe_results,
            evidence_refs=batch.evidence_refs,
        )

    @property
    def constraint_count(self) -> int:
        return len(self._relations)

    @property
    def total_exclusion_pairs(self) -> int:
        return sum(len(ids) for ids in self._relations.values())
