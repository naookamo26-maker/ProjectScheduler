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
GUIDE_NEW_TITLE = Path(__file__).resolve().parent.parent / "data" / "Guide_Sample_NewTitle.pschedule"


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
        assert SCHEMA_VERSION == "19"
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


def test_discarding_the_draft_restores_everything_including_status_changes(tmp_path):
    """「変更を破棄」で「状態も元に戻す」を選ぶと、最後に確定した時点へすべて戻す
    （タスクの状態の変更も戻す）。"""
    from gui.plan_actions import confirm_all
    from gui.plan_confirmation import CONFIRMED, PlanState

    db, ids = _plan_project(tmp_path)
    df, _, _ = _compute(db)
    confirm_all(db, df)
    j0, j1 = ids["jobs"][:2]
    db.update_job_task_override_fields(j0, ids["t1"], override_days=9, tags="案")
    db.update_job_task_override_fields(j1, ids["t2"], status="in_progress")
    db.add_job("追加ジョブ", db.list_workflows()[0]["id"], None, 3)

    db.discard_draft(restore_statuses=True)

    state = PlanState(db)
    assert state.status == CONFIRMED
    assert db.list_all_job_task_overrides() == []  # 日数・タグも状態も戻る
    assert all(j["name"] != "追加ジョブ" for j in db.list_jobs())


def test_discarding_the_draft_keeps_the_statuses_and_actual_dates_by_default(tmp_path):
    """「変更を破棄」は既定では計画の変更だけを戻し、進行中・完了にした状態と実績は
    残す（実際に起きたことの記録であって、計画の変更ではないため）。破棄で消える
    ジョブの状態は捨てる。"""
    from gui.plan_actions import confirm_all
    from gui.plan_confirmation import CONFIRMED, PlanState

    db, ids = _plan_project(tmp_path)
    _with_display(db)
    confirm_all(db, _compute(db)[0])
    j0, j1 = ids["jobs"][:2]
    db.update_job_task_override_fields(j0, ids["t1"], override_days=9, tags="案")
    db.update_job_task_override_fields(j1, ids["t1"], status="done")
    extra = db.add_job("追加ジョブ", db.list_workflows()[0]["id"], None, 3)
    db.update_job_task_override_fields(extra, ids["t1"], status="in_progress")
    assert db.status_changes_since_base() == 2

    db.discard_draft()

    overrides = {(o["job_id"], o["workflow_task_id"]): o for o in db.list_all_job_task_overrides()}
    assert set(overrides) == {(j1, ids["t1"])}
    assert overrides[(j1, ids["t1"])]["status"] == "done"
    assert overrides[(j1, ids["t1"])]["override_days"] is None
    assert {(f["job_id"], f["workflow_task_id"]) for f in db.list_task_facts()} == {(j1, ids["t1"])}
    assert PlanState(db).status == CONFIRMED  # 確定した日程どおりに完了した
    assert db.status_changes_since_base() == 1


def _with_display(db):
    """GUI（gui/main.py）と同じく、表示中の日程を最後の計算結果から返すようにする。"""
    from gui.plan_actions import confirmed_rows_from_result

    def provider(keys):
        df, _, _ = _compute(db)
        only = None if keys is None else set(keys)
        return confirmed_rows_from_result(db, df, only_keys=only, keep_done_facts=False)
    db.displayed_rows_provider = provider


def test_changing_the_status_does_not_move_a_task_that_the_draft_moved(tmp_path):
    """回帰テスト: 変更案で動いていたタスクを進行中・完了にすると、確定した位置へ
    飛び戻っていた。状態を変えてもバーはその場から動かない。後続の変更案も崩れない。"""
    from gui.plan_actions import confirm_all
    from gui.plan_confirmation import DRAFT

    db, ids = _plan_project(tmp_path)
    _with_display(db)
    confirm_all(db, _compute(db)[0])
    j0 = ids["jobs"][0]
    db.set_draft_move(j0, ids["t1"], "2026-04-20")
    before = _positions(_compute(db)[0])

    db.update_job_task_override_fields(j0, ids["t1"], status="in_progress")
    after_df, state, _info = _compute(db)
    assert _positions(after_df) == before
    assert state.status == DRAFT and (j0, ids["t1"]) in state.changed  # 変更案のまま

    db.update_job_task_override_fields(j0, ids["t1"], status="done")
    assert _positions(_compute(db)[0]) == before


def test_changing_the_status_does_not_move_a_task_pushed_by_its_predecessor(tmp_path):
    """前のタスクの変更に押されて動いていたタスク（開始日は触っていない）も、状態を
    変えたときに確定した位置へ戻らない。"""
    from gui.plan_actions import confirm_all

    db, ids = _plan_project(tmp_path)
    _with_display(db)
    confirm_all(db, _compute(db)[0])
    confirmed = _positions(_compute(db)[0])
    j0 = ids["jobs"][0]
    db.update_job_task_override_fields(j0, ids["t1"], override_days=9)
    before = _positions(_compute(db)[0])
    assert before[_k(j0, ids["t2"])] != confirmed[_k(j0, ids["t2"])]  # 前提: 後続が押されて動いた

    db.update_job_task_override_fields(j0, ids["t2"], status="in_progress")
    assert _positions(_compute(db)[0]) == before


def test_an_in_progress_task_can_be_extended_but_a_done_task_keeps_its_days(tmp_path):
    """進行中のタスクは開始日だけを固定し、日数を延ばせる（後続も合わせて動く）。完了した
    タスクは日数も実績のまま（ワークフロー側の日数を変えても伸び縮みしない）。進行中で
    延ばした日数は、完了にしてもそのまま。"""
    from gui.plan_actions import confirm_all

    db, ids = _plan_project(tmp_path)
    _with_display(db)
    confirm_all(db, _compute(db)[0])
    j0 = ids["jobs"][0]
    db.update_job_task_override_fields(j0, ids["t1"], status="in_progress")
    start, end = _positions(_compute(db)[0])[_k(j0, ids["t1"])]
    db.update_job_task_override_fields(j0, ids["t1"], override_days=8)
    pos = _positions(_compute(db)[0])
    assert pos[_k(j0, ids["t1"])][0] == start
    assert pos[_k(j0, ids["t1"])][1] > end  # 延びた
    assert pos[_k(j0, ids["t2"])][0] >= pos[_k(j0, ids["t1"])][1]  # 後続も押される
    extended = pos[_k(j0, ids["t1"])]

    db.update_job_task_override_fields(j0, ids["t1"], status="done")
    assert _positions(_compute(db)[0])[_k(j0, ids["t1"])] == extended
    db.update_job_task_override_fields(j0, ids["t1"], override_days=3)  # 完了後は変えても動かない
    assert _positions(_compute(db)[0])[_k(j0, ids["t1"])] == extended


def test_status_changes_in_an_unconfirmed_plan_do_not_jump_to_old_dates(tmp_path):
    """回帰テスト: 「未確定に戻す」の後に残った古い確定日程があると、完了にしたときに
    その日付へ戻っていた。未確定の計画でも、状態を変えたときの表示中の日程で固定し、
    未着手に戻したら固定を外す。"""
    from gui.plan_actions import confirm_all

    db, ids = _plan_project(tmp_path)
    _with_display(db)
    confirm_all(db, _compute(db)[0])
    j0 = ids["jobs"][0]
    db.update_job_task_override_fields(j0, ids["t1"], status="in_progress")
    db.clear_confirmation()
    db.update_job_task_override_fields(j0, ids["t1"], status=None)
    db.update_job_task_override_fields(j0, ids["t1"], override_days=9)
    before = _positions(_compute(db)[0])

    db.update_job_task_override_fields(j0, ids["t1"], status="done")
    assert _positions(_compute(db)[0]) == before
    db.update_job_task_override_fields(j0, ids["t1"], status=None)
    assert all((r["job_id"], r["workflow_task_id"]) != (j0, ids["t1"]) for r in db.list_task_facts())


def test_discarding_also_reverts_the_dates_recorded_by_a_status_change(tmp_path):
    """「状態も元に戻す」を選んで破棄すると、状態を変えたときに記録した実績も状態と
    一緒に確定した時点へ戻る。選ばなければ、変更案の位置で始めた実績は残り、確定とは
    違うので「変更あり」のままになる（確定済みと言いながら表示が違う、にならない）。"""
    from gui.plan_actions import confirm_all
    from gui.plan_confirmation import DRAFT, PlanState

    for restore in (True, False):
        db, ids = _plan_project(tmp_path / str(restore))
        _with_display(db)
        confirm_all(db, _compute(db)[0])
        confirmed = _positions(_compute(db)[0])
        j0 = ids["jobs"][0]
        db.set_draft_move(j0, ids["t1"], "2026-04-20")
        db.update_job_task_override_fields(j0, ids["t1"], status="in_progress")
        moved = _positions(_compute(db)[0])[_k(j0, ids["t1"])]

        db.discard_draft(restore_statuses=restore)

        if restore:
            assert db.list_all_job_task_overrides() == []
            assert _positions(_compute(db)[0]) == confirmed
        else:
            state = PlanState(db)
            assert state.status == DRAFT and (j0, ids["t1"]) in state.changed
            assert _positions(_compute(db)[0])[_k(j0, ids["t1"])] == moved
        db.close()


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
    """「未確定に戻す」でも、進行中・完了のタスクは実施した事実なので実績の日程の
    まま固定する。未着手のタスクだけが自由に計算し直される。"""
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
    assert db.list_confirmed_schedule() == []
    assert {(r["job_id"], r["workflow_task_id"]) for r in db.list_task_facts()} == {done, started}

    # 自由に計算すると、全体が最速側へ詰まる（未着手のタスクは動く）
    db.set_distribution_ratio(0.0)
    after_df, state, _ = _compute(db)
    assert state.status == UNCONFIRMED
    after = _positions(after_df)
    assert after[_k(*done)] == before[_k(*done)]
    assert after[_k(*started)] == before[_k(*started)]
    assert any(after[k] != before[k] for k in before if k not in (_k(*done), _k(*started)))

    # 未着手に戻したタスクは、実績を消して自由に置かれる
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


# -- 確定後の変更で、収まらないタスクの扱い（押し下げ） ----------------------------------------


def test_a_lengthened_task_pushes_the_tasks_behind_it_instead_of_jumping_to_a_gap(tmp_path):
    """確定後に日数を延ばしたタスク（とその後続）が、同じチームの確定の位置に固定した
    タスクに阻まれて元の場所に収まらないときは、阻んだタスクのほうを後ろへずらす。

    以前は阻んだタスクを動かさず、日数ぶん連続して空いている最初の隙間へ置いていた
    ため、日数を数日延ばしただけで後続が他のジョブの後ろ（数か月先になることもある）へ
    飛んでいた。"""
    db, ids, before = _confirmed_project(tmp_path)
    j2, j3 = ids["jobs"][1], ids["jobs"][2]
    db.update_job_task_override_fields(j2, ids["t1"], override_days=8)
    df, _state, info = _compute(db)
    after = _positions(df)
    # 延ばしたタスクはその場で延び、後続はその分だけずれる（隙間へ飛ばない）
    assert after[_k(j2, ids["t1"])][0] == before[_k(j2, ids["t1"])][0]
    assert 0 <= (after[_k(j2, ids["t2"])][0] - after[_k(j2, ids["t1"])][1]).days <= 2
    # 後ろに控えていた別のジョブのタスクを押し下げる（確定より前には出さない）
    for task in ("t1", "t2"):
        key = _k(j3, ids[task])
        assert 0 < (after[key][0] - before[key][0]).days <= 7
    for key in before:
        assert after[key][0] >= before[key][0]
    assert (df["Constraint_Violation"] != "").sum() == 0
    assert set(info["pushed"]) == {_k(j3, ids["t1"]), _k(j3, ids["t2"])}
    assert all(origins == {_k(j2, ids["t1"])} for origins in info["pushed"].values())


def test_a_lengthened_task_keeps_its_place_before_a_higher_priority_job(tmp_path):
    """延ばしたタスクがその場に残り、後ろに控えるタスクのほうがずれる——後ろのタスクの
    ジョブのほうが優先度が高くても同じ（延ばしたタスクとその後続を先に置く）。

    詰め直しを優先度の順に行うだけだと、後ろに控えていた優先度の高いタスクが先に
    ラインを取り、延ばしたタスクのほうがその後ろへ回っていた。"""
    from gui.plan_actions import confirm_all

    db, ids = _plan_project(tmp_path, lines=1, jobs=2)
    high, low = ids["jobs"]
    db.set_distribution_ratio(0.0)
    # 優先度の低いジョブを先に置いて確定する（開始固定日で並びを決めて確定し、固定を
    # 外してもう一度確定する）
    db.update_job_task_override_fields(low, ids["t1"], start_pin_date="2026-04-06")
    db.update_job_task_override_fields(high, ids["t1"], start_pin_date="2026-04-20")
    with patch("gui.plan_actions._now", return_value="2026-04-01T09:00:00"):
        confirm_all(db, _compute(db)[0])
        db.update_job_task_override_fields(low, ids["t1"], start_pin_date=None)
        db.update_job_task_override_fields(high, ids["t1"], start_pin_date=None)
        df = _compute(db)[0]
        confirm_all(db, df)
    before = _positions(df)
    assert before[_k(low, ids["t2"])][1] <= before[_k(high, ids["t1"])][0]
    assert _positions(_compute(db)[0]) == before

    db.update_job_task_override_fields(low, ids["t2"], override_days=8)
    after = _positions(_compute(db)[0])
    assert after[_k(low, ids["t2"])][0] == before[_k(low, ids["t2"])][0]
    assert after[_k(high, ids["t1"])][0] >= after[_k(low, ids["t2"])][1]
    assert (after[_k(high, ids["t1"])][0] - before[_k(high, ids["t1"])][0]).days <= 7


def test_lengthening_an_in_progress_task_pushes_the_next_job_on_the_same_line(tmp_path):
    """進行中のタスクの日数を延ばして、同じチームの次の確定済みタスク（別のジョブ）に
    重なったら、その次のタスクを押し下げる。進行中のタスクは実績の開始日で固定する
    ので影響範囲には入らず、以前はライン数の超過のまま残っていた。"""
    db, ids, before = _confirmed_project(tmp_path)
    j1, j4 = ids["jobs"][0], ids["jobs"][3]
    first = _k(j4, ids["t1"])
    assert min(before, key=lambda k: before[k][0]) == first
    db.update_job_task_override_fields(j4, ids["t1"], status="in_progress")
    db.update_job_task_override_fields(j4, ids["t1"], override_days=12)
    df, _state, info = _compute(db)
    after = _positions(df)
    assert after[first][0] == before[first][0]
    assert after[first][1] > before[_k(j1, ids["t1"])][0]
    nxt = _k(j1, ids["t1"])
    assert after[nxt][0] >= after[first][1]
    assert (after[nxt][0] - before[nxt][0]).days <= 7
    for key in before:
        assert after[key][0] >= before[key][0]
    assert (df["Constraint_Violation"] != "").sum() == 0
    assert info["pushed"][nxt] == {first}


def test_tasks_added_after_confirming_do_not_take_the_line_from_pushed_tasks(tmp_path):
    """押し下げのときも、確定していないタスク（確定の後に足したジョブ）はこれまでどおり
    確定済みのタスクの空きに入る。

    押し下げた計算で確定していないタスクを先に置いていたため、優先度の最も低い足した
    ジョブが、押し下げた確定済みのタスクより先にラインを取り、関係の無いジョブ（旅立ち）の
    タスクを40日以上押し出していた（利用者ガイドの6章の場面）。"""
    from gui.plan_actions import confirm_all

    path = tmp_path / "guide.pschedule"
    shutil.copy(GUIDE_NEW_TITLE, path)
    db = ProjectDatabase.open_existing(str(path))
    try:
        with patch("gui.plan_actions._now", return_value="2026-04-01T09:00:00"):
            confirm_all(db, _compute(db)[0])
        jobs = {j["name"]: j for j in db.list_jobs()}
        maou = jobs["魔王"]
        tasks = {t["name"]: t["id"] for t in db.list_workflow_tasks(maou["workflow_id"])}
        master = next(m["id"] for m in db.list_milestones() if m["name"] == "マスターアップ")
        villager = db.add_job("村人", maou["workflow_id"], master, 5, "サブ")
        df = _compute(db)[0]
        before = _positions(df)
        names = dict(zip(df["Job_ID"], df["Job_Name"]))

        db.update_job_task_override_fields(maou["id"], tasks["モーション制作"], override_days=30)
        df, _state, info = _compute(db)
        after = _positions(df)
        motion = _k(maou["id"], tasks["モーション制作"])
        assert after[motion][0] == before[motion][0]
        for key in before:
            if names[key[0]] in ("旅立ち",) or key[0] == f"JOB_{villager:03d}":
                assert after[key] == before[key], key
            assert after[key][0] >= before[key][0]
            assert (after[key][0] - before[key][0]).days <= 14, key
        assert info["pushed"]
    finally:
        db.close()


def test_dragging_a_task_later_pushes_the_next_task_and_starting_it_moves_nothing(tmp_path):
    """確定後にドラッグで後ろへ動かしたタスクが、同じチームの次の確定済みタスクに重なったら、
    次のタスクを押し下げる（日数を延ばしたときと同じ）。その後にドラッグしたタスクを進行中
    にしても、何も動かない。

    以前はドラッグした位置で重なったまま（ライン数の超過）で、進行中にした途端に押し下げが
    始まって、状態を変えただけで別のジョブのタスクが動いていた。"""
    db, ids, before = _confirmed_project(tmp_path)
    _with_display(db)
    j2, j3 = ids["jobs"][1], ids["jobs"][2]
    build, nxt = _k(j2, ids["t2"]), _k(j3, ids["t1"])
    assert before[build][1] <= before[nxt][0]
    db.set_draft_move(j2, ids["t2"], (before[build][0] + pd.Timedelta(days=2)).isoformat())
    df, _state, info = _compute(db)
    dragged = _positions(df)
    assert dragged[build][1] > before[nxt][0]
    assert dragged[nxt][0] >= dragged[build][1]
    assert (df["Constraint_Violation"] != "").sum() == 0
    assert info["pushed"][nxt] == {build}

    db.update_job_task_override_fields(j2, ids["t2"], status="in_progress")
    assert _positions(_compute(db)[0]) == dragged


def test_confirming_the_selected_change_also_confirms_the_tasks_it_pushed(tmp_path):
    """「選択した変更を確定」は、その変更が押し下げた他のジョブのタスクも一緒に確定する。
    残すと、確定した変更がラインを取ったまま押し下げた側が確定の位置へ戻り、ライン数の
    超過になる（確定した直後に表示が変わる）。別の変更とその影響は残す。"""
    from gui.plan_actions import confirm_selected, pushed_tasks
    from gui.plan_confirmation import DRAFT, successor_map

    db, ids, before = _confirmed_project(tmp_path)
    j2, j3, j4 = ids["jobs"][1:]
    db.update_job_task_override_fields(j2, ids["t1"], override_days=8)
    db.update_job_task_override_fields(j4, ids["t2"], override_days=6)
    df, _state, info = _compute(db)
    shown = _positions(df)
    with patch("gui.plan_actions._now", return_value="2026-04-01T09:00:00"):
        count = confirm_selected(db, df, {(j2, ids["t1"])}, successor_map(db), pushed_tasks(info))
    assert count == 4  # J2 の設計・実装と、押し下げた J3 の設計・実装
    df2, state, _info = _compute(db)
    assert _positions(df2) == shown
    assert state.status == DRAFT
    assert state.changed == {(j4, ids["t2"])}
    assert (df2["Constraint_Violation"] != "").sum() == 0


def test_a_task_confirmed_against_its_dependency_stays_when_an_upstream_input_changes(tmp_path):
    """確定した位置そのものが依存に反しているタスク（先行タスクより前へドラッグして確定
    した等）は、先行タスクに位置の変わらない変更（ジョブ間の依存の追加など）があっても
    確定の位置のまま。先行タスクが確定より遅れたときだけ置き直す。

    以前は先行タスクの変更で影響範囲に入ると、依存に合わせて後ろへ押し出されていた
    （「選択した変更を確定」した直後に、確定したタスクが動いていた）。"""
    from gui.plan_actions import confirm_all
    from gui.plan_confirmation import DRAFT

    db = ProjectDatabase.create_new(str(tmp_path / "plan.pschedule"))
    db.set_project("P", "2026-04-06")
    db.set_distribution_ratio(0.0)
    team_a, team_b = db.add_team("チームA", 1), db.add_team("チームB", 1)
    wf = db.add_workflow("WF")
    design_id = db.add_workflow_task(wf, "設計", team_a, 5)
    build_id = db.add_workflow_task(wf, "実装", team_b, 5)
    db.add_task_dependency(wf, design_id, build_id)
    ms = db.add_milestone("リリース", "2026-12-25")
    j1, j2 = db.add_job("ジョブ1", wf, ms, 1), db.add_job("ジョブ2", wf, ms, 2)
    design, build = _k(j2, design_id), _k(j2, build_id)
    with patch("gui.plan_actions._now", return_value="2026-04-01T09:00:00"):
        confirm_all(db, _compute(db)[0])
        before = _positions(_compute(db)[0])
        # 実装を設計の最終日に重ねて（依存に反して）確定する
        db.set_draft_move(j2, build_id, (before[design][1] - pd.Timedelta(days=1)).isoformat())
        df = _compute(db)[0]
        confirm_all(db, df)
    confirmed = _positions(df)
    assert confirmed[build][0] < confirmed[design][1]

    # 設計に、位置の変わらない変更（先に終わっている J1 の設計への依存）を足す
    db.add_external_dependency(j2, design_id, j1, design_id)
    df, state, _info = _compute(db)
    assert state.status == DRAFT
    assert _positions(df) == confirmed

    # 設計が確定より遅れたら、実装は設計の後ろへ置き直す
    db.update_job_task_override_fields(j2, design_id, override_days=7)
    after = _positions(_compute(db)[0])
    assert after[build][0] >= after[design][1]


def test_discarding_after_confirming_part_of_a_priority_change_returns_to_confirmed(tmp_path):
    """ジョブの優先度を変えた後、そのジョブの一部のタスクだけを「選択した変更を確定」
    してから「変更を破棄」すると、確定済みに戻る。

    優先度はジョブ全体で1つなので、一部を確定した時点で確定した側の値になる。一緒に
    確定しなかったタスク（優先度のほかにも変更があるもの）の確定行の指紋が古い優先度の
    ままだったため、破棄した後もそのタスクだけ「変更あり」のままだった。"""
    from gui.plan_actions import confirm_selected, pushed_tasks
    from gui.plan_confirmation import CONFIRMED, DRAFT, successor_map

    db, ids, before = _confirmed_project(tmp_path)
    j1, j3 = ids["jobs"][0], ids["jobs"][2]
    job = next(j for j in db.list_jobs() if j["id"] == j3)
    db.update_job(j3, job["name"], job["workflow_id"], job["default_milestone_id"], 2, job["tags"])
    db.add_external_dependency(j3, ids["t1"], j1, ids["t1"])
    df, state, info = _compute(db)
    assert state.changed == {(j3, ids["t1"]), (j3, ids["t2"])}
    confirm_selected(db, df, {(j3, ids["t2"])}, successor_map(db), pushed_tasks(info))
    state = _compute(db)[1]
    assert state.status == DRAFT and state.changed == {(j3, ids["t1"])}

    db.discard_draft()
    df, state, _info = _compute(db)
    assert state.status == CONFIRMED
    assert state.changed == set()
