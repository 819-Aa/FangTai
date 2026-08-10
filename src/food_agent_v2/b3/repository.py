"""B3 固定事实 Repository（T11）。

B3 只经 Repository 消费固定 Artifact（data_builds 唯一 ready 构建 +
fixed_artifact_records），不再解析原始食材字符串、不再读取 JSONL/CSV（INV-023）。
未知 ID、构建身份不一致、缺失记录或 Schema 错误一律 fail-closed。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from food_agent_v2.core.config import load_config


class RepositoryError(RuntimeError):
    """B3 Repository 确定性失败。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass
class RecipeHealthIngredientView:
    recipe_id: int
    ingredient_ids: list[int]
    ingredient_evidence_paths: list[str]
    unresolved_occurrence_count: int
    composition_expansion_status: str
    catalog_eligibility: str


@dataclass
class RecipeRetrievalView:
    recipe_id: int
    name: str
    ingredient_display_names: list[str]
    ingredient_ids: list[int]
    searchable_fields: dict[str, str]
    step_summary: str | None
    time_reference: str | None
    ingredient_family_ids: list[int]


@dataclass
class RecipeTimeView:
    recipe_id: int
    ingredient_ids: list[int]
    steps: list[dict] = field(default_factory=list)


class ArtifactRecordSource(Protocol):
    """固定 Artifact 记录源（MySQL 生产实现或测试 Fake）。"""

    def ready_build_id(self) -> str: ...
    def records(self, artifact_name: str, build_id: str) -> list[dict]: ...


class RecipeCatalogRepository(Protocol):
    def ready_build_id(self) -> str: ...
    def get_health_view(self, recipe_ids: Sequence[int], build_id: str) -> Sequence[RecipeHealthIngredientView]: ...
    def get_retrieval_view(self, recipe_ids: Sequence[int], build_id: str) -> Sequence[RecipeRetrievalView]: ...
    def get_time_view(self, recipe_ids: Sequence[int], build_id: str) -> Sequence[RecipeTimeView]: ...
    def registry_entries(self, build_id: str) -> list[dict]: ...
    def alias_entries(self, build_id: str) -> list[dict]: ...


class MySQLArtifactRecordSource:
    """从 MySQL data_builds / fixed_artifact_records 读取（H04 初始化结果）。"""

    def __init__(self) -> None:
        self._connection = None
        self._cursor = None

    def _connect(self):
        if self._connection is not None:
            return self._connection
        import pymysql

        cfg = load_config().mysql
        self._connection = pymysql.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
            charset="utf8mb4",
            autocommit=True,
        )
        self._cursor = self._connection.cursor()
        return self._connection

    @property
    def cursor(self):
        self._connect()
        return self._cursor

    def ready_build_id(self) -> str:
        self.cursor.execute("SELECT build_id FROM data_builds WHERE status='ready'")
        rows = self.cursor.fetchall()
        if len(rows) != 1:
            raise RepositoryError(
                "BUILD_IDENTITY_UNAVAILABLE",
                f"data_builds 中 ready 构建必须恰好一个，实际 {len(rows)}",
            )
        return str(rows[0][0])

    def records(self, artifact_name: str, build_id: str) -> list[dict]:
        self.cursor.execute(
            "SELECT payload FROM fixed_artifact_records "
            "WHERE build_id=%s AND artifact_name=%s ORDER BY record_index",
            (build_id, artifact_name),
        )
        records: list[dict] = []
        for (payload,) in self.cursor.fetchall():
            record = json.loads(payload)
            if str(record.get("build_id")) != build_id:
                raise RepositoryError(
                    "BUILD_IDENTITY_MISMATCH",
                    f"{artifact_name} 记录 build_id 与查询不一致",
                )
            records.append(record)
        return records


class FixedDataRepository:
    """内存只读固定事实索引（启动时按 Artifact 批量加载）。"""

    def __init__(self, source: ArtifactRecordSource) -> None:
        self._source = source
        self._build_id: str | None = None

    def ready_build_id(self) -> str:
        if self._build_id is None:
            self._build_id = self._source.ready_build_id()
        return self._build_id

    def _records(self, artifact_name: str, build_id: str) -> list[dict]:
        records = self._source.records(artifact_name, build_id)
        for record in records:
            if str(record.get("build_id")) != build_id:
                raise RepositoryError(
                    "BUILD_IDENTITY_MISMATCH",
                    f"{artifact_name} 记录 build_id 与查询不一致",
                )
        return records

    def get_health_view(self, recipe_ids: Sequence[int], build_id: str) -> list[RecipeHealthIngredientView]:
        rows = self._records("recipe_health_views", build_id)
        by_id = {int(r["recipe_id"]): r for r in rows}
        views: list[RecipeHealthIngredientView] = []
        for recipe_id in recipe_ids:
            record = by_id.get(int(recipe_id))
            if record is None:
                raise RepositoryError("UNKNOWN_RECIPE_ID", f"健康视图缺失 recipe {recipe_id}")
            views.append(
                RecipeHealthIngredientView(
                    recipe_id=int(record["recipe_id"]),
                    ingredient_ids=[int(i) for i in record.get("ingredient_ids", [])],
                    ingredient_evidence_paths=list(record.get("ingredient_evidence_paths", [])),
                    unresolved_occurrence_count=int(record.get("unresolved_occurrence_count", 0)),
                    composition_expansion_status=record.get("composition_expansion_status", "unknown"),
                    catalog_eligibility=record.get("catalog_eligibility", "unknown"),
                )
            )
        return views

    def get_retrieval_view(self, recipe_ids: Sequence[int], build_id: str) -> list[RecipeRetrievalView]:
        rows = self._records("recipe_retrieval_build_views", build_id)
        by_id = {int(r["recipe_id"]): r for r in rows}
        views: list[RecipeRetrievalView] = []
        for recipe_id in recipe_ids:
            record = by_id.get(int(recipe_id))
            if record is None:
                raise RepositoryError("UNKNOWN_RECIPE_ID", f"检索视图缺失 recipe {recipe_id}")
            views.append(
                RecipeRetrievalView(
                    recipe_id=int(record["recipe_id"]),
                    name=record.get("name", ""),
                    ingredient_display_names=list(record.get("ingredient_display_names", [])),
                    ingredient_ids=[int(i) for i in record.get("ingredient_ids", [])],
                    searchable_fields=dict(record.get("searchable_fields", {}) or {}),
                    step_summary=record.get("step_summary"),
                    time_reference=record.get("time_reference"),
                    ingredient_family_ids=[int(i) for i in record.get("ingredient_family_ids", [])],
                )
            )
        return views

    def get_time_view(self, recipe_ids: Sequence[int], build_id: str) -> list[RecipeTimeView]:
        rows = self._records("recipe_step_binding_views", build_id)
        by_id = {int(r["recipe_id"]): r for r in rows}
        views: list[RecipeTimeView] = []
        for recipe_id in recipe_ids:
            record = by_id.get(int(recipe_id))
            if record is None:
                raise RepositoryError("UNKNOWN_RECIPE_ID", f"步骤视图缺失 recipe {recipe_id}")
            views.append(
                RecipeTimeView(
                    recipe_id=int(record["recipe_id"]),
                    ingredient_ids=[int(i) for i in record.get("ingredient_ids", [])],
                    steps=list(record.get("steps", [])),
                )
            )
        return views

    def all_health_views(self, build_id: str) -> list[RecipeHealthIngredientView]:
        rows = self._records("recipe_health_views", build_id)
        return [
            RecipeHealthIngredientView(
                recipe_id=int(r["recipe_id"]),
                ingredient_ids=[int(i) for i in r.get("ingredient_ids", [])],
                ingredient_evidence_paths=list(r.get("ingredient_evidence_paths", [])),
                unresolved_occurrence_count=int(r.get("unresolved_occurrence_count", 0)),
                composition_expansion_status=r.get("composition_expansion_status", "unknown"),
                catalog_eligibility=r.get("catalog_eligibility", "unknown"),
            )
            for r in rows
        ]

    def all_retrieval_views(self, build_id: str) -> list[RecipeRetrievalView]:
        rows = self._records("recipe_retrieval_build_views", build_id)
        return [
            RecipeRetrievalView(
                recipe_id=int(r["recipe_id"]),
                name=r.get("name", ""),
                ingredient_display_names=list(r.get("ingredient_display_names", [])),
                ingredient_ids=[int(i) for i in r.get("ingredient_ids", [])],
                searchable_fields=dict(r.get("searchable_fields", {}) or {}),
                step_summary=r.get("step_summary"),
                time_reference=r.get("time_reference"),
                ingredient_family_ids=[int(i) for i in r.get("ingredient_family_ids", [])],
            )
            for r in rows
        ]

    def all_time_views(self, build_id: str) -> list[RecipeTimeView]:
        rows = self._records("recipe_step_binding_views", build_id)
        return [
            RecipeTimeView(
                recipe_id=int(r["recipe_id"]),
                ingredient_ids=[int(i) for i in r.get("ingredient_ids", [])],
                steps=list(r.get("steps", [])),
            )
            for r in rows
        ]

    def registry_entries(self, build_id: str) -> list[dict]:
        return self._records("ingredient_registry", build_id)

    def alias_entries(self, build_id: str) -> list[dict]:
        return self._records("ingredient_aliases", build_id)


def default_mysql_repository() -> FixedDataRepository:
    return FixedDataRepository(MySQLArtifactRecordSource())
