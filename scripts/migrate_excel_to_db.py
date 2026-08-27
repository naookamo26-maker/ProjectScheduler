"""
既存の入力用Excel（project_scheduler.py が読む多シート形式）を、GUIが使う
SQLite形式（.pschedule）へ一度だけ変換する移行スクリプト。

GUIの常設インポート機能ではない——サンプルデータや、これまで手動編集して
きたExcelを新方式に一度だけ移行するためのCLIツールとして使う。

使い方:
    python scripts/migrate_excel_to_db.py <入力.xlsx> <出力.pschedule>

例:
    python scripts/migrate_excel_to_db.py \\
        data/Project_Schedule_Sample_GameDev_v22.xlsx \\
        data/Project_Schedule_Sample_GameDev_v22.pschedule
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.db import ProjectDatabase  # noqa: E402
from project_scheduler import _load_data  # noqa: E402


def _clean(value):
    return str(value) if pd.notna(value) and str(value).strip() != "" else None


def migrate(xlsx_path, db_path):
    (df_project, df_teams, df_ms, df_wf, df_jobs, df_jtasks,
     df_holidays, df_extdeps, df_wf_names) = _load_data(xlsx_path)

    db = ProjectDatabase.create_new(db_path)

    # -- Project ------------------------------------------------------------
    proj_row = df_project.iloc[0]
    start_date = pd.to_datetime(proj_row["Start_Date"]).strftime("%Y-%m-%d")
    db.set_project(str(proj_row["Project_Name"]), start_date)

    # -- Teams（Workflowsが参照する未定義チームは仮登録して補う） -------------
    team_id_map = {}
    for _, row in df_teams.iterrows():
        team_str_id = str(row["Team_ID"])
        name = _clean(row.get("Team_Name")) or team_str_id
        max_lines = int(row["Max_Lines"])
        team_id_map[team_str_id] = db.add_team(name, max_lines)

    def _resolve_team(team_str_id):
        if team_str_id not in team_id_map:
            print(f"警告: 未定義のチーム '{team_str_id}' を仮登録します（ライン数999）")
            team_id_map[team_str_id] = db.add_team(team_str_id, 999)
        return team_id_map[team_str_id]

    # -- Milestones -----------------------------------------------------------
    milestone_id_map = {}
    for ms_id, row in df_ms.iterrows():
        name = _clean(row.get("Milestone_Name")) or str(ms_id)
        end_date = pd.to_datetime(row["End_Date"]).strftime("%Y-%m-%d")
        milestone_id_map[str(ms_id)] = db.add_milestone(name, end_date)

    # -- Holidays ---------------------------------------------------------------
    for _, row in df_holidays.iterrows():
        d = pd.to_datetime(row.get("Date"))
        if pd.isna(d):
            continue
        team_str_id = _clean(row.get("Team_ID"))
        team_pk = _resolve_team(team_str_id) if team_str_id else None
        db.add_holiday(d.strftime("%Y-%m-%d"), team_pk)

    # -- Workflow_Names（表示名マップ） ------------------------------------------
    wf_name_map = {}
    for _, row in df_wf_names.iterrows():
        wid, wname = _clean(row.get("Workflow_ID")), _clean(row.get("Workflow_Name"))
        if wid and wname:
            wf_name_map[wid] = wname

    # -- Workflows + workflow_tasks ------------------------------------------------
    workflow_id_map = {}
    task_id_map = {}  # (workflow_str_id, task_str_id) -> workflow_task PK
    for wf_str_id, wf_group in df_wf.groupby("Workflow_ID", sort=False):
        wf_str_id = str(wf_str_id)
        display_name = wf_name_map.get(wf_str_id, wf_str_id)
        wf_pk = db.add_workflow(display_name)
        workflow_id_map[wf_str_id] = wf_pk
        for _, trow in wf_group.iterrows():
            task_str_id = str(trow["Task_ID"])
            team_pk = _resolve_team(str(trow.get("Team_ID")))
            task_pk = db.add_workflow_task(
                wf_pk, str(trow["Task_Name"]), team_pk, int(trow["Default_Days"])
            )
            task_id_map[(wf_str_id, task_str_id)] = task_pk

    # -- Internal_Depends（全タスク登録後に2周目で解決） --------------------------
    for wf_str_id, wf_group in df_wf.groupby("Workflow_ID", sort=False):
        wf_str_id = str(wf_str_id)
        wf_pk = workflow_id_map[wf_str_id]
        for _, trow in wf_group.iterrows():
            succ_pk = task_id_map[(wf_str_id, str(trow["Task_ID"]))]
            deps = trow.get("Internal_Depends")
            if not pd.notna(deps):
                continue
            for dep in str(deps).split(","):
                dep = dep.strip()
                if not dep:
                    continue
                pred_pk = task_id_map.get((wf_str_id, dep))
                if pred_pk is None:
                    print(f"警告: 依存先タスク '{dep}' が見つかりません（WF={wf_str_id}）。スキップします")
                    continue
                db.add_task_dependency(wf_pk, pred_pk, succ_pk)

    # ノードグラフの座標はDBに保存しない（gui/node_canvas.pyのreload()/
    # auto_arrangeが表示のたびに依存の深さから計算し直すため、ここで初期値を
    # 与える必要が無い）。

    # -- Jobs -----------------------------------------------------------------------
    job_id_map = {}
    job_workflow_map = {}  # job_str_id -> workflow_str_id（Job_Tasks/External_Dependencies解決用）
    for _, row in df_jobs.iterrows():
        job_str_id = str(row["Job_ID"])
        wf_str_id = str(row["Workflow_ID"])
        wf_pk = workflow_id_map.get(wf_str_id)
        if wf_pk is None:
            print(f"警告: ジョブ '{job_str_id}' の Workflow_ID '{wf_str_id}' が見つかりません。スキップします")
            continue
        ms_str_id = _clean(row.get("Default_Milestone_ID"))
        ms_pk = milestone_id_map.get(ms_str_id) if ms_str_id else None
        priority_val = row.get("Priority")
        priority = int(priority_val) if pd.notna(priority_val) else 100
        job_pk = db.add_job(str(row["Job_Name"]), wf_pk, ms_pk, priority)
        job_id_map[job_str_id] = job_pk
        job_workflow_map[job_str_id] = wf_str_id

    # -- Job_Tasks（既定から外れる場合のみ upsert） --------------------------------
    for (job_str_id, task_str_id), row in df_jtasks.iterrows():
        job_str_id, task_str_id = str(job_str_id), str(task_str_id)
        job_pk = job_id_map.get(job_str_id)
        wf_str_id = job_workflow_map.get(job_str_id)
        if job_pk is None or wf_str_id is None:
            continue
        task_pk = task_id_map.get((wf_str_id, task_str_id))
        if task_pk is None:
            print(f"警告: Job_Tasks の '{job_str_id}:{task_str_id}' に対応するタスクが見つかりません。スキップします")
            continue

        is_active = str(row.get("Is_Active", "Y")).strip().upper() != "N"
        override_days_val = row.get("Override_Days")
        override_days = int(override_days_val) if pd.notna(override_days_val) else None
        ms_str_id = _clean(row.get("Milestone_ID"))
        ms_pk = milestone_id_map.get(ms_str_id) if ms_str_id else None
        team_str_id = _clean(row.get("Team_ID"))
        team_pk = _resolve_team(team_str_id) if team_str_id else None

        if not is_active or override_days is not None or ms_pk is not None or team_pk is not None:
            db.upsert_job_task_override(
                job_pk, task_pk, is_active=is_active, override_days=override_days,
                milestone_id=ms_pk, team_id=team_pk,
            )

    # -- External_Dependencies --------------------------------------------------------
    skipped = 0
    for _, row in df_extdeps.iterrows():
        job_str_id = _clean(row.get("Job_ID"))
        task_str_id = _clean(row.get("Task_ID"))
        dep_job_str_id = _clean(row.get("Depends_On_Job_ID"))
        dep_task_str_id = _clean(row.get("Depends_On_Task_ID"))
        if not (job_str_id and task_str_id and dep_job_str_id and dep_task_str_id):
            skipped += 1
            continue

        job_pk = job_id_map.get(job_str_id)
        dep_job_pk = job_id_map.get(dep_job_str_id)
        wf_str_id = job_workflow_map.get(job_str_id)
        dep_wf_str_id = job_workflow_map.get(dep_job_str_id)
        if job_pk is None or dep_job_pk is None or wf_str_id is None or dep_wf_str_id is None:
            skipped += 1
            continue

        task_pk = task_id_map.get((wf_str_id, task_str_id))
        dep_task_pk = task_id_map.get((dep_wf_str_id, dep_task_str_id))
        if task_pk is None or dep_task_pk is None:
            skipped += 1
            continue

        try:
            db.add_external_dependency(job_pk, task_pk, dep_job_pk, dep_task_pk)
        except Exception as e:
            print(f"警告: 外部依存の登録に失敗しました（{job_str_id}:{task_str_id} <- "
                  f"{dep_job_str_id}:{dep_task_str_id}）: {e}")
            skipped += 1

    if skipped:
        print(f"External_Dependencies: {skipped} 行をスキップしました")

    db.close()
    print(f"移行完了: {xlsx_path} -> {db_path}")
    print(f"  チーム {len(team_id_map)} 件 / マイルストーン {len(milestone_id_map)} 件 / "
          f"ワークフロー {len(workflow_id_map)} 件 / タスク {len(task_id_map)} 件 / "
          f"ジョブ {len(job_id_map)} 件")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    migrate(sys.argv[1], sys.argv[2])
