"""
実績（進行中・完了のタスクの日程。task_facts）と、確定・変更案の破棄まわりの回帰テスト。

実際に画面を操作して見つかった「意図しない変化」を1つずつ固定する:
- 完了のタスクが、後から足した休業日・年をまたぐ確定で伸び縮みした
- 状態を変えたときに日程が記録されず、後の編集で完了のタスクが動いた
- 先行タスクより前に置いて確定すると、確定した直後に後ろへ飛んだ
- 「選択した変更を確定」した分が「変更を破棄」で消えた・戻った
- Undo の後に確定すると、次の「変更を破棄」で確定する前に戻った
- 変更案で動かしたタスクに、開始固定日が効かなかった
"""

import contextlib
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pd = pytest.importorskip("pandas")

from gui.db import ProjectDatabase  # noqa: E402

# 分類: scheduler（スケジューリング結果の比較に pandas が要る）
pytestmark = pytest.mark.scheduler


def _project(tmp_path, start="2026-10-05"):
    """チームA（1ライン）で 設計(3)→実装(4)→試験(2) のジョブ2件と、別のワークフロー
    準備(2)→作業(3) のジョブ1件。"""
    db = ProjectDatabase.create_new(str(tmp_path / "facts.pschedule"))
    db.set_project("P", start)
    team = db.add_team("チームA", 1)
    ms = db.add_milestone("最終", "2027-06-30")
    wf = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf, "設計", team, 3)
    t2 = db.add_workflow_task(wf, "実装", team, 4)
    t3 = db.add_workflow_task(wf, "試験", team, 2)
    db.add_task_dependency(wf, t1, t2)
    db.add_task_dependency(wf, t2, t3)
    wf2 = db.add_workflow("WF2")
    u1 = db.add_workflow_task(wf2, "準備", team, 2)
    u2 = db.add_workflow_task(wf2, "作業", team, 3)
    db.add_task_dependency(wf2, u1, u2)
    jobs = [db.add_job("J1", wf, ms, 1), db.add_job("J2", wf, ms, 2), db.add_job("J3", wf2, ms, 3)]
    _with_display(db)
    return db, {"team": team, "wf": wf, "wf2": wf2, "t": [t1, t2, t3], "u": [u1, u2], "jobs": jobs, "ms": ms}


def _compute(db):
    """ScheduleCache と同じ手順で、確定・実績を踏まえて計算する。"""
    from gui.gantt_generator import build_frames, build_plan, compute_schedule_from_frames, compute_schedule_with_plan
    from gui.plan_confirmation import PlanState

    state = PlanState(db)
    plan = build_plan(db, state)
    frames = build_frames(db)
    ratio = db.get_project()["distribution_ratio"]
    with contextlib.redirect_stdout(io.StringIO()):
        if plan is None:
            df = compute_schedule_from_frames(frames, verbose=False, distribution_ratio=ratio)
        else:
            df, _info = compute_schedule_with_plan(frames, plan, verbose=False, distribution_ratio=ratio)
    return df, state


def _with_display(db):
    """GUI（gui/main.py）と同じく、状態を変えたときに今の日程を実績として渡す。"""
    from gui.plan_actions import confirmed_rows_from_result

    def provider(keys):
        df, _ = _compute(db)
        only = None if keys is None else set(keys)
        return confirmed_rows_from_result(db, df, only_keys=only, keep_done_facts=False)
    db.displayed_rows_provider = provider


def _pos(db):
    df, _ = _compute(db)
    return {(int(j[4:]), int(t[2:])): (s.date().isoformat(), e.date().isoformat())
            for j, t, s, e in zip(df["Job_ID"], df["Task_ID"], df["Start_Date"], df["End_Date"])}


def _confirm(db, when="2026-10-01T09:00:00"):
    from gui.plan_actions import confirm_all

    with patch("gui.plan_actions._now", return_value=when):
        confirm_all(db, _compute(db)[0])


def _confirmed(db):
    return {(r["job_id"], r["workflow_task_id"]): (r["start_date"], r["end_date"])
            for r in db.list_confirmed_schedule()}


def _status(db):
    from gui.plan_confirmation import PlanState

    return PlanState(db).status


# -- 実績は休業日・年をまたぐ確定で動かない -----------------------------------------------


@pytest.mark.parametrize("confirm", [True, False])
def test_a_done_task_keeps_its_dates_when_a_holiday_is_added_inside(tmp_path, confirm):
    """回帰テスト: 完了のタスクの期間中に休業日を足すと、終了日が延びていた（確定済みでも
    未確定でも）。完了のタスクは記録した開始日〜終了日のまま。"""
    db, ids = _project(tmp_path)
    if confirm:
        _confirm(db)
    key = (ids["jobs"][0], ids["t"][1])  # J1/実装（4日）
    db.update_job_task_override_fields(*key, status="done")
    before = _pos(db)[key]
    middle = (pd.Timestamp(before[0]) + pd.Timedelta(days=1)).date().isoformat()
    db.add_holiday(middle)
    assert _pos(db)[key] == before


def test_an_actual_start_on_a_holiday_is_kept(tmp_path):
    """実績は実際に作業した日なので、休業日（土曜）に始めた記録も翌稼働日へずらさない。
    進行中なら、その日を1日目として今の日数で終わる。"""
    db, ids = _project(tmp_path)
    key = (ids["jobs"][2], ids["u"][0])  # J3/準備（2日）
    db.update_job_task_override_fields(*key, status="in_progress")
    db.update_task_fact(*key, "2026-10-10")  # 土曜日
    assert _pos(db)[key] == ("2026-10-10", "2026-10-14")  # 土・火の2日（10-12 はスポーツの日）
    db.update_job_task_override_fields(*key, status="done")
    db.update_task_fact(*key, "2026-10-10", "2026-10-11")
    assert _pos(db)[key] == ("2026-10-10", "2026-10-11")


def test_confirming_in_the_next_year_keeps_last_years_holidays(tmp_path):
    """回帰テスト: 確定した日（計算の下限）が翌年だと、祝日を翌年の分からしか作らず、
    前年の祝日にかかる確定済み・完了のタスクの終了日が変わっていた。"""
    db, ids = _project(tmp_path)
    key = (ids["jobs"][2], ids["u"][0])
    db.update_job_task_override_fields(*key, start_pin_date="2026-10-09")  # 10-12 はスポーツの日
    _confirm(db, when="2027-01-05T09:00:00")
    assert _status(db) == "confirmed"
    assert _pos(db)[key] == _confirmed(db)[key] == ("2026-10-09", "2026-10-14")


# -- 実績の記録 ---------------------------------------------------------------------------


def test_a_task_added_after_confirmation_records_its_actual_dates(tmp_path):
    """回帰テスト: 確定後に足したジョブのタスクを進行中にしても記録されず、後の編集
    （配置の変更など）で動いていた。確定行の有無に関係なく実績を記録する。"""
    db, ids = _project(tmp_path)
    _confirm(db)
    job = db.add_job("J4", ids["wf2"], ids["ms"], 1)
    key = (job, ids["u"][0])
    before = _pos(db)[key]
    db.update_job_task_override_fields(*key, status="in_progress")
    assert {(f["job_id"], f["workflow_task_id"]) for f in db.list_task_facts()} == {key}
    db.set_distribution_ratio(0.0)
    assert _pos(db)[key][0] == before[0]


def test_changing_back_to_not_started_removes_the_actual_dates(tmp_path):
    db, ids = _project(tmp_path)
    key = (ids["jobs"][0], ids["t"][0])
    db.update_job_task_override_fields(*key, status="in_progress")
    db.update_job_task_override_fields(*key, status="done")
    assert len(db.list_task_facts()) == 1
    db.update_job_task_override_fields(*key, status=None)
    assert db.list_task_facts() == []


def test_marking_done_keeps_the_start_and_takes_the_end_from_the_current_days(tmp_path):
    """進行中→完了では、記録した開始日を残し、終了日を今の日数で延ばした位置にする。"""
    db, ids = _project(tmp_path)
    key = (ids["jobs"][0], ids["t"][0])
    db.update_job_task_override_fields(*key, status="in_progress")
    start = _pos(db)[key][0]
    db.update_job_task_override_fields(*key, override_days=5)
    extended = _pos(db)[key]
    assert extended[0] == start
    db.update_job_task_override_fields(*key, status="done")
    fact = db.list_task_facts()[0]
    assert (fact["start_date"], fact["end_date"]) == extended


# -- 確定した直後に動かない -------------------------------------------------------------


def test_a_task_placed_before_its_predecessor_stays_where_it_was_confirmed(tmp_path):
    """回帰テスト: 先行タスクの終わりより前にドラッグして確定すると、確定した直後に
    先行の後ろへ飛び、「確定済み」なのに表示が確定と違っていた。置いた位置のまま、
    依存の違反として見せる。"""
    db, ids = _project(tmp_path)
    _confirm(db)
    pred, succ = (ids["jobs"][0], ids["t"][0]), (ids["jobs"][0], ids["t"][1])
    db.set_draft_move(*succ, _pos(db)[pred][0])
    placed = _pos(db)[succ]
    _confirm(db)
    assert _status(db) == "confirmed"
    assert _pos(db)[succ] == placed == _confirmed(db)[succ]
    df, _ = _compute(db)
    row = df[(df["Job_ID"] == f"JOB_{succ[0]:03d}") & (df["Task_ID"] == f"T_{succ[1]:03d}")].iloc[0]
    assert row["Constraint_Violation"] != ""


def test_a_holiday_that_extends_a_confirmed_task_still_pushes_its_successor(tmp_path):
    """先行が確定より後ろへ延びた（休業日を足した）ときは、これまでどおり後続を押す。"""
    db, ids = _project(tmp_path)
    _confirm(db)
    pred, succ = (ids["jobs"][0], ids["t"][1]), (ids["jobs"][0], ids["t"][2])
    before = _pos(db)
    middle = (pd.Timestamp(before[pred][0]) + pd.Timedelta(days=1)).date().isoformat()
    db.add_holiday(middle)
    after = _pos(db)
    assert after[pred][1] > before[pred][1]
    assert after[succ][0] >= after[pred][1]


# -- 変更を破棄 --------------------------------------------------------------------------


def test_discarding_keeps_a_job_whose_tasks_were_confirmed_by_selection(tmp_path):
    """回帰テスト: 確定後に足したジョブのタスクを「選択した変更を確定」してから
    「変更を破棄」すると、ジョブごと消えていた。"""
    from gui.plan_actions import confirm_selected
    from gui.plan_confirmation import successor_map

    db, ids = _project(tmp_path)
    _confirm(db)
    job = db.add_job("J4", ids["wf2"], ids["ms"], 1)
    new = {(job, u) for u in ids["u"]}
    confirm_selected(db, _compute(db)[0], new, successor_map(db))
    positions = {k: v for k, v in _pos(db).items() if k in new}
    db.update_job_task_override_fields(ids["jobs"][0], ids["t"][0], override_days=6)

    db.discard_draft()

    assert "J4" in [j["name"] for j in db.list_jobs()]
    assert _status(db) == "confirmed"
    assert {k: v for k, v in _pos(db).items() if k in new} == positions


def test_discarding_after_confirming_a_workflow_change_for_one_task(tmp_path):
    """回帰テスト: ワークフローの既定の日数を変えて、1つのタスクだけ確定してから破棄
    すると、既定が戻ってそのタスクが確定から変わり、破棄した直後から「変更あり」に
    なっていた。確定したタスクには、確定した日数を上書きとして残す。"""
    from gui.plan_actions import confirm_selected
    from gui.plan_confirmation import successor_map

    db, ids = _project(tmp_path)
    _confirm(db)
    job3 = ids["jobs"][2]
    job4 = db.add_job("J4", ids["wf2"], ids["ms"], 4)
    _confirm(db)
    db.update_workflow_task(ids["u"][0], "準備", ids["team"], 3)
    confirm_selected(db, _compute(db)[0], {(job3, ids["u"][0])}, successor_map(db))
    confirmed = _pos(db)[(job3, ids["u"][0])]

    db.discard_draft()

    assert _status(db) == "confirmed"
    assert _pos(db)[(job3, ids["u"][0])] == confirmed
    overrides = {(o["job_id"], o["workflow_task_id"]): o for o in db.list_all_job_task_overrides()}
    assert overrides[(job3, ids["u"][0])]["override_days"] == 3
    assert (job4, ids["u"][0]) not in overrides
    assert next(t for t in db.list_workflow_tasks(ids["wf2"]) if t["id"] == ids["u"][0])["default_days"] == 2


def test_confirming_a_priority_change_for_one_task_confirms_the_whole_job(tmp_path):
    """優先度はジョブ単位の入力なので、1タスクだけ確定すると同じジョブの他のタスクが
    「変更あり」のまま残り、破棄の後も確定済みに戻らない。その変更だけを受けた同じ
    ジョブのタスクは一緒に確定する。"""
    from gui.plan_actions import confirm_selected
    from gui.plan_confirmation import successor_map

    db, ids = _project(tmp_path)
    _confirm(db)
    job = next(j for j in db.list_jobs() if j["id"] == ids["jobs"][1])
    db.update_job(job["id"], job["name"], job["workflow_id"], job["default_milestone_id"], 5, job["tags"])
    count = confirm_selected(db, _compute(db)[0], {(job["id"], ids["t"][0])}, successor_map(db))
    assert count == 3
    db.discard_draft()
    assert _status(db) == "confirmed"


def test_confirming_after_an_undo_is_kept_by_a_later_discard(tmp_path):
    """回帰テスト: Undo（DBの読み込み直し）の後に確定すると、破棄用のスナップショットに
    確定が入らず、次の「変更を破棄」で確定する前（未確定）に戻っていた。"""
    db, ids = _project(tmp_path)
    blob = db.serialize_state()
    db.set_distribution_ratio(0.3)
    db.restore_state(blob)  # Undo と同じ読み込み直し
    _confirm(db)
    db.set_draft_move(ids["jobs"][0], ids["t"][0], "2026-12-01")
    db.discard_draft()
    assert _status(db) == "confirmed"


def test_a_pin_set_after_moving_a_task_in_a_draft_takes_effect(tmp_path):
    """回帰テスト: 変更案でドラッグしたタスクに開始固定日を設定しても、ドラッグした
    位置のままだった（固定日は保存されて隠れ、未確定に戻すとその日へ飛んだ）。
    後からした操作を優先する。"""
    db, ids = _project(tmp_path)
    _confirm(db)
    key = (ids["jobs"][2], ids["u"][0])
    db.set_draft_move(*key, "2027-02-18")
    db.update_job_task_override_fields(*key, start_pin_date="2027-02-02")
    assert db.list_draft_moves() == []
    assert _pos(db)[key][0] == "2027-02-02"


def test_migrating_a_v18_file_moves_the_recorded_dates_into_task_facts(tmp_path):
    """v18 までは進行中・完了のタスクの日程を確定行に書いていた。開くと実績へ写し、
    確定していないファイルに残っていた確定行（実績だけ）は消す。"""
    from gui.db_schema import migrate

    db, ids = _project(tmp_path)
    key = (ids["jobs"][0], ids["t"][0])
    db.update_job_task_override_fields(*key, status="done")
    conn = db._conn
    conn.execute("DROP TABLE task_facts")
    conn.execute(
        "INSERT INTO confirmed_schedule(job_id, workflow_task_id, start_date, end_date, days, team_id, "
        "input_signature) VALUES (?, ?, '2026-10-05', '2026-10-08', 3, ?, 'x')", (*key, ids["team"]),
    )
    conn.execute("UPDATE schema_meta SET value = '18' WHERE key = 'schema_version'")
    conn.commit()
    migrate(conn)
    assert [(f["job_id"], f["workflow_task_id"], f["start_date"], f["end_date"]) for f in db.list_task_facts()] \
        == [(*key, "2026-10-05", "2026-10-08")]
    assert db.list_confirmed_schedule() == []
