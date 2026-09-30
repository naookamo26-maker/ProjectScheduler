"""
計画の確定と変更案（docs/roadmap.md §8）の判定。Qt・pandasに依存しない。

- 入力の指紋（task_signatures / global_signature）: 確定したときに実際に使われた
  入力を記録しておき、今の入力と食い違うタスクを「変更あり」とみなす。どのタブの
  どの操作で変えたかは追わない（経路が多すぎて漏れるため。§8-7）
- 状態（plan_status）: 未確定／確定済み／変更案あり
- 影響範囲（release_set）: 変更の起点と、依存関係でその先にあるタスク。ただし
  進行中・完了のタスクは動かさない（実施した事実なので）

スケジューラへの組み込み（影響範囲以外を確定の位置に固定して計算する）は
gui/gantt_generator.py の compute_schedule_with_plan が行う。
"""

import hashlib
import json
from collections import defaultdict

UNCONFIRMED = "unconfirmed"
CONFIRMED = "confirmed"
DRAFT = "draft"

_STARTED = ("in_progress", "done")


def _digest(value):
    return hashlib.sha1(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def _task_inputs(db):
    """{(job_id, workflow_task_id): 指紋の材料} と {(job_id, workflow_task_id): is_active}。

    材料は、そのタスクの日程を決める入力のうちタスク単位のもの: 実効の日数・
    チーム・有効/無効・手動ピン・ジョブの優先度・先行タスク（ワークフロー内は
    種別とラグ、ジョブ間は相手）。マイルストーンは日程を動かさない（超過の判定だけ）ので含めない。
    """
    conn = db._conn
    rows = conn.execute(
        "SELECT j.id AS job_id, j.workflow_id, wt.id AS workflow_task_id, wt.default_days, "
        "wt.team_id AS default_team_id, o.is_active, o.override_days, o.team_id AS override_team_id, "
        "o.start_pin_date, j.priority "
        "FROM jobs j JOIN workflow_tasks wt ON wt.workflow_id = j.workflow_id "
        "LEFT JOIN job_task_overrides o ON o.job_id = j.id AND o.workflow_task_id = wt.id"
    ).fetchall()
    preds = defaultdict(list)
    for d in conn.execute(
        "SELECT predecessor_task_id, successor_task_id, dep_type, lag_days FROM task_dependencies"
    ).fetchall():
        preds[d["successor_task_id"]].append(("wf", d["predecessor_task_id"], d["dep_type"], d["lag_days"]))
    ext = defaultdict(list)
    for e in conn.execute(
        "SELECT job_id, workflow_task_id, depends_on_job_id, depends_on_workflow_task_id "
        "FROM job_external_dependencies WHERE is_active = 1"
    ).fetchall():
        ext[(e["job_id"], e["workflow_task_id"])].append(
            ("job", e["depends_on_job_id"], e["depends_on_workflow_task_id"])
        )
    inputs = {}
    active = {}
    for r in rows:
        key = (r["job_id"], r["workflow_task_id"])
        is_active = r["is_active"] is None or bool(r["is_active"])
        active[key] = is_active
        inputs[key] = {
            "days": r["override_days"] or r["default_days"],
            "team": r["override_team_id"] or r["default_team_id"],
            "active": is_active,
            "pin": r["start_pin_date"],
            "priority": r["priority"],
            "preds": sorted(preds.get(r["workflow_task_id"], [])) + sorted(ext.get(key, [])),
        }
    return inputs, active


def task_signatures(db):
    """{(job_id, workflow_task_id): 指紋}（全ジョブの全タスク）。"""
    inputs, _active = _task_inputs(db)
    return {key: _digest(value) for key, value in inputs.items()}


def effective_task_values(db):
    """{(job_id, workflow_task_id): (実効の日数, 実効のチームID)}（確定行に書く値）。"""
    inputs, _active = _task_inputs(db)
    return {key: (v["days"], v["team"]) for key, v in inputs.items()}


def global_signature(db):
    """全体設定の指紋。確定を壊さない変更（ライン数を増やす・休業日を減らす・
    配置コントロール）は、影響範囲だけを動かす方式では反映
    できないので、ここで「変更あり」として数えて知らせる（§8-7 (c)）。"""
    conn = db._conn
    value = {
        "holidays": [tuple(r) for r in conn.execute(
            "SELECT date, COALESCE(team_id, 0) FROM holidays ORDER BY date, team_id"
        ).fetchall()],
        "teams": [tuple(r) for r in conn.execute(
            "SELECT id, COALESCE(max_lines, -1) FROM teams ORDER BY id"
        ).fetchall()],
        "capacity": [tuple(r) for r in conn.execute(
            "SELECT team_id, start_date, COALESCE(lines, -1) FROM team_capacity_changes "
            "ORDER BY team_id, start_date"
        ).fetchall()],
        "ratio": conn.execute("SELECT distribution_ratio FROM project WHERE id = 1").fetchone()[0],
        "start": conn.execute("SELECT start_date FROM project WHERE id = 1").fetchone()[0],
    }
    return _digest(value)


class PlanState:
    """ある時点のDBの、確定に関する状態をまとめたもの（読み取り専用）。

    - status: UNCONFIRMED / CONFIRMED / DRAFT
    - confirmed: {(job_id, workflow_task_id): 確定行}
    - changed: 変更の起点（指紋が食い違う、確定行の無い有効なタスク、変更案で
      ドラッグしたタスク、実績が確定と違う進行中・完了のタスク）
    - global_changed: 全体設定が確定時から変わったか
    - started: 進行中・完了のタスク
    - draft_moves: {(job_id, workflow_task_id): 'YYYY-MM-DD'}
    - facts: 進行中・完了のタスクの実績（task_facts の行。確定していなくても、その
      日程で固定して計算する）
    - in_progress: 進行中のタスク（開始日だけ固定し、日数・チームは今の入力に従う）
    - inputs: {(job_id, workflow_task_id): 指紋の材料（実効の日数・チーム等）}
    - pending_replan: 全面再計画を実行中なら (基準日 D, 実行日 T)、そうでなければ None
    - replan_released: 全面再計画で置き直す未着手タスク（確定開始日が T より前、
      または D 以降。T〜D の前日に始まる予定のものは確定のまま残す。§8-6）
    """

    def __init__(self, db):
        self.confirmed = {
            (r["job_id"], r["workflow_task_id"]): r for r in db.list_confirmed_schedule()
        }
        self.draft_moves = {
            (r["job_id"], r["workflow_task_id"]): r["start_date"] for r in db.list_draft_moves()
        }
        project = db.get_project()
        self.confirmed_at = project["confirmed_at"]
        self.replan_base_date = project["replan_base_date"]
        self.replanned_at = project["replanned_at"]
        self.pending_replan = (
            (project["pending_replan_base_date"], project["pending_replanned_at"])
            if project["pending_replan_base_date"] else None
        )
        self.replan_released = set()
        overrides = db.list_all_job_task_overrides()
        self.started = {
            (r["job_id"], r["workflow_task_id"]) for r in overrides if r["status"] in _STARTED
        }
        self.in_progress = {
            (r["job_id"], r["workflow_task_id"]) for r in overrides if r["status"] == "in_progress"
        }
        # 実績（進行中・完了のタスクの実際の日程。確定とは別に持つ）
        self.facts = {
            (r["job_id"], r["workflow_task_id"]): r for r in db.list_task_facts()
            if (r["job_id"], r["workflow_task_id"]) in self.started
        }
        if not self.confirmed_at:
            # 未確定（一度も確定していない、または「未確定に戻す」の後）。進行中・完了の
            # タスクは実績（facts）で固定し、それ以外は自由に計算する
            self.confirmed = {}
            self.draft_moves = {}
            self.status = UNCONFIRMED
            self.changed = set()
            self.global_changed = False
            self.inputs, self.active = _task_inputs(db) if self.facts else ({}, {})
            return
        inputs, self.active = _task_inputs(db)
        self.inputs = inputs
        signatures = {key: _digest(value) for key, value in inputs.items()}
        changed = set()
        for key, is_active in self.active.items():
            row = self.confirmed.get(key)
            if row is None:
                if is_active:
                    changed.add(key)  # 確定後に足したジョブ・タスク
            elif row["input_signature"] != signatures[key]:
                changed.add(key)
        # 確定行はあるが、もうジョブ／タスクとして存在しないもの（削除した）
        changed |= {key for key in self.confirmed if key not in self.active}
        changed |= set(self.draft_moves)
        # 実績が確定した日程と違う（遅れて・早く始めた、変更案で動いていた位置で
        # 始めた、実績の日付を直した）。確定済みと言いながら表示が確定と違う、に
        # ならないよう「変更あり」にする（後続もこれに合わせて動く）
        for key, fact in self.facts.items():
            row = self.confirmed.get(key)
            if row is None or not self.active.get(key, False):
                continue
            if fact["start_date"] != row["start_date"] or (
                    key not in self.in_progress and fact["end_date"] != row["end_date"]):
                changed.add(key)
        self.changed = changed
        self.global_changed = project["confirmed_global_signature"] != global_signature(db)
        if self.pending_replan:
            base, executed = self.pending_replan
            self.replan_released = {
                key for key, row in self.confirmed.items()
                if key not in self.started and self.active.get(key, False)
                and (row["start_date"] < executed or row["start_date"] >= base)
            }
        self.status = DRAFT if (changed or self.global_changed or self.pending_replan) else CONFIRMED

    @property
    def quiet_before(self):
        """この日より前に始まるタスクの違反は報告しない（全面再計画を実行した日 T。
        再計画を繰り返すと過去の固定同士の重なりが積み上がるため。§8-6）。"""
        if self.pending_replan:
            return self.pending_replan[1]
        return self.replanned_at


def successor_map(db):
    """{(job_id, workflow_task_id): [後続の (job_id, workflow_task_id), ...]}
    （ワークフロー内・ジョブ間の依存。無効なタスクも含めた生の依存）。"""
    conn = db._conn
    succ = defaultdict(list)
    wf_succ = defaultdict(list)
    for d in conn.execute(
        "SELECT workflow_id, predecessor_task_id, successor_task_id FROM task_dependencies"
    ).fetchall():
        wf_succ[d["predecessor_task_id"]].append(d["successor_task_id"])
    for j in conn.execute(
        "SELECT j.id AS job_id, wt.id AS task_id FROM jobs j "
        "JOIN workflow_tasks wt ON wt.workflow_id = j.workflow_id"
    ).fetchall():
        for s in wf_succ.get(j["task_id"], ()):
            succ[(j["job_id"], j["task_id"])].append((j["job_id"], s))
    for e in conn.execute(
        "SELECT job_id, workflow_task_id, depends_on_job_id, depends_on_workflow_task_id "
        "FROM job_external_dependencies WHERE is_active = 1"
    ).fetchall():
        succ[(e["depends_on_job_id"], e["depends_on_workflow_task_id"])].append(
            (e["job_id"], e["workflow_task_id"])
        )
    return succ


def downstream(keys, successors):
    """keys とその後続（依存関係でたどれる先すべて）。"""
    seen = set(keys)
    stack = list(keys)
    while stack:
        key = stack.pop()
        for nxt in successors.get(key, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def shared_change_companions(db, state, targets, base_shared):
    """「選択した変更を確定」の対象（targets）と同じジョブ単位・ワークフロー単位の
    変更（ジョブの優先度・ワークフロー内の依存）だけを受けた、他のタスク。

    これらの入力はジョブ・ワークフロー全体で1つなので、1タスクだけ確定することは
    できない（破棄用のスナップショットに書き込むと、同じジョブの他のタスクが確定
    から変わったことになり、「変更を破棄」の直後から「変更あり」になる）。その変更
    以外に変わっていないタスクは一緒に確定する。base_shared は
    ProjectDatabase.draft_base_shared_inputs() の戻り値（None なら広げない）。"""
    if not base_shared or not targets:
        return set()
    inputs, _active = _task_inputs(db)
    task_workflow = {
        r["id"]: r["workflow_id"] for r in db._conn.execute("SELECT id, workflow_id FROM workflow_tasks")
    }

    def wf_preds(key):
        return [p for p in inputs[key]["preds"] if p[0] == "wf"]

    def base_wf_preds(task_id):
        return sorted(tuple(p) for p in base_shared["wf_preds"].get(task_id, []))

    priority_jobs = {
        k[0] for k in targets
        if k in inputs and k[0] in base_shared["priority"]
        and base_shared["priority"][k[0]] != inputs[k]["priority"]
    }
    dep_workflows = set()
    for k in targets:
        workflow = task_workflow.get(k[1])
        if k not in inputs or workflow not in base_shared["workflows"]:
            continue
        tasks = [t for t, w in task_workflow.items() if w == workflow]
        if any(sorted(tuple(p) for p in wf_preds(key)) != base_wf_preds(key[1])
               for key in inputs if key[1] in tasks and key[0] == k[0]):
            dep_workflows.add(workflow)
    if not priority_jobs and not dep_workflows:
        return set()
    companions = set()
    for key in state.changed - set(targets):
        row = state.confirmed.get(key)
        if row is None or key not in inputs:
            continue
        value = dict(inputs[key])
        if key[0] in priority_jobs:
            value["priority"] = base_shared["priority"].get(key[0], value["priority"])
        if task_workflow.get(key[1]) in dep_workflows:
            value["preds"] = [list(p) for p in base_wf_preds(key[1])] + [
                p for p in inputs[key]["preds"] if p[0] != "wf"
            ]
        if value != inputs[key] and _digest(value) == row["input_signature"]:
            companions.add(key)
    return companions


def release_set(state, successors):
    """確定の位置から外して計算し直すタスク（変更の起点とその後続。ただし
    進行中・完了のタスクは除く）。"""
    released = downstream(state.changed, successors) | state.replan_released
    return {key for key in released if key not in state.started}


# ジョブ単位の確定状態（ジョブ作成タブの「確定」列と絞り込み。§8-9）
JOB_CONFIRMED = "confirmed"
JOB_CHANGED = "changed"
JOB_PARTIAL = "partial"
JOB_UNCONFIRMED = "unconfirmed"


def job_plan_summary(state):
    """{job_id: (状態, 変更のあるタスク数, 確定行の無いタスク数)}。

    状態は JOB_UNCONFIRMED（有効なタスクに確定行が1つも無い＝確定後に足した
    ジョブ）、JOB_CHANGED（確定後に入力が変わったタスクがある）、JOB_PARTIAL
    （一部のタスクだけ確定行が無い）、JOB_CONFIRMED の順に判定する。
    確定後に無効にした・削除したタスクは「変更」に数える。"""
    counts = defaultdict(lambda: [0, 0, 0])  # 変更, 未確定, 確定行あり
    for key, is_active in state.active.items():
        c = counts[key[0]]
        if not is_active:
            if key in state.confirmed:
                c[0] += 1
            continue
        if key not in state.confirmed:
            c[1] += 1
        else:
            c[2] += 1
            if key in state.changed:
                c[0] += 1
    for key in state.confirmed:
        if key not in state.active:
            counts[key[0]][0] += 1
    summary = {}
    for job_id, (changed, unconfirmed, confirmed) in counts.items():
        if confirmed == 0 and unconfirmed:
            kind = JOB_UNCONFIRMED
        elif changed:
            kind = JOB_CHANGED
        elif unconfirmed:
            kind = JOB_PARTIAL
        else:
            kind = JOB_CONFIRMED
        summary[job_id] = (kind, changed, unconfirmed)
    return summary


def replan_preview(db, base_date, executed_on):
    """全面再計画ダイアログに出す件数（§8-6）。

    - kept: 確定開始日が 実行日 T〜基準日 D の前日 の未着手タスク（今の確定のまま残す）
    - replaced: それ以外の未着手タスクのうち確定行のあるもの（D 以降に置き直す）
    - pinned_before: D より前に手動ピン（開始固定日）がある未着手タスク
      （ピンは入力なので、その日付のまま置かれる）
    """
    state = PlanState(db)
    kept = replaced = 0
    for key, row in state.confirmed.items():
        if key in state.started or not state.active.get(key, False):
            continue
        if executed_on <= row["start_date"] < base_date:
            kept += 1
        else:
            replaced += 1
    pinned_before = sum(
        1 for r in db.list_all_job_task_overrides()
        if r.get("start_pin_date") and r["start_pin_date"] < base_date
        and r["status"] not in _STARTED and (r["is_active"] is None or r["is_active"])
    )
    return {"kept": kept, "replaced": replaced, "pinned_before": pinned_before}
