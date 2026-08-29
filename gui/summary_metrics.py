"""
タブ5「プロジェクト分析」の集計（Qt非依存の純粋関数）。

ガントチャートタブが計算済みのスケジューリング結果（`result_df`）を集計する
だけで、このモジュール自身はスケジューリングを一切行わない
（docs/project_analysis_tab_design.md参照）。

`result_df` の `End_Date` は exclusive（`project_scheduler.py` の
`_WorkCalendar.business_end` 参照——「営業日n日分の終了日」は最後の稼働日の
"翌日"の序数）。このモジュールの集計はすべてこの規約に合わせてある:

- 「日付dにタスクが稼働している」は `Start_Date <= d < End_Date`。
- マイルストーンの締切・「最終終了日」等の表示・差分計算は、他のタブ
  （`gui/gantt_view.py` のツールチップ等）と同じくEnd_Dateをそのまま使う
  （減算で調整しない）。`project_scheduler.py` の `Deadline_Overrun_Days`
  （`max(0, End_Date - 締切)`）と同じ基準に揃えることで、超過件数・スラックの
  符号が一致する。
"""

import pandas as pd

STATUS_NOT_STARTED = "not_started"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"


def task_status_series(result_df, task_status_map):
    """タスクごとの状態（未着手/進行中/完了）を、result_dfと同じ添字のSeriesで
    返す。空のresult_dfには空のSeriesを返す。

    task_status_map: {(Job_ID, Task_ID): "in_progress"/"done"}
    （`gui/gantt_generator.build_display()` の `task_status`。
    `gui/tab_jobs.py`のタスク上書き欄でユーザーが実際に記録した状態で、
    日付からの推測ではない）。マップに無いタスクは未着手として扱う
    （job_task_overrides.status のNULL＝未着手という既定と同じ）。"""
    if result_df.empty:
        return pd.Series(dtype=object)
    keys = zip(result_df["Job_ID"], result_df["Task_ID"])
    return pd.Series(
        [task_status_map.get(key, STATUS_NOT_STARTED) for key in keys],
        index=result_df.index,
    )


def peak_concurrency(result_df):
    """全体（チームを問わない）の同時タスク数のピークと、それが最初に現れる
    月（"YYYY-MM"）を (ピーク, 月) で返す。0件なら (0, None)。

    差分＋累積和で O(タスク数 + 日数)。Start_Dateで+1、End_Date（exclusive）で
    -1を立てて日付順に累積和を取ると、その日に稼働しているタスク数になる
    （End_Dateがexclusiveなので、その日には既に-1が効いていて過不足なく数える
    ——タスクが手前で終わっていても後ろで始まっていても二重に数えない）。"""
    if result_df.empty:
        return 0, None
    deltas = pd.concat([
        pd.Series(1, index=result_df["Start_Date"].values),
        pd.Series(-1, index=result_df["End_Date"].values),
    ]).groupby(level=0).sum().sort_index()
    counts = deltas.cumsum()
    peak = int(counts.max())
    if peak <= 0:
        return 0, None
    peak_day = counts.idxmax()
    return peak, pd.Timestamp(peak_day).strftime("%Y-%m")


def compute_kpi(result_df, project_start, milestones, task_status_map):
    """KPIタイル6枚ぶんの値を辞書で返す。

    milestones: [(id, name, due_date(pd.Timestamp)), ...]（締切順、
    プロジェクト開始日マーカーは含めない。
    `gui/gantt_generator.build_display()` の `milestone_markers` から
    "PROJECT_START" を除いたものをそのまま渡せる）。
    project_start: pd.Timestamp。task_status_map: task_status_series参照。"""
    jobs_count = int(result_df["Job_ID"].nunique()) if not result_df.empty else 0
    tasks_count = len(result_df)
    plan_end = result_df["End_Date"].max() if not result_df.empty else None

    last_due = milestones[-1][2] if milestones else None
    milestone_margin_days = (
        (last_due - plan_end).days if plan_end is not None and last_due is not None else None
    )

    status = task_status_series(result_df, task_status_map)
    done = int((status == STATUS_DONE).sum())
    in_progress = int((status == STATUS_IN_PROGRESS).sum())
    not_started = int((status == STATUS_NOT_STARTED).sum())

    peak, peak_month = peak_concurrency(result_df)

    if result_df.empty:
        overrun_tasks = overrun_jobs = max_overrun_days = 0
    else:
        overruns = result_df[result_df["Deadline_Overrun_Days"] > 0]
        overrun_tasks = len(overruns)
        overrun_jobs = int(overruns["Job_ID"].nunique())
        max_overrun_days = int(overruns["Deadline_Overrun_Days"].max()) if not overruns.empty else 0

    start_pin_violations = (
        0 if result_df.empty else int((result_df["Constraint_Violation"] != "").sum())
    )

    return {
        "jobs": jobs_count,
        "tasks": tasks_count,
        "project_start": project_start,
        "plan_end": plan_end,
        "milestone_margin_days": milestone_margin_days,
        "done": done,
        "in_progress": in_progress,
        "not_started": not_started,
        "peak": peak,
        "peak_month": peak_month,
        "overrun_tasks": overrun_tasks,
        "overrun_jobs": overrun_jobs,
        "max_overrun_days": max_overrun_days,
        "start_pin_violations": start_pin_violations,
    }


def compute_milestone_rows(result_df, milestones, today):
    """マイルストーン別サマリーの基本列（内訳モードによらず共通）。
    戻り値は milestones と同じ順の辞書のリスト:
    milestone_id / name / due_date / remaining_days / last_end_date /
    slack_days（Noneならタスクなし） / overrun_count / on_time。"""
    today_ts = pd.Timestamp(today)
    rows = []
    for ms_id, name, due in milestones:
        group = result_df[result_df["Milestone_ID"] == ms_id] if not result_df.empty else result_df
        if group.empty:
            last_end, slack_days, overrun_count = None, None, 0
        else:
            last_end = group["End_Date"].max()
            slack_days = (due - last_end).days
            overrun_count = int((group["Deadline_Overrun_Days"] > 0).sum())
        remaining_days = max(0, (due - today_ts).days)
        rows.append({
            "milestone_id": ms_id,
            "name": name,
            "due_date": due,
            "remaining_days": remaining_days,
            "last_end_date": last_end,
            "slack_days": slack_days,
            "overrun_count": overrun_count,
            "on_time": slack_days is None or slack_days >= 0,
        })
    return rows


def compute_milestone_breakdown_all(result_df, milestones, task_status_map):
    """マイルストーン別サマリー「全体」内訳: milestonesと同じ順で
    {jobs（延べ）, tasks, done, in_progress, not_started} の辞書のリスト。"""
    status = task_status_series(result_df, task_status_map)
    rows = []
    for ms_id, _name, _due in milestones:
        mask = result_df["Milestone_ID"] == ms_id if not result_df.empty else None
        if mask is None or not mask.any():
            rows.append({"jobs": 0, "tasks": 0, "done": 0, "in_progress": 0, "not_started": 0})
            continue
        group_status = status[mask]
        rows.append({
            "jobs": int(result_df.loc[mask, "Job_ID"].nunique()),
            "tasks": int(mask.sum()),
            "done": int((group_status == STATUS_DONE).sum()),
            "in_progress": int((group_status == STATUS_IN_PROGRESS).sum()),
            "not_started": int((group_status == STATUS_NOT_STARTED).sum()),
        })
    return rows
