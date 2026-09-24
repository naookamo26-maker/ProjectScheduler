"""
DB（ProjectDatabase）の内容から project_scheduler.py を直接呼び出して
ガントチャートを生成する。Excelファイル/バッファは一切経由しない。

内部整数PKから project_scheduler.py が期待する文字列ID（"WF_001" 等）への
変換はここで一度だけ行う（GUIの画面上にはこの文字列IDは一切表示されない）。
"""

from datetime import date

import pandas as pd

from gui.db import parse_tags
from gui.gantt_edit import format_entity_id
from project_scheduler import (
    _TEAM_COLOR_OVERFLOW,
    _build_team_capacity_schedule,
    _build_team_color_map,
    format_dependency_ref,
    generate_jp_holidays,
    run_resource_constrained_scheduler_from_frames,
)


def _fmt(prefix, entity_id, width=3):
    return format_entity_id(prefix, entity_id, width)


def validate_for_generation(db):
    """ガントチャート生成前の入力充足チェック。空リストなら生成可能。
    ここで弾いておかないと、必須列を持たない空のDataFrameがそのまま
    project_scheduler.py に渡り、分かりにくいエラーになるため。"""
    errors = []
    proj = db.get_project()
    if not proj["project_name"].strip():
        errors.append("プロジェクト名が未設定です（基本情報設定タブ）")
    if not proj["start_date"]:
        errors.append("開発開始日が未設定です（基本情報設定タブ）")
    if not db.list_teams():
        errors.append("チームが1件も登録されていません（基本情報設定タブ）")
    if not db.list_milestones():
        errors.append("マイルストーンが1件も登録されていません（基本情報設定タブ）")
    workflows = db.list_workflows()
    if not workflows:
        errors.append("ワークフローが1件も登録されていません（ワークフロー設計タブ）")
    elif not any(db.list_workflow_tasks(w["id"]) for w in workflows):
        errors.append("タスクを持つワークフローが1件もありません（ワークフロー設計タブ）")
    if not db.list_jobs():
        errors.append("ジョブが1件も登録されていません（ジョブ作成タブ）")
    return errors


def build_frames(db):
    """DBの内容から project_scheduler.py が要求する形のDataFrame群を組み立てる。

    Returns: dict（project/teams/milestones/workflows/workflow_names/jobs/
    job_tasks/holidays/external_dependencies をキーとする）。
    job_tasks/holidays/external_dependencies は該当データが無ければ None
    （_load_data_from_frames の「シート/データが存在しない」扱いに対応）。
    """
    proj = db.get_project()
    df_project = pd.DataFrame([{
        "Project_ID": "PRJ_001",
        "Project_Name": proj["project_name"],
        "Start_Date": proj["start_date"],
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
        milestone_markers.append(("PROJECT_START", "プロジェクト開始", pd.to_datetime(proj["start_date"])))
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
    ワーカースレッドで実行する。gui/tab_gantt.py の _ScheduleWorker を参照）。
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


def generate_gantt(db, plotly_output_path=None, **scheduler_kwargs):
    """build_frames() の結果を project_scheduler.run_resource_constrained_scheduler_from_frames()
    にそのまま渡す。SchedulingError系（循環依存・リソース不足・マイルストーン不整合等）は
    そのまま呼び出し元に伝播させる（GUI側でダイアログに変換する）。

    distribution_ratio を明示指定しなければ、プロジェクト設定
    （db.get_project()["distribution_ratio"]）を既定値として使う——
    ガントチャートタブで調整した基準点が、メニューの「ガントチャートを
    生成」（HTMLファイル出力）でもそのまま使われるようにするため。"""
    frames = build_frames(db)
    scheduler_kwargs.setdefault("distribution_ratio", db.get_project()["distribution_ratio"])
    return run_resource_constrained_scheduler_from_frames(
        frames["project"], frames["teams"], frames["milestones"], frames["workflows"],
        frames["jobs"], frames["job_tasks"], frames["holidays"], frames["external_dependencies"],
        frames["workflow_names"], df_team_capacity=frames["team_capacity"],
        plotly_output_path=plotly_output_path,
        **scheduler_kwargs,
    )
