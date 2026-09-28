"""
開始固定日（ピン）と分散配置（配置コントロール）の組み合わせのテスト。

固定したタスクは動かす幅が0なので、以前は固定を1つ含むだけでそのジョブのずらし量が
0になり、同じジョブの固定していないタスクがすべて最速（プロジェクト開始日の直後）へ
寄っていた。ずらし量は固定が無いとした場合の日程から決め、固定は置ける範囲の制約と
してだけ扱う（project_scheduler._calc_unpinned_dates）。
"""

import contextlib
import io
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pd = pytest.importorskip("pandas")

from gui.db import ProjectDatabase  # noqa: E402

# 分類: scheduler（スケジューリング結果の比較に pandas が要る）
pytestmark = pytest.mark.scheduler

DATA = Path(__file__).resolve().parent.parent / "data"
SAMPLE = DATA / "Project_Schedule_Sample_GameDev_v22.pschedule"
LARGE_SAMPLE = DATA / "Project_Schedule_Sample_AAA_Large.pschedule"


def _open_copy(tmp_path, source):
    path = tmp_path / source.name
    shutil.copy(source, path)
    return ProjectDatabase.open_existing(str(path))


@pytest.fixture
def sample_db(tmp_path):
    db = _open_copy(tmp_path, SAMPLE)
    yield db
    db.close()


def _schedule(db):
    from gui.gantt_generator import build_frames, compute_schedule_from_frames

    with contextlib.redirect_stdout(io.StringIO()):
        df = compute_schedule_from_frames(
            build_frames(db), verbose=False, distribution_ratio=db.get_project()["distribution_ratio"],
        )
    return {(r.Job_ID, r.Task_ID): (r.Start_Date, r.End_Date) for r in df.itertuples()}


def _pin(db, key, start):
    """ガントの「開始日を固定」と同じく、タスク上書きの開始固定日だけを書き換える。"""
    job_id, task_id = int(key[0][4:]), int(key[1][2:])
    db.update_job_task_override_fields(job_id, task_id, start_pin_date=start.date().isoformat())


def _job_tasks(schedule, job_id):
    return sorted((k for k in schedule if k[0] == job_id), key=lambda k: schedule[k][0])


@pytest.mark.parametrize("job_id", ["JOB_011", "JOB_021", "JOB_036"])
def test_pinning_a_task_where_it_is_moves_nothing(sample_db, job_id):
    """回帰テスト: ジョブの中ほどのタスクを今の開始日のまま固定したら、同じジョブの
    前のタスクがプロジェクト開始日の直後へ寄り、タスク同士が大きく離れた。"""
    before = _schedule(sample_db)
    tasks = _job_tasks(before, job_id)
    middle = tasks[len(tasks) // 2]
    _pin(sample_db, middle, before[middle][0])

    after = _schedule(sample_db)
    moved = {k: (before[k][0].date(), after[k][0].date()) for k in before if before[k] != after[k]}
    assert moved == {}


def test_pinning_a_task_pushed_past_its_deadline_where_it_is_moves_nothing(tmp_path):
    """回帰テスト: リソース不足で締切から逆算した最遅日より後ろに置かれていたタスクを
    その位置のまま固定すると、前工程の最遅開始がかえって緩み、前工程が後ろへ動いた
    （大規模サンプルの JOB_234 で、ロケーションコンセプトが 01/30 → 04/15）。"""
    db = _open_copy(tmp_path, LARGE_SAMPLE)
    try:
        before = _schedule(db)
        key = ("JOB_234", "T_024")
        _pin(db, key, before[key][0])
        after = _schedule(db)
    finally:
        db.close()
    moved = {k: (before[k][0].date(), after[k][0].date()) for k in before if before[k] != after[k]}
    assert moved == {}


def test_pinning_later_pushes_only_the_successors(sample_db):
    """後ろへ固定すると後工程が押されるだけで、前工程は元の位置に留まる。"""
    before = _schedule(sample_db)
    tasks = _job_tasks(before, "JOB_011")
    middle = tasks[len(tasks) // 2]
    _pin(sample_db, middle, before[middle][0] + pd.Timedelta(days=21))

    after = _schedule(sample_db)
    for key in tasks:
        if before[key][1] <= before[middle][0]:  # 固定したタスクより前に終わっていたもの
            assert after[key] == before[key], key
        assert after[key][0] >= before[key][0], key  # どれも前へは動かない


def test_pinning_earlier_does_not_pull_the_rest_of_the_job_to_the_project_start(sample_db):
    """前へ固定しても、固定の手前に収まらない前工程が寄るだけで、ジョブ全体が
    最速側へ詰まることはない（固定より後のタスクは元の位置のまま）。"""
    before = _schedule(sample_db)
    tasks = _job_tasks(before, "JOB_011")
    middle = tasks[len(tasks) // 2]
    new_start = before[middle][0] - pd.Timedelta(days=14)
    _pin(sample_db, middle, new_start)

    after = _schedule(sample_db)
    assert after[middle][0] == new_start
    for key in tasks:
        if before[key][0] > before[middle][0]:  # 固定したタスクより後に始まっていたもの
            assert after[key] == before[key], key
        elif key != middle:
            assert after[key][0] >= before[key][0] - pd.Timedelta(days=14), key
