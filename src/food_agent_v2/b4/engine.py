"""B4 健康规则引擎（T12）。

- PASS/EXCLUDE 只在固定关系全集与覆盖完整时产生；缺关系/缺覆盖/未覆盖食材
  一律抛确定性错误，绝不产出业务结论（fail-closed）。
- 明确食材禁忌必须预绑定标准 ingredient_id（T10），不在此做运行时关键词解析；
  已删除关键词/类别/模型/营养分推断硬命中的路径（R-001/R-003/R-018）。
- 最终复核（validate_selected_menu）重新执行同一评估核心并绑定 menu_hash。
"""

from __future__ import annotations

from food_agent_v2.b2.schemas import (
    CodedHealthConstraint,
    ExplicitFoodTabooConstraint,
)
from food_agent_v2.b4.repository import HealthDataRepository
from food_agent_v2.b4.schemas import (
    FinalValidationResult,
    HealthEvaluationReceipt,
    RecipeHealthResult,
)


class NUTRITION_HEALTH_BOUNDARY_VIOLATION(Exception):
    """营养值进入 B4 输入。"""


class HEALTH_INGREDIENT_SET_INCOMPLETE(Exception):
    """health_ingredient_ids 少于完整并集，或明确禁忌缺少 ingredient_id。"""


class HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE(Exception):
    """某个 constraint_code 的覆盖审核未完成。"""


class HealthRuleEngine:
    """确定性二元健康评估引擎。"""

    def __init__(self, repository: HealthDataRepository | None = None) -> None:
        self._repository = repository or HealthDataRepository()
        self._relations: dict[str, set[int]] = {}
        self._coverage: dict[str, set[int]] = {}
        self._coverage_complete: set[str] = set()
        self._loaded = False

    def load_relations(self, path=None) -> None:
        """从 Repository 加载关系全集与覆盖（path 兼容旧签名，数据来源是 Repository）。"""
        build_id = self._repository.ready_build_id()
        self._relations = self._repository.relations(build_id)
        coverage = self._repository.coverage(build_id)
        self._coverage = {
            code: set(info["covered_ingredient_ids"])
            for code, info in coverage.items()
        }
        self._coverage_complete = {
            code for code, info in coverage.items() if info.get("status") == "complete"
        }
        if not self._relations or not self._coverage:
            raise HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE("健康关系或覆盖为空，无法评估")
        self._loaded = True

    def evaluate_recipe(
        self,
        recipe_id: int,
        ingredient_ids: list[int],
        constraints: list[CodedHealthConstraint | ExplicitFoodTabooConstraint],
        participant_ref: str,
    ) -> RecipeHealthResult:
        """单道菜对单个参与者的健康评估；数据不完整时 fail-closed。"""
        if not self._loaded:
            raise HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE("Relations not loaded")

        hitting: list[dict] = []

        for constraint in constraints:
            if isinstance(constraint, ExplicitFoodTabooConstraint):
                # 明确禁忌必须预绑定 ingredient_id；不在此做运行时解析。
                taboo_iid = constraint.taboo_ingredient_id
                if taboo_iid is None:
                    raise HEALTH_INGREDIENT_SET_INCOMPLETE(
                        f"明确食材禁忌缺少 ingredient_id: {constraint.taboo_ingredient_name}"
                    )
                if taboo_iid in ingredient_ids:
                    hitting.append(
                        {
                            "constraint_code": f"taboo:{constraint.taboo_ingredient_name}",
                            "ingredient_id": taboo_iid,
                            "source_refs": constraint.source_refs,
                        }
                    )
                continue

            if isinstance(constraint, CodedHealthConstraint):
                code = constraint.constraint_code
                if code not in self._coverage_complete:
                    raise HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE(
                        f"constraint_code 覆盖未完成: {code}"
                    )
                covered = self._coverage.get(code, set())
                for ingredient_id in ingredient_ids:
                    if ingredient_id not in covered:
                        raise HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE(
                            f"constraint_code {code} 未覆盖食材 id={ingredient_id}"
                        )
                excluded = self._relations.get(code, set())
                for ingredient_id in ingredient_ids:
                    if ingredient_id in excluded:
                        hitting.append(
                            {
                                "constraint_code": code,
                                "ingredient_id": ingredient_id,
                                "source_refs": constraint.source_refs,
                            }
                        )

        if hitting:
            return RecipeHealthResult(
                recipe_id=recipe_id,
                participant_ref=participant_ref,
                verdict="EXCLUDE",
                hitting_constraints=hitting,
                evidence_refs=[
                    f"constraint_hit:{h['constraint_code']}:{h['ingredient_id']}"
                    for h in hitting
                ],
            )
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

        for recipe_id in recipe_ids:
            if recipe_id not in recipe_ingredient_map:
                raise HEALTH_INGREDIENT_SET_INCOMPLETE(
                    f"缺失菜品食材集合（不能按空集评估）: recipe {recipe_id}"
                )
            ingredient_ids = recipe_ingredient_map[recipe_id]
            for participant_ref, constraints in constraint_sets.items():
                result = self.evaluate_recipe(
                    recipe_id, ingredient_ids, constraints, participant_ref
                )
                results.append(result)
                if result.verdict == "EXCLUDE":
                    safe.discard(recipe_id)
                    excluded.add(recipe_id)

        return HealthEvaluationReceipt(
            evaluation_id=f"eval_{len(results)}",
            request_id=None,
            retrieval_result_ref=None,
            constraint_set_refs=list(constraint_sets.keys()),
            participant_recipe_results=results,
            safe_recipe_ids=sorted(safe),
            excluded_recipe_ids=sorted(excluded),
            input_fingerprint=(
                f"recipes:{len(recipe_ids)}_"
                f"constraints:{sum(len(c) for c in constraint_sets.values())}"
            ),
        )

    def validate_selected_menu(
        self,
        selected_recipe_ids: list[int],
        recipe_ingredient_map: dict[int, list[int]],
        constraint_sets: dict[str, list[CodedHealthConstraint | ExplicitFoodTabooConstraint]],
        plan_id: str,
        menu_hash: str,
    ) -> FinalValidationResult:
        """最终健康复核：重新执行同一核心并绑定 menu_hash（INV-001）。"""
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
