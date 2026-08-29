"""
gui/summary_metrics.py の純粋関数（Qt非依存）の単体テスト。
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 分類: scheduler（Qt非依存だがpandas/numpyが必要）。
pytestmark = pytest.mark.scheduler

pd = pytest.importorskip("pandas")

from gui.summary_metrics import (  # noqa: E402
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_NOT_STARTED,
    compute_kpi,
    compute_milestone_breakdown_all,
    compute_milestone_rows,
    compute_team_summary_rows,
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
    rows = compute_milestone_rows(df, milestones, date(2026, 1, 1))
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
    rows = compute_milestone_rows(df, milestones, date(2026, 1, 1))
    row = rows[0]
    assert row["slack_days"] == (_ts("2026-01-20") - _ts("2026-01-10")).days
    assert row["on_time"] is True


def test_milestone_row_with_no_tasks_has_no_slack_but_counts_as_on_time():
    milestones = [("MS_1", "MS1", _ts("2026-01-20"))]
    rows = compute_milestone_rows(EMPTY_DF, milestones, date(2026, 1, 1))
    row = rows[0]
    assert row["last_end_date"] is None
    assert row["slack_days"] is None
    assert row["overrun_count"] == 0
    assert row["on_time"] is True


def test_milestone_row_remaining_days_is_zero_after_due_date():
    milestones = [("MS_1", "MS1", _ts("2026-01-01"))]
    rows = compute_milestone_rows(EMPTY_DF, milestones, date(2026, 2, 1))
    assert rows[0]["remaining_days"] == 0


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


def test_compute_team_summary_rows_pinned_days_follows_a_mid_period_capacity_change():
    """上限が期間中に変わる場合も、変化点をまたいで正しく判定できること。
    同時タスク数は常に1（Aのみ）。上限は01-05を境に1→3に変わるので、
    「上限=1」と一致する01-01〜01-04の4日だけがpinned。"""
    df = _team_result_df([("J1", "T1", "TEAM_1", "2026-01-01", "2026-01-10", 0, False)])
    capacity_schedule = {"TEAM_1": [(pd.Timestamp.min, 1), (_ts("2026-01-05"), 3)]}
    rows = compute_team_summary_rows(df, {"TEAM_1": "チームA"}, capacity_schedule)
    assert rows[0]["pinned_days"] == 4
