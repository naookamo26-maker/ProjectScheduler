"""
gui/resource_histogram.py の純粋関数（Qt/DB非依存）の単体テスト。

compute_step_segments は将来ガントチャートタブでの実績値表示にも使い回す
想定の汎用関数のため、境界値（範囲外・範囲境界ちょうど・変化点の重複）を
中心に検証する。
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.resource_histogram import (  # noqa: E402
    compute_step_segments,
    histogram_axis_range,
    shared_boundaries,
    team_capacity_breakpoints,
    value_at,
)

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


# -- team_capacity_breakpoints ------------------------------------------------

def test_team_capacity_breakpoints_no_changes_is_just_the_default():
    team = {"max_lines": 2}
    assert team_capacity_breakpoints(team, [], "2026-01-01") == [(D0, 2)]


def test_team_capacity_breakpoints_includes_sorted_changes():
    team = {"max_lines": 2}
    changes = [
        {"start_date": "2026-03-01", "lines": 6},
        {"start_date": "2026-02-01", "lines": 4},
    ]
    result = team_capacity_breakpoints(team, changes, "2026-01-01")
    assert result == [
        (D0, 2),
        (date(2026, 2, 1), 4),
        (date(2026, 3, 1), 6),
    ]


# -- histogram_axis_range ------------------------------------------------------

def test_histogram_axis_range_falls_back_to_default_window_without_data():
    start, end = histogram_axis_range("2026-01-01", [], [])
    assert start == D0
    assert end == D0 + timedelta(days=90 + 3)


def test_histogram_axis_range_uses_latest_milestone():
    start, end = histogram_axis_range(
        "2026-01-01", [{"end_date": "2026-03-01"}], [],
    )
    assert start == D0
    assert end == date(2026, 3, 1) + timedelta(days=3)


def test_histogram_axis_range_uses_latest_capacity_change_if_later_than_milestones():
    start, end = histogram_axis_range(
        "2026-01-01",
        [{"end_date": "2026-02-01"}],
        [{"start_date": "2026-05-01", "lines": 3}],
    )
    assert end == date(2026, 5, 1) + timedelta(days=3)


def test_histogram_axis_range_never_ends_before_start():
    """マイルストーン・変動点が開発開始日より前しか無い（データ不整合）
    場合でも、表示範囲の終端が開始日より前にならないようにする。"""
    start, end = histogram_axis_range(
        "2026-06-01", [{"end_date": "2026-01-01"}], [],
    )
    assert start == date(2026, 6, 1)
    assert end >= start


# -- value_at / shared_boundaries（積み上げ表示用の補助関数） ------------------------

def test_value_at_returns_zero_before_any_breakpoint():
    assert value_at([(_d(5), 3)], D0) == 0


def test_value_at_returns_latest_applicable_value():
    bps = [(D0, 2), (_d(5), 4)]
    assert value_at(bps, _d(3)) == 2
    assert value_at(bps, _d(5)) == 4
    assert value_at(bps, _d(100)) == 4


def test_shared_boundaries_unions_change_dates_across_series_within_range():
    bps_a = [(D0, 2), (_d(5), 4)]
    bps_b = [(D0, 1), (_d(8), 9)]
    result = shared_boundaries({"a": bps_a, "b": bps_b}, D0, _d(10))
    assert result == [D0, _d(5), _d(8), _d(10)]


def test_shared_boundaries_ignores_change_dates_outside_range():
    bps_a = [(_d(-5), 2), (_d(20), 4)]
    result = shared_boundaries({"a": bps_a}, D0, _d(10))
    assert result == [D0, _d(10)]
