"""
計画の確定と再計画（docs/roadmap.md §8）のテスト。

- 段階1: ジョブの安定キー（jobs.stable_key）と、スキーマ v17 への移行
"""

import shutil
import sys
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
        assert SCHEMA_VERSION == "17"
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
