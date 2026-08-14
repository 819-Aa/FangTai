"""性能 harness 双 TTFT 与判定测试（L2 Task 8）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from perf_harness import evaluate_turn, measure_turn, multi_turn_average  # noqa: E402


def test_measure_turn_distinguishes_visible_and_authoritative():
    events = [("answer_started", 100), ("answer_ready", 1200)]
    r = measure_turn(events)
    assert r["visible_ttft_ms"] == 100
    assert r["authoritative_ttft_ms"] == 1200


def test_failed_turn_never_counts_as_pass():
    r = evaluate_turn(status="failed", visible_ttft_ms=10,
                      authoritative_ttft_ms=None, e2e_ms=20)
    assert r["passed"] is False


def test_completed_turn_passes():
    r = evaluate_turn(status="completed", visible_ttft_ms=10,
                      authoritative_ttft_ms=1200, e2e_ms=1500)
    assert r["passed"] is True


def test_multi_turn_average_is_per_case():
    assert multi_turn_average([1000, 3000, 2000]) == 2000
