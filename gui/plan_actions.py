"""
計画の確定・変更案の操作（docs/roadmap.md §8-6）。DBへの書き込みは
gui/db.py のメソッドを通す（いずれも1つのUndo単位）。

- confirm_all: 「確定する」「変更を確定」。今の計算結果で確定行を丸ごと置き換える
- confirm_selected: 「選択した変更を確定」。選んだタスクと、その変更の影響で
  動いた後続タスク・押し下げたタスクだけを確定する（残すと、確定した日程と
  それらの確定が矛盾しうるため）

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


def _now():
    return datetime.now().isoformat(timespec="seconds")


def confirmed_rows_from_result(db, result_df, only_keys=None, keep_done_facts=True):
    """計算結果から確定行を作る。only_keys（(job_id, workflow_task_id) の集合）を
    渡すとそのタスクだけ。

    日数・チームは実効の入力値を書く。ただし完了のタスクで実績（task_facts）がある
    ものは、実績の日数・チームにする（実施した事実なので、ワークフロー側の値を直しても
    遡って変えない。§8-2）。進行中のタスクは日数・チームを今の入力に従わせる
    （gui/gantt_generator.build_plan）ので実績の値を使わない。keep_done_facts=False
    なら表示中の値そのもの（状態を変えたときの実績の記録。ProjectDatabase._record_status_fact）。"""
    signatures = task_signatures(db)
    effective = effective_task_values(db)
    old = {(r["job_id"], r["workflow_task_id"]): r for r in db.list_task_facts()}
    done = {
        (r["job_id"], r["workflow_task_id"])
        for r in db.list_all_job_task_overrides() if r["status"] == "done"
    } if keep_done_facts else set()
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
        if key in done and key in old:
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


def pushed_tasks(plan_info):
    """確定を踏まえた計算の情報（compute_schedule_with_plan の info）から、押し下げで
    動いたタスク {(job_id, workflow_task_id): {動かした変更の起点, ...}}。"""
    def ids(key):
        return (parse_entity_id(key[0]), parse_entity_id(key[1]))

    return {
        ids(k): {ids(o) for o in origins}
        for k, origins in ((plan_info or {}).get("pushed") or {}).items()
    }


def selected_targets(state, keys, successors, pushed=None):
    """「選択した変更を確定」で確定するタスク: 選んだタスク（(job_id,
    workflow_task_id) の集合）のうち変更のあるものと、その影響で動いた後続、
    その変更が押し下げた他のタスク（pushed は pushed_tasks の戻り値）。
    空なら、選んだタスクには確定していない変更が無い。

    押し下げたタスクを残すと、確定した変更がそのラインを取ったまま、押し下げた側が
    確定の位置へ戻ってライン数の超過になる（確定した直後に表示が変わる）。"""
    from gui.plan_confirmation import downstream

    changed_or_affected = downstream(state.changed, successors)
    targets = {k for k in downstream(set(keys), successors) if k in changed_or_affected}
    targets |= {k for k in keys if k in state.changed}
    roots = {k for k in targets if k in state.changed}
    targets |= {k for k, origins in (pushed or {}).items() if origins & roots}
    return targets


def confirm_selected(db, result_df, keys, successors, pushed=None):
    """選んだタスクの変更と、その影響で動いた後続・押し下げたタスクを確定する
    （selected_targets）。確定したタスクの数を返す。

    successors は gui/plan_confirmation.successor_map(db) の戻り値、pushed は
    pushed_tasks(result_df と同じ計算の info) の戻り値。

    ジョブの優先度・ワークフロー内の依存の変更（ジョブ・ワークフロー全体で1つの入力）は、
    その変更だけを受けた同じジョブ・ワークフローのタスクも一緒に確定する
    （gui/plan_confirmation.shared_change_companions）。"""
    from gui.plan_confirmation import PlanState, shared_change_companions

    state = PlanState(db)
    targets = selected_targets(state, keys, successors, pushed)
    if not targets:
        return 0
    companions = shared_change_companions(db, state, targets, db.draft_base_shared_inputs())
    if companions:
        targets = selected_targets(state, set(keys) | companions, successors, pushed)
    rows = confirmed_rows_from_result(db, result_df, only_keys=targets)
    written = {(r["job_id"], r["workflow_task_id"]) for r in rows}
    # 結果に載っていない（無効にした・削除した）タスクの確定行は消す
    remove = [k for k in targets if k not in written and k in state.confirmed]
    db.update_confirmed_rows(rows, remove, _now())
    return len(targets)
