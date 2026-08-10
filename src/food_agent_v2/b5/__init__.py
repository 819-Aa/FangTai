"""B5 时间与步骤规划（T13）。

- 严格时间结论只在确定性高权威证据（deterministic_high）下产生 true/false；
  否则为 unknown（INV-021）。
- 已移除 LLM/40% 公式的严格权威路径。
- 数据来源为固定 step_tasks Artifact（Repository），不再读取 JSONL（INV-023）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from food_agent_v2.b3.repository import MySQLArtifactRecordSource
from food_agent_v2.contracts.build import canonical_json_hash

Authority = Literal["deterministic_high", "deterministic_partial", "model_estimate"]
StrictTimeFeasible = bool | Literal["unknown"]


class TimeRecordSource(Protocol):
    def ready_build_id(self) -> str: ...
    def records(self, artifact_name: str, build_id: str) -> list[dict]: ...


class MenuScheduleResult(BaseModel):
    """菜单调度结果：三值严格时间 + 权威级别 + 缺失事实 + 内容寻址散列。"""

    model_config = ConfigDict(extra="forbid")

    recipe_ids: list[int]
    makespan_seconds: int | None
    strict_time_feasible: StrictTimeFeasible
    authority: Authority
    missing_facts: tuple[str, ...] = ()
    schedule_hash: str
    time_source: str = "task_graph"


@dataclass
class RecipeTimeProfile:
    """单菜时间画像（来自固定 step_tasks Artifact）。"""

    recipe_id: int
    name: str
    total_steps: int
    total_active_seconds: int
    total_equipment_seconds: int
    total_passive_seconds: int
    total_elapsed_range: tuple[int, int]
    overall_confidence: str
    authority: str
    strict_time_feasible: str
    step_tasks: list[dict] = field(default_factory=list)


class TimeProfileService:
    """时间画像查询与菜单调度服务（确定性任务图）。"""

    def __init__(self, source: TimeRecordSource | None = None) -> None:
        self._source = source or MySQLArtifactRecordSource()
        self._profiles: dict[int, RecipeTimeProfile] = {}
        self._loaded = False

    def load(self, path=None) -> None:
        """从固定 step_tasks Artifact 加载（path 兼容旧签名）。"""
        build_id = self._source.ready_build_id()
        rows = self._source.records("step_tasks", build_id)
        for rec in rows:
            rid = int(rec["recipe_id"])
            tasks = list(rec.get("steps", []))
            active = sum((t.get("duration_seconds") or 0) for t in tasks if t.get("step_type") == "active")
            equip = sum((t.get("duration_seconds") or 0) for t in tasks if t.get("step_type") == "equipment")
            passive = sum((t.get("duration_seconds") or 0) for t in tasks if t.get("step_type") == "passive")
            total = rec.get("total_duration_seconds")
            authority = rec.get("authority", "deterministic_partial")
            self._profiles[rid] = RecipeTimeProfile(
                recipe_id=rid,
                name="",
                total_steps=len(tasks),
                total_active_seconds=active,
                total_equipment_seconds=equip,
                total_passive_seconds=passive,
                total_elapsed_range=(
                    int(rec.get("total_duration_min_seconds") or 0),
                    int(total or 0),
                ),
                overall_confidence="high" if authority == "deterministic_high" else "medium",
                authority=authority,
                strict_time_feasible=str(rec.get("strict_time_feasible", "unknown")),
                step_tasks=tasks,
            )
        self._loaded = True

    def get_recipe_time_profile(self, recipe_id: int) -> RecipeTimeProfile | None:
        return self._profiles.get(recipe_id)

    def _task_graph_makespan(self, profiles: list[RecipeTimeProfile]) -> tuple[int, list[str]]:
        """确定性任务图关键路径（对齐固定 step_tasks Artifact 字段）。

        - 被动等待（passive）不占用厨师/设备，可并行；
        - active/equipment 占用厨师或对应设备资源（equipment_type 或 cook），串行；
        - 未知时长不估算并记录缺失事实。
        """
        from collections import defaultdict

        resource_free: dict[str, int] = defaultdict(int)
        makespan = 0
        missing: list[str] = []
        for profile in profiles:
            rid = profile.recipe_id
            for task in profile.step_tasks:
                idx = task.get("step_index", 0)
                step_type = task.get("step_type", "active")
                dur = task.get("duration_seconds")
                if dur is None or dur <= 0:
                    missing.append(f"recipe:{rid}/step:{idx}")
                    dur = 0
                if step_type == "passive":
                    # 被动等待不占用资源，可并行；只推进整体 makespan。
                    end = int(dur)
                else:
                    resource = task.get("equipment_type") or "cook"
                    start = resource_free.get(resource, 0)
                    end = start + int(dur)
                    resource_free[resource] = end
                makespan = max(makespan, end)
        return makespan, missing

    def compute_menu_schedule(
        self, recipe_ids: list[int], time_limit_minutes: int | None = None
    ) -> MenuScheduleResult:
        """基于确定性任务图计算菜单调度与三值严格时间（INV-021）。"""
        profiles = [self._profiles[rid] for rid in recipe_ids if rid in self._profiles]
        if not profiles:
            return MenuScheduleResult(
                recipe_ids=list(recipe_ids),
                makespan_seconds=None,
                strict_time_feasible="unknown",
                authority="deterministic_partial",
                missing_facts=("no_time_profiles",),
                schedule_hash=canonical_json_hash(
                    {"recipe_ids": sorted(recipe_ids), "missing": True}
                ),
            )

        makespan, missing = self._task_graph_makespan(profiles)
        all_high = all(p.overall_confidence == "high" for p in profiles)
        authority: Authority = "deterministic_high" if all_high else "deterministic_partial"

        # 严格时间结论必须有高权威确定性证据：缺失时长或低权威一律 unknown（INV-021）。
        if (
            time_limit_minutes is not None
            and all_high
            and makespan > 0
            and not missing
        ):
            strict: StrictTimeFeasible = makespan <= time_limit_minutes * 60
        else:
            strict = "unknown"

        build_id = self._source.ready_build_id()
        schedule_hash = canonical_json_hash(
            {
                "build_id": build_id,
                "recipe_ids": sorted(recipe_ids),
                "makespan_seconds": makespan,
                "authority": authority,
            }
        )
        return MenuScheduleResult(
            recipe_ids=list(recipe_ids),
            makespan_seconds=makespan if makespan > 0 else None,
            strict_time_feasible=strict,
            authority=authority,
            missing_facts=tuple(missing),
            schedule_hash=schedule_hash,
        )

    @property
    def profile_count(self) -> int:
        return len(self._profiles)


# 模块级单例
_service: TimeProfileService | None = None


def get_time_service() -> TimeProfileService:
    global _service
    if _service is None:
        _service = TimeProfileService()
        _service.load()
    return _service
