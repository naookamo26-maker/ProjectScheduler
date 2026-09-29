"""
計画の確定と再計画（docs/roadmap.md §8）のテスト。

- 段階1: ジョブの安定キー（jobs.stable_key）と、スキーマ v17 への移行
"""

import shutil
import sys
from unittest.mock import patch
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pd = pytest.importorskip("pandas")

from gui.db import ProjectDatabase  # noqa: E402
from gui.db_schema import SCHEMA_VERSION  # noqa: E402

# 分類: scheduler（スケジューリング結果の比較に pandas が要る）
pytestmark = pytest.mark.scheduler

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "Project_Schedule_Sample_GameDev_v22.pschedule"


def _schedule(frames, **kwargs):
    import contextlib
    import io

    from gui.gantt_generator import compute_schedule_from_frames

    with contextlib.redirect_stdout(io.StringIO()):
        df = compute_schedule_from_frames(frames, verbose=False, **kwargs)
    return df.sort_values(["Job_ID", "Task_ID"]).reset_index(drop=True)


def test_migrating_to_v17_keeps_every_scheduled_date(tmp_path):
    """旧形式（v16）のファイルを開くと v17 に移行され、既存ジョブの安定キーには
    今の内部IDの文字列が入る。スケジューラはこれまで Job_ID をばらつきの種に
    していたので、移行しただけでは1件も日程が動かない。"""
    from gui.gantt_generator import build_frames

    path = tmp_path / "sample.pschedule"
    shutil.copy(SAMPLE, path)
    db = ProjectDatabase.open_existing(str(path))
    try:
        assert SCHEMA_VERSION == "18"
        jobs = db.list_jobs()
        assert all(j["stable_key"] == f"JOB_{j['id']:03d}" for j in jobs)

        frames = build_frames(db)
        migrated = _schedule(frames)
        # 移行前と同じ条件（安定キー無し＝Job_ID を種にする）で計算した結果と一致する
        legacy_frames = dict(frames, jobs=frames["jobs"].drop(columns=["Jitter_Key"]))
        legacy = _schedule(legacy_frames)
        pd.testing.assert_frame_equal(
            migrated[["Job_ID", "Task_ID", "Start_Date", "End_Date"]],
            legacy[["Job_ID", "Task_ID", "Start_Date", "End_Date"]],
        )
    finally:
        db.close()


def test_new_and_duplicated_jobs_get_their_own_stable_keys(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    team = db.add_team("チームA", 1)
    wf = db.add_workflow("WF")
    db.add_workflow_task(wf, "作業", team, 3)
    job = db.add_job("ジョブ1", wf, None)
    copy = db.duplicate_job(job)
    keys = {j["id"]: j["stable_key"] for j in db.list_jobs()}
    # 作成時の内部IDの文字列で固定する（同じ操作なら同じ配置になるよう、ランダムにしない）
    assert keys[job] == f"JOB_{job:03d}"
    assert keys[copy] == f"JOB_{copy:03d}"


def test_the_stable_key_not_the_internal_id_decides_the_placement(tmp_path):
    """同じ安定キーなら、内部ID（Job_ID）が変わっても配置は変わらない。"""
    from gui.gantt_generator import build_frames

    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    db.set_project("P", "2026-04-06")
    team = db.add_team("チームA", 3)
    wf = db.add_workflow("WF")
    t1 = db.add_workflow_task(wf, "設計", team, 5)
    t2 = db.add_workflow_task(wf, "実装", team, 5)
    db.add_task_dependency(wf, t1, t2)
    ms = db.add_milestone("リリース", "2026-09-30")
    for i in range(3):
        db.add_job(f"ジョブ{i}", wf, ms, 1)
    frames = build_frames(db)
    base = _schedule(frames, distribution_ratio=0.5)

    renamed_jobs = frames["jobs"].copy()
    renamed_jobs["Job_ID"] = renamed_jobs["Job_ID"].str.replace("JOB_", "JOB_9")
    renamed = dict(frames, jobs=renamed_jobs)
    moved = _schedule(renamed, distribution_ratio=0.5)
    moved["Job_ID"] = moved["Job_ID"].str.replace("JOB_9", "JOB_")
    moved = moved.sort_values(["Job_ID", "Task_ID"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(
        base[["Job_ID", "Task_ID", "Start_Date"]], moved[["Job_ID", "Task_ID", "Start_Date"]]
    )


def test_replan_base_date_becomes_the_lower_bound_passed_to_the_scheduler(tmp_path):
    from gui.gantt_generator import build_frames

    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    db.set_project("P", "2026-04-06")
    assert build_frames(db)["project"].iloc[0]["Start_Date"] == "2026-04-06"
    db._conn.execute("UPDATE project SET replan_base_date = '2026-06-01' WHERE id = 1")
    assert build_frames(db)["project"].iloc[0]["Start_Date"] == "2026-06-01"
    # 開発開始日より前の基準日は無視する（開始日より前には置かない）
    db._conn.execute("UPDATE project SET replan_base_date = '2026-01-01' WHERE id = 1")
    assert build_frames(db)["project"].iloc[0]["Start_Date"] == "2026-04-06"


# -- 段階2: 確定と変更案（中核） -------------------------------------------------------


def _plan_project(tmp_path, lines=2, jobs=3):
    """チームA（lines本）に 設計→実装 のジョブを jobs 件。開発開始は2026-04-06。"""
    db = ProjectDatabase.create_new(str(tmp_path / "plan.pschedule"))
    db.set_project("P", "2026-04-06")
    team = db.add_team("チームA", lines)
    wf = db.add_workflow("WF")
    t1 = db.add_workflow_task(wf, "設計", team, 5)
    t2 = db.add_workflow_task(wf, "実装", team, 5)
    db.add_task_dependency(wf, t1, t2)
    ms = db.add_milestone("リリース", "2026-12-25")
    job_ids = [db.add_job(f"ジョブ{i}", wf, ms, i + 1) for i in range(jobs)]
    return db, {"team": team, "t1": t1, "t2": t2, "jobs": job_ids}


def _compute(db):
    """ScheduleCache と同じ手順で、確定を踏まえて計算する。"""
    import contextlib
    import io

    from gui.gantt_generator import build_frames, build_plan, compute_schedule_with_plan
    from gui.plan_confirmation import PlanState

    state = PlanState(db)
    plan = build_plan(db, state)
    frames = build_frames(db)
    ratio = db.get_project()["distribution_ratio"]
    with contextlib.redirect_stdout(io.StringIO()):
        if plan is None:
            df = _schedule(frames, distribution_ratio=ratio)
            info = None
        else:
            df, info = compute_schedule_with_plan(frames, plan, verbose=False, distribution_ratio=ratio)
    df = df.sort_values(["Job_ID", "Task_ID"]).reset_index(drop=True)
    return df, state, info


def _positions(df):
    return {(j, t): (s.date(), e.date()) for j, t, s, e in
            zip(df["Job_ID"], df["Task_ID"], df["Start_Date"], df["End_Date"])}


def _k(job_id, task_id):
    return (f"JOB_{job_id:03d}", f"T_{task_id:03d}")


def test_recomputing_right_after_confirming_reproduces_the_confirmed_dates(tmp_path):
    """§8-6 の自己検査: 確定した直後に計算し直すと、確定とタスク単位で一致する。"""
    from gui.plan_actions import confirm_all
    from gui.plan_confirmation import CONFIRMED, UNCONFIRMED

    db, ids = _plan_project(tmp_path)
    df, state, _ = _compute(db)
    assert state.status == UNCONFIRMED
    confirm_all(db, df)
    again, state, info = _compute(db)
    assert state.status == CONFIRMED
    assert _positions(again) == _positions(df)
    assert info["released"] == set() and info["runs"] == 1


def test_editing_a_task_moves_only_it_and_its_successors(tmp_path):
    from gui.plan_actions import confirm_all
    from gui.plan_confirmation import DRAFT

    db, ids = _plan_project(tmp_path, lines=1)
    df, _, _ = _compute(db)
    confirm_all(db, df)
    before = _positions(df)
    j0 = ids["jobs"][0]
    db.update_job_task_override_fields(j0, ids["t1"], override_days=9)

    after_df, state, info = _compute(db)
    after = _positions(after_df)
    assert state.status == DRAFT
    assert state.changed == {(j0, ids["t1"])}
    assert info["released"] == {_k(j0, ids["t1"]), _k(j0, ids["t2"])}
    for key, pos in before.items():
        if key[0] != _k(j0, 0)[0]:
            assert after[key] == pos, f"{key} は無関係なのに動いた"
    assert after[_k(j0, ids["t1"])] != before[_k(j0, ids["t1"])]


def test_started_tasks_stay_where_they_were_confirmed(tmp_path):
    from gui.plan_actions import confirm_all

    db, ids = _plan_project(tmp_path)
    df, _, _ = _compute(db)
    confirm_all(db, df)
    j0 = ids["jobs"][0]
    db.update_job_task_override_fields(j0, ids["t1"], status="done")
    confirm_all(db, _compute(db)[0])
    before = _positions(_compute(db)[0])
    # 完了したタスクの日数をワークフロー側で変えても、確定の位置・日数のまま
    db.update_job_task_override_fields(j0, ids["t1"], override_days=12)
    after_df, state, info = _compute(db)
    assert (j0, ids["t1"]) in state.changed
    assert _k(j0, ids["t1"]) not in info["released"]
    assert _positions(after_df)[_k(j0, ids["t1"])] == before[_k(j0, ids["t1"])]


def test_a_global_change_is_counted_but_moves_nothing(tmp_path):
    from gui.plan_actions import confirm_all
    from gui.plan_confirmation import DRAFT

    db, ids = _plan_project(tmp_path)
    df, _, _ = _compute(db)
    confirm_all(db, df)
    db.set_distribution_ratio(0.2)
    after_df, state, info = _compute(db)
    assert state.status == DRAFT and state.global_changed and state.changed == set()
    assert _positions(after_df) == _positions(df)


def test_changing_a_job_priority_changes_only_that_job(tmp_path):
    """ジョブの優先度はジョブ単位の入力なので、全体設定の変更には数えない
    （ジョブを足しただけで「全体設定の変更」と出ないように）。"""
    from gui.plan_actions import confirm_all

    db, ids = _plan_project(tmp_path)
    df, _, _ = _compute(db)
    confirm_all(db, df)
    job = next(j for j in db.list_jobs() if j["id"] == ids["jobs"][2])
    db.update_job(job["id"], job["name"], job["workflow_id"], job["default_milestone_id"], 1, "")
    db.add_job("追加", job["workflow_id"], job["default_milestone_id"], 4)
    state = _compute(db)[1]
    assert not state.global_changed
    assert {(ids["jobs"][2], ids["t1"]), (ids["jobs"][2], ids["t2"])} <= state.changed
    assert all(k[0] != ids["jobs"][0] for k in state.changed)


def test_released_tasks_never_move_before_their_confirmed_start(tmp_path):
    """影響範囲のタスクは、確定していた位置より前には動かない（前倒しはドラッグか
    全面再計画で明示的に行う）。日数を減らしても後続は前へ詰めない。"""
    from gui.plan_actions import confirm_all

    db, ids = _plan_project(tmp_path, lines=1, jobs=3)
    db.set_distribution_ratio(0.0)  # 最速に詰める配置（縮めた分だけ前へ詰めたくなる）
    df, _, _ = _compute(db)
    # 確定した日（影響範囲の下限）を開発開始より前にして、下限に邪魔されずに
    # 前へ詰められる状況を作る
    with patch("gui.plan_actions._now", return_value="2026-04-01T09:00:00"):
        confirm_all(db, df)
    before = _positions(df)
    for job_id in ids["jobs"]:
        db.update_job_task_override_fields(job_id, ids["t1"], override_days=2)
    after_df, _state, info = _compute(db)
    after = _positions(after_df)
    assert info["released"]
    for key in info["released"]:
        assert after[key][0] >= before[key][0]


def test_reducing_lines_releases_the_conflicting_tasks_and_recomputes(tmp_path):
    """ライン数を減らすと固定同士がぶつかる。ぶつかった未着手のタスクとその後続を
    影響範囲に加えて計算し直し、違反が残らない。"""
    from gui.plan_actions import confirm_all

    db, ids = _plan_project(tmp_path, lines=3, jobs=3)
    df, _, _ = _compute(db)
    confirm_all(db, df)
    team = db.list_teams()[0]
    db.update_team(team["id"], team["name"], 1)
    after_df, state, info = _compute(db)
    assert state.global_changed
    assert info["runs"] >= 2
    assert info["released"]
    assert (after_df["Constraint_Violation"] == "").all()


def test_confirming_only_the_selected_change_keeps_the_other_as_a_draft(tmp_path):
    from gui.plan_actions import confirm_all, confirm_selected
    from gui.plan_confirmation import PlanState, successor_map

    db, ids = _plan_project(tmp_path)
    df, _, _ = _compute(db)
    confirm_all(db, df)
    j0, j1 = ids["jobs"][:2]
    db.update_job_task_override_fields(j0, ids["t1"], override_days=8)
    db.update_job_task_override_fields(j1, ids["t1"], override_days=7)
    draft_df, _, _ = _compute(db)

    count = confirm_selected(db, draft_df, {(j0, ids["t1"])}, successor_map(db))
    assert count == 2  # 選んだタスクと、影響で動いた後続（実装）
    state = PlanState(db)
    assert state.changed == {(j1, ids["t1"])}
    confirmed = {(r["job_id"], r["workflow_task_id"]): r for r in db.list_confirmed_schedule()}
    new_pos = _positions(draft_df)[_k(j0, ids["t2"])]
    assert confirmed[(j0, ids["t2"])]["start_date"] == new_pos[0].isoformat()


def test_draft_moves_move_the_task_without_a_manual_pin(tmp_path):
    from gui.plan_actions import confirm_all

    db, ids = _plan_project(tmp_path)
    df, _, _ = _compute(db)
    confirm_all(db, df)
    j0 = ids["jobs"][0]
    db.set_draft_move(j0, ids["t1"], "2026-06-01")
    after_df, state, _ = _compute(db)
    assert (j0, ids["t1"]) in state.changed
    assert _positions(after_df)[_k(j0, ids["t1"])][0] == pd.Timestamp("2026-06-01").date()
    assert all(o["start_pin_date"] is None for o in db.list_all_job_task_overrides())
    # 確定すると確定行に書き込まれ、変更案の記録は消える
    confirm_all(db, after_df)
    assert db.list_draft_moves() == []


def test_discarding_the_draft_restores_inputs_but_keeps_progress_updates(tmp_path):
    from gui.plan_actions import confirm_all
    from gui.plan_confirmation import CONFIRMED, PlanState

    db, ids = _plan_project(tmp_path)
    df, _, _ = _compute(db)
    confirm_all(db, df)
    j0, j1 = ids["jobs"][:2]
    db.update_job_task_override_fields(j0, ids["t1"], override_days=9, tags="案")
    db.update_job_task_override_fields(j1, ids["t2"], status="in_progress")
    db.add_job("追加ジョブ", db.list_workflows()[0]["id"], None, 3)

    db.discard_draft()

    state = PlanState(db)
    assert state.status == CONFIRMED
    rows = {(o["job_id"], o["workflow_task_id"]): o for o in db.list_all_job_task_overrides()}
    assert (j0, ids["t1"]) not in rows  # 日数・タグの変更は戻る
    assert rows[(j1, ids["t2"])]["status"] == "in_progress"  # 進捗は残る
    assert all(j["name"] != "追加ジョブ" for j in db.list_jobs())


def test_discarding_after_confirming_a_selection_reverts_later_edits_of_that_task(tmp_path):
    """「選択した変更を確定」したタスクをさらに編集してから破棄すると、確定した
    時点の入力に戻る。以前は破棄の時点の入力（後の編集）がそのまま残っていた。"""
    from gui.plan_actions import confirm_all, confirm_selected
    from gui.plan_confirmation import CONFIRMED, PlanState, successor_map

    db, ids = _plan_project(tmp_path)
    team_b = db.add_team("チームB", 1)
    df, _, _ = _compute(db)
    confirm_all(db, df)
    j0 = ids["jobs"][0]
    db.update_job_task_override_fields(j0, ids["t1"], team_id=team_b)
    df, _, _ = _compute(db)
    confirm_selected(db, df, {(j0, ids["t1"])}, successor_map(db))
    team_c = db.add_team("チームC", 1)
    db.update_job_task_override_fields(j0, ids["t1"], team_id=team_c, override_days=9)

    db.discard_draft()

    rows = {(o["job_id"], o["workflow_task_id"]): o for o in db.list_all_job_task_overrides()}
    assert rows[(j0, ids["t1"])]["override_team_id"] == team_b  # 確定したチームに戻る
    assert rows[(j0, ids["t1"])]["override_days"] is None
    assert all(t["name"] != "チームC" for t in db.list_teams())
    assert PlanState(db).status == CONFIRMED


def test_clearing_the_confirmation_returns_to_free_simulation(tmp_path):
    from gui.plan_actions import confirm_all
    from gui.plan_confirmation import UNCONFIRMED, PlanState

    db, ids = _plan_project(tmp_path)
    confirm_all(db, _compute(db)[0])
    db.clear_confirmation()
    assert PlanState(db).status == UNCONFIRMED
    assert not db.has_draft_base()


def test_clearing_the_confirmation_keeps_started_and_done_tasks_where_they_were(tmp_path):
    """「未確定に戻す」でも、進行中・完了のタスクは実施した事実なので確定していた
    日程のまま固定する。未着手のタスクだけが自由に計算し直される。"""
    from gui.plan_actions import confirm_all
    from gui.plan_confirmation import UNCONFIRMED, PlanState

    db, ids = _plan_project(tmp_path, lines=1, jobs=3)
    df = _compute(db)[0]
    confirm_all(db, df)
    before = _positions(df)
    done = (ids["jobs"][2], ids["t1"])
    started = (ids["jobs"][1], ids["t1"])
    db.update_job_task_override_fields(*done, status="done")
    db.update_job_task_override_fields(*started, status="in_progress")
    db.clear_confirmation()
    assert not db.has_confirmation()
    assert {(r["job_id"], r["workflow_task_id"]) for r in db.list_confirmed_schedule()} == {done, started}

    # 自由に計算すると、全体が最速側へ詰まる（未着手のタスクは動く）
    db.set_distribution_ratio(0.0)
    after_df, state, _ = _compute(db)
    assert state.status == UNCONFIRMED
    after = _positions(after_df)
    assert after[_k(*done)] == before[_k(*done)]
    assert after[_k(*started)] == before[_k(*started)]
    assert any(after[k] != before[k] for k in before if k not in (_k(*done), _k(*started)))

    # 未着手に戻したタスクは、残っていた確定行を使わずに自由に置かれる
    db.update_job_task_override_fields(*done, status=None)
    assert set(PlanState(db).facts) == {started}


# -- 全面再計画（§8-6） --------------------------------------------------------------


def _confirmed_project(tmp_path, jobs=4):
    """確定日を開発開始より前（2026-04-01）にして確定したプロジェクト。"""
    from gui.plan_actions import confirm_all

    db, ids = _plan_project(tmp_path, lines=1, jobs=jobs)
    df = _compute(db)[0]
    with patch("gui.plan_actions._now", return_value="2026-04-01T09:00:00"):
        confirm_all(db, df)
    return db, ids, _positions(df)


def test_full_replan_places_every_unstarted_task_on_or_after_the_base_date(tmp_path):
    from gui.plan_confirmation import DRAFT

    db, ids, before = _confirmed_project(tmp_path)
    done = (ids["jobs"][0], ids["t1"])
    db.update_job_task_override_fields(*done, status="done")
    db.start_full_replan("2026-06-01", "2026-06-01")
    df, state, _ = _compute(db)
    assert state.status == DRAFT
    after = _positions(df)
    assert after[_k(*done)] == before[_k(*done)]
    for key, (start, _end) in after.items():
        if key != _k(*done):
            assert start >= pd.Timestamp("2026-06-01").date()
    # まだ確定は書き換わらない
    assert db.get_project()["replan_base_date"] is None


def test_full_replan_with_a_future_base_date_keeps_tasks_starting_before_it(tmp_path):
    """基準日 D が未来のとき、今日 T〜D の前日に始まる予定の未着手タスクは今の確定の
    まま残し、それ以外（遅れているもの・D 以降のもの）を D 以降に置き直す。

    ただし残すはずのタスクでも、先行タスクが遅れていて D 以降へ置き直されると
    予定どおりには始められない（依存の違反として影響範囲に加わる）。ここでは先行
    タスクを持たない「設計」だけで、残ることを確かめる。"""
    db, ids, before = _confirmed_project(tmp_path)
    design = f"T_{ids['t1']:03d}"
    design_starts = sorted(s for (_j, t), (s, _e) in before.items() if t == design)
    executed, base = design_starts[1].isoformat(), design_starts[2].isoformat()
    db.start_full_replan(base, executed)
    df, state, info = _compute(db)
    after = _positions(df)
    kept = [key for key, (start, _e) in before.items()
            if executed <= start.isoformat() < base and key[1] == design]
    assert kept
    for key in kept:
        assert after[key] == before[key]
    for key, (start, _e) in before.items():
        if not executed <= start.isoformat() < base:
            assert after[key][0].isoformat() >= base
    assert (df["Constraint_Violation"] != "").sum() == 0


def test_confirming_a_full_replan_adopts_the_base_date(tmp_path):
    from gui.plan_actions import confirm_all
    from gui.plan_confirmation import CONFIRMED, PlanState

    db, ids, _before = _confirmed_project(tmp_path)
    db.start_full_replan("2026-06-01", "2026-05-25")
    df = _compute(db)[0]
    confirm_all(db, df)
    project = db.get_project()
    assert (project["replan_base_date"], project["replanned_at"]) == ("2026-06-01", "2026-05-25")
    assert project["pending_replan_base_date"] is None
    # 確定した直後に計算し直すと、確定とタスク単位で一致する
    again, state, _ = _compute(db)
    assert state.status == CONFIRMED
    assert _positions(again) == _positions(df)
    assert PlanState(db).quiet_before == "2026-05-25"


def test_discarding_a_full_replan_returns_to_the_previous_confirmation(tmp_path):
    from gui.plan_confirmation import CONFIRMED

    db, ids, before = _confirmed_project(tmp_path)
    db.start_full_replan("2026-06-01", "2026-06-01")
    db.discard_draft()
    df, state, _ = _compute(db)
    assert state.status == CONFIRMED
    assert db.get_project()["pending_replan_base_date"] is None
    assert _positions(df) == before


def test_violations_before_the_replan_day_are_not_reported():
    from gui.gantt_generator import _quiet_past_violations

    df = pd.DataFrame({
        "Start_Date": pd.to_datetime(["2026-05-01", "2026-06-10"]),
        "Constraint_Violation": ["ライン数の超過", "ライン数の超過"],
        "Constraint_Violation_Days": [2, 3],
    })
    quiet = _quiet_past_violations(df, "2026-06-01")
    assert list(quiet["Constraint_Violation"]) == ["", "ライン数の超過"]
    assert list(quiet["Constraint_Violation_Days"]) == [0, 3]
    assert list(df["Constraint_Violation"]) == ["ライン数の超過", "ライン数の超過"]


# -- HTML出力（§8-9） ----------------------------------------------------------------


def _generate(db, path, **kwargs):
    import contextlib
    import io

    from gui.gantt_generator import generate_gantt

    with contextlib.redirect_stdout(io.StringIO()):
        df = generate_gantt(db, plotly_output_path=str(path), verbose=False, **kwargs)
    title = path.read_text(encoding="utf-8").split("<title>")[1].split("</title>")[0]
    return df, title


def test_html_output_of_an_unconfirmed_plan_has_no_note(tmp_path):
    db, _ids = _plan_project(tmp_path)
    _df, title = _generate(db, tmp_path / "a.html")
    assert title == "P スケジュール"


def test_html_output_follows_the_confirmation(tmp_path):
    db, ids, before = _confirmed_project(tmp_path)
    df, title = _generate(db, tmp_path / "a.html")
    assert title == "P スケジュール（確定した日程 2026-04-01）"
    assert _positions(df) == before


def test_html_output_during_a_draft_can_choose_the_confirmed_or_the_draft_schedule(tmp_path):
    from gui.gantt_generator import PLAN_OUTPUT_CONFIRMED, PLAN_OUTPUT_DRAFT

    db, ids, before = _confirmed_project(tmp_path)
    job = next(j for j in db.list_jobs() if j["id"] == ids["jobs"][0])
    new_job = db.add_job("追加", job["workflow_id"], job["default_milestone_id"], 9)
    db.update_job_task_override_fields(ids["jobs"][0], ids["t1"], override_days=9)

    confirmed_df, title = _generate(db, tmp_path / "c.html", plan_output=PLAN_OUTPUT_CONFIRMED)
    assert title == "P スケジュール（確定した日程 2026-04-01）"
    # 確定後に足したジョブは含めず、確定した日程のまま出す
    assert f"JOB_{new_job:03d}" not in set(confirmed_df["Job_ID"])
    assert _positions(confirmed_df) == before

    draft_df, title = _generate(db, tmp_path / "d.html", plan_output=PLAN_OUTPUT_DRAFT)
    assert title == "P スケジュール（変更案・未確定）"
    assert _positions(draft_df) == _positions(_compute(db)[0])
    assert f"JOB_{new_job:03d}" in set(draft_df["Job_ID"])
