"""T13 B5 严格时间三值语义测试。

strict_time_feasible 只在 deterministic_high 且有时长数据时产生 true/false；
否则 unknown；缺失时长记录到 missing_facts；schedule_hash 内容寻址。
"""

from food_agent_v2.b5 import MenuScheduleResult, TimeProfileService


class FakeTimeSource:
    def ready_build_id(self) -> str:
        return "build-b5"

    def records(self, artifact_name: str, build_id: str) -> list[dict]:
        return [
            {
                "recipe_id": 1,
                "authority": "deterministic_high",
                "strict_time_feasible": "true",
                "total_duration_seconds": 600,
                "total_duration_min_seconds": 600,
                "steps": [
                    {"step_index": 1, "step_type": "active", "duration_seconds": 300, "confidence": "high"},
                    {"step_index": 2, "step_type": "equipment", "duration_seconds": 300, "confidence": "high"},
                ],
            },
            {
                "recipe_id": 2,
                "authority": "deterministic_high",
                "strict_time_feasible": "true",
                "total_duration_seconds": 300,
                "total_duration_min_seconds": 300,
                "steps": [
                    {"step_index": 1, "step_type": "active", "duration_seconds": 300, "confidence": "high"},
                ],
            },
            {
                "recipe_id": 3,
                "authority": "deterministic_partial",
                "strict_time_feasible": "unknown",
                "total_duration_seconds": 600,
                "total_duration_min_seconds": 600,
                "steps": [
                    {"step_index": 1, "step_type": "active", "duration_seconds": 600, "confidence": "low"},
                    {"step_index": 2, "step_type": "active", "duration_seconds": None, "confidence": "low"},
                ],
            },
            {
                "recipe_id": 4,
                "authority": "deterministic_high",
                "strict_time_feasible": "true",
                "total_duration_seconds": 900,
                "total_duration_min_seconds": 900,
                "steps": [
                    {"step_index": 1, "step_type": "active", "duration_seconds": 300, "confidence": "high"},
                    {"step_index": 2, "step_type": "passive", "duration_seconds": 600, "confidence": "high"},
                ],
            },
            {
                "recipe_id": 5,
                "authority": "deterministic_high",
                "strict_time_feasible": "unknown",
                "total_duration_seconds": 300,
                "total_duration_min_seconds": 300,
                "steps": [
                    {"step_index": 1, "step_type": "active", "duration_seconds": 300, "confidence": "high"},
                    {"step_index": 2, "step_type": "active", "duration_seconds": None, "confidence": "high"},
                ],
            },
        ]


class TestStrictTimeSemantics:
    def _service(self) -> TimeProfileService:
        service = TimeProfileService(FakeTimeSource())
        service.load()
        return service

    def test_menu_schedule_is_model(self) -> None:
        service = self._service()
        result = service.compute_menu_schedule([1, 2], time_limit_minutes=20)
        assert isinstance(result, MenuScheduleResult)

    def test_deterministic_high_feasible_true(self) -> None:
        result = self._service().compute_menu_schedule([1, 2], time_limit_minutes=20)
        assert result.authority == "deterministic_high"
        assert result.strict_time_feasible is True

    def test_deterministic_high_infeasible_false(self) -> None:
        result = self._service().compute_menu_schedule([1, 2], time_limit_minutes=10)
        assert result.authority == "deterministic_high"
        assert result.strict_time_feasible is False

    def test_unknown_without_time_limit(self) -> None:
        result = self._service().compute_menu_schedule([1, 2])
        assert result.strict_time_feasible == "unknown"

    def test_unknown_low_authority(self) -> None:
        result = self._service().compute_menu_schedule([3], time_limit_minutes=20)
        assert result.authority == "deterministic_partial"
        assert result.strict_time_feasible == "unknown"

    def test_missing_facts_recorded(self) -> None:
        result = self._service().compute_menu_schedule([3], time_limit_minutes=20)
        assert any("recipe:3/step:2" in fact for fact in result.missing_facts)

    def test_schedule_hash_content_addressed(self) -> None:
        service = self._service()
        h1 = service.compute_menu_schedule([1, 2], time_limit_minutes=20).schedule_hash
        h2 = service.compute_menu_schedule([1, 2], time_limit_minutes=20).schedule_hash
        h3 = service.compute_menu_schedule([1, 3], time_limit_minutes=20).schedule_hash
        assert h1 == h2
        assert h1 != h3

    def test_unknown_not_truthy(self) -> None:
        result = self._service().compute_menu_schedule([1, 2])
        assert result.strict_time_feasible == "unknown"
        assert result.strict_time_feasible is not True

    def test_passive_does_not_occupy_cook(self) -> None:
        # active 300 + passive 600：被动等待并行，makespan=600（而非 900）。
        result = self._service().compute_menu_schedule([4], time_limit_minutes=20)
        assert result.makespan_seconds == 600
        assert result.strict_time_feasible is True

    def test_missing_duration_forces_unknown(self) -> None:
        # deterministic_high 但存在缺失时长 → 严格时间 unknown（INV-021）。
        result = self._service().compute_menu_schedule([5], time_limit_minutes=20)
        assert result.authority == "deterministic_high"
        assert result.strict_time_feasible == "unknown"
        assert any("recipe:5/step:2" in fact for fact in result.missing_facts)
