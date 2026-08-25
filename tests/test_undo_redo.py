"""
Undo/Redo基盤（gui/db.py の @undoable / undo_group、gui/undo_manager.py）の
単体テスト。Qt非依存——ダミーのUI状態キャプチャ/復元callableを差し込んだ
ProjectDatabase + UndoManager の組み合わせだけで検証する。

Qtを介したGUIレベルの選択・フォーカス復元は tests/test_gui_undo_redo.py 参照。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.db import ProjectDatabase  # noqa: E402
from gui.undo_manager import UndoManager  # noqa: E402


def _attach_dummy_undo_manager(db):
    """UI状態は単なる連番として記録するダミーのUndoManagerをdbに接続する
    （Qtに依存せず、DB層のUndo/Redoの記録タイミング・粒度だけを検証するため）。"""
    calls = {"capture": 0, "restore": []}

    def capture():
        calls["capture"] += 1
        return calls["capture"]

    def restore(value):
        calls["restore"].append(value)

    manager = UndoManager(
        db=db,
        capture_ui_state=capture,
        restore_ui_state=restore,
        apply_db_state=db.restore_state,
    )
    db.undo_manager = manager
    return manager, calls


def test_add_team_can_be_undone_and_redone(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    db.add_team("チームA", 2)
    assert db.list_teams() != []
    assert manager.can_undo()
    assert manager.undo_label() == "チーム「チームA」を追加"
    assert not manager.can_redo()

    manager.undo()
    assert db.list_teams() == []
    assert manager.can_redo()
    assert manager.redo_label() == "チーム「チームA」を追加"

    manager.redo()
    assert [t["name"] for t in db.list_teams()] == ["チームA"]
    db.close()


def test_nested_calls_collapse_into_one_undo_entry(tmp_path):
    """add_job_dependency_link は内部で sync_dependency_templates を呼ぶが、
    Undoスタックには1エントリだけ積まれ、Undo1回で両方まとめて元に戻ること。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    team_id = db.add_team("チームA", 1)
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    t1 = db.add_workflow_task(wf1, "A", team_id, 1)
    t2 = db.add_workflow_task(wf2, "B", team_id, 1)
    j1 = db.add_job("J1", wf1, None, 100)
    j2 = db.add_job("J2", wf2, None, 100)
    db.add_dependency_template(wf1, t1, wf2, t2)

    stack_size_before = len(manager._undo_stack)
    db.add_job_dependency_link(j1, j2)
    assert len(db.list_external_dependencies(job_id=j1)) == 1  # 自動展開済み
    assert len(manager._undo_stack) == stack_size_before + 1  # 1エントリだけ

    manager.undo()
    assert db.list_job_dependency_links(j1) == []
    assert db.list_external_dependencies(job_id=j1) == []  # 自動展開分も一緒に戻る
    db.close()


def test_explicit_undo_group_collapses_multiple_calls(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    team_id = db.add_team("チームA", 1)
    stack_size_before = len(manager._undo_stack)
    with db.undo_group("複数フィールドをまとめて変更"):
        db.update_team(team_id, "チームA改", 3)
        db.add_holiday("2026-01-01")
    assert len(manager._undo_stack) == stack_size_before + 1

    manager.undo()
    assert db.list_teams()[0]["name"] == "チームA"
    assert db.list_holidays() == []
    db.close()


def test_noop_group_does_not_push_entry(tmp_path):
    """変更が実際には起きなかった場合はUndoエントリを積まない
    （例外での早期リターンや、既に無効化されている行の再無効化など）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    stack_size_before = len(manager._undo_stack)
    with db.undo_group("何もしない"):
        pass
    assert len(manager._undo_stack) == stack_size_before
    db.close()


def test_workflow_node_edit_helpers_are_grouped(tmp_path):
    """cascade_milestone_to_successors / enforce_milestone_floor のように、
    内部で複数回 upsert_job_task_override を呼ぶメソッドも、Undo1回で
    まとめて元に戻ること。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    team_id = db.add_team("チームA", 1)
    ms_early = db.add_milestone("早期MS", "2026-01-31")
    ms_late = db.add_milestone("後期MS", "2026-06-30")
    wf = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf, "タスク1", team_id, 3)
    t2 = db.add_workflow_task(wf, "タスク2", team_id, 3)
    db.add_task_dependency(wf, t1, t2)
    job = db.add_job("ジョブ1", wf, ms_early, 100)

    db.upsert_job_task_override(job, t1, milestone_id=ms_late)
    stack_size_before = len(manager._undo_stack)
    changed = db.cascade_milestone_to_successors(job, t1)
    assert changed == [t2]
    assert len(manager._undo_stack) == stack_size_before + 1  # 1エントリだけ

    manager.undo()
    # cascadeで作られたタスク2の上書き（後期MS）が消え、ジョブの既定（早期MS）に戻る。
    assert db.effective_milestone(job, t2)["milestone_id"] == ms_early
    db.close()


def test_commit_without_undoable_raises_when_manager_attached(tmp_path):
    """変更系メソッドに @undoable を付け忘れて self._commit() を直接呼ぶと、
    Undo管理が有効な場面では例外が送出されること（記録漏れの検知ガード）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    _attach_dummy_undo_manager(db)

    with pytest.raises(AssertionError):
        db._commit()
    db.close()


def test_commit_without_manager_is_unaffected(tmp_path):
    """undo_managerが未設定（GUIを介さない従来通りの利用、既存テスト全般）の
    場合はガードが働かず、通常のCRUDとして問題なく動くこと。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    db.add_team("チームA", 1)
    assert db.list_teams() != []
    db.close()


def test_ui_state_captured_before_change_and_restored_in_order(tmp_path):
    """undo()/redo()が、DBスナップショットと対になるUI状態を正しいタイミングで
    キャプチャ・復元していることを確認する。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, calls = _attach_dummy_undo_manager(db)

    db.add_team("チームA", 1)  # 変更前の状態としてUI状態#1をキャプチャ
    manager.undo()  # 現在地をUI状態#2としてredoスタックへ退避し、#1を復元
    assert calls["restore"][-1] == 1

    manager.redo()  # 現在地をUI状態#3としてundoスタックへ戻し、#2を復元
    assert calls["restore"][-1] == 2
    db.close()


def test_undo_stack_is_capped(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    from gui.undo_manager import _MAX_STACK_SIZE

    for i in range(_MAX_STACK_SIZE + 20):
        db.add_team(f"チーム{i}", 1)
    assert len(manager._undo_stack) == _MAX_STACK_SIZE
