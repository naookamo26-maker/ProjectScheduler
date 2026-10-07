"""
ガントチャート（gui/gantt_view.py）の、土曜日・休日の帯と、バーに出す項目
（タスク名＋日数など。オプションで選ぶ）のテスト。

build_gantt_scenes() はDB/スケジューラーを介さずDataFrameだけで呼べるため、
qapp だけで直接検証する。
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 分類: gui（PySide6 が必要）
pytestmark = pytest.mark.gui

pytest.importorskip("PySide6")
pd = pytest.importorskip("pandas")

from PySide6.QtGui import QFont, QFontMetrics, QTransform  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui.gantt_edit import WorkDayCalendar  # noqa: E402
from gui.gantt_view import (  # noqa: E402
    _HOLIDAY_BAND_COLOR,
    _HOLIDAY_LABEL_COLOR,
    _SATURDAY_BAND_COLOR,
    _SATURDAY_LABEL_COLOR,
    BAR_LABEL_DAYS,
    BAR_LABEL_MILESTONE,
    BAR_LABEL_PERIOD,
    BAR_LABEL_SLACK,
    BAR_LABEL_TEAM,
    DAY_COUNT_CALENDAR,
    DAY_COUNT_WORK,
    DAY_WIDTH,
    LEFT_MARGIN,
    BarLabelOptions,
    FrozenGanttPane,
    TaskLabelFacts,
    build_gantt_scenes,
    compose_bar_label,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# 2026-10-05 は月曜日。10-10（土）・10-11（日）・10-12（月・スポーツの日）
MON = date(2026, 10, 5)
SAT = date(2026, 10, 10)
SUN = date(2026, 10, 11)
HOLIDAY_MON = date(2026, 10, 12)
COMMON_OFF = date(2026, 10, 14)  # 水曜日。全チーム共通の休業日
TEAM_OFF = date(2026, 10, 15)    # 木曜日。チームAだけの休業日

_DISPLAY = {
    "team_names": {"TEAM_001": "チームA"},
    "team_colors": {"TEAM_001": "#cccccc"},
    "workflow_names": {"WF_001": "WF1"},
    "workflow_colors": {"WF_001": "#dddddd"},
    "milestone_markers": [("MS_001", "α版", pd.Timestamp("2026-10-30"))],
    "common_holiday_dates": {COMMON_OFF},
    "holidays_by_team": {"TEAM_001": {TEAM_OFF}},
    "jp_holiday_dates": {HOLIDAY_MON},
    "job_tags": {},
}


def _df(task_name="設計", start="2026-10-05", end="2026-10-17", overrun=0):
    return pd.DataFrame([{
        "Job_ID": "JOB_001", "Task_ID": "T_001", "Job_Name": "ジョブA", "Task_Name": task_name,
        "Workflow_ID": "WF_001", "Team_ID": "TEAM_001", "Milestone_ID": "MS_001",
        "Start_Date": pd.Timestamp(start), "End_Date": pd.Timestamp(end),
        "Deadline_Overrun_Days": overrun, "Constraint_Violation": "", "Resource_Adjusted": False,
    }])


def _pane(scenes, day_px, row_scale=4.0):
    pane = FrozenGanttPane()
    pane.resize(900, 400)
    pane.setScene(scenes)
    pane.body.setTransform(QTransform.fromScale(day_px / DAY_WIDTH, row_scale))
    pane.body.transformChanged.emit()
    return pane


# -- 土曜日・休日の帯と日付ラベルの色 -------------------------------------------------

def _band_days(scenes, color):
    """本体の帯のうち color のものが覆う日付の集合。"""
    axis_start = scenes.body.gantt_axis_start
    days = set()
    for band in scenes.body.gantt_day_bands:
        if band.brush().color() != color:
            continue
        for polygon in band.path().toFillPolygons():
            rect = polygon.boundingRect()
            first = round((rect.left() - LEFT_MARGIN) / DAY_WIDTH)
            for offset in range(round(rect.width() / DAY_WIDTH)):
                days.add(date.fromordinal(axis_start.toordinal() + first + offset))
    return days


def test_saturdays_get_a_blue_band_and_sundays_holidays_a_red_band(qapp):
    scenes = build_gantt_scenes(_df(), _DISPLAY)
    saturdays = _band_days(scenes, _SATURDAY_BAND_COLOR)
    off_days = _band_days(scenes, _HOLIDAY_BAND_COLOR)
    assert SAT in saturdays
    assert {SUN, HOLIDAY_MON, COMMON_OFF} <= off_days
    # チーム別の休業日は帯にしない（1つのジョブに複数のチームが混ざるため）
    assert TEAM_OFF not in off_days | saturdays
    assert MON not in off_days | saturdays


def test_a_saturday_holiday_gets_the_red_band(qapp):
    display = dict(_DISPLAY, jp_holiday_dates={SAT})
    scenes = build_gantt_scenes(_df(), display)
    assert SAT in _band_days(scenes, _HOLIDAY_BAND_COLOR)
    assert SAT not in _band_days(scenes, _SATURDAY_BAND_COLOR)


def test_bands_appear_only_when_day_labels_are_shown(qapp):
    scenes = build_gantt_scenes(_df(), _DISPLAY)
    bands = scenes.body.gantt_day_bands + scenes.header.gantt_day_bands
    assert bands and not any(band.isVisible() for band in bands)

    pane = _pane(scenes, day_px=20)  # 日の補助線は出るが、日付ラベルはまだ出ない
    assert not any(band.isVisible() for band in bands)

    pane.body.setTransform(QTransform.fromScale(50 / DAY_WIDTH, 4.0))
    pane.body.transformChanged.emit()
    assert all(band.isVisible() for band in bands)


def test_day_labels_are_blue_on_saturdays_and_red_on_days_off(qapp):
    scenes = build_gantt_scenes(_df(), _DISPLAY)
    colors = {label.text(): label.brush().color() for _h, _b, label in scenes.header.gantt_day_ticks}
    assert colors[str(SAT.day)] == _SATURDAY_LABEL_COLOR
    assert colors[str(SUN.day)] == _HOLIDAY_LABEL_COLOR
    assert colors[str(COMMON_OFF.day)] == _HOLIDAY_LABEL_COLOR
    assert colors[str(TEAM_OFF.day)] == _HOLIDAY_LABEL_COLOR  # 表示中のチームの休業日
    assert colors["13"] not in (_SATURDAY_LABEL_COLOR, _HOLIDAY_LABEL_COLOR)

    # 月曜日の祝日は週の目盛り（MM/DD）のラベル。日付ラベルを出すほど拡大したときだけ赤字
    week = {full: label for _h, _b, label, _m, full, _c in scenes.header.gantt_week_ticks}
    pane = _pane(scenes, day_px=50)
    assert week[HOLIDAY_MON.strftime("%m/%d")].brush().color() == _HOLIDAY_LABEL_COLOR
    pane.body.setTransform(QTransform.fromScale(5 / DAY_WIDTH, 4.0))
    pane.body.transformChanged.emit()
    assert week[HOLIDAY_MON.strftime("%m/%d")].brush().color() != _HOLIDAY_LABEL_COLOR


# -- バーに出す項目の文言 -------------------------------------------------------------

def _facts(start=MON, end=date(2026, 10, 17), overrun=0, deadline=date(2026, 10, 30)):
    return TaskLabelFacts(
        start, end, team_key="TEAM_001", team_name="チームA", milestone_name="α版",
        deadline=deadline, overrun_days=overrun,
        calendar=WorkDayCalendar.from_display(_DISPLAY),
    )


def test_days_are_counted_in_working_days_or_calendar_days(qapp):
    facts = _facts()
    # 10/05〜10/16 の12日のうち、土日・祝日・共通休業日・チームの休業日を除く7日
    assert facts.text(BAR_LABEL_DAYS, DAY_COUNT_WORK) == "7営業日"
    assert facts.text(BAR_LABEL_DAYS, DAY_COUNT_CALENDAR) == "12日"


def test_slack_counts_days_to_the_deadline_and_overrun_in_calendar_days(qapp):
    facts = _facts()
    # 10/17〜10/29（締切 10/30 の前日まで）: 13日、うち営業日は 10/19〜23・26〜29 の9日
    assert facts.text(BAR_LABEL_SLACK, DAY_COUNT_WORK) == "余裕9営業日"
    assert facts.text(BAR_LABEL_SLACK, DAY_COUNT_CALENDAR) == "余裕13日"
    late = _facts(overrun=3)
    assert late.text(BAR_LABEL_SLACK, DAY_COUNT_WORK) == "超過3日"
    assert _facts(deadline=None).text(BAR_LABEL_SLACK, DAY_COUNT_WORK) is None


def test_period_team_and_milestone(qapp):
    facts = _facts()
    assert facts.text(BAR_LABEL_PERIOD, DAY_COUNT_WORK) == "10/05〜10/16"
    assert _facts(end=date(2026, 10, 6)).text(BAR_LABEL_PERIOD, DAY_COUNT_WORK) == "10/05"
    assert facts.text(BAR_LABEL_TEAM, DAY_COUNT_WORK) == "チームA"
    assert facts.text(BAR_LABEL_MILESTONE, DAY_COUNT_WORK) == "α版"


# -- バーに収まるかどうかで文言を決める ----------------------------------------------

@pytest.fixture
def metrics(qapp):
    font = QFont()
    font.setPointSize(9)
    return QFontMetrics(font)


def _w(metrics, text):
    return metrics.horizontalAdvance(text)


def test_extras_go_on_a_second_line_when_the_bar_is_tall(metrics):
    h = metrics.height() * 2 + 10
    text = compose_bar_label("設計", ["5営業日", "チームA"], 400, h, metrics)
    assert text == "設計\n5営業日 · チームA"


def test_extras_follow_the_name_on_one_line_when_the_bar_is_low(metrics):
    h = metrics.height() + 1
    text = compose_bar_label("設計", ["5営業日", "チームA"], 400, h, metrics)
    assert text == "設計 · 5営業日 · チームA"


def test_extras_that_do_not_fit_whole_are_dropped_from_the_end(metrics):
    h = metrics.height() + 1
    width = _w(metrics, "設計 · 5営業日") + 12  # 余白(10px)を残して「5営業日」までは収まる
    assert compose_bar_label("設計", ["5営業日", "チームA"], width, h, metrics) == "設計 · 5営業日"
    # 余白を残せないほど狭ければ、項目は添えずタスク名だけ
    width = _w(metrics, "設計 · 5営業日") + 2
    assert compose_bar_label("設計", ["5営業日", "チームA"], width, h, metrics) == "設計"


def test_a_name_that_does_not_fit_gets_no_extras(metrics):
    h = metrics.height() * 2 + 10
    name = "とても長いタスクの名前" * 3
    text = compose_bar_label(name, ["5営業日"], _w(metrics, name) / 2, h, metrics)
    assert text is not None and "5営業日" not in text


def test_without_the_name_only_whole_extras_are_shown(metrics):
    h = metrics.height() + 1
    assert compose_bar_label("", ["5営業日", "チームA"], 400, h, metrics) == "5営業日 · チームA"
    assert compose_bar_label("", ["5営業日"], _w(metrics, "5営業日"), h, metrics) is None
    assert compose_bar_label("", [], 400, h, metrics) is None


def test_pane_redraws_labels_when_the_options_change(qapp):
    scenes = build_gantt_scenes(_df(), _DISPLAY)
    pane = _pane(scenes, day_px=50)
    label = scenes.body.gantt_task_labels[0][0]
    assert label.text() == "設計\n7営業日"  # 既定はタスク名＋日数（営業日）

    pane.set_bar_label_options(BarLabelOptions(True, (BAR_LABEL_DAYS, BAR_LABEL_SLACK), DAY_COUNT_CALENDAR))
    assert label.text() == "設計\n12日 · 余裕13日"

    pane.set_bar_label_options(BarLabelOptions(True, (), DAY_COUNT_WORK))
    assert label.text() == "設計"

    # 縮小してバーが小さくなると、添える項目は消える
    pane.set_bar_label_options(BarLabelOptions(True, (BAR_LABEL_DAYS,), DAY_COUNT_WORK))
    pane.body.setTransform(QTransform.fromScale(4 / DAY_WIDTH, 1.0))
    pane.body.transformChanged.emit()
    assert "営業日" not in label.text()
