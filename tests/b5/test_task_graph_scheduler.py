from food_agent_v2.b1.schemas import StepTask
from food_agent_v2.b5.scheduler import schedule_task_graphs


def _task(
    atom_id: str,
    seconds: int,
    task_type: str,
    resources=(),
    depends_on=(),
) -> StepTask:
    return StepTask(
        atom_id=atom_id,
        text=atom_id,
        duration_seconds=seconds,
        task_type=task_type,
        resources=tuple(resources),
        depends_on=tuple(depends_on),
    )


def test_dependency_critical_path_and_active_seconds() -> None:
    graph = {
        1: (
            _task("prep", 120, "manual", ["cook"]),
            _task("rest", 600, "passive", [], ["prep"]),
            _task("serve", 60, "manual", ["cook"], ["rest"]),
        )
    }

    result = schedule_task_graphs(graph)

    assert result.active_seconds == 180
    assert result.estimated_makespan_seconds == 780


def test_single_cook_serializes_independent_manual_tasks() -> None:
    result = schedule_task_graphs(
        {
            1: (_task("r1", 300, "manual", ["cook"]),),
            2: (_task("r2", 300, "manual", ["cook"]),),
        }
    )

    assert result.estimated_makespan_seconds == 600


def test_two_burners_allow_two_tasks_but_not_three() -> None:
    graphs = {
        rid: (_task(f"r{rid}", 300, "unattended_equipment", ["burner"]),)
        for rid in (1, 2, 3)
    }

    assert schedule_task_graphs({1: graphs[1], 2: graphs[2]}).estimated_makespan_seconds == 300
    assert schedule_task_graphs(graphs).estimated_makespan_seconds == 600


def test_single_oven_serializes_unattended_runs() -> None:
    result = schedule_task_graphs(
        {
            1: (_task("oven-1", 300, "unattended_equipment", ["oven"]),),
            2: (_task("oven-2", 300, "unattended_equipment", ["oven"]),),
        }
    )

    assert result.estimated_makespan_seconds == 600


def test_unattended_oven_and_passive_wait_do_not_hold_cook() -> None:
    result = schedule_task_graphs(
        {
            1: (_task("oven", 600, "unattended_equipment", ["oven"]),),
            2: (_task("manual", 300, "manual", ["cook"]),),
            3: (_task("rest", 500, "passive"),),
        }
    )

    assert result.estimated_makespan_seconds == 600
    assert result.active_seconds == 300


def test_menu_makespan_and_30_minute_expected_hard_filter() -> None:
    graphs = {
        1: (_task("r1", 1200, "manual", ["cook"]),),
        2: (_task("r2", 900, "manual", ["cook"]),),
    }

    result = schedule_task_graphs(graphs, max_estimated_time_seconds=1800)

    assert result.estimated_makespan_seconds == 2100
    assert result.estimated_time_feasible is False
