"""
DB（ProjectDatabase）の内容から project_scheduler.py を直接呼び出して
ガントチャートを生成する。Excelファイル/バッファは一切経由しない。

内部整数PKから project_scheduler.py が期待する文字列ID（"WF_001" 等）への
変換はここで一度だけ行う（GUIの画面上にはこの文字列IDは一切表示されない）。
"""

import bisect
from datetime import date

import pandas as pd

from gui.db import MAX_SUPPORTED_DATE, parse_tags
from gui.gantt_edit import format_entity_id
from project_scheduler import (
    _TEAM_COLOR_OVERFLOW,
    _build_team_capacity_schedule,
    _build_team_color_map,
    format_dependency_ref,
    generate_jp_holidays,
    run_resource_constrained_scheduler_from_frames,
)
from i18n import tr


def _fmt(prefix, entity_id, width=3):
    return format_entity_id(prefix, entity_id, width)


def validate_for_generation(db):
    """ガントチャート生成前の入力充足チェック。空リストなら生成可能。
    ここで弾いておかないと、必須列を持たない空のDataFrameがそのまま
    project_scheduler.py に渡り、分かりにくいエラーになるため。"""
    errors = []
    proj = db.get_project()
    if not proj["project_name"].strip():
        errors.append(tr("プロジェクト名が未設定です（基本情報設定タブ）"))
    if not proj["start_date"]:
        errors.append(tr("開発開始日が未設定です（基本情報設定タブ）"))
    if not db.list_teams():
        errors.append(tr("チームが1件も登録されていません（基本情報設定タブ）"))
    if not db.list_milestones():
        errors.append(tr("マイルストーンが1件も登録されていません（基本情報設定タブ）"))
    workflows = db.list_workflows()
    if not workflows:
        errors.append(tr("ワークフローが1件も登録されていません（ワークフロー設計タブ）"))
    elif not any(db.list_workflow_tasks(w["id"]) for w in workflows):
        errors.append(tr("タスクを持つワークフローが1件もありません（ワークフロー設計タブ）"))
    if not db.list_jobs():
        errors.append(tr("ジョブが1件も登録されていません（ジョブ作成タブ）"))
    errors.extend(_dates_out_of_range(db, proj))
    return errors


def _dates_out_of_range(db, proj):
    """扱える上限（MAX_SUPPORTED_DATE）より後の日付を、どこにあるかが分かる文言で返す。

    GUIの日付欄はこの上限より後を入力できないが、上限を設ける前に保存したファイルには
    残りうる。そのまま計算すると pandas の日付の範囲を超えて想定外のエラー
    （OverflowError 等）になり、原因が分からないため、ここで止める。
    日付は 'YYYY-MM-DD' の文字列で持っているので、文字列のまま比べられる。"""
    limit = MAX_SUPPORTED_DATE
    errors = []
    if proj["start_date"] and proj["start_date"] > limit:
        errors.append(tr("開発開始日が {max_date} より後です（基本情報設定タブ）", max_date=limit))
    for m in db.list_milestones():
        if m["end_date"] and m["end_date"] > limit:
            errors.append(tr("マイルストーン「{name}」の締切日が {max_date} より後です（基本情報設定タブ）",
                             name=m["name"], max_date=limit))
    for h in db.list_holidays():
        if h["date"] and h["date"] > limit:
            errors.append(tr("休業日（{date}）が {max_date} より後です（基本情報設定タブ）",
                             date=h["date"], max_date=limit))
    for t in db.list_teams():
        for c in db.list_team_capacity_changes(t["id"]):
            if c["start_date"] and c["start_date"] > limit:
                errors.append(tr("チーム「{team}」の同時ライン数の変動点（{date}）が {max_date} より後です（基本情報設定タブ）",
                                 team=t["name"], date=c["start_date"], max_date=limit))
    job_names = {j["id"]: j["name"] for j in db.list_jobs()}
    for r in db.list_all_job_task_overrides():
        if r["start_pin_date"] and r["start_pin_date"] > limit:
            errors.append(tr("ジョブ「{job}」のタスクの開始固定日（{date}）が {max_date} より後です（ジョブ作成タブ）",
                             job=job_names.get(r["job_id"], "?"), date=r["start_pin_date"], max_date=limit))
    return errors


def build_frames(db):
    """DBの内容から project_scheduler.py が要求する形のDataFrame群を組み立てる。

    Returns: dict（project/teams/milestones/workflows/workflow_names/jobs/
    job_tasks/holidays/external_dependencies をキーとする）。
    job_tasks/holidays/external_dependencies は該当データが無ければ None
    （_load_data_from_frames の「シート/データが存在しない」扱いに対応）。
    """
    proj = db.get_project()
    # 全面再計画の基準日（docs/roadmap.md §8-3）。確定行を持たないタスクを
    # この日より前に置かないよう、スケジューラへ渡す開始日を差し替える
    # （開発開始日そのものは変えない——稼働本数の起点等に使われているため）。
    start_date = proj["start_date"]
    if proj.get("replan_base_date") and start_date and proj["replan_base_date"] > start_date:
        start_date = proj["replan_base_date"]
    df_project = pd.DataFrame([{
        "Project_ID": "PRJ_001",
        "Project_Name": proj["project_name"],
        "Start_Date": start_date,
    }])

    teams = db.list_teams()
    team_str = {t["id"]: _fmt("TEAM", t["id"]) for t in teams}
    df_teams = pd.DataFrame([{
        "Team_ID": team_str[t["id"]], "Max_Lines": t["max_lines"], "Team_Name": t["name"],
    } for t in teams])

    capacity_rows = []
    for t in teams:
        for c in db.list_team_capacity_changes(t["id"]):
            capacity_rows.append({
                "Team_ID": team_str[t["id"]], "Start_Date": c["start_date"], "Lines": c["lines"],
            })
    df_team_capacity = pd.DataFrame(capacity_rows) if capacity_rows else None

    milestones = db.list_milestones()
    ms_str = {m["id"]: _fmt("MS", m["id"]) for m in milestones}
    # マイルストーンが1件も無い場合でも、project_scheduler.py側の必須列
    # チェック（Milestone_ID/End_Date）を通せるよう、列だけは常に持たせておく
    # （project_scheduler._parse_tasksが「マイルストーン未指定かつ1件も無い」を
    # 開発開始日+5年のフォールバックとして扱えるようにするため）。
    df_ms = pd.DataFrame([{
        "Milestone_ID": ms_str[m["id"]], "End_Date": m["end_date"], "Milestone_Name": m["name"],
    } for m in milestones], columns=["Milestone_ID", "End_Date", "Milestone_Name"])

    workflows = db.list_workflows()
    wf_str = {w["id"]: _fmt("WF", w["id"]) for w in workflows}
    df_wf_names = pd.DataFrame([{
        "Workflow_ID": wf_str[w["id"]], "Workflow_Name": w["name"],
    } for w in workflows])

    task_str = {}
    wf_rows = []
    for w in workflows:
        deps = db.list_task_dependencies(w["id"])
        deps_by_succ = {}
        for d in deps:
            deps_by_succ.setdefault(d["successor_task_id"], []).append(d)
        tasks = db.list_workflow_tasks(w["id"])
        for t in tasks:
            task_str[t["id"]] = _fmt("T", t["id"])
        for t in tasks:
            preds = deps_by_succ.get(t["id"], [])
            # 種別・ラグが既定（FS・0）の依存は "T_003" のまま。それ以外だけ
            # "T_003(SS+2)" の形になる（format_dependency_ref を参照）。
            internal_depends = ",".join(
                format_dependency_ref(
                    task_str[d["predecessor_task_id"]], d["dep_type"], d["lag_days"]
                )
                for d in preds
            ) if preds else None
            wf_rows.append({
                "Workflow_ID": wf_str[w["id"]],
                "Task_ID": task_str[t["id"]],
                "Task_Name": t["name"],
                "Default_Days": t["default_days"],
                "Internal_Depends": internal_depends,
                "Team_ID": team_str[t["team_id"]],
            })
    df_wf = pd.DataFrame(wf_rows)

    jobs = db.list_jobs()
    job_str = {j["id"]: _fmt("JOB", j["id"]) for j in jobs}
    df_jobs = pd.DataFrame([{
        "Job_ID": job_str[j["id"]],
        "Job_Name": j["name"],
        "Workflow_ID": wf_str[j["workflow_id"]],
        "Default_Milestone_ID": ms_str.get(j["default_milestone_id"]),
        "Priority": j["priority"],
        # 配置のばらつきの種（作成時に決めて変えない安定キー。§8-1）
        "Jitter_Key": j["stable_key"],
    } for j in jobs])

    jt_rows = [{
        "Job_ID": job_str[r["job_id"]],
        "Task_ID": task_str[r["workflow_task_id"]],
        "Is_Active": "Y" if r["is_active"] else "N",
        "Override_Days": r["override_days"],
        "Milestone_ID": ms_str.get(r["override_milestone_id"]),
        "Team_ID": team_str.get(r["override_team_id"]),
        # 開始固定日（実績確定・外部都合のピン留め）。既定値と全く同じ扱いで、
        # 未設定はNaN（他のOverride_Days等と同じく「上書きなし」を表す）。
        "Start_Pin_Date": r["start_pin_date"],
    } for r in db.list_all_job_task_overrides()]
    df_jtasks = pd.DataFrame(jt_rows) if jt_rows else None

    holidays = db.list_holidays()
    df_holidays = pd.DataFrame([{
        "Date": h["date"], "Team_ID": team_str.get(h["team_id"]),
    } for h in holidays]) if holidays else None

    ext_deps = [e for e in db.list_external_dependencies() if e["is_active"]]
    df_extdeps = pd.DataFrame([{
        "Job_ID": job_str[e["job_id"]],
        "Task_ID": task_str[e["workflow_task_id"]],
        "Depends_On_Job_ID": job_str[e["depends_on_job_id"]],
        "Depends_On_Task_ID": task_str[e["depends_on_workflow_task_id"]],
    } for e in ext_deps]) if ext_deps else None

    return {
        "project": df_project, "teams": df_teams, "milestones": df_ms, "workflows": df_wf,
        "workflow_names": df_wf_names, "jobs": df_jobs, "job_tasks": df_jtasks,
        "holidays": df_holidays, "external_dependencies": df_extdeps,
        "team_capacity": df_team_capacity,
    }


def build_display(db):
    """ガントチャートの表示に必要な補助情報（表示名・色・マイルストーン）を組み立てる。

    build_frames() と同じくDBを読むので、必ずGUIスレッドから呼ぶこと
    （スケジューリング本体だけを別スレッドへ逃がす分割については
    compute_schedule_from_frames() を参照）。

    Returns: 表示用の補助情報を持つ辞書
        - team_names: {Team_ID(文字列): チーム名}
        - team_colors: {Team_ID(文字列): 16進色}
        - workflow_names: {Workflow_ID(文字列): ワークフロー名}
        - workflow_colors: {Workflow_ID(文字列): 16進色}（チーム別表示でバーを
          ワークフロー別に色分けする際に使う。チーム色と同じ固定パレット）
        - milestone_markers: [(id, 名前, pd.Timestamp), ...]（プロジェクト開始日を含む、締切順）
        - common_holiday_dates: {datetime.date, ...}（全チーム共通の休業日）
        - holidays_by_team: {Team_ID(文字列): {datetime.date, ...}}（チーム別の休業日）
        - job_tags: {Job_ID(文字列): ジョブ タグ（カンマ区切りの1文字列）}（ガント
          チャートタブのジョブ タグ絞り込み用。result_dfにはタグを持たせて
          いないため）
        - job_task_tags: {Job_ID(文字列): タスク タグ（カンマ区切りの1文字列）}
          （ガントチャートタブのタスク タグ絞り込み用。ジョブが持つ全タスクの
          タスク タグを1つの集合にまとめたもの——job_tagsと違い、個々の
          タスクどのタグを持つかまでは表現しない。ジョブ単位で「いずれかの
          タスクがそのタグを持つか」だけを見る絞り込みのため十分）
        - task_status: {(Job_ID(文字列), Task_ID(文字列)): "in_progress"/"done"}
          （プロジェクト分析タブの「タスクの状態」集計用。job_task_overrides.status
          にユーザーが記録した実際の進捗——result_dfにはこの情報を持たせて
          いないため、job_tags/job_task_tagsと同じ理由でここに含める。
          エントリの無いタスクは未着手を表す）
        - team_capacity_schedule: {Team_ID(文字列): [(適用開始日, ライン数), ...]}
          （プロジェクト分析タブの「チーム別サマリー」用。project_scheduler.py の
          `_build_team_capacity_schedule` をそのまま呼ぶ——スケジューリング本体が
          実際に使ったのと同じ区分定数関数を、結果を作り直さずに読めるように
          するため。ライン数「指定なし」は `project_scheduler._UNLIMITED_LINES`）
        - jp_holiday_dates: {datetime.date, ...}（日本の祝日。スケジューラと同じ
          generate_jp_holidays で求める。ガント上でのドラッグを営業日単位で
          吸着させるため——gui/gantt_edit.py の WorkDayCalendar）
        - start_pins: {(Job_ID(文字列), Task_ID(文字列)): datetime.date}
          （手動ピン＝開始固定日。ガント上でバーに印を付けるため）
    """
    teams = db.list_teams()
    team_str = {t["id"]: _fmt("TEAM", t["id"]) for t in teams}
    team_names = {team_str[t["id"]]: t["name"] for t in teams}
    team_order_str = [team_str[t["id"]] for t in teams]
    team_colors_by_display_name = _build_team_color_map(team_order_str, team_names)
    team_colors = {
        sid: team_colors_by_display_name.get(team_names[sid], _TEAM_COLOR_OVERFLOW)
        for sid in team_order_str
    }

    workflows = db.list_workflows()
    workflow_str = {w["id"]: _fmt("WF", w["id"]) for w in workflows}
    workflow_names = {workflow_str[w["id"]]: w["name"] for w in workflows}
    workflow_order_str = [workflow_str[w["id"]] for w in workflows]
    workflow_colors_by_display_name = _build_team_color_map(workflow_order_str, workflow_names)
    workflow_colors = {
        sid: workflow_colors_by_display_name.get(workflow_names[sid], _TEAM_COLOR_OVERFLOW)
        for sid in workflow_order_str
    }

    proj = db.get_project()
    milestone_markers = []
    if proj["start_date"]:
        milestone_markers.append(("PROJECT_START", tr("プロジェクト開始"), pd.to_datetime(proj["start_date"])))
    for m in db.list_milestones():
        milestone_markers.append((_fmt("MS", m["id"]), m["name"], pd.to_datetime(m["end_date"])))
    milestone_markers.sort(key=lambda marker: marker[2])

    # ガントチャートの日付軸で、休業日の日付ラベルを赤字にするための情報
    # （gui/gantt_view.py参照）。全チーム共通（team_idがNULL）と、チーム別の
    # 休業日を分けて持つ——表示中のチームが分かって初めて「どの休業日が
    # 関係するか」が決まるため、判定自体はgantt_view.py側で行う。
    common_holiday_dates = set()
    holidays_by_team = {}
    for h in db.list_holidays():
        d = date.fromisoformat(h["date"])
        if h["team_id"] is None:
            common_holiday_dates.add(d)
        else:
            holidays_by_team.setdefault(team_str[h["team_id"]], set()).add(d)

    job_tags = {_fmt("JOB", j["id"]): j["tags"] for j in db.list_jobs()}

    # タスク タグは job_task_overrides 側にしか持たない（タスク自体は既定では
    # タグ無し）ため、全ジョブ分の上書き行から job_id 単位のタグ集合へ潰す。
    job_task_tag_sets = {}
    for o in db.list_all_job_task_overrides():
        tags = parse_tags(o["tags"])
        if tags:
            job_task_tag_sets.setdefault(o["job_id"], set()).update(tags)
    job_task_tags = {
        _fmt("JOB", j["id"]): ", ".join(sorted(job_task_tag_sets.get(j["id"], ())))
        for j in db.list_jobs()
    }

    # 実際の進捗（ユーザーが手動で記録した状態）。result_dfには持たせていない
    # ため、job_tags/job_task_tagsと同じ理由でここに含める。エントリの無い
    # タスクは未着手を表す（job_task_overrides.statusのNULLと同じ既定）。
    task_status = {
        (_fmt("JOB", o["job_id"]), _fmt("T", o["workflow_task_id"])): o["status"]
        for o in db.list_all_job_task_overrides()
        if o["status"] is not None
    }

    # チーム別サマリーの「上限に張り付いた日数」・上限の破線に使う区分定数関数。
    # build_frames() の df_teams/df_team_capacity と同じ組み立て方（team_str
    # による文字列ID変換）をここでも独立に行う——build_frames()側の結果
    # （frames）はワーカースレッドへ渡した後は保持されないため。
    df_teams_for_capacity = pd.DataFrame(
        [{"Team_ID": team_str[t["id"]], "Max_Lines": t["max_lines"]} for t in teams]
    )
    capacity_change_rows = [
        {"Team_ID": team_str[t["id"]], "Start_Date": c["start_date"], "Lines": c["lines"]}
        for t in teams for c in db.list_team_capacity_changes(t["id"])
    ]
    df_team_capacity_for_schedule = pd.DataFrame(
        capacity_change_rows, columns=["Team_ID", "Start_Date", "Lines"]
    )
    team_capacity_schedule = _build_team_capacity_schedule(
        df_teams_for_capacity, df_team_capacity_for_schedule
    )

    # 日本の祝日（営業日の計算用）。範囲はプロジェクト開始の前年から、最も遅い
    # マイルストーン（無ければ開始）の5年後まで——ドラッグで締切の先へ
    # 動かす場合も十分に覆える幅にしておく。
    marker_years = [m[2].year for m in milestone_markers] or [date.today().year]
    jp_holiday_dates = {
        ts.date() for ts in generate_jp_holidays(min(marker_years) - 1, max(marker_years) + 5)
    }

    start_pins = {
        (_fmt("JOB", o["job_id"]), _fmt("T", o["workflow_task_id"])):
            date.fromisoformat(o["start_pin_date"])
        for o in db.list_all_job_task_overrides()
        if o["start_pin_date"]
    }

    return {
        "team_names": team_names, "team_colors": team_colors,
        "workflow_names": workflow_names, "workflow_colors": workflow_colors,
        "milestone_markers": milestone_markers,
        "common_holiday_dates": common_holiday_dates, "holidays_by_team": holidays_by_team,
        "job_tags": job_tags, "job_task_tags": job_task_tags, "task_status": task_status,
        "team_capacity_schedule": team_capacity_schedule,
        "jp_holiday_dates": jp_holiday_dates, "start_pins": start_pins,
    }


def compute_schedule_from_frames(frames, **scheduler_kwargs):
    """build_frames() が返したDataFrame群だけを使ってスケジューリングを実行する。

    DBには一切触れないため、**GUIスレッド以外から呼んでも安全**（ガント
    チャートタブは、タスク数の多いプロジェクトでUIが固まらないよう、この関数を
    ワーカースレッドで実行する。gui/schedule_cache.py の _ScheduleThread を参照）。
    sqlite3の接続はスレッドをまたげず、そもそも計算中にGUI側がDBを書き換えると
    結果が壊れるため、「DBを読むのはGUIスレッド、計算だけ別スレッド」という
    分割にしてある。

    SchedulingError系の例外はそのまま呼び出し元に伝播させる。
    """
    return run_resource_constrained_scheduler_from_frames(
        frames["project"], frames["teams"], frames["milestones"], frames["workflows"],
        frames["jobs"], frames["job_tasks"], frames["holidays"], frames["external_dependencies"],
        frames["workflow_names"], df_team_capacity=frames["team_capacity"],
        **scheduler_kwargs,
    )


def compute_schedule(db, **scheduler_kwargs):
    """DBの現在の設定でスケジューリングだけを実行し、ファイル出力せずに結果を
    返す（メニューの generate_gantt() はファイル出力までを一度に行うのに対し、
    こちらはタブ内表示に必要な最小限の表示用補助情報だけを添えて返す）。

    build_frames() → compute_schedule_from_frames() → build_display() を
    まとめて同期実行する薄いラッパー。GUIから使う場合は、計算部分だけを
    ワーカースレッドへ逃がすため、この3つを個別に呼ぶ（gui/tab_gantt.py）。

    distribution_ratio を明示指定しなければ、プロジェクト設定
    （db.get_project()["distribution_ratio"]、ガントチャートタブの
    「配置コントロール」で調整・保存する値）を既定値として使う。

    Returns: (result_df, display) のタプル。
      result_df: run_resource_constrained_scheduler_from_frames() の戻り値そのもの。
      display: build_display() の戻り値。

    SchedulingError系の例外はそのまま呼び出し元に伝播させる。
    """
    scheduler_kwargs.setdefault("distribution_ratio", db.get_project()["distribution_ratio"])
    result_df = compute_schedule_from_frames(build_frames(db), **scheduler_kwargs)
    return result_df, build_display(db)


PLAN_OUTPUT_DRAFT = "draft"
PLAN_OUTPUT_CONFIRMED = "confirmed"


def generate_gantt(db, plotly_output_path=None, plan_output=PLAN_OUTPUT_DRAFT, **scheduler_kwargs):
    """メニューの「ガントチャートを生成」（HTMLファイル出力）。ガントチャートタブと
    同じく、計画の確定（§8）を踏まえて計算する。SchedulingError系（循環依存・
    リソース不足・マイルストーン不整合等）はそのまま呼び出し元に伝播させる
    （GUI側でダイアログに変換する）。

    plan_output: 変更案の最中に、どちらの日程を出力するか（§8-9）。
      PLAN_OUTPUT_DRAFT: 変更案（ガントチャートタブに出ているもの）
      PLAN_OUTPUT_CONFIRMED: 確定した日程（確定後に足したタスクは含めない）
    出力したHTMLの見出しに、どちらの日程かを書き添える。

    distribution_ratio を明示指定しなければ、プロジェクト設定
    （db.get_project()["distribution_ratio"]）を既定値として使う——
    ガントチャートタブで調整した基準点が、HTML出力でもそのまま使われるようにするため。"""
    from gui.plan_confirmation import CONFIRMED, DRAFT, PlanState

    frames = build_frames(db)
    scheduler_kwargs.setdefault("distribution_ratio", db.get_project()["distribution_ratio"])
    state = PlanState(db)
    note = None
    if state.status == DRAFT and plan_output == PLAN_OUTPUT_CONFIRMED:
        plan = build_confirmed_plan(state)
        note = tr("確定した日程 {date}", date=(state.confirmed_at or '')[:10]).strip()
    else:
        plan = build_plan(db, state)
        if state.status == CONFIRMED:
            note = tr("確定した日程 {date}", date=(state.confirmed_at or '')[:10]).strip()
        elif state.status == DRAFT:
            note = tr("変更案・未確定")
    scheduler_kwargs["plotly_output_path"] = plotly_output_path
    scheduler_kwargs["plotly_title_note"] = note
    if plan is None:
        return compute_schedule_from_frames(frames, **scheduler_kwargs)
    # 違反による再計算のたびに書き出し、最後の計算結果が残る
    result_df, _info = compute_schedule_with_plan(frames, plan, **scheduler_kwargs)
    return result_df


# -- 計画の確定と変更案（docs/roadmap.md §8） ---------------------------------------
#
# 確定済みのファイルでは、影響範囲（gui/plan_confirmation.release_set）の外に
# あるタスクを確定の位置に固定して計算する。固定は開始固定日（パス1で先に予約
# する仕組み）をそのまま使い、日数・チームも確定行の値にそろえる。計算は
# ワーカースレッドで走るので、DBから組み立てた材料（plan）はGUIスレッドで
# 先に作り、DB接続を持たない普通の辞書として渡す。

# 違反・押し下げによる影響範囲の拡大は、この回数の計算で打ち切る（§8-7）。
PLAN_MAX_RUNS = 5


def build_plan(db, state):
    """PlanState から、スケジューラに渡す材料を作る（GUIスレッドで呼ぶ）。
    state.status が未確定なら None。"""
    from gui.plan_confirmation import UNCONFIRMED, release_set, successor_map

    def key(k):
        return (_fmt("JOB", k[0]), _fmt("T", k[1]))

    def team_str(team_id):
        return _fmt("TEAM", team_id) if team_id is not None else None

    def fixed_value(row, k=None):
        # 確定した位置: (開始, 日数, チーム, 実績か, 実績の終了日)。実績の記録が無い
        # 進行中のタスク（v18 以前に記録できなかったもの）は、日数・チームを今の入力に従わせる
        team_id, days = row["team_id"], row["days"]
        if k is not None and k in state.in_progress and k in state.inputs:
            team_id, days = state.inputs[k]["team"], state.inputs[k]["days"]
        return (row["start_date"], days, team_str(team_id), False, None)

    def fact_value(k, fact):
        # 実績は休業日でも動かさない。進行中のタスクは開始日だけを固定し、日数・
        # チームは今の入力に従う（遅れている進行中のタスクを延ばせるように）。完了の
        # タスクは開始日〜終了日をそのまま使う（休業日を足しても、ワークフローの既定の
        # 日数を後で変えても伸び縮みしない）
        if k in state.in_progress and k in state.inputs:
            return (fact["start_date"], state.inputs[k]["days"], team_str(state.inputs[k]["team"]), True, None)
        return (fact["start_date"], fact["days"], team_str(fact["team_id"]), True, fact["end_date"])

    facts = {key(k): fact_value(k, f) for k, f in state.facts.items() if state.active.get(k, False)}
    if state.status == UNCONFIRMED:
        # 未確定でも、進行中・完了のタスクは実績で固定する。それ以外は通常どおり
        # 自由に計算する
        if not facts:
            return None
        return {"fixed": facts, "not_before": {}, "released": set(), "started": set(facts),
                "draft_moves": {}, "successors": {}, "lower_bound": None}
    successors_int = successor_map(db)
    released_int = release_set(state, successors_int)

    fixed = {}
    for k, row in state.confirmed.items():
        if k in released_int or not state.active.get(k, False) or k in state.facts:
            continue
        fixed[key(k)] = fixed_value(row, k)
    fixed.update(facts)
    lower_bound = max(
        (d for d in (state.replan_base_date, (state.confirmed_at or "")[:10]) if d), default=None,
    )
    if state.pending_replan:
        # 基準日は過去の日付も指定できる（警告して許可）ので、確定日より優先する
        lower_bound = state.pending_replan[0]
    # 影響範囲のタスクは、確定していた位置より前へは動かさない（合意した日程を
    # 前倒しするのは、ドラッグでの明示的な移動か全面再計画に限る。§8-7）
    not_before = {
        key(k): state.confirmed[k]["start_date"] for k in released_int if k in state.confirmed
    }
    if state.pending_replan:
        # 全面再計画は、未着手タスクを基準日以降に全体として組み直す（前倒しも許す）
        not_before = {}
    return {
        "fixed": fixed,
        "not_before": not_before,
        "released": {key(k) for k in released_int},
        "started": {key(k) for k in state.started},
        "draft_moves": {key(k): d for k, d in state.draft_moves.items()},
        "successors": {key(k): [key(n) for n in v] for k, v in successors_int.items()},
        "lower_bound": lower_bound,
        "quiet_before": state.quiet_before,
        # 違反による影響範囲の拡大の判定用（compute_schedule_with_plan）
        "confirmed_end": {key(k): r["end_date"] for k, r in state.confirmed.items()},
        "global_changed": state.global_changed,
        # 押し下げ・確定の位置へ戻す処理（compute_schedule_with_plan）を行うか。全面再計画は
        # 未着手タスクを全体として組み直すので行わない
        "push_down": not state.pending_replan,
        # 変更の起点（押し下げたタスクがどの変更のせいで動いたかをたどる。「選択した
        # 変更を確定」で一緒に確定するため）と、影響範囲のタスクの確定した位置（確定した
        # ときから依存に反していた位置へ戻すため）
        "changed": {key(k) for k in state.changed},
        "confirmed_value": {
            key(k): fixed_value(state.confirmed[k], k) for k in released_int
            if k in state.confirmed and state.active.get(k, False)
            and k not in state.started and k not in state.draft_moves
        } if not state.pending_replan else {},
    }


def build_confirmed_plan(state):
    """確定した日程そのものを出力するための材料（HTML出力で「確定した日程」を
    選んだとき）。確定行のあるタスクはすべて確定の位置・日数・チームに固定し、
    確定後に足したタスク（確定行の無いもの）は出力に含めない。"""
    def key(k):
        return (_fmt("JOB", k[0]), _fmt("T", k[1]))

    fixed = {}
    exclude = set()
    for k, is_active in state.active.items():
        if not is_active:
            continue
        row = state.confirmed.get(k)
        if row is None:
            exclude.add(key(k))
            continue
        team = _fmt("TEAM", row["team_id"]) if row["team_id"] is not None else None
        fixed[key(k)] = (row["start_date"], row["days"], team, False, None)
    return {
        "fixed": fixed, "exclude": exclude, "not_before": {}, "released": set(),
        "started": set(fixed), "draft_moves": {}, "successors": {}, "lower_bound": None,
        "quiet_before": state.replanned_at,
    }


def _apply_plan_to_frames(frames, fixed, plan):
    """確定の位置に固定するタスクを、開始固定日・日数・チームの上書きとして
    job_tasks に書き込んだ frames の写しを返す（DBは変えない）。

    fixed の値は (開始, 日数, チーム[, 実績か, 実績の終了日])。実績は休業日でも
    次の稼働日へ送らず（Start_Pin_Exact）、終了日があればそのまま使う（Fixed_End_Date）。"""
    frames = dict(frames)
    columns = ["Job_ID", "Task_ID", "Is_Active", "Override_Days", "Milestone_ID", "Team_ID",
               "Start_Pin_Date", "Not_Before", "Start_Pin_Exact", "Fixed_End_Date", "Level_Rank"]
    rows = {}
    if frames.get("job_tasks") is not None:
        for r in frames["job_tasks"].to_dict("records"):
            rows[(r["Job_ID"], r["Task_ID"])] = r
    for k, value in fixed.items():
        start, days, team = value[:3]
        exact, end = (value[3], value[4]) if len(value) > 3 else (False, None)
        r = rows.setdefault(k, {"Job_ID": k[0], "Task_ID": k[1], "Is_Active": "Y"})
        r["Start_Pin_Date"] = start
        r["Override_Days"] = days
        if team is not None:
            r["Team_ID"] = team
        if exact:
            r["Start_Pin_Exact"] = "Y"
        if end:
            r["Fixed_End_Date"] = end
    for k in plan.get("exclude", ()):
        r = rows.setdefault(k, {"Job_ID": k[0], "Task_ID": k[1]})
        r["Is_Active"] = "N"
    for k, start in plan.get("not_before", {}).items():
        if k in fixed or k in plan["draft_moves"]:
            continue
        r = rows.setdefault(k, {"Job_ID": k[0], "Task_ID": k[1], "Is_Active": "Y"})
        r["Not_Before"] = start
    for k, start in plan["draft_moves"].items():
        if k in fixed:
            continue
        r = rows.setdefault(k, {"Job_ID": k[0], "Task_ID": k[1], "Is_Active": "Y"})
        r["Start_Pin_Date"] = start
    for k, rank in plan.get("level_rank", {}).items():
        if k in fixed:
            continue
        r = rows.setdefault(k, {"Job_ID": k[0], "Task_ID": k[1], "Is_Active": "Y"})
        r["Level_Rank"] = rank
    frames["job_tasks"] = pd.DataFrame(list(rows.values()), columns=columns) if rows else None
    lower_bound = plan.get("lower_bound")
    if lower_bound:
        project = frames["project"].copy()
        current = str(project.iloc[0]["Start_Date"])
        if not current or lower_bound > current:
            project.loc[project.index[0], "Start_Date"] = lower_bound
        frames["project"] = project
    return frames


def _quiet_past_violations(result_df, quiet_before):
    """全面再計画を実行した日より前に始まるタスクの違反を消す（§8-6「過去の違反を
    ノイズにしない」）。もう変えようがない過去の固定同士の重なりなので報告しない。"""
    if not quiet_before or result_df.empty:
        return result_df
    past = (result_df["Start_Date"] < pd.Timestamp(quiet_before)) & (result_df["Constraint_Violation"] != "")
    if not past.any():
        return result_df
    result_df = result_df.copy()
    result_df.loc[past, "Constraint_Violation"] = ""
    if "Constraint_Violation_Days" in result_df.columns:
        result_df.loc[past, "Constraint_Violation_Days"] = 0
    return result_df


def _has_late_predecessor(key, plan, predecessors, ends):
    """key の先行タスク（無効のタスクは飛ばしてその先行）に、確定した終了日より後ろで
    終わるもの・確定した位置を持たないものがあるか。"""
    confirmed_end = plan.get("confirmed_end") or {}

    def late(k, seen):
        for pred in predecessors.get(k, ()):
            if pred in seen:
                continue
            seen.add(pred)
            end = ends.get(pred)
            if end is None:
                # 無効のタスク（スケジューラは依存をその先行へ繋ぎ替える）
                if late(pred, seen):
                    return True
                continue
            planned = confirmed_end.get(pred)
            if planned is None or end > planned:
                return True
        return False

    return late(key, set())


def _is_newly_broken(key, violation_days, overbooked, plan, predecessors, ends):
    """確定の位置に固定したタスク key の違反が、確定した後の変化で起きたものか
    （compute_schedule_with_plan。影響範囲に加えて置き直すか）。

    plan に confirmed_end が無い（確定の位置を持たない材料）ときは、これまでどおり
    どの違反も置き直す。"""
    if plan.get("confirmed_end") is None:
        return True
    if overbooked and plan.get("global_changed"):
        return True
    if violation_days <= 0:
        return False
    return _has_late_predecessor(key, plan, predecessors, ends)


def _release_origins(released, plan):
    """{影響範囲のタスク: そのタスクを動かしうる変更の起点の集合}（依存でたどる）。"""
    origins = {}
    for root in plan.get("changed", ()):
        for k in _downstream({root}, plan["successors"]):
            if k in released:
                origins.setdefault(k, set()).add(root)
    return origins


def _downstream(keys, successors):
    from gui.plan_confirmation import downstream
    return downstream(keys, successors)


def _blocking_fixed_tasks(result_df, plan, fixed, started, released):
    """確定の位置に固定したタスクのうち、ラインを空けるために押し下げるもの。
    {押し下げるタスク: 押し下げる原因のタスクの集合} を返す。原因は次の2つ。

    - 影響範囲のタスクが、チームのライン数が空いていなかったために着手可能日より後ろへ
      ずれた: 同じチームで、着手可能日〜置いた終了日の間に始まるタスクを押し下げる。
      着手可能日より前に始まっているものは、時間の上で先にある仕事なので押し下げない
    - 開始日を決めて置いたタスク（進行中・完了の実績、変更案の移動、開始固定日）が、
      確定した終了日より後ろで終わる（進行中のタスクの日数を延ばした、ドラッグで後ろへ
      動かした等）: 同じチームで、確定した終了日（それより後ろに置いたなら置いた開始日）〜
      今の終了日の間に始まるタスクを押し下げる。前へ動かして重なった分は押し下げない
      （前にある仕事を後ろへ回すことになるので、ライン数の超過として見せる）

    押し下げるのは、同じチームで確定の位置に固定している未着手のタスク。開始固定日・
    変更案の移動（ドラッグ・ずらす）で置いたタスクと、進行中・完了のタスクは押し下げない
    （利用者が置いた位置や実際に起きたことなので。前から押されたタスクがその手前に入り
    きらなければ、その後ろへ回る）。"""
    if "Earliest_Start" not in result_df.columns:
        return {}
    confirmed_end = plan.get("confirmed_end") or {}
    by_team = {}
    windows = []
    for job, task, team, start, end, earliest in zip(
        result_df["Job_ID"], result_df["Task_ID"], result_df["Team_ID"], result_df["Start_Date"],
        result_df["End_Date"], result_df["Earliest_Start"],
    ):
        k = (job, task)
        if k in fixed and k not in started:
            by_team.setdefault(team, []).append((start, k))
            continue
        if pd.isna(earliest):
            # 開始日を決めて置いたタスク（パス1）
            if k in confirmed_end and end.date().isoformat() > confirmed_end[k]:
                windows.append((k, team, max(start, pd.Timestamp(confirmed_end[k])), end))
        elif k in released and k in plan["not_before"] and start > earliest:
            windows.append((k, team, earliest, end))
    if not windows:
        return {}
    for items in by_team.values():
        items.sort()
    blockers = {}
    for k, team, window_start, window_end in windows:
        items = by_team.get(team)
        if not items:
            continue
        lo = bisect.bisect_left(items, (window_start,))
        for start, b in items[lo:]:
            if start >= window_end:
                break
            if b != k:
                blockers.setdefault(b, set()).add(k)
    return blockers


def _back_to_confirmed(result_df, plan, values, released, started, changed, predecessors, ends):
    """影響範囲のタスクのうち、確定した位置へ戻すもの。

    確定した位置そのものが依存に反していた（先行タスクより前へドラッグして確定した、
    一部だけ確定した等）タスクは、上流の別の変更で影響範囲に入ると、依存に合わせて
    後ろへ押し出されていた（先行タスクは確定どおりなのに、確定した直後や無関係な変更の
    後に動いた）。確定の位置に固定したタスクと同じく（_is_newly_broken）、先行タスクが
    確定より遅れていない間は確定の位置に置き、違反として見せる。

    対象は、自身の入力は変わっておらず（変更の起点でない）、移動の記録も無く、ライン数の
    ためではなく（着手可能日どおりに置いた）確定より後ろへずれたもの。values は
    {タスク: 確定の位置の値}。"""
    if not values or "Earliest_Start" not in result_df.columns:
        return set()
    back = set()
    for job, task, start, earliest in zip(
        result_df["Job_ID"], result_df["Task_ID"], result_df["Start_Date"], result_df["Earliest_Start"],
    ):
        k = (job, task)
        value = values.get(k)
        if (value is None or k not in released or k in started or k in changed
                or pd.isna(earliest) or start != earliest):
            continue
        if start.date().isoformat() <= value[0]:
            continue
        if not _has_late_predecessor(k, plan, predecessors, ends):
            back.add(k)
    return back


def compute_schedule_with_plan(frames, plan, **scheduler_kwargs):
    """確定を踏まえて計算する（ワーカースレッドから呼んでよい。DBに触れない）。

    影響範囲の外を確定の位置に固定して計算し、次のどれかに当たれば影響範囲を直して
    計算し直す。PLAN_MAX_RUNS 回で打ち切り、残った違反はそのまま返す。

    1. 固定したタスクが固定どおりに置けなくなった（_is_newly_broken）: それとその後続を
       影響範囲に加える。置けなくなった、とみなすのは次の場合だけ
       - 依存の違反で、先行タスクが確定した終了日より後ろへずれている（休業日を足して
         延びた、進行中のタスクが遅れている等）
       - ライン数の超過で、全体の設定（ライン数・休業日等）が確定から変わっている
       確定した日程そのものが依存やライン数に反している（先行タスクより前へドラッグして
       確定した、一部だけ確定した後に先行の変更を破棄した等）ときは、確定どおりに置いて
       違反として見せる。以前はこれも影響範囲に加えていたため、確定した直後にタスクが
       後ろへ飛び、「確定済み」なのに表示が確定した日程と違っていた。
    2. 影響範囲のタスクが、確定の位置に固定したタスクにラインを阻まれて着手可能日より
       後ろへずれた、または開始日を決めて置いたタスクが確定より後ろへはみ出した
       （_blocking_fixed_tasks）: 阻んだタスクとその後続も影響範囲に加えて詰め直す
       （押し下げ）。以前は阻んだタスクを動かさず、日数ぶん連続して空いている最初の
       隙間へ置いていたため、日数を5日延ばしただけのタスク（やその後続）が数か月先へ
       飛んでいた。影響範囲のタスクは確定した開始日より前へは動かないので、押し下げた
       タスクも確定より前には出ない
    置き直す確定済みのタスクは、確定していた開始日の順にスケジューラに置かせる
    （Level_Rank）。並びを保ったまま、阻まれた分だけ後ろへずれる
    3. 影響範囲のタスクが、確定した位置そのものの依存の違反のために後ろへずれた
       （_back_to_confirmed）: 確定の位置に戻す（1. と同じ考え方）

    Returns: (result_df, info)。info は {"released": 影響範囲のキー集合（押し下げのために
    加えたが動かなかったタスクは含めない）, "runs": 計算回数, "pushed": {押し下げで動いた
    タスク: 動かした変更の起点の集合}}。"""
    fixed = dict(plan["fixed"])
    plan = dict(plan, not_before=dict(plan.get("not_before", {})))
    released = set(plan["released"])
    started = plan["started"]
    changed = plan.get("changed") or set()
    predecessors = {}
    for pred, succs in plan["successors"].items():
        for succ in succs:
            predecessors.setdefault(succ, []).append(pred)
    origins = _release_origins(released, plan)
    for k in started & changed:
        # 日数を延ばした進行中のタスク等が押し下げたタスクは、そのタスクの変更が起点
        origins.setdefault(k, set()).add(k)
    pushed_from = {}  # 押し下げで影響範囲に加えたタスク: 確定の位置の値
    kept_back = set()  # 確定した位置へ戻したタスク
    # 確定した位置へ戻すと、影響範囲のタスクのラインを阻んでしまうタスク（確定した日程
    # そのものがライン数も超えていた等）。戻さずに置き直す
    not_back = set()
    runs = 0
    while True:
        runs += 1
        if plan.get("push_down", False):
            # 置き直す確定済みのタスクは、確定していた開始日の順に置く（並びを保ったまま、
            # 阻まれた分だけ後ろへずらす）。確定していないタスク（確定の後に足したジョブ等）
            # は順位を付けず最後に置く——これまでどおり確定済みのタスクの空きに入る
            plan["level_rank"] = {
                k: date.fromisoformat(start).toordinal()
                for k, start in plan["not_before"].items() if k in released and k not in fixed
            }
        result_df = compute_schedule_from_frames(
            _apply_plan_to_frames(frames, fixed, plan), **scheduler_kwargs
        )
        result_df = _quiet_past_violations(result_df, plan.get("quiet_before"))
        if runs >= PLAN_MAX_RUNS or result_df.empty:
            break
        broken = result_df[result_df["Constraint_Violation"] != ""]
        ends = {
            k: end.date().isoformat()
            for k, end in zip(zip(result_df["Job_ID"], result_df["Task_ID"]), result_df["End_Date"])
        }
        violated = {
            k for k, days, overbooked in zip(
                zip(broken["Job_ID"], broken["Task_ID"]), broken["Constraint_Violation_Days"],
                broken["Constraint_Overbooked"] if "Constraint_Overbooked" in broken.columns
                else [False] * len(broken),
            )
            if k in fixed and k not in started
            and _is_newly_broken(k, days, overbooked, plan, predecessors, ends)
        }
        added = {k for k in _downstream(violated, plan["successors"]) if k not in started}
        push_down = plan.get("push_down", False)
        blockers = _blocking_fixed_tasks(
            result_df, plan, fixed, started, released,
        ) if push_down else {}
        not_back |= kept_back & set(blockers)
        for b, by in blockers.items():
            roots = set().union(*(origins.get(k, ()) for k in by))
            for k in _downstream({b}, plan["successors"]):
                if k in started:
                    continue
                added.add(k)
                if k in fixed or k in pushed_from:
                    origins.setdefault(k, set()).update(roots)
                    if k in fixed:
                        pushed_from[k] = fixed[k]
        values = {**pushed_from, **(plan.get("confirmed_value") or {})}
        back = _back_to_confirmed(
            result_df, plan, values, released, started, changed, predecessors, ends,
        ) - added - kept_back - not_back if push_down else set()
        if not added - released and not back:
            break
        for k in back:
            fixed[k] = values[k]
            released.discard(k)
            kept_back.add(k)
        for k in added:
            was = fixed.pop(k, None)
            if was is not None:
                plan["not_before"][k] = was[0]
            kept_back.discard(k)
        released |= added
    starts = {
        k: (start.date().isoformat(), end.date().isoformat())
        for k, start, end in zip(
            zip(result_df["Job_ID"], result_df["Task_ID"]), result_df["Start_Date"], result_df["End_Date"],
        )
    } if not result_df.empty else {}
    moved = {
        k for k, was in pushed_from.items()
        if k in starts and k in released and starts[k][0] != was[0]
    }
    pushed = {k: frozenset(origins.get(k, ())) for k in moved}
    shown_released = {k for k in released if k not in pushed_from or k in moved}
    return result_df, {"released": shown_released, "runs": runs, "pushed": pushed}
