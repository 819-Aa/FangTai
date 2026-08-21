import pytest

from food_agent_v2.b5 import TimeGraphUnavailableError, TimeProfileService


class FakeTimeSource:
    def ready_build_id(self) -> str:
        return "build-b5-v2"

    def records(self, artifact_name: str, build_id: str) -> list[dict]:
        assert artifact_name == "step_tasks"
        return [
            {
                "recipe_id": 1,
                "recipe_name": "快炒菜",
                "step_tasks": [
                    {
                        "atom_id": "r1-prep",
                        "text": "切配",
                        "duration_seconds": 300,
                        "task_type": "manual",
                        "resources": ["cook"],
                        "depends_on": [],
                    },
                    {
                        "atom_id": "r1-fry",
                        "text": "翻炒",
                        "duration_seconds": 300,
                        "task_type": "attended_equipment",
                        "resources": ["cook", "burner"],
                        "depends_on": ["r1-prep"],
                    },
                ],
            },
            {
                "recipe_id": 2,
                "recipe_name": "烤菜",
                "step_tasks": [
                    {
                        "atom_id": "r2-place",
                        "text": "放入烤箱",
                        "duration_seconds": 60,
                        "task_type": "manual",
                        "resources": ["cook"],
                        "depends_on": [],
                    },
                    {
                        "atom_id": "r2-bake",
                        "text": "烤10分钟",
                        "duration_seconds": 600,
                        "task_type": "unattended_equipment",
                        "resources": ["oven"],
                        "depends_on": ["r2-place"],
                    },
                ],
            },
        ]


def _service() -> TimeProfileService:
    service = TimeProfileService(FakeTimeSource())
    service.load()
    return service


def test_single_recipe_profile_has_active_and_estimated_elapsed_seconds() -> None:
    profile = _service().get_recipe_time_profile(2)

    assert profile is not None
    assert profile.active_seconds == 60
    assert profile.estimated_elapsed_seconds == 660


def test_no_limit_is_estimated_feasible_and_limit_is_boolean() -> None:
    service = _service()

    unlimited = service.compute_menu_schedule([1, 2])
    assert unlimited.estimated_time_feasible is True
    assert unlimited.estimated_makespan_seconds > 0
    assert service.compute_menu_schedule(
        [1, 2], max_estimated_time_seconds=1800
    ).estimated_time_feasible is True
    assert service.compute_menu_schedule(
        [1, 2], max_estimated_time_seconds=60
    ).estimated_time_feasible is False


def test_missing_requested_profile_is_service_failure_not_unknown() -> None:
    with pytest.raises(TimeGraphUnavailableError):
        _service().compute_menu_schedule([1, 99])


def test_schedule_hash_is_content_addressed_and_stable() -> None:
    service = _service()
    first = service.compute_menu_schedule([1, 2]).schedule_hash
    second = service.compute_menu_schedule([2, 1]).schedule_hash
    other = service.compute_menu_schedule([1]).schedule_hash

    assert first == second
    assert first != other


def test_load_rejects_incomplete_time_graph() -> None:
    class BrokenSource(FakeTimeSource):
        def records(self, artifact_name: str, build_id: str) -> list[dict]:
            return [{"recipe_id": 1, "recipe_name": "坏数据", "step_tasks": []}]

    with pytest.raises(TimeGraphUnavailableError):
        TimeProfileService(BrokenSource()).load()
