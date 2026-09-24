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
      ドラッグしたタスク）
    - global_changed: 全体設定が確定時から変わったか
    - started: 進行中・完了のタスク
    - draft_moves: {(job_id, workflow_task_id): 'YYYY-MM-DD'}
    - facts: 進行中・完了のタスクの確定行（未確定でも、その日程で固定して計算する）
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
        self.started = {
            (r["job_id"], r["workflow_task_id"])
            for r in db.list_all_job_task_overrides() if r["status"] in _STARTED
        }
        self.facts = {k: r for k, r in self.confirmed.items() if k in self.started}
        if not self.confirmed_at:
            # 未確定（一度も確定していない、または「未確定に戻す」の後）。残っている
            # 確定行は進行中・完了のタスクのもの（facts）だけを使う
            self.confirmed = {}
            self.draft_moves = {}
            self.status = UNCONFIRMED
            self.changed = set()
            self.global_changed = False
            self.active = _task_inputs(db)[1] if self.facts else {}
            return
        inputs, self.active = _task_inputs(db)
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
        self.changed = changed
        self.global_changed = project["confirmed_global_signature"] != global_signature(db)
        self.status = DRAFT if (changed or self.global_changed) else CONFIRMED


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


def release_set(state, successors):
    """確定の位置から外して計算し直すタスク（変更の起点とその後続。ただし
    進行中・完了のタスクは除く）。"""
    return {key for key in downstream(state.changed, successors) if key not in state.started}
