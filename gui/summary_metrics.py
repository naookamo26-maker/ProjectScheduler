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

import bisect

import pandas as pd

from gui.resource_histogram import compute_step_segments
from project_scheduler import _UNLIMITED_LINES
from i18n import N_, tr

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


def compute_milestone_rows(result_df, milestones):
    """マイルストーン別サマリーの基本列（内訳モードによらず常にプロジェクト
    全体で計算する列）。戻り値は milestones と同じ順の辞書のリスト:
    milestone_id / name / due_date / last_end_date /
    slack_days（Noneならタスクなし） / overrun_count / on_time。"""
    rows = []
    for ms_id, name, due in milestones:
        group = result_df[result_df["Milestone_ID"] == ms_id] if not result_df.empty else result_df
        if group.empty:
            last_end, slack_days, overrun_count = None, None, 0
        else:
            last_end = group["End_Date"].max()
            slack_days = (due - last_end).days
            overrun_count = int((group["Deadline_Overrun_Days"] > 0).sum())
        rows.append({
            "milestone_id": ms_id,
            "name": name,
            "due_date": due,
            "last_end_date": last_end,
            "slack_days": slack_days,
            "overrun_count": overrun_count,
            "on_time": slack_days is None or slack_days >= 0,
        })
    return rows


def compute_milestone_cumulative_progress_pct(df, milestones):
    """マイルストーン別サマリーの「進捗」列（%）。タスクの完了状態とは無関係の
    **計画上の指標**。milestones は締切の早い順に並んでいる前提で、
    「そのマイルストーンまでに割り当てられているタスクが、df全体のタスク数の
    うち何%を占めるか」の累積値を、milestonesと同じ順のリストで返す
    （マイルストーンが進むほど単調増加し、最後のマイルストーンで必ず
    100%になる。タスク1件は必ずどれか1つのマイルストーンに属するため
    ——重複も漏れも無い）。dfが0件なら全マイルストーンで0.0。

    df は呼び出し側の内訳選択で変わる——「全体」なら result_df をそのまま、
    「チーム別」「ワークフロー別」ならその対象で絞り込んだ結果を渡すことで、
    分母（＝100%の基準）が選択対象ごとの件数になる
    （`gui/tab_analysis.py._render_milestone_table` 参照）。"""
    total = len(df)
    if total == 0:
        return [0.0] * len(milestones)
    cumulative = 0
    progress = []
    for ms_id, _name, _due in milestones:
        cumulative += int((df["Milestone_ID"] == ms_id).sum())
        progress.append(cumulative / total * 100)
    return progress


def team_concurrency_steps(result_df, team_id=None):
    """指定チーム（Noneなら全チーム合算）の同時タスク数を、変化点だけを持つ
    階段関数として [(日付, 同時タスク数), ...]（日付昇順）で返す。0件なら
    空リスト。`gui/resource_histogram.py` の `compute_step_segments()` と
    組み合わせれば、任意区間の区間列（積み上げグラフ・詳細グラフの描画用）に
    変換できる。

    peak_concurrency() と同じ差分＋累積和（O(タスク数)）。値が変わる日付
    でしか記録しないため、階段関数の折れ点＝ピークの候補が漏れなく含まれる
    （区間内で値が変わるのは折れ点だけのため、最大値は必ずいずれかの折れ点で
    観測される）。"""
    df = result_df if team_id is None else result_df[result_df["Team_ID"] == team_id]
    if df.empty:
        return []
    deltas = pd.concat([
        pd.Series(1, index=df["Start_Date"].values),
        pd.Series(-1, index=df["End_Date"].values),
    ]).groupby(level=0).sum().sort_index()
    counts = deltas.cumsum()
    return list(zip(counts.index, counts.values.astype(int)))


# グラフのX軸の粒度。チーム別サマリーは週次固定、ワークフロー別サマリーは
# 利用者が3段階から選ぶ（docs/project_analysis_tab_design.md §2-4）。
GRANULARITY_MONTH = "month"
GRANULARITY_WEEK = "week"
GRANULARITY_DAY = "day"


def period_start_of(ts, granularity):
    """ts が属する期間の開始日（月次ならその月の1日、週次ならその週の月曜、
    日次ならその日）。"""
    day = pd.Timestamp(ts).normalize()
    if granularity == GRANULARITY_MONTH:
        return day.replace(day=1)
    if granularity == GRANULARITY_WEEK:
        return day - pd.Timedelta(days=day.weekday())
    return day


def period_end_of(start, granularity):
    """開始日 start の期間の終了日（**exclusive**＝次の期間の開始日）。
    `result_df` の `End_Date` と同じ半開区間 [start, end) の規約に揃える。"""
    if granularity == GRANULARITY_MONTH:
        return pd.Timestamp(start) + pd.offsets.MonthBegin(1)
    if granularity == GRANULARITY_WEEK:
        return pd.Timestamp(start) + pd.Timedelta(days=7)
    return pd.Timestamp(start) + pd.Timedelta(days=1)


def period_starts(range_start, range_end, granularity=GRANULARITY_WEEK):
    """[range_start, range_end] を覆う各期間の開始日（`pd.Timestamp`）の昇順リスト
    （グラフのX軸の目盛りにあたる）。"""
    cursor = period_start_of(range_start, granularity)
    last = pd.Timestamp(range_end).normalize()
    periods = []
    while cursor <= last:
        periods.append(cursor)
        cursor = period_end_of(cursor, granularity)
    return periods


def period_index(ts, first_start, granularity):
    """ts が先頭期間 first_start から数えて何番目の期間に入るか（0始まり）。
    範囲の外でも外挿した値（負・末尾超え）を返すので、呼び出し側で丸める。"""
    day = pd.Timestamp(ts).normalize()
    if granularity == GRANULARITY_MONTH:
        return (day.year - first_start.year) * 12 + (day.month - first_start.month)
    if granularity == GRANULARITY_WEEK:
        return (day - first_start).days // 7
    return (day - first_start).days


def week_starts(range_start, range_end):
    """[range_start, range_end] を覆う各週の月曜日（`pd.Timestamp`）の昇順リスト。

    チーム別サマリーのグラフは、日次の変化点をそのまま描くと営業日数ぶんの
    ギザギザになって読めないため、**週次に集計してから描く**。月次にすると
    数か月しかない短いプロジェクトでは点が数個しか並ばず形が読めないので、
    週を単位にしている（この関数がそのX軸の目盛りにあたる）。"""
    return period_starts(range_start, range_end, GRANULARITY_WEEK)


def _week_index(ts, first_monday):
    return (pd.Timestamp(ts).normalize() - first_monday).days // 7


def _iter_step_weeks(steps, weeks):
    """階段関数 steps の各区間を、その区間が覆う週インデックスの範囲へ展開して
    (週インデックス, 値) を順に返す。

    steps は変化点の列なので、値 value は [day, next_day) の間だけ有効。
    末尾の点は必ず0（全タスクが終わる）ため、ペアで走査すれば漏れない。
    区間は時間軸を分割しているだけなので、総反復回数は
    O(区間数 + 週数)——タスク数が増えても跳ね上がらない。"""
    if not weeks:
        return
    first_monday = weeks[0]
    last_index = len(weeks) - 1
    for (day, value), (next_day, _next) in zip(steps, steps[1:]):
        if value <= 0:
            continue
        lo = max(_week_index(day, first_monday), 0)
        hi = min(_week_index(pd.Timestamp(next_day) - pd.Timedelta(days=1), first_monday), last_index)
        for i in range(lo, hi + 1):
            yield i, value


def weekly_concurrency_by_team(result_df, team_ids, range_start, range_end):
    """週ごとの「その週の最大同時タスク数」を、チームごとの系列として返す。

    Returns: {team_id: [値, ...]}（`week_starts(range_start, range_end)` と同じ順）。

    週内の**最大**を採るのは、この画面が「その週に最大何本同時に走るか」＝
    必要な人数を読む場所であるため（平均を採ると、週の一部だけ突出した山が
    均されて消えてしまい、`compute_team_summary_rows` が出すピーク・ピーク時期
    とも食い違う）。"""
    weeks = week_starts(range_start, range_end)
    series = {}
    for team_id in team_ids:
        values = [0] * len(weeks)
        for i, value in _iter_step_weeks(team_concurrency_steps(result_df, team_id), weeks):
            if value > values[i]:
                values[i] = value
        series[team_id] = values
    return series


def weekly_peak_breakdown_by_team(result_df, team_ids, range_start, range_end):
    """週ごとに「その週で**全体の**同時タスク数が最大になった日」を選び、
    その日の各チームの同時タスク数（＝合計の内訳）を返す。

    Returns: ({team_id: [値, ...]}, [週ごとの合計, ...])
    （いずれも `week_starts(range_start, range_end)` と同じ順）。

    積み上げグラフ専用の集計。`weekly_concurrency_by_team()`（チームごとの
    週内最大）をそのまま積み上げてはいけない——設計案§3が明示しているとおり
    **「全体のピークは各チームのピークの和にはならない（時期がずれるため）」**
    ので、単純に足すと実際には同時に起きていない高さの山を描いてしまう。
    ある1日を選んでその日の内訳を積めば、合計は必ず実在した同時タスク数に
    なり、KPIタイルの「同時タスク数のピーク」とも一致する
    （どの週でも `sum(series[t][i] for t) == totals[i]`）。"""
    weeks = week_starts(range_start, range_end)
    totals = [0] * len(weeks)
    peak_days = [None] * len(weeks)

    # 全チーム合算の階段関数から、週ごとの「合計が最大の日」を拾う。
    # 区間 [day, next_day) は合計が一定なので、その週に入る先頭の日
    # （週をまたぐ場合はその週の月曜）を代表日にすれば十分。
    total_steps = team_concurrency_steps(result_df)
    for (day, value), (next_day, _next) in zip(total_steps, total_steps[1:]):
        if value <= 0:
            continue
        for i, _v in _iter_step_weeks([(day, value), (next_day, 0)], weeks):
            if value > totals[i]:
                totals[i] = value
                peak_days[i] = max(pd.Timestamp(day), weeks[i])

    series = {}
    for team_id in team_ids:
        values = [0] * len(weeks)
        steps = team_concurrency_steps(result_df, team_id)
        if steps:
            days = [pd.Timestamp(day) for day, _value in steps]
            counts = [value for _day, value in steps]
            for i, peak_day in enumerate(peak_days):
                if peak_day is None:
                    continue
                # peak_day の時点で有効な値＝peak_day以下で最後の変化点の値。
                k = bisect.bisect_right(days, peak_day) - 1
                if k >= 0:
                    values[i] = counts[k]
        series[team_id] = values
    return series, totals


def weekly_capacity(capacity_periods, weeks):
    """週ごとの設定上限（その週の月曜時点で有効な値）。上限が「指定なし」
    （`project_scheduler._UNLIMITED_LINES`）の週は None を返す——呼び出し側は
    Noneの週に破線を描かないことで「まだ人数を決めていない」ことを表す
    （docs/project_analysis_tab_design.md §2-3）。

    capacity_periods: `gui/gantt_generator.build_display()` の
    team_capacity_schedule の1チームぶん（[(適用開始日, ライン数), ...]）。"""
    periods = sorted(capacity_periods)
    values = []
    for week_start in weeks:
        cap = None
        for start, lines in periods:
            if start <= week_start:
                cap = lines
            else:
                break
        values.append(None if cap is None or cap == _UNLIMITED_LINES else cap)
    return values


def _team_pinned_days(result_df, team_id, capacity_periods):
    """指定チームで、同時タスク数が設定上限に達していた（＝上限が効いていた）
    日数を返す。対象期間はそのチームの実際の活動期間
    （最初のStart_Date〜最後のEnd_Date、exclusive）に限る——期間外は
    そもそも「上限に張り付く」以前にタスクが存在しないため。

    上限を一度も設定していない（期間を通じて`_UNLIMITED_LINES`）チームは
    Noneを返す（設計案§3「上限が『指定なし』のチームでは空欄」）。"""
    df = result_df[result_df["Team_ID"] == team_id]
    if df.empty:
        return None
    range_start = df["Start_Date"].min()
    range_end = df["End_Date"].max()
    cap_segments = compute_step_segments(capacity_periods, range_start, range_end)
    if all(v == _UNLIMITED_LINES for _s, _e, v in cap_segments):
        return None

    concurrency_segments = compute_step_segments(
        team_concurrency_steps(result_df, team_id), range_start, range_end
    )
    boundaries = sorted(
        {p for seg in concurrency_segments for p in (seg[0], seg[1])}
        | {p for seg in cap_segments for p in (seg[0], seg[1])}
    )

    def _value_at(segments, point):
        for seg_start, seg_end, value in segments:
            if seg_start <= point < seg_end:
                return value
        return None

    total_days = 0
    for a, b in zip(boundaries, boundaries[1:]):
        concurrency = _value_at(concurrency_segments, a)
        cap = _value_at(cap_segments, a)
        if concurrency is not None and cap is not None and cap != _UNLIMITED_LINES and cap > 0:
            if concurrency == cap:
                total_days += (b - a).days
    return total_days


def compute_team_summary_rows(result_df, team_names, team_capacity_schedule):
    """チーム別サマリーの表の行。team_names（{Team_ID: 名前}、
    `gui/gantt_generator.build_display()` の同名キー）と同じ順で、
    team_id/name/tasks/peak/peak_month/pinned_days（Noneなら『指定なし』の
    まま上限を一度も設定していない）/resource_adjusted（押し出された件数）/
    overrun の辞書のリストを返す。

    team_capacity_schedule: `gui/gantt_generator.build_display()` の同名キー
    （{Team_ID: [(適用開始日, ライン数), ...]}）。"""
    rows = []
    for team_id, name in team_names.items():
        df = result_df[result_df["Team_ID"] == team_id] if not result_df.empty else result_df
        steps = team_concurrency_steps(result_df, team_id)
        peak = max((count for _day, count in steps), default=0)
        peak_month = None
        if peak > 0:
            peak_day = next(day for day, count in steps if count == peak)
            peak_month = pd.Timestamp(peak_day).strftime("%Y-%m")
        capacity_periods = team_capacity_schedule.get(team_id, [])
        rows.append({
            "team_id": team_id,
            "name": name,
            "tasks": int(len(df)),
            "peak": int(peak),
            "peak_month": peak_month,
            "pinned_days": _team_pinned_days(result_df, team_id, capacity_periods),
            "resource_adjusted": int(df["Resource_Adjusted"].sum()) if not df.empty else 0,
            "overrun": int((df["Deadline_Overrun_Days"] > 0).sum()) if not df.empty else 0,
        })
    return rows


ALL_TEAMS_ROW_NAME = N_("全チーム")


def compute_all_teams_row(result_df):
    """チーム別サマリーの表の先頭に置く「全チーム」行。

    `compute_team_summary_rows()` と同じ形の辞書を返す（team_id は None）。
    ピークは各チームのピークの和ではなく**全体の同時タスク数の最大**
    （`peak_concurrency()` と同じ値＝KPIタイルの「同時タスク数のピーク」と
    一致する）。「上限に張り付いた日数」はチーム単位の上限に対してしか
    意味を持たないので None（表では空欄）。"""
    peak, peak_month = peak_concurrency(result_df)
    empty = result_df.empty
    return {
        "team_id": None,
        "name": tr(ALL_TEAMS_ROW_NAME),
        "tasks": int(len(result_df)),
        "peak": peak,
        "peak_month": peak_month,
        "pinned_days": None,
        "resource_adjusted": 0 if empty else int(result_df["Resource_Adjusted"].sum()),
        "overrun": 0 if empty else int((result_df["Deadline_Overrun_Days"] > 0).sum()),
    }


def active_task_counts(df, periods, granularity):
    """期間ごとの**稼働タスク件数**（その期間に1日でも走っているタスクの本数）
    を、periods と同じ順のリストで返す。

    同時タスク数（`weekly_concurrency_by_team()` 等）とは別の量。開始件数でも
    同時本数でもなく「その期間に走っているか」の1/0をタスクごとに数えるので、
    **長いタスクが期間をまたいでも山が消えない**（設計案§2-4）。1タスクは
    1期間で高々1回しか数えないため、ワークフローごとの値を積み上げれば
    合計は「その期間に走っているタスクの総数」になる（重複しない）。

    タスクが稼働している最後の日は `End_Date - 1日`（End_Dateはexclusive）。
    期間の範囲へ丸めた上で差分＋累積和を取るので O(タスク数 + 期間数)。"""
    if not periods:
        return []
    counts = [0] * (len(periods) + 1)
    if df.empty:
        return counts[:-1]
    first_start = periods[0]
    last_index = len(periods) - 1
    for start, end in zip(df["Start_Date"], df["End_Date"]):
        last_active_day = pd.Timestamp(end) - pd.Timedelta(days=1)
        if last_active_day < start:
            continue  # 稼働日を1日も持たないタスク（防御的）
        lo = max(period_index(start, first_start, granularity), 0)
        hi = min(period_index(last_active_day, first_start, granularity), last_index)
        if hi < lo:
            continue  # 描画範囲の外
        counts[lo] += 1
        counts[hi + 1] -= 1
    running = 0
    values = []
    for i in range(len(periods)):
        running += counts[i]
        values.append(running)
    return values


def active_task_counts_by_workflow(result_df, workflow_ids, periods, granularity):
    """`active_task_counts()` をワークフローごとに集計する。

    Returns: ({workflow_id: [値, ...]}, [期間ごとの合計, ...])
    （いずれも periods と同じ順）。合計は各系列の和として求める——積み上げ
    グラフの高さと必ず一致させるため（1タスクは1ワークフローにしか属さないので
    重複して数えることはない）。"""
    series = {}
    for workflow_id in workflow_ids:
        df = result_df[result_df["Workflow_ID"] == workflow_id] if not result_df.empty else result_df
        series[workflow_id] = active_task_counts(df, periods, granularity)
    totals = [sum(values[i] for values in series.values()) for i in range(len(periods))]
    return series, totals


def _job_duration_days(df):
    """ジョブごとの所要期間（最初のタスク開始〜最後のタスク終了の暦日）のリスト。
    `End_Date` はexclusiveなので、その差がそのまま暦日数になる。"""
    if df.empty:
        return []
    spans = df.groupby("Job_ID").agg(start=("Start_Date", "min"), end=("End_Date", "max"))
    return [int((end - start).days) for start, end in zip(spans["start"], spans["end"])]


def compute_workflow_summary_rows(result_df, workflow_names):
    """ワークフロー別サマリーの表の行。workflow_names（{Workflow_ID: 名前}、
    `gui/gantt_generator.build_display()` の同名キー）と同じ順で、
    workflow_id / name / jobs / tasks / median_duration_days（0件ならNone） /
    overrun の辞書のリストを返す。

    所要期間は**合計を取らず中央値だけ**を見る（設計案§3）——パイプラインの
    長さの比較に使う量であって、量の集計ではないため。"""
    rows = []
    for workflow_id, name in workflow_names.items():
        df = result_df[result_df["Workflow_ID"] == workflow_id] if not result_df.empty else result_df
        durations = sorted(_job_duration_days(df))
        if durations:
            mid = len(durations) // 2
            median = (
                durations[mid] if len(durations) % 2
                else (durations[mid - 1] + durations[mid]) / 2
            )
        else:
            median = None
        rows.append({
            "workflow_id": workflow_id,
            "name": name,
            "jobs": int(df["Job_ID"].nunique()) if not df.empty else 0,
            "tasks": int(len(df)),
            "median_duration_days": median,
            "overrun": int((df["Deadline_Overrun_Days"] > 0).sum()) if not df.empty else 0,
        })
    return rows


ALL_WORKFLOWS_ROW_NAME = N_("全ワークフロー")


def compute_all_workflows_row(result_df):
    """ワークフロー別サマリーの表の先頭に置く「全ワークフロー」行。

    `compute_workflow_summary_rows()` と同じ形の辞書を返す（workflow_id は
    None）。所要期間の中央値はジョブ単位の量なので、全ワークフローを
    まとめたジョブ集合からそのまま求める（compute_all_teams_row と同じ
    考え方）。"""
    durations = sorted(_job_duration_days(result_df))
    if durations:
        mid = len(durations) // 2
        median = (
            durations[mid] if len(durations) % 2
            else (durations[mid - 1] + durations[mid]) / 2
        )
    else:
        median = None
    empty = result_df.empty
    return {
        "workflow_id": None,
        "name": tr(ALL_WORKFLOWS_ROW_NAME),
        "jobs": 0 if empty else int(result_df["Job_ID"].nunique()),
        "tasks": int(len(result_df)),
        "median_duration_days": median,
        "overrun": 0 if empty else int((result_df["Deadline_Overrun_Days"] > 0).sum()),
    }


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
