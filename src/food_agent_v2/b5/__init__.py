"""B5 时间画像与 CP-SAT 菜单预计时长服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from food_agent_v2.b1.schemas import StepTask
from food_agent_v2.b3.repository import MySQLArtifactRecordSource
from food_agent_v2.b5.scheduler import (
    KitchenCapacity,
    ScheduleResult,
    TaskGraphScheduleError,
    schedule_task_graphs,
)


class TimeRecordSource(Protocol):
    def ready_build_id(self) -> str: ...
    def records(self, artifact_name: str, build_id: str) -> list[dict]: ...


class TimeGraphUnavailableError(RuntimeError):
    """固定时间图缺失或损坏；ready 系统不得降级成 unknown。"""


@dataclass(frozen=True)
class RecipeTimeProfile:
    recipe_id: int
    name: str
    active_seconds: int
    estimated_elapsed_seconds: int
    step_tasks: tuple[StepTask, ...]


class TimeProfileService:
    """只读取已发布 step_tasks，并在线做确定性 CP-SAT 排程。"""

    def __init__(
        self,
        source: TimeRecordSource | None = None,
        *,
        capacity: KitchenCapacity | None = None,
    ) -> None:
        self._source = source or MySQLArtifactRecordSource()
        self._capacity = capacity or KitchenCapacity()
        self._profiles: dict[int, RecipeTimeProfile] = {}
        self._loaded = False
        self._build_id = ""

    def load(self, path=None) -> None:
        """从同一 ready build 的固定 step_tasks Artifact 加载并复验每道菜。"""
        del path
        build_id = self._source.ready_build_id()
        rows = self._source.records("step_tasks", build_id)
        profiles: dict[int, RecipeTimeProfile] = {}
        for record in rows:
            try:
                recipe_id = int(record["recipe_id"])
                if recipe_id in profiles:
                    raise TimeGraphUnavailableError(f"recipe_id={recipe_id} 时间画像重复")
                tasks = tuple(
                    StepTask.model_validate(task) for task in record.get("step_tasks", ())
                )
                if not tasks:
                    raise TimeGraphUnavailableError(f"recipe_id={recipe_id} 时间图为空")
                scheduled = schedule_task_graphs(
                    {recipe_id: tasks},
                    capacity=self._capacity,
                )
                _assert_optional_summary(record, scheduled)
                profiles[recipe_id] = RecipeTimeProfile(
                    recipe_id=recipe_id,
                    name=str(record.get("recipe_name") or record.get("name") or ""),
                    active_seconds=scheduled.active_seconds,
                    estimated_elapsed_seconds=scheduled.estimated_makespan_seconds,
                    step_tasks=tasks,
                )
            except TimeGraphUnavailableError:
                raise
            except (KeyError, TypeError, ValueError, ValidationError, TaskGraphScheduleError) as exc:
                recipe_id = record.get("recipe_id", "unknown")
                raise TimeGraphUnavailableError(
                    f"recipe_id={recipe_id} 时间图无效"
                ) from exc
        if not profiles:
            raise TimeGraphUnavailableError("step_tasks Artifact 为空")
        self._profiles = profiles
        self._build_id = build_id
        self._loaded = True

    def get_recipe_time_profile(self, recipe_id: int) -> RecipeTimeProfile | None:
        return self._profiles.get(int(recipe_id))

    def compute_menu_schedule(
        self,
        recipe_ids: list[int],
        max_estimated_time_seconds: int | None = None,
    ) -> ScheduleResult:
        """计算菜单预计 makespan；给上限时返回严格 true/false 比较结果。"""
        normalized = [int(recipe_id) for recipe_id in recipe_ids]
        if not normalized or len(set(normalized)) != len(normalized):
            raise TimeGraphUnavailableError("菜单 recipe_ids 为空或重复")
        missing = tuple(sorted(set(normalized) - set(self._profiles)))
        if missing:
            raise TimeGraphUnavailableError(f"缺少时间图 recipe_ids={missing}")
        return schedule_task_graphs(
            {
                recipe_id: self._profiles[recipe_id].step_tasks
                for recipe_id in normalized
            },
            max_estimated_time_seconds=max_estimated_time_seconds,
            capacity=self._capacity,
        )

    @property
    def profile_count(self) -> int:
        return len(self._profiles)


def _assert_optional_summary(record: dict, scheduled: ScheduleResult) -> None:
    if record.get("active_seconds") is not None and int(record["active_seconds"]) != (
        scheduled.active_seconds
    ):
        raise TimeGraphUnavailableError("active_seconds 与任务图不一致")
    if record.get("estimated_elapsed_seconds") is not None and int(
        record["estimated_elapsed_seconds"]
    ) != scheduled.estimated_makespan_seconds:
        raise TimeGraphUnavailableError("estimated_elapsed_seconds 与任务图不一致")


_service: TimeProfileService | None = None


def get_time_service() -> TimeProfileService:
    global _service
    if _service is None:
        _service = TimeProfileService()
        _service.load()
    return _service


__all__ = [
    "KitchenCapacity",
    "RecipeTimeProfile",
    "ScheduleResult",
    "TimeGraphUnavailableError",
    "TimeProfileService",
    "get_time_service",
    "schedule_task_graphs",
]
