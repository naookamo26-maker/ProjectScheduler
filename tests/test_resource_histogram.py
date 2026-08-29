"""
gui/resource_histogram.py の純粋関数（Qt/DB非依存）の単体テスト。

compute_step_segments はプロジェクト分析タブの「上限に張り付いた日数」の
集計（gui/summary_metrics._team_pinned_days）に使い回している汎用関数のため、
境界値（範囲外・範囲境界ちょうど・変化点の重複）を中心に検証する。
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 分類: gui（gui/resource_histogram.py はgui/配下のモジュールのため）。
pytestmark = pytest.mark.gui

pytest.importorskip("PySide6")

from gui.resource_histogram import compute_step_segments  # noqa: E402

D0 = date(2026, 1, 1)


def _d(days):
    return D0 + timedelta(days=days)


# -- compute_step_segments ---------------------------------------------------

def test_compute_step_segments_empty_breakpoints_is_empty():
    assert compute_step_segments([], D0, _d(10)) == []


def test_compute_step_segments_range_start_after_range_end_is_empty():
    assert compute_step_segments([(D0, 3)], _d(10), D0) == []


def test_compute_step_segments_single_breakpoint_fills_whole_range():
    assert compute_step_segments([(D0, 3)], D0, _d(10)) == [(D0, _d(10), 3)]


def test_compute_step_segments_multiple_breakpoints_produce_a_staircase():
    segs = compute_step_segments([(D0, 3), (_d(5), 5)], D0, _d(10))
    assert segs == [(D0, _d(5), 3), (_d(5), _d(10), 5)]


def test_compute_step_segments_breakpoint_before_range_start_still_applies():
    segs = compute_step_segments([(_d(-5), 2), (D0, 3)], D0, _d(10))
    assert segs == [(D0, _d(10), 3)]


def test_compute_step_segments_no_data_before_first_breakpoint_is_skipped():
    """range_start時点でまだ有効な変化点が無ければ、最初の変化点までの
    区間は作らない（「値が定まらない」区間を描かせないため）。"""
    segs = compute_step_segments([(_d(2), 4)], D0, _d(10))
    assert segs == [(_d(2), _d(10), 4)]


def test_compute_step_segments_breakpoint_beyond_range_end_is_ignored():
    segs = compute_step_segments([(D0, 3), (_d(20), 9)], D0, _d(10))
    assert segs == [(D0, _d(10), 3)]


def test_compute_step_segments_duplicate_date_keeps_last_value():
    segs = compute_step_segments([(D0, 3), (D0, 7)], D0, _d(10))
    assert segs == [(D0, _d(10), 7)]


def test_compute_step_segments_breakpoint_exactly_at_range_end():
    segs = compute_step_segments([(D0, 3), (_d(10), 5)], D0, _d(10))
    assert segs == [(D0, _d(10), 3), (_d(10), _d(10), 5)]
