"""PerformanceBudget 请求级预算测试（L2 Task 8）。"""

from __future__ import annotations

from food_agent_v2.c3.perf import PerformanceBudget


def test_remaining_seconds_positive_immediately():
    budget = PerformanceBudget(5.0)
    assert budget.remaining_seconds() > 0


def test_require_true_with_remaining_budget():
    budget = PerformanceBudget(5.0)
    assert budget.require("retrieval") is True


def test_allow_optional_requires_enough_budget():
    budget = PerformanceBudget(5.0)
    assert budget.allow_optional(2.5) is True
    assert budget.allow_optional(10.0) is False


def test_budget_exhausted_blocks_optional():
    budget = PerformanceBudget(0.01)
    import time
    time.sleep(0.02)
    assert budget.allow_optional(2.5) is False
    assert budget.require("answer") is False
