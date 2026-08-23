"""
DB（ProjectDatabase）の内容から project_scheduler.py を直接呼び出して
ガントチャートを生成する。Excelファイル/バッファは一切経由しない。

内部整数PKから project_scheduler.py が期待する文字列ID（"WF_001" 等）への
変換はここで一度だけ行う（GUIの画面上にはこの文字列IDは一切表示されない）。
"""

import pandas as pd

from project_scheduler import run_resource_constrained_scheduler_from_frames


def _fmt(prefix, entity_id, width=3):
    return f"{prefix}_{entity_id:0{width}d}"


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
        errors.append("ジョブが1件も登録されていません（ジョブタブ）")
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

    milestones = db.list_milestones()
    ms_str = {m["id"]: _fmt("MS", m["id"]) for m in milestones}
    df_ms = pd.DataFrame([{
        "Milestone_ID": ms_str[m["id"]], "End_Date": m["end_date"], "Milestone_Name": m["name"],
    } for m in milestones])

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
            deps_by_succ.setdefault(d["successor_task_id"], []).append(d["predecessor_task_id"])
        tasks = db.list_workflow_tasks(w["id"])
        for t in tasks:
            task_str[t["id"]] = _fmt("T", t["id"])
        for t in tasks:
            preds = deps_by_succ.get(t["id"], [])
            internal_depends = ",".join(task_str[p] for p in preds) if preds else None
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

    jt_rows = []
    for j in jobs:
        for r in db.list_job_tasks_with_overrides(j["id"]):
            if r["override_id"] is None:
                continue
            jt_rows.append({
                "Job_ID": job_str[j["id"]],
                "Task_ID": task_str[r["workflow_task_id"]],
                "Is_Active": "Y" if r["is_active"] else "N",
                "Override_Days": r["override_days"],
                "Milestone_ID": ms_str.get(r["override_milestone_id"]),
                "Team_ID": team_str.get(r["override_team_id"]),
            })
    df_jtasks = pd.DataFrame(jt_rows) if jt_rows else None

    holidays = db.list_holidays()
    df_holidays = pd.DataFrame([{
        "Date": h["date"], "Team_ID": team_str.get(h["team_id"]),
    } for h in holidays]) if holidays else None

    ext_deps = db.list_external_dependencies()
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
    }


def generate_gantt(db, mermaid_output_path=None, plotly_output_path=None, **scheduler_kwargs):
    """build_frames() の結果を project_scheduler.run_resource_constrained_scheduler_from_frames()
    にそのまま渡す。SchedulingError系（循環依存・リソース不足・マイルストーン不整合等）は
    そのまま呼び出し元に伝播させる（GUI側でダイアログに変換する）。"""
    frames = build_frames(db)
    return run_resource_constrained_scheduler_from_frames(
        frames["project"], frames["teams"], frames["milestones"], frames["workflows"],
        frames["jobs"], frames["job_tasks"], frames["holidays"], frames["external_dependencies"],
        frames["workflow_names"],
        mermaid_output_path=mermaid_output_path, plotly_output_path=plotly_output_path,
        **scheduler_kwargs,
    )
