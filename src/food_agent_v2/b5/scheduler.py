"""用 OR-Tools CP-SAT 调度单菜或多菜的完整步骤任务图。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from ortools.sat.python import cp_model

from food_agent_v2.b1.schemas import StepTask
from food_agent_v2.contracts.build import canonical_json_hash


@dataclass(frozen=True)
class KitchenCapacity:
    cook: int = 1
    burner: int = 2
    oven: int = 1
    steamer: int = 1
    microwave: int = 1
    blender: int = 1

    def __post_init__(self) -> None:
        if any(value <= 0 for value in asdict(self).values()):
            raise ValueError("厨房容量必须为正整数")


@dataclass(frozen=True)
class ScheduleResult:
    recipe_ids: tuple[int, ...]
    active_seconds: int
    estimated_makespan_seconds: int
    estimated_time_feasible: bool
    schedule_hash: str


class TaskGraphScheduleError(RuntimeError):
    pass


def schedule_task_graphs(
    graphs: Mapping[int, tuple[StepTask, ...]],
    *,
    max_estimated_time_seconds: int | None = None,
    capacity: KitchenCapacity | None = None,
) -> ScheduleResult:
    """最小化共享家庭厨房资源约束下所有菜谱的预计完工时间。"""
    if not graphs:
        raise TaskGraphScheduleError("没有可调度的菜谱时间图")
    if max_estimated_time_seconds is not None and max_estimated_time_seconds < 0:
        raise ValueError("预计时间上限不得为负")
    capacity = capacity or KitchenCapacity()
    normalized = {int(recipe_id): tuple(tasks) for recipe_id, tasks in graphs.items()}
    for recipe_id, tasks in normalized.items():
        _validate_graph_shape(recipe_id, tasks)

    model = cp_model.CpModel()
    horizon = max(
        1,
        sum(task.duration_seconds for tasks in normalized.values() for task in tasks),
    )
    starts: dict[tuple[int, str], cp_model.IntVar] = {}
    ends: dict[tuple[int, str], cp_model.IntVar] = {}
    intervals: dict[tuple[int, str], cp_model.IntervalVar] = {}

    for recipe_id in sorted(normalized):
        for task in normalized[recipe_id]:
            key = (recipe_id, task.atom_id)
            safe_name = f"r{recipe_id}_{len(starts)}"
            start = model.new_int_var(0, horizon, f"start_{safe_name}")
            end = model.new_int_var(0, horizon, f"end_{safe_name}")
            starts[key] = start
            ends[key] = end
            if task.duration_seconds == 0:
                model.add(end == start)
            else:
                intervals[key] = model.new_interval_var(
                    start,
                    task.duration_seconds,
                    end,
                    f"interval_{safe_name}",
                )

    for recipe_id, tasks in normalized.items():
        for task in tasks:
            key = (recipe_id, task.atom_id)
            for dependency in task.depends_on:
                model.add(starts[key] >= ends[(recipe_id, dependency)])

    resource_intervals: dict[str, list[cp_model.IntervalVar]] = {
        resource: [] for resource in asdict(capacity)
    }
    for recipe_id, tasks in normalized.items():
        for task in tasks:
            key = (recipe_id, task.atom_id)
            interval = intervals.get(key)
            if interval is None:
                continue
            for resource in _capacity_resources_for(task):
                resource_intervals[resource].append(interval)

    capacities = asdict(capacity)
    for resource, occupied in resource_intervals.items():
        if occupied:
            model.add_cumulative(occupied, [1] * len(occupied), capacities[resource])

    makespan = model.new_int_var(0, horizon, "menu_makespan")
    model.add_max_equality(makespan, list(ends.values()))
    model.minimize(makespan)

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    status = solver.solve(model)
    if status != cp_model.OPTIMAL:
        raise TaskGraphScheduleError(f"CP-SAT 未得到最优排程: status={status}")

    makespan_seconds = int(solver.value(makespan))
    active_seconds = sum(
        task.duration_seconds
        for tasks in normalized.values()
        for task in tasks
        if task.task_type in {"manual", "attended_equipment"}
    )
    recipe_ids = tuple(sorted(normalized))
    schedule_payload = {
        "recipe_ids": recipe_ids,
        "capacity": capacities,
        "graphs": {
            str(recipe_id): [task.model_dump(mode="json") for task in normalized[recipe_id]]
            for recipe_id in recipe_ids
        },
        "active_seconds": active_seconds,
        "estimated_makespan_seconds": makespan_seconds,
    }
    return ScheduleResult(
        recipe_ids=recipe_ids,
        active_seconds=active_seconds,
        estimated_makespan_seconds=makespan_seconds,
        estimated_time_feasible=(
            True
            if max_estimated_time_seconds is None
            else makespan_seconds <= max_estimated_time_seconds
        ),
        schedule_hash=canonical_json_hash(schedule_payload),
    )


def _validate_graph_shape(recipe_id: int, tasks: tuple[StepTask, ...]) -> None:
    if not tasks:
        raise TaskGraphScheduleError(f"recipe_id={recipe_id} 时间图为空")
    atom_ids = [task.atom_id for task in tasks]
    if len(set(atom_ids)) != len(atom_ids):
        raise TaskGraphScheduleError(f"recipe_id={recipe_id} atom_id 重复")
    known = set(atom_ids)
    for task in tasks:
        if set(task.depends_on) - known:
            raise TaskGraphScheduleError(f"recipe_id={recipe_id} 依赖不存在")
        if task.atom_id in task.depends_on:
            raise TaskGraphScheduleError(f"recipe_id={recipe_id} 存在自依赖")


def _capacity_resources_for(task: StepTask) -> tuple[str, ...]:
    if task.task_type == "manual":
        return ("cook",)
    if task.task_type == "attended_equipment":
        return ("cook",) + tuple(
            resource
            for resource in task.resources
            if resource in {"burner", "oven", "steamer", "microwave", "blender"}
        )
    if task.task_type == "unattended_equipment":
        return tuple(
            resource
            for resource in task.resources
            if resource in {"burner", "oven", "steamer", "microwave", "blender"}
        )
    return ()
