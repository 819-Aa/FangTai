import pytest

from food_agent_v2.b1.step_atomizer import OVERNIGHT_SECONDS, parse_explicit_duration


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("焯30秒", 30),
        ("煮12分钟", 720),
        ("炖2小时", 7200),
        ("烤1.5小时", 5400),
        ("静置半小时", 1800),
        ("醒面一刻钟", 900),
        ("醒面四分之一小时", 900),
        ("蒸10-20分钟", 900),
        ("炖1～1.5小时", 4500),
        ("冷藏隔夜", OVERNIGHT_SECONDS),
        ("焯30秒，再煮2分钟", 150),
    ],
)
def test_parse_supported_explicit_duration_expressions(text: str, expected: int) -> None:
    assert parse_explicit_duration(text) == expected


def test_missing_explicit_duration_is_not_estimated_by_parser() -> None:
    assert parse_explicit_duration("翻炒至熟") is None


def test_reversed_range_still_uses_mathematical_midpoint() -> None:
    assert parse_explicit_duration("蒸20-10分钟") == 900
