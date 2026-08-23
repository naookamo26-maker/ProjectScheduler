"""
GUI(gui/db.py, gui/gantt_generator.py) と project_scheduler.py の連携を
end-to-endで検証するスモークテスト。PySide6のQGraphicsシーン等、Qtの
イベントループが必要なウィジェット層は対象外（gui/db.py・gui/gantt_generator.py
はいずれもQt非依存であり、Qtを一切importせずに検証できる）。ウィジェット層の
動作確認は開発時にheadless Qt（QT_QPA_PLATFORM=offscreen）で個別に実施済み。

実行方法:
    pip install pytest
    pytest tests/test_gui_gantt_smoke.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.db import (  # noqa: E402
    DuplicateNameError,
    ProjectDatabase,
    ProjectDatabaseError,
    ReferencedEntityError,
)
from gui.gantt_generator import generate_gantt, validate_for_generation  # noqa: E402
from project_scheduler import SchedulingError  # noqa: E402

SAMPLE_DB = Path(__file__).resolve().parent.parent / "data" / "Project_Schedule_Sample_GameDev_v22.pschedule"


# -- ProjectDatabase の基本CRUD・永続化 --------------------------------------------

def test_create_and_reopen_preserves_data(tmp_path):
    path = tmp_path / "project.pschedule"
    db = ProjectDatabase.create_new(str(path))
    db.set_project("テストプロジェクト", "2026-01-01")
    team_id = db.add_team("チームA", 2)
    db.save()  # 明示的に保存するまでファイルには書き込まれない
    db.close()

    db2 = ProjectDatabase.open_existing(str(path))
    assert db2.get_project() == {"project_name": "テストプロジェクト", "start_date": "2026-01-01"}
    assert db2.list_teams() == [{"id": team_id, "name": "チームA", "max_lines": 2}]
    db2.close()


def test_changes_are_not_written_until_save(tmp_path):
    """明示的にsave()するまで、ディスク上のファイルには変更が反映されないこと。"""
    path = tmp_path / "project.pschedule"
    db = ProjectDatabase.create_new(str(path))
    assert db.is_dirty()
    db.save()
    assert not db.is_dirty()
    assert path.exists()

    db.add_team("チームA", 1)
    assert db.is_dirty()

    db2 = ProjectDatabase.open_existing(str(path))
    assert db2.list_teams() == []  # まだ保存していないので反映されない
    db2.close()

    db.save()
    assert not db.is_dirty()
    db3 = ProjectDatabase.open_existing(str(path))
    assert len(db3.list_teams()) == 1
    db3.close()
    db.close()


def test_save_as_writes_to_new_path_and_updates_path(tmp_path):
    original = tmp_path / "original.pschedule"
    renamed = tmp_path / "renamed.pschedule"
    db = ProjectDatabase.create_new(str(original))
    db.add_team("チームA", 1)
    db.save_as(str(renamed))

    assert db.path == str(renamed)
    assert renamed.exists()
    assert not original.exists()
    assert not db.is_dirty()
    db.close()

    reopened = ProjectDatabase.open_existing(str(renamed))
    assert len(reopened.list_teams()) == 1
    reopened.close()


def test_duplicate_names_are_rejected(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    db.add_team("チームA", 1)
    with pytest.raises(DuplicateNameError):
        db.add_team("チームA", 2)
    db.close()


def test_referenced_team_cannot_be_deleted(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    wf_id = db.add_workflow("WF1")
    db.add_workflow_task(wf_id, "タスク1", team_id, 3)
    with pytest.raises(ReferencedEntityError):
        db.delete_team(team_id)
    db.close()


def test_job_task_override_is_diff_only(tmp_path):
    """既定値のままなら上書き行を作らず、既定に戻すと上書き行が消えることを確認する。"""
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    wf_id = db.add_workflow("WF1")
    task_id = db.add_workflow_task(wf_id, "タスク1", team_id, 3)
    job_id = db.add_job("ジョブ1", wf_id, None, 100)

    rows = db.list_job_tasks_with_overrides(job_id)
    assert len(rows) == 1
    assert rows[0]["override_id"] is None  # 既定のまま＝上書き行なし

    db.upsert_job_task_override(job_id, task_id, is_active=False)
    rows = db.list_job_tasks_with_overrides(job_id)
    assert rows[0]["override_id"] is not None
    assert rows[0]["is_active"] == 0

    db.clear_job_task_override(job_id, task_id)
    rows = db.list_job_tasks_with_overrides(job_id)
    assert rows[0]["override_id"] is None
    db.close()


# -- workflow_dependency_templates: 編集・循環防止 -----------------------------------

def _build_three_workflows(db):
    team_id = db.add_team("チームA", 1)
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    wf3 = db.add_workflow("WF3")
    t1 = db.add_workflow_task(wf1, "A", team_id, 1)
    t2 = db.add_workflow_task(wf2, "B", team_id, 1)
    t3 = db.add_workflow_task(wf3, "C", team_id, 1)
    return (wf1, t1), (wf2, t2), (wf3, t3)


def test_dependency_template_can_be_updated_in_place(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    (wf1, t1), (wf2, t2), (wf3, t3) = _build_three_workflows(db)

    tpl_id = db.add_dependency_template(wf1, t1, wf2, t2)
    db.update_dependency_template(tpl_id, t1, wf3, t3)

    templates = db.list_dependency_templates(wf1)
    assert len(templates) == 1
    assert templates[0]["depends_on_workflow_id"] == wf3
    db.close()


def test_dependency_template_rejects_direct_and_transitive_cycles(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    (wf1, t1), (wf2, t2), (wf3, t3) = _build_three_workflows(db)

    db.add_dependency_template(wf1, t1, wf2, t2)  # WF1 depends on WF2
    tpl2 = db.add_dependency_template(wf2, t2, wf3, t3)  # WF2 depends on WF3

    with pytest.raises(ProjectDatabaseError):
        db.add_dependency_template(wf3, t3, wf1, t1)  # WF3 -> WF1 closes the loop

    with pytest.raises(ProjectDatabaseError):
        db.update_dependency_template(tpl2, t2, wf1, t1)  # editing into a cycle

    db.close()


# -- gantt_generator: DB -> DataFrame -> project_scheduler.py -----------------------

def _build_minimal_project(db_path):
    """チーム1・マイルストーン1・依存ありタスク2つのワークフロー1・ジョブ1、
    という最小構成のプロジェクトを組み立てる。"""
    db = ProjectDatabase.create_new(str(db_path))
    db.set_project("スモークテストプロジェクト", "2026-01-01")
    team_id = db.add_team("チームA", 2)
    ms_id = db.add_milestone("マイルストーン1", "2026-03-01")
    wf_id = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf_id, "デザイン", team_id, 3)
    t2 = db.add_workflow_task(wf_id, "実装", team_id, 5)
    db.add_task_dependency(wf_id, t1, t2)
    db.add_job("ジョブ1", wf_id, ms_id, 1)
    return db


def test_validate_for_generation_blocks_empty_project(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    errors = validate_for_generation(db)
    assert errors  # 未完成なプロジェクトはエラーが1件以上ある
    db.close()


def test_validate_for_generation_passes_minimal_project(tmp_path):
    db = _build_minimal_project(tmp_path / "project.pschedule")
    assert validate_for_generation(db) == []
    db.close()


def test_generate_gantt_minimal_project(tmp_path):
    db = _build_minimal_project(tmp_path / "project.pschedule")
    md_path = tmp_path / "schedule_gantt.md"
    html_path = tmp_path / "schedule_gantt.html"

    result_df = generate_gantt(
        db, mermaid_output_path=str(md_path), plotly_output_path=str(html_path), verbose=False,
    )

    assert len(result_df) == 2
    assert md_path.exists() and md_path.stat().st_size > 0
    assert html_path.exists() and html_path.stat().st_size > 0
    assert "デザイン" in md_path.read_text(encoding="utf-8")
    db.close()


def test_generate_gantt_rejects_circular_dependency(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    db.set_project("循環依存テスト", "2026-01-01")
    team_id = db.add_team("チームA", 1)
    ms_id = db.add_milestone("マイルストーン1", "2026-03-01")
    wf_id = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf_id, "タスク1", team_id, 3)
    t2 = db.add_workflow_task(wf_id, "タスク2", team_id, 3)
    # ここでは gui/node_canvas.py の事前チェックを経由しないため、DB層は
    # 直接の循環（1<->2）以外なら書き込めてしまう。project_scheduler.py側の
    # CircularDependencyError検出を確認する。
    db.add_task_dependency(wf_id, t1, t2)
    db.add_task_dependency(wf_id, t2, t1)
    db.add_job("ジョブ1", wf_id, ms_id, 1)

    with pytest.raises(SchedulingError):
        generate_gantt(db, verbose=False)
    db.close()


# -- 移行済みサンプルデータ（data/Project_Schedule_Sample_GameDev_v22.pschedule）--------

@pytest.mark.skipif(not SAMPLE_DB.exists(), reason="サンプル.pscheduleが見つかりません")
def test_sample_pschedule_generates_full_schedule(tmp_path):
    db = ProjectDatabase.open_existing(str(SAMPLE_DB))
    assert validate_for_generation(db) == []

    md_path = tmp_path / "schedule_gantt.md"
    html_path = tmp_path / "schedule_gantt.html"
    result_df = generate_gantt(
        db, mermaid_output_path=str(md_path), plotly_output_path=str(html_path), verbose=False,
    )

    # data/Project_Schedule_Sample_GameDev_v22.xlsx をCLIで直接実行した場合と
    # 同じ340行になることを確認する（DB移行・GUI側の変換で欠落/重複が無いこと）。
    assert len(result_df) == 340
    assert md_path.stat().st_size > 0
    assert html_path.stat().st_size > 0
    db.close()
