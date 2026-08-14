"""AuthoritativeAnswerBuilder 变更摘要测试（L1 Task 7）。"""

from __future__ import annotations

from food_agent_v2.c3.authoritative_answer import compute_changes


def test_compute_changes_exact_removed_added():
    removed, added = compute_changes((1, 2, 3), (1, 4, 3))
    assert removed == (2,)
    assert added == (4,)


def test_compute_changes_no_change():
    removed, added = compute_changes((1, 2, 3), (1, 2, 3))
    assert removed == ()
    assert added == ()


def test_compute_changes_full_replace():
    removed, added = compute_changes((1, 2), (3, 4))
    assert removed == (1, 2)
    assert added == (3, 4)
