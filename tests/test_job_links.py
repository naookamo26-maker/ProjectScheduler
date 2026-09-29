"""
スケジューリング結果に添えるジョブ間の依存（project_scheduler._cross_job_links）のテスト。
ガントチャートタブの依存の矢印と「依存のつながり」の並びに使う。
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

# 分類: scheduler（スケジューリング結果を使うので pandas が要る）
pytestmark = pytest.mark.scheduler

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "Project_Schedule_Sample_GameDev_v22.pschedule"


@pytest.fixture
def sample_db(tmp_path):
    path = tmp_path / "sample.pschedule"
    shutil.copy(SAMPLE, path)
    db = ProjectDatabase.open_existing(str(path))
    yield db
    db.close()


def _links(db):
    from gui.gantt_generator import build_frames, compute_schedule_from_frames

    with contextlib.redirect_stdout(io.StringIO()):
        df = compute_schedule_from_frames(build_frames(db), verbose=False,
                                          distribution_ratio=db.get_project()["distribution_ratio"])
    return df.attrs["job_links"]


def test_links_are_the_cross_job_dependencies_the_scheduler_used(sample_db):
    expected = {
        ((f"JOB_{e['depends_on_job_id']:03d}", f"T_{e['depends_on_workflow_task_id']:03d}"),
         (f"JOB_{e['job_id']:03d}", f"T_{e['workflow_task_id']:03d}"))
        for e in sample_db.list_external_dependencies() if e["is_active"]
    }
    links = _links(sample_db)
    assert {(p, s) for p, s, _broken in links} == expected
    assert not any(broken for *_x, broken in links)  # 依存どおりに置かれている


def test_a_pin_before_the_predecessor_ends_marks_the_link_as_broken(sample_db):
    e = sample_db.list_external_dependencies()[0]
    sample_db.update_job_task_override_fields(e["job_id"], e["workflow_task_id"], start_pin_date="2026-09-10")
    broken = [(p, s) for p, s, b in _links(sample_db) if b]
    assert broken == [((f"JOB_{e['depends_on_job_id']:03d}", f"T_{e['depends_on_workflow_task_id']:03d}"),
                       (f"JOB_{e['job_id']:03d}", f"T_{e['workflow_task_id']:03d}"))]


def test_a_dependency_on_a_disabled_task_follows_the_rewired_predecessor(sample_db):
    """無効にしたタスクへの依存は、スケジューラが手前のタスクへ繋ぎ替えたものを返す。"""
    e = sample_db.list_external_dependencies()[0]
    pred_job, pred_task = e["depends_on_job_id"], e["depends_on_workflow_task_id"]
    sample_db.update_job_task_override_fields(pred_job, pred_task, is_active=False)
    links = _links(sample_db)
    succ = (f"JOB_{e['job_id']:03d}", f"T_{e['workflow_task_id']:03d}")
    preds = [p for p, s, _b in links if s == succ]
    assert preds and all(p[0] == f"JOB_{pred_job:03d}" and p[1] != f"T_{pred_task:03d}" for p in preds)


def test_links_are_not_copied_when_the_result_is_sliced(sample_db):
    """回帰テスト: 依存の一覧を list のまま attrs に載せていたため、pandas が行の取り出し・
    絞り込みのたびに deepcopy し、大きな計画（依存558件・16,230行）ではガントの描画1回に
    3分かかっていた。中身は変えないので、コピーを求められても同じものを返す。"""
    import copy
    import pickle

    from project_scheduler import JobLinks

    from gui.gantt_generator import build_frames, compute_schedule_from_frames

    with contextlib.redirect_stdout(io.StringIO()):
        df = compute_schedule_from_frames(build_frames(sample_db), verbose=False,
                                          distribution_ratio=sample_db.get_project()["distribution_ratio"])
    links = df.attrs["job_links"]
    assert isinstance(links, JobLinks) and len(links) > 0
    assert copy.deepcopy(links) is links
    assert df.iloc[:3].attrs["job_links"] is links
    assert df[df["Job_ID"] == df["Job_ID"].iloc[0]].attrs["job_links"] is links
    assert next(df.iterrows())[1].attrs["job_links"] is links
    assert list(pickle.loads(pickle.dumps(links))) == list(links)  # ワーカースレッドとの受け渡し等でも壊れない
