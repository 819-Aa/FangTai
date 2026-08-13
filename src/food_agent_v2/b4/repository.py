"""B4 健康数据 Repository（T12）。

从 data_builds 唯一 ready 构建 + fixed_artifact_records 读取健康关系全集与覆盖
（health_relation_decisions 为 constraint_code × ingredient_id 完整矩阵，
health_relation_coverage 为每码完整覆盖记录）。不再读取 JSONL/CSV（INV-023）。
"""

from __future__ import annotations

from typing import Protocol

from food_agent_v2.b3.repository import MySQLArtifactRecordSource, RepositoryError


class HealthRecordSource(Protocol):
    def ready_build_id(self) -> str: ...
    def records(self, artifact_name: str, build_id: str) -> list[dict]: ...


class HealthDataRepository:
    """固定健康关系与覆盖的内存只读索引。"""

    def __init__(self, source: HealthRecordSource | None = None) -> None:
        self._source = source or MySQLArtifactRecordSource()
        self._build_id: str | None = None

    def ready_build_id(self) -> str:
        if self._build_id is None:
            self._build_id = self._source.ready_build_id()
        return self._build_id

    def relations(self, build_id: str) -> dict[str, set[int]]:
        """constraint_code → 硬排除食材 id 集合（仅 approved hard_exclude 决定）。

        INV-003：只有 review_status=approved 的 hard_exclude 关系才能触发硬排除；
        pending/rejected 关系绝不进入硬排除集合。
        """
        decisions = self._source.records("health_relation_decisions", build_id)
        relations: dict[str, set[int]] = {}
        for decision in decisions:
            if (decision.get("decision") == "hard_exclude"
                    and decision.get("review_status") == "approved"):
                relations.setdefault(decision["constraint_code"], set()).add(
                    int(decision["ingredient_id"])
                )
        if not relations:
            raise RepositoryError("HEALTH_RELATION_SET_EMPTY", "健康关系全集为空")
        return relations

    def coverage(self, build_id: str) -> dict[str, dict]:
        """constraint_code → 覆盖信息（covered_ingredient_ids + 状态）。"""
        coverage_records = self._source.records("health_relation_coverage", build_id)
        decisions = self._source.records("health_relation_decisions", build_id)

        covered_by_code: dict[str, set[int]] = {}
        for decision in decisions:
            covered_by_code.setdefault(decision["constraint_code"], set()).add(
                int(decision["ingredient_id"])
            )

        coverage: dict[str, dict] = {}
        for record in coverage_records:
            code = record["constraint_code"]
            coverage[code] = {
                "status": record.get("coverage_status"),
                "covered_ingredient_ids": sorted(covered_by_code.get(code, set())),
                "relation_count": record.get("relation_count"),
                "reviewed_ingredient_count": record.get("reviewed_ingredient_count"),
            }
        if not coverage:
            raise RepositoryError("HEALTH_COVERAGE_EMPTY", "健康覆盖记录为空")
        return coverage
