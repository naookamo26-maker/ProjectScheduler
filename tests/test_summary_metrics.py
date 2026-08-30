"""
gui/summary_metrics.py の純粋関数（Qt非依存）の単体テスト。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 分類: scheduler（Qt非依存だがpandas/numpyが必要）。
pytestmark = pytest.mark.scheduler

pd = pytest.importorskip("pandas")

from gui.summary_metrics import (  # noqa: E402
    GRANULARITY_DAY,
    GRANULARITY_MONTH,
    GRANULARITY_WEEK,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_NOT_STARTED,
    compute_all_teams_row,
    compute_all_workflows_row,
    compute_kpi,
    compute_milestone_breakdown_all,
    compute_milestone_cumulative_progress_pct,
    compute_milestone_rows,
    compute_team_summary_rows,
    compute_workflow_summary_rows,
    active_task_counts,
    active_task_counts_by_workflow,
    period_starts,
    week_starts,
    weekly_capacity,
    weekly_concurrency_by_team,
    weekly_peak_breakdown_by_team,
    peak_concurrency,
    task_status_series,
    team_concurrency_steps,
)
from project_scheduler import _UNLIMITED_LINES  # noqa: E402


def _ts(s):
    return pd.Timestamp(s)


def _result_df(rows):
    """rows: [(job, task, team, wf, ms, start, end, overrun, violation), ...]
    result_dfに必要な最小限の列だけを持つDataFrameを組み立てる。"""
    return pd.DataFrame([{
        "Job_ID": job, "Task_ID": task, "Team_ID": team, "Workflow_ID": wf,
        "Milestone_ID": ms, "Start_Date": _ts(start), "End_Date": _ts(end),
        "Deadline_Overrun_Days": overrun, "Constraint_Violation": violation,
    } for job, task, team, wf, ms, start, end, overrun, violation in rows])


EMPTY_DF = pd.DataFrame(columns=[
    "Job_ID", "Task_ID", "Team_ID", "Workflow_ID", "Milestone_ID",
    "Start_Date", "End_Date", "Deadline_Overrun_Days", "Constraint_Violation",
])


# -- task_status_series -------------------------------------------------------

def test_task_status_defaults_to_not_started_when_absent_from_the_map():
    df = _result_df([("J1", "T1", "TEAM_1", "WF_1", "MS_1",
                       "2026-02-01", "2026-02-05", 0, "")])
    status = task_status_series(df, {})
    assert status.iloc[0] == STATUS_NOT_STARTED


def test_task_status_uses_the_recorded_value_for_a_matching_job_task_key():
    df = _result_df([("J1", "T1", "TEAM_1", "WF_1", "MS_1",
                       "2026-02-01", "2026-02-05", 0, "")])
    assert task_status_series(df, {("J1", "T1"): STATUS_IN_PROGRESS}).iloc[0] == STATUS_IN_PROGRESS
    assert task_status_series(df, {("J1", "T1"): STATUS_DONE}).iloc[0] == STATUS_DONE


def test_task_status_ignores_entries_for_other_job_task_keys():
    df = _result_df([("J1", "T1", "TEAM_1", "WF_1", "MS_1",
                       "2026-02-01", "2026-02-05", 0, "")])
    status = task_status_series(df, {("J1", "T2"): STATUS_DONE, ("J2", "T1"): STATUS_DONE})
    assert status.iloc[0] == STATUS_NOT_STARTED


def test_task_status_series_is_empty_for_empty_result_df():
    assert task_status_series(EMPTY_DF, {}).empty


# -- peak_concurrency ----------------------------------------------------------

def test_peak_concurrency_of_empty_result_is_zero_and_no_month():
    assert peak_concurrency(EMPTY_DF) == (0, None)


def test_peak_concurrency_counts_overlapping_tasks():
    # T1: 02/01-02/10, T2: 02/05-02/15, T3: 02/08-02/09 -> 02/08だけ3本重なる。
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-02-01", "2026-02-10", 0, ""),
        ("J1", "T2", "TEAM_1", "WF_1", "MS_1", "2026-02-05", "2026-02-15", 0, ""),
        ("J2", "T3", "TEAM_1", "WF_1", "MS_1", "2026-02-08", "2026-02-09", 0, ""),
    ])
    peak, month = peak_concurrency(df)
    assert peak == 3
    assert month == "2026-02"


def test_peak_concurrency_excludes_the_exclusive_end_date_itself():
    # T1が02/05に終わる(exclusive)のと同時にT2が02/05に始まっても重ならない。
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-02-01", "2026-02-05", 0, ""),
        ("J2", "T2", "TEAM_1", "WF_1", "MS_1", "2026-02-05", "2026-02-10", 0, ""),
    ])
    peak, _month = peak_concurrency(df)
    assert peak == 1


# -- compute_kpi -----------------------------------------------------------------

def test_compute_kpi_on_empty_result_df():
    kpi = compute_kpi(EMPTY_DF, _ts("2026-01-01"), [], {})
    assert kpi["jobs"] == 0
    assert kpi["tasks"] == 0
    assert kpi["plan_end"] is None
    assert kpi["milestone_margin_days"] is None
    assert kpi["peak"] == 0
    assert kpi["peak_month"] is None
    assert kpi["overrun_tasks"] == 0
    assert kpi["start_pin_violations"] == 0


def test_compute_kpi_aggregates_jobs_tasks_overruns_violations_and_recorded_status():
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-01-05", "2026-01-10", 0, ""),
        ("J1", "T2", "TEAM_1", "WF_1", "MS_1", "2026-01-10", "2026-01-20", 5, ""),
        ("J2", "T1", "TEAM_1", "WF_1", "MS_1", "2026-01-15", "2026-01-25", 3, "開始固定日を満たせません"),
    ])
    milestones = [("MS_1", "MS1", _ts("2026-01-18"))]
    task_status_map = {("J1", "T1"): STATUS_DONE, ("J1", "T2"): STATUS_IN_PROGRESS}
    kpi = compute_kpi(df, _ts("2026-01-01"), milestones, task_status_map)
    assert kpi["jobs"] == 2
    assert kpi["tasks"] == 3
    assert kpi["plan_end"] == _ts("2026-01-25")
    assert kpi["milestone_margin_days"] == (_ts("2026-01-18") - _ts("2026-01-25")).days
    assert kpi["overrun_tasks"] == 2
    assert kpi["overrun_jobs"] == 2
    assert kpi["max_overrun_days"] == 5
    assert kpi["start_pin_violations"] == 1
    # J1:T1は記録上「完了」、J1:T2は「進行中」、J2:T1はマップに無いので未着手。
    assert kpi["done"] == 1
    assert kpi["in_progress"] == 1
    assert kpi["not_started"] == 1


# -- compute_milestone_rows -------------------------------------------------------

def test_milestone_row_slack_is_negative_when_overrun():
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-01-01", "2026-02-01", 0, ""),
    ])
    milestones = [("MS_1", "MS1", _ts("2026-01-20"))]
    rows = compute_milestone_rows(df, milestones)
    row = rows[0]
    assert row["last_end_date"] == _ts("2026-02-01")
    assert row["slack_days"] == (_ts("2026-01-20") - _ts("2026-02-01")).days
    assert row["slack_days"] < 0
    assert row["on_time"] is False


def test_milestone_row_slack_is_positive_when_finished_early():
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-01-01", "2026-01-10", 0, ""),
    ])
    milestones = [("MS_1", "MS1", _ts("2026-01-20"))]
    rows = compute_milestone_rows(df, milestones)
    row = rows[0]
    assert row["slack_days"] == (_ts("2026-01-20") - _ts("2026-01-10")).days
    assert row["on_time"] is True


def test_milestone_row_with_no_tasks_has_no_slack_but_counts_as_on_time():
    milestones = [("MS_1", "MS1", _ts("2026-01-20"))]
    rows = compute_milestone_rows(EMPTY_DF, milestones)
    row = rows[0]
    assert row["last_end_date"] is None
    assert row["slack_days"] is None
    assert row["overrun_count"] == 0
    assert row["on_time"] is True


# -- compute_milestone_cumulative_progress_pct ---------------------------------------

def test_milestone_progress_is_independent_of_task_status():
    """進捗%はタスクの完了状態と無関係（計画上のタスク配分だけで決まる）。
    全タスク未着手でも、マイルストーンに割り当てられていれば進捗に数える。"""
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-01-01", "2026-01-05", 0, ""),
        ("J1", "T2", "TEAM_1", "WF_1", "MS_1", "2026-01-05", "2026-01-10", 0, ""),
        ("J2", "T1", "TEAM_1", "WF_1", "MS_2", "2026-01-01", "2026-01-05", 0, ""),
    ])
    milestones = [("MS_1", "MS1", _ts("2026-06-01")), ("MS_2", "MS2", _ts("2026-07-01"))]
    progress = compute_milestone_cumulative_progress_pct(df, milestones)
    # MS_1に2件、MS_2に1件。全体は3件なので累積は 2/3, 3/3。
    assert progress[0] == pytest.approx(200 / 3)
    assert progress[1] == pytest.approx(100.0)


def test_milestone_progress_reaches_100_percent_at_the_last_milestone():
    """タスクは必ずどれか1つのマイルストーンに属するので、最後のマイルストーン
    では累積が必ず100%になる（重複も漏れも無い）。"""
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-01-01", "2026-01-05", 0, ""),
        ("J2", "T1", "TEAM_1", "WF_1", "MS_2", "2026-01-01", "2026-01-05", 0, ""),
        ("J3", "T1", "TEAM_1", "WF_1", "MS_3", "2026-01-01", "2026-01-05", 0, ""),
    ])
    milestones = [
        ("MS_1", "MS1", _ts("2026-03-01")),
        ("MS_2", "MS2", _ts("2026-06-01")),
        ("MS_3", "MS3", _ts("2026-09-01")),
    ]
    progress = compute_milestone_cumulative_progress_pct(df, milestones)
    assert progress[-1] == pytest.approx(100.0)
    assert progress == sorted(progress)  # 単調増加であること。


def test_milestone_progress_is_zero_when_there_are_no_tasks():
    milestones = [("MS_1", "MS1", _ts("2026-01-20"))]
    assert compute_milestone_cumulative_progress_pct(EMPTY_DF, milestones) == [0.0]


def test_milestone_progress_denominator_follows_the_df_passed_in():
    """「進捗」の基準（分母）は呼び出し側が渡すdf次第——チーム別/ワークフロー別
    表示のときは、絞り込んだdfを渡すことでその対象の件数が基準になる
    （gui/tab_analysis.py._render_milestone_table参照）。TEAM_1だけに絞ると、
    TEAM_2のタスクは分母からも分子からも除外される。"""
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-01-01", "2026-01-05", 0, ""),
        ("J1", "T2", "TEAM_1", "WF_1", "MS_2", "2026-01-05", "2026-01-10", 0, ""),
        ("J2", "T1", "TEAM_2", "WF_1", "MS_1", "2026-01-01", "2026-01-05", 0, ""),
    ])
    milestones = [("MS_1", "MS1", _ts("2026-06-01")), ("MS_2", "MS2", _ts("2026-07-01"))]
    team1_only = df[df["Team_ID"] == "TEAM_1"]
    progress = compute_milestone_cumulative_progress_pct(team1_only, milestones)
    # TEAM_1は2件（MS_1に1件、MS_2に1件）なので累積は 1/2, 2/2。
    assert progress[0] == pytest.approx(50.0)
    assert progress[1] == pytest.approx(100.0)


# -- compute_milestone_breakdown_all ------------------------------------------------

def test_milestone_breakdown_all_matches_task_counts_and_recorded_status():
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-01-01", "2026-01-05", 0, ""),
        ("J1", "T2", "TEAM_1", "WF_1", "MS_1", "2026-01-05", "2026-01-10", 0, ""),
        ("J2", "T1", "TEAM_1", "WF_1", "MS_2", "2026-01-01", "2026-01-05", 0, ""),
    ])
    milestones = [("MS_1", "MS1", _ts("2026-02-01")), ("MS_2", "MS2", _ts("2026-02-01"))]
    task_status_map = {("J1", "T1"): STATUS_DONE, ("J1", "T2"): STATUS_DONE, ("J2", "T1"): STATUS_DONE}
    rows = compute_milestone_breakdown_all(df, milestones, task_status_map)
    assert rows[0] == {"jobs": 1, "tasks": 2, "done": 2, "in_progress": 0, "not_started": 0}
    assert rows[1] == {"jobs": 1, "tasks": 1, "done": 1, "in_progress": 0, "not_started": 0}


def test_milestone_breakdown_all_is_zeroed_for_a_milestone_with_no_tasks():
    milestones = [("MS_1", "MS1", _ts("2026-02-01"))]
    rows = compute_milestone_breakdown_all(EMPTY_DF, milestones, {})
    assert rows[0] == {"jobs": 0, "tasks": 0, "done": 0, "in_progress": 0, "not_started": 0}


def test_milestone_breakdown_all_on_a_team_filtered_result_df_only_counts_that_team():
    """gui/tab_analysis.py は「チーム別」「ワークフロー別」で選んだ1件の対象に
    result_dfを絞り込んでから compute_milestone_breakdown_all() に渡す
    （表示列は「全体」と同じ構成のまま）。事前に絞り込まれたDataFrameに
    対しても、その対象ぶんだけの集計になることを確認する。"""
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-01-01", "2026-01-05", 0, ""),
        ("J1", "T2", "TEAM_2", "WF_1", "MS_1", "2026-01-05", "2026-01-10", 0, ""),
        ("J2", "T1", "TEAM_1", "WF_1", "MS_1", "2026-01-01", "2026-01-05", 0, ""),
    ])
    milestones = [("MS_1", "MS1", _ts("2026-02-01"))]
    team1_only = df[df["Team_ID"] == "TEAM_1"]
    rows = compute_milestone_breakdown_all(team1_only, milestones, {})
    assert rows[0] == {"jobs": 2, "tasks": 2, "done": 0, "in_progress": 0, "not_started": 2}


# -- team_concurrency_steps / compute_team_summary_rows（チーム別サマリー） --------------

def _team_result_df(rows):
    """rows: [(job, task, team, start, end, overrun, adjusted), ...]
    チーム別サマリーの集計に必要な最小限の列だけを持つDataFrameを組み立てる。"""
    return pd.DataFrame([{
        "Job_ID": job, "Task_ID": task, "Team_ID": team,
        "Start_Date": _ts(start), "End_Date": _ts(end),
        "Deadline_Overrun_Days": overrun, "Resource_Adjusted": adjusted,
    } for job, task, team, start, end, overrun, adjusted in rows])


EMPTY_TEAM_DF = pd.DataFrame(columns=[
    "Job_ID", "Task_ID", "Team_ID", "Start_Date", "End_Date",
    "Deadline_Overrun_Days", "Resource_Adjusted",
])


def test_team_concurrency_steps_only_counts_the_given_team():
    df = _team_result_df([
        ("J1", "T1", "TEAM_1", "2026-01-01", "2026-01-10", 0, False),
        ("J2", "T1", "TEAM_2", "2026-01-01", "2026-01-10", 0, False),
    ])
    steps = team_concurrency_steps(df, "TEAM_1")
    assert steps == [(_ts("2026-01-01"), 1), (_ts("2026-01-10"), 0)]


def test_team_concurrency_steps_sums_all_teams_when_team_id_is_none():
    df = _team_result_df([
        ("J1", "T1", "TEAM_1", "2026-01-01", "2026-01-10", 0, False),
        ("J2", "T1", "TEAM_2", "2026-01-05", "2026-01-15", 0, False),
    ])
    steps = dict(team_concurrency_steps(df))
    assert steps[_ts("2026-01-05")] == 2  # 両チームのタスクが重なる


def test_team_concurrency_steps_of_empty_result_is_empty():
    assert team_concurrency_steps(EMPTY_TEAM_DF, "TEAM_1") == []


def test_compute_team_summary_rows_counts_peak_tasks_adjusted_and_overrun():
    # A: 01-01〜01-06、B: 01-03〜01-08 -> 01-03〜01-05の3日だけ2本重なる。
    df = _team_result_df([
        ("J1", "T1", "TEAM_1", "2026-01-01", "2026-01-06", 5, True),
        ("J1", "T2", "TEAM_1", "2026-01-03", "2026-01-08", 0, False),
    ])
    team_names = {"TEAM_1": "チームA"}
    capacity_schedule = {"TEAM_1": [(pd.Timestamp.min, 2)]}
    rows = compute_team_summary_rows(df, team_names, capacity_schedule)
    assert len(rows) == 1
    row = rows[0]
    assert row["team_id"] == "TEAM_1"
    assert row["name"] == "チームA"
    assert row["tasks"] == 2
    assert row["peak"] == 2
    assert row["peak_month"] == "2026-01"
    assert row["pinned_days"] == 3  # 上限2に達していた01-03,04,05の3日
    assert row["resource_adjusted"] == 1
    assert row["overrun"] == 1


def test_compute_team_summary_rows_includes_a_team_with_no_tasks():
    """タスクを1件も持たないチームも表に出す（ピーク0・空欄扱い）。"""
    team_names = {"TEAM_1": "チームA", "TEAM_2": "チームB（未使用）"}
    rows = compute_team_summary_rows(EMPTY_TEAM_DF, team_names, {})
    assert [r["team_id"] for r in rows] == ["TEAM_1", "TEAM_2"]
    for row in rows:
        assert row["tasks"] == 0
        assert row["peak"] == 0
        assert row["peak_month"] is None
        assert row["pinned_days"] is None
        assert row["resource_adjusted"] == 0
        assert row["overrun"] == 0


def test_compute_team_summary_rows_pinned_days_is_none_when_never_capped():
    """上限を一度も設定していない（指定なしのまま）チームは、タスクがあっても
    pinned_daysはNone（設計案§3「上限が『指定なし』のチームでは空欄」）。"""
    df = _team_result_df([("J1", "T1", "TEAM_1", "2026-01-01", "2026-01-10", 0, False)])
    capacity_schedule = {"TEAM_1": [(pd.Timestamp.min, _UNLIMITED_LINES)]}
    rows = compute_team_summary_rows(df, {"TEAM_1": "チームA"}, capacity_schedule)
    assert rows[0]["pinned_days"] is None


# -- compute_all_teams_row（表の先頭「全チーム」行） ------------------------------------

def test_all_teams_row_peak_is_the_overall_peak_not_the_sum_of_team_peaks():
    """「全チーム」行のピークは、各チームのピークの和ではなく全体の最大
    （＝KPIタイルの「同時タスク数のピーク」と一致する）。TEAM_1が前半に2本、
    TEAM_2が後半に2本で重ならないので、和なら4だが実際の最大は2。"""
    df = _team_result_df([
        ("J1", "T1", "TEAM_1", "2026-01-05", "2026-01-10", 0, False),
        ("J1", "T2", "TEAM_1", "2026-01-05", "2026-01-10", 0, False),
        ("J2", "T1", "TEAM_2", "2026-01-20", "2026-01-25", 0, False),
        ("J2", "T2", "TEAM_2", "2026-01-20", "2026-01-25", 0, False),
    ])
    row = compute_all_teams_row(df)
    overall_peak, overall_month = peak_concurrency(df)
    assert row["peak"] == overall_peak == 2
    assert row["peak_month"] == overall_month


def test_all_teams_row_totals_tasks_adjusted_and_overrun_across_teams():
    df = _team_result_df([
        ("J1", "T1", "TEAM_1", "2026-01-05", "2026-01-10", 3, True),
        ("J2", "T1", "TEAM_2", "2026-01-20", "2026-01-25", 0, False),
    ])
    row = compute_all_teams_row(df)
    assert row["team_id"] is None       # 個別チームの行と見分けるための目印
    assert row["name"] == "全チーム"
    assert row["tasks"] == 2
    assert row["resource_adjusted"] == 1
    assert row["overrun"] == 1
    # 上限はチーム単位の設定なので、全チームまとめた行では空欄にする。
    assert row["pinned_days"] is None


def test_all_teams_row_on_empty_result_df():
    row = compute_all_teams_row(EMPTY_TEAM_DF)
    assert row["tasks"] == 0
    assert row["peak"] == 0
    assert row["peak_month"] is None
    assert row["resource_adjusted"] == 0
    assert row["overrun"] == 0


# -- 週次集計（チーム別サマリーのグラフ用） -------------------------------------------

def test_week_starts_are_mondays_covering_both_ends():
    """2026-01-15は木曜。その週の月曜（01-12）から始まり、range_endを含む
    週まで並ぶこと。"""
    assert week_starts(_ts("2026-01-15"), _ts("2026-02-02")) == [
        _ts("2026-01-12"), _ts("2026-01-19"), _ts("2026-01-26"), _ts("2026-02-02"),
    ]


def test_week_starts_of_a_range_inside_one_week_is_one_entry():
    assert week_starts(_ts("2026-01-13"), _ts("2026-01-16")) == [_ts("2026-01-12")]


def test_weekly_concurrency_takes_the_max_within_each_week_not_the_average():
    """週内の最大を採る（平均だと、週の一部だけ突出した山が均されて消える）。
    T1が2週間ずっと1本、T2が2週目の火・水だけ重なって2本になる。"""
    df = _team_result_df([
        ("J1", "T1", "TEAM_1", "2026-01-12", "2026-01-26", 0, False),
        ("J1", "T2", "TEAM_1", "2026-01-20", "2026-01-22", 0, False),
    ])
    series = weekly_concurrency_by_team(df, ["TEAM_1"], _ts("2026-01-12"), _ts("2026-01-26"))
    assert series["TEAM_1"][0] == 1   # 1週目: T1のみ
    assert series["TEAM_1"][1] == 2   # 2週目: 重なった瞬間の2本
    assert series["TEAM_1"][2] == 0   # 3週目: 01-26はEnd_Date(exclusive)なので0


def test_weekly_concurrency_spans_every_week_a_long_task_runs_through():
    """1本のタスクが数週間にまたがる場合、その全週に1が立つこと
    （変化点のある週だけでなく、間の週も埋まる）。"""
    df = _team_result_df([("J1", "T1", "TEAM_1", "2026-01-12", "2026-02-09", 0, False)])
    series = weekly_concurrency_by_team(df, ["TEAM_1"], _ts("2026-01-12"), _ts("2026-02-09"))
    assert series["TEAM_1"] == [1, 1, 1, 1, 0]


def test_weekly_concurrency_separates_teams():
    df = _team_result_df([
        ("J1", "T1", "TEAM_1", "2026-01-12", "2026-01-19", 0, False),
        ("J2", "T1", "TEAM_2", "2026-01-19", "2026-01-26", 0, False),
    ])
    series = weekly_concurrency_by_team(
        df, ["TEAM_1", "TEAM_2"], _ts("2026-01-12"), _ts("2026-01-26")
    )
    assert series["TEAM_1"] == [1, 0, 0]
    assert series["TEAM_2"] == [0, 1, 0]


def test_weekly_concurrency_of_a_team_with_no_tasks_is_all_zero():
    df = _team_result_df([("J1", "T1", "TEAM_1", "2026-01-12", "2026-01-26", 0, False)])
    series = weekly_concurrency_by_team(
        df, ["TEAM_1", "TEAM_2"], _ts("2026-01-12"), _ts("2026-01-26")
    )
    assert series["TEAM_2"] == [0, 0, 0]


def test_weekly_peak_breakdown_totals_are_a_real_simultaneous_count_not_a_sum_of_peaks():
    """積み上げグラフの高さ＝実在した同時タスク数であること。

    設計案§3が明示しているとおり「全体のピークは各チームのピークの和には
    ならない（時期がずれるため）」——TEAM_1は週の月〜火に2本、TEAM_2は木〜金に
    2本で、両者が重なる日は無い。チームごとの週内最大を足すと4になるが、
    実際に同時に走った本数の最大は2。"""
    df = _team_result_df([
        ("J1", "T1", "TEAM_1", "2026-01-12", "2026-01-14", 0, False),
        ("J1", "T2", "TEAM_1", "2026-01-12", "2026-01-14", 0, False),
        ("J2", "T1", "TEAM_2", "2026-01-15", "2026-01-17", 0, False),
        ("J2", "T2", "TEAM_2", "2026-01-15", "2026-01-17", 0, False),
    ])
    team_ids = ["TEAM_1", "TEAM_2"]
    args = (df, team_ids, _ts("2026-01-12"), _ts("2026-01-17"))

    # 単純な週内最大の和なら 2+2=4 になってしまう。
    naive = weekly_concurrency_by_team(*args)
    assert naive["TEAM_1"][0] + naive["TEAM_2"][0] == 4

    series, totals = weekly_peak_breakdown_by_team(*args)
    assert totals[0] == 2  # 実際に同時に走った最大は2本
    assert series["TEAM_1"][0] + series["TEAM_2"][0] == totals[0]


def test_weekly_peak_breakdown_totals_always_equal_the_sum_of_the_bands():
    """積み上げの不変条件: どの週でも「帯の合計＝totals」であること
    （グラフの高さと合計ツールチップが食い違わないため）。"""
    df = _team_result_df([
        ("J1", "T1", "TEAM_1", "2026-01-05", "2026-03-10", 0, False),
        ("J1", "T2", "TEAM_2", "2026-01-20", "2026-02-15", 0, False),
        ("J2", "T1", "TEAM_2", "2026-02-01", "2026-03-01", 0, False),
        ("J2", "T2", "TEAM_3", "2026-01-01", "2026-03-20", 0, False),
    ])
    team_ids = ["TEAM_1", "TEAM_2", "TEAM_3"]
    series, totals = weekly_peak_breakdown_by_team(
        df, team_ids, _ts("2026-01-01"), _ts("2026-03-20")
    )
    for i in range(len(totals)):
        assert sum(series[team_id][i] for team_id in team_ids) == totals[i]


def test_weekly_peak_breakdown_max_total_equals_the_overall_peak():
    """週ごとの合計の最大は、全体のピーク（peak_concurrency）と一致すること
    ——グラフのピーク目印とKPIタイルが食い違わないため。"""
    df = _team_result_df([
        ("J1", "T1", "TEAM_1", "2026-01-05", "2026-02-10", 0, False),
        ("J1", "T2", "TEAM_2", "2026-01-20", "2026-02-15", 0, False),
    ])
    _series, totals = weekly_peak_breakdown_by_team(
        df, ["TEAM_1", "TEAM_2"], _ts("2026-01-05"), _ts("2026-02-15")
    )
    overall_peak, _month = peak_concurrency(df)
    assert max(totals) == overall_peak


def test_weekly_peak_breakdown_is_all_zero_for_a_team_with_no_tasks():
    df = _team_result_df([("J1", "T1", "TEAM_1", "2026-01-12", "2026-01-26", 0, False)])
    series, _totals = weekly_peak_breakdown_by_team(
        df, ["TEAM_1", "TEAM_2"], _ts("2026-01-12"), _ts("2026-01-26")
    )
    assert series["TEAM_2"] == [0, 0, 0]


def test_weekly_capacity_returns_none_for_unlimited_weeks():
    """上限「指定なし」の週はNone（呼び出し側が破線を描かない目印）。"""
    weeks = [_ts("2026-01-12"), _ts("2026-01-19")]
    assert weekly_capacity([(pd.Timestamp.min, _UNLIMITED_LINES)], weeks) == [None, None]


def test_weekly_capacity_follows_a_mid_period_change():
    """週の月曜時点で有効な値を採る。2026-01-19から3になるので、2週目以降が3。"""
    weeks = [_ts("2026-01-12"), _ts("2026-01-19"), _ts("2026-01-26")]
    periods = [(pd.Timestamp.min, 1), (_ts("2026-01-19"), 3)]
    assert weekly_capacity(periods, weeks) == [1, 3, 3]


def test_weekly_capacity_keeps_zero_meaning_inactive():
    """0（稼働なし）はNULL（指定なし）と区別してそのまま返す。"""
    assert weekly_capacity([(pd.Timestamp.min, 0)], [_ts("2026-01-12")]) == [0]


def test_compute_team_summary_rows_pinned_days_follows_a_mid_period_capacity_change():
    """上限が期間中に変わる場合も、変化点をまたいで正しく判定できること。
    同時タスク数は常に1（Aのみ）。上限は01-05を境に1→3に変わるので、
    「上限=1」と一致する01-01〜01-04の4日だけがpinned。"""
    df = _team_result_df([("J1", "T1", "TEAM_1", "2026-01-01", "2026-01-10", 0, False)])
    capacity_schedule = {"TEAM_1": [(pd.Timestamp.min, 1), (_ts("2026-01-05"), 3)]}
    rows = compute_team_summary_rows(df, {"TEAM_1": "チームA"}, capacity_schedule)
    assert rows[0]["pinned_days"] == 4


# -- period_starts / active_task_counts（ワークフロー別サマリー） ------------------------

def test_period_starts_covers_the_range_at_each_granularity():
    """月次は各月の1日、週次は各週の月曜、日次は各日。いずれも範囲を覆う。"""
    assert period_starts("2026-02-10", "2026-04-03", GRANULARITY_MONTH) == [
        _ts("2026-02-01"), _ts("2026-03-01"), _ts("2026-04-01"),
    ]
    assert period_starts("2026-02-10", "2026-02-24", GRANULARITY_WEEK) == [
        _ts("2026-02-09"), _ts("2026-02-16"), _ts("2026-02-23"),
    ]
    assert period_starts("2026-02-10", "2026-02-12", GRANULARITY_DAY) == [
        _ts("2026-02-10"), _ts("2026-02-11"), _ts("2026-02-12"),
    ]


def test_active_task_counts_counts_a_task_once_per_period_it_spans():
    """稼働タスク件数は「その期間に1日でも走っているタスクの本数」。長いタスクが
    期間をまたいでも、またいだ全期間で数える（開始件数だと山が消える）。"""
    df = _result_df([("J1", "T1", "TEAM_1", "WF_1", "MS_1",
                       "2026-02-10", "2026-04-02", 0, "")])
    periods = period_starts("2026-02-01", "2026-04-30", GRANULARITY_MONTH)
    # 2月・3月・4月。End_Dateはexclusiveなので最終稼働日は04-01＝4月も1件。
    assert active_task_counts(df, periods, GRANULARITY_MONTH) == [1, 1, 1]


def test_active_task_counts_excludes_periods_outside_the_task_span():
    """End_Dateはexclusive。02-02終了のタスクは02-02の日次期間には入らない。"""
    df = _result_df([("J1", "T1", "TEAM_1", "WF_1", "MS_1",
                       "2026-02-01", "2026-02-02", 0, "")])
    periods = period_starts("2026-02-01", "2026-02-03", GRANULARITY_DAY)
    assert active_task_counts(df, periods, GRANULARITY_DAY) == [1, 0, 0]


def test_active_task_counts_is_all_zero_for_an_empty_result_df():
    periods = period_starts("2026-02-01", "2026-02-03", GRANULARITY_DAY)
    assert active_task_counts(EMPTY_DF, periods, GRANULARITY_DAY) == [0, 0, 0]


def test_active_task_counts_by_workflow_totals_match_the_sum_of_the_series():
    """積み上げグラフの高さと合計が必ず一致すること（1タスクは1ワークフローに
    しか属さないので、重複して数えない）。"""
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-02-02", "2026-02-20", 0, ""),
        ("J2", "T1", "TEAM_1", "WF_2", "MS_1", "2026-02-09", "2026-02-13", 0, ""),
        ("J3", "T1", "TEAM_1", "WF_2", "MS_1", "2026-02-16", "2026-02-27", 0, ""),
    ])
    periods = period_starts("2026-02-02", "2026-02-27", GRANULARITY_WEEK)
    series, totals = active_task_counts_by_workflow(
        df, ["WF_1", "WF_2"], periods, GRANULARITY_WEEK,
    )
    assert series["WF_1"] == [1, 1, 1, 0]
    assert series["WF_2"] == [0, 1, 1, 1]
    assert totals == [1, 2, 2, 1]
    for i in range(len(periods)):
        assert sum(values[i] for values in series.values()) == totals[i]


# -- compute_workflow_summary_rows ---------------------------------------------

def test_compute_workflow_summary_rows_counts_jobs_tasks_and_overrun():
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-02-02", "2026-02-06", 0, ""),
        ("J1", "T2", "TEAM_1", "WF_1", "MS_1", "2026-02-06", "2026-02-10", 3, ""),
        ("J2", "T1", "TEAM_1", "WF_2", "MS_1", "2026-02-02", "2026-02-04", 0, ""),
    ])
    rows = compute_workflow_summary_rows(df, {"WF_1": "WF1", "WF_2": "WF2"})
    assert [row["name"] for row in rows] == ["WF1", "WF2"]
    assert rows[0]["jobs"] == 1 and rows[0]["tasks"] == 2 and rows[0]["overrun"] == 1
    assert rows[1]["jobs"] == 1 and rows[1]["tasks"] == 1 and rows[1]["overrun"] == 0


def test_workflow_median_duration_is_the_span_of_each_job_not_the_sum():
    """所要期間は「1ジョブの最初のタスク開始〜最後のタスク終了」の暦日で、
    合計せず中央値だけを見る（設計案§3）。J1は8日、J2は2日、J3は4日→中央値4。"""
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-02-02", "2026-02-06", 0, ""),
        ("J1", "T2", "TEAM_1", "WF_1", "MS_1", "2026-02-06", "2026-02-10", 0, ""),
        ("J2", "T1", "TEAM_1", "WF_1", "MS_1", "2026-03-02", "2026-03-04", 0, ""),
        ("J3", "T1", "TEAM_1", "WF_1", "MS_1", "2026-04-02", "2026-04-06", 0, ""),
    ])
    rows = compute_workflow_summary_rows(df, {"WF_1": "WF1"})
    assert rows[0]["median_duration_days"] == 4


def test_workflow_median_duration_of_an_even_number_of_jobs_averages_the_middle_two():
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-02-02", "2026-02-04", 0, ""),
        ("J2", "T1", "TEAM_1", "WF_1", "MS_1", "2026-03-02", "2026-03-07", 0, ""),
    ])
    rows = compute_workflow_summary_rows(df, {"WF_1": "WF1"})
    assert rows[0]["median_duration_days"] == 3.5


def test_workflow_row_for_a_workflow_without_tasks_is_zero_and_has_no_median():
    """タスクが1件も無いワークフローでも行は出す（中央値だけNone＝表では空欄）。"""
    df = _result_df([("J1", "T1", "TEAM_1", "WF_1", "MS_1",
                       "2026-02-02", "2026-02-06", 0, "")])
    rows = compute_workflow_summary_rows(df, {"WF_1": "WF1", "WF_2": "WF2"})
    assert rows[1]["jobs"] == 0 and rows[1]["tasks"] == 0
    assert rows[1]["median_duration_days"] is None


def test_compute_workflow_summary_rows_on_empty_result_df():
    rows = compute_workflow_summary_rows(EMPTY_DF, {"WF_1": "WF1"})
    assert rows[0]["jobs"] == 0 and rows[0]["tasks"] == 0
    assert rows[0]["median_duration_days"] is None and rows[0]["overrun"] == 0


# -- compute_all_workflows_row（表の先頭「全ワークフロー」行） ---------------------------

def test_all_workflows_row_totals_jobs_tasks_and_overrun_across_workflows():
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-02-02", "2026-02-06", 3, ""),
        ("J2", "T1", "TEAM_1", "WF_2", "MS_1", "2026-02-02", "2026-02-04", 0, ""),
    ])
    row = compute_all_workflows_row(df)
    assert row["workflow_id"] is None       # 個別ワークフローの行と見分けるための目印
    assert row["name"] == "全ワークフロー"
    assert row["jobs"] == 2
    assert row["tasks"] == 2
    assert row["overrun"] == 1


def test_all_workflows_row_median_duration_spans_jobs_across_all_workflows():
    """所要期間の中央値は、ワークフローをまたいだジョブ全体から求める
    （compute_workflow_summary_rowsの各行と同じ定義。J1は8日、J2は2日→中央値5）。"""
    df = _result_df([
        ("J1", "T1", "TEAM_1", "WF_1", "MS_1", "2026-02-02", "2026-02-06", 0, ""),
        ("J1", "T2", "TEAM_1", "WF_1", "MS_1", "2026-02-06", "2026-02-10", 0, ""),
        ("J2", "T1", "TEAM_1", "WF_2", "MS_1", "2026-03-02", "2026-03-04", 0, ""),
    ])
    row = compute_all_workflows_row(df)
    assert row["median_duration_days"] == 5


def test_all_workflows_row_on_empty_result_df():
    row = compute_all_workflows_row(EMPTY_DF)
    assert row["jobs"] == 0
    assert row["tasks"] == 0
    assert row["median_duration_days"] is None
    assert row["overrun"] == 0
