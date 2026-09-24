"""
ガントチャート上での編集用の、Qtに依存しない部品（gui/gantt_edit.py）のテスト。

分類は core（Qt・pandas いずれにも依存しない）。
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.gantt_edit import WorkDayCalendar, parse_entity_id  # noqa: E402

pytestmark = pytest.mark.core

# 2026-05-01（金）〜。5/4〜5/6 は祝日（みどりの日等）として与える。
MON_0427 = date(2026, 4, 27)
FRI_0501 = date(2026, 5, 1)
SAT_0502 = date(2026, 5, 2)
MON_0504 = date(2026, 5, 4)
THU_0507 = date(2026, 5, 7)
FRI_0508 = date(2026, 5, 8)


@pytest.fixture
def cal():
    return WorkDayCalendar(
        common_holidays={date(2026, 5, 6)},
        holidays_by_team={"TEAM_002": {THU_0507}},
        jp_holidays={MON_0504, date(2026, 5, 5)},
    )


def test_parse_entity_id():
    assert parse_entity_id("JOB_012") == 12
    assert parse_entity_id("T_1234") == 1234
    assert parse_entity_id("TEAM_003") == 3


def test_weekends_and_holidays_are_not_working_days(cal):
    assert cal.is_working(FRI_0501)
    assert not cal.is_working(SAT_0502)
    assert not cal.is_working(MON_0504)       # 祝日
    assert not cal.is_working(date(2026, 5, 6))  # 全チーム共通の休業日
    assert cal.is_working(THU_0507)            # チーム指定なしなら稼働日
    assert not cal.is_working(THU_0507, "TEAM_002")  # そのチームだけの休業日


def test_next_working_snaps_forward_over_weekend_and_holidays(cal):
    assert cal.next_working(SAT_0502) == THU_0507
    assert cal.next_working(SAT_0502, "TEAM_002") == FRI_0508
    assert cal.next_working(FRI_0501) == FRI_0501


def test_shift_counts_working_days_in_both_directions(cal):
    assert cal.shift(FRI_0501, 1) == THU_0507
    assert cal.shift(FRI_0501, 1, "TEAM_002") == FRI_0508
    assert cal.shift(THU_0507, -1) == FRI_0501
    assert cal.shift(MON_0427, 5) == THU_0507  # 4/28,29,30,5/1,5/7
    assert cal.shift(FRI_0501, 0) == FRI_0501


def test_diff_is_the_inverse_of_shift(cal):
    for n in (-6, -1, 0, 1, 3, 10):
        for team in (None, "TEAM_002"):
            moved = cal.shift(MON_0427, n, team)
            assert cal.diff(MON_0427, moved, team) == n


def test_diff_snaps_a_non_working_target_forward(cal):
    # 土曜に落としたら次の稼働日（木曜）とみなす
    assert cal.diff(FRI_0501, SAT_0502) == 1


def test_duration_helpers_use_exclusive_end(cal):
    end = cal.end_exclusive(FRI_0501, 2)
    assert end == FRI_0508  # 5/1 と 5/7 の2日。終了は翌日の5/8（exclusive）
    assert cal.count(FRI_0501, end) == 2
    assert cal.end_exclusive(FRI_0501, 1) == SAT_0502
