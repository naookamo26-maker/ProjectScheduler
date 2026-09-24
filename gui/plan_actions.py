"""
計画の確定・変更案の操作（docs/roadmap.md §8-6）。DBへの書き込みは
gui/db.py のメソッドを通す（いずれも1つのUndo単位）。

- confirm_all: 「確定する」「変更を確定」。今の計算結果で確定行を丸ごと置き換える
- confirm_selected: 「選択した変更を確定」。選んだタスクと、その変更の影響で
  動いた後続タスクだけを確定する（後続を残すと、確定した日程と後続の確定が
  矛盾しうるため）

どちらも、画面に出ている計算結果（ScheduleCache の result_df）を確定する。
呼び出し側は、その結果が今のDBの内容に対するもの（cache.is_fresh()）で
あることを確かめてから呼ぶ。
"""

from datetime import datetime

from gui.gantt_edit import parse_entity_id
from gui.plan_confirmation import (
    effective_task_values,
    global_signature,
    task_signatures,
)

_STARTED = ("in_progress", "done")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def confirmed_rows_from_result(db, result_df, only_keys=None):
    """計算結果から確定行を作る。only_keys（(job_id, workflow_task_id) の集合）を
    渡すとそのタスクだけ。

    日数・チームは実効の入力値を書く。ただし進行中・完了のタスクで既に確定行が
    あるものは、確定行の日数・チームのまま残す（実施した事実なので、ワークフロー
    側の値を直しても遡って変えない。§8-2）。"""
    signatures = task_signatures(db)
    effective = effective_task_values(db)
    old = {(r["job_id"], r["workflow_task_id"]): r for r in db.list_confirmed_schedule()}
    started = {
        (r["job_id"], r["workflow_task_id"])
        for r in db.list_all_job_task_overrides() if r["status"] in _STARTED
    }
    rows = []
    for job_key, task_key, start, end, team_key in zip(
        result_df["Job_ID"], result_df["Task_ID"], result_df["Start_Date"],
        result_df["End_Date"], result_df["Team_ID"],
    ):
        key = (parse_entity_id(job_key), parse_entity_id(task_key))
        if only_keys is not None and key not in only_keys:
            continue
        if key not in signatures:
            continue
        days, _team = effective[key]
        team_id = parse_entity_id(team_key) if isinstance(team_key, str) and team_key else None
        if key in started and key in old:
            days, team_id = old[key]["days"], old[key]["team_id"]
        rows.append({
            "job_id": key[0], "workflow_task_id": key[1],
            "start_date": start.date().isoformat(), "end_date": end.date().isoformat(),
            "days": int(days), "team_id": team_id, "input_signature": signatures[key],
        })
    return rows


def confirm_all(db, result_df):
    """今の計算結果を確定する（「確定する」「変更を確定」）。"""
    db.replace_confirmed_schedule(
        confirmed_rows_from_result(db, result_df), _now(), global_signature(db),
    )


def selected_targets(state, keys, successors):
    """「選択した変更を確定」で確定するタスク: 選んだタスク（(job_id,
    workflow_task_id) の集合）のうち変更のあるものと、その影響で動いた後続。
    空なら、選んだタスクには確定していない変更が無い。"""
    from gui.plan_confirmation import downstream

    changed_or_affected = downstream(state.changed, successors)
    targets = {k for k in downstream(set(keys), successors) if k in changed_or_affected}
    targets |= {k for k in keys if k in state.changed}
    return targets


def confirm_selected(db, result_df, keys, successors):
    """選んだタスクの変更と、その影響で動いた後続を確定する（selected_targets）。
    確定したタスクの数を返す。

    successors は gui/plan_confirmation.successor_map(db) の戻り値。"""
    from gui.plan_confirmation import PlanState

    state = PlanState(db)
    targets = selected_targets(state, keys, successors)
    if not targets:
        return 0
    rows = confirmed_rows_from_result(db, result_df, only_keys=targets)
    written = {(r["job_id"], r["workflow_task_id"]) for r in rows}
    # 結果に載っていない（無効にした・削除した）タスクの確定行は消す
    remove = [k for k in targets if k not in written and k in state.confirmed]
    db.update_confirmed_rows(rows, remove, _now())
    return len(targets)
