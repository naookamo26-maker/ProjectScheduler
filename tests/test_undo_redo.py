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


def _attach_dummy_undo_manager(db, ui_states=None, on_restore=None):
    """ダミーのUndoManagerをdbに接続する（Qtに依存せず、DB層のUndo/Redoの
    記録タイミング・粒度だけを検証するため）。

    ui_states: capture_ui_state が順に返す値のリスト（省略時は連番）。
    on_restore: restore_ui_state から追加で呼ぶcallable（GUI側の再読込が
        DBに書き込む状況を模す用途）。

    schedule_after_capture には「即座に実行する」callableを渡し、Qtの
    イベントループ無しでも「操作直後のUI状態」の記録経路を検証できるように
    する（実アプリでは QTimer.singleShot(0, ...) 相当）。"""
    calls = {"capture": 0, "restore": []}

    def capture():
        calls["capture"] += 1
        if ui_states is not None:
            return ui_states[min(calls["capture"] - 1, len(ui_states) - 1)]
        return calls["capture"]

    def restore(value):
        calls["restore"].append(value)
        if on_restore is not None:
            on_restore()

    manager = UndoManager(
        db=db,
        capture_ui_state=capture,
        restore_ui_state=restore,
        apply_db_state=db.restore_state,
        schedule_after_capture=lambda fn: fn(),
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


def test_undo_restores_before_state_and_redo_restores_after_state(tmp_path):
    """1エントリが操作の「前」「後」両方のUI状態を持ち、Undoは前を、Redoは
    後を復元すること（＝Redoは「Ctrl+Zを押した時の画面」ではなく「操作直後の
    画面」へ戻る）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, calls = _attach_dummy_undo_manager(db, ui_states=["操作前", "操作直後"])

    db.add_team("チームA", 1)
    entry = manager._undo_stack[-1]
    assert entry.before_ui == "操作前"
    assert entry.after_ui == "操作直後"  # 予約された取得処理で埋まっている

    manager.undo()
    assert calls["restore"][-1] == "操作前"

    manager.redo()
    assert calls["restore"][-1] == "操作直後"
    db.close()


def test_redo_survives_db_writes_during_undo_application(tmp_path):
    """回帰テスト: Undo/Redoの適用中にGUI側の再読込がDBへ書き込んでも、
    それが新しい操作として記録されずRedoが可能なままであること。

    現に gui/tab_jobs.py の refresh_choices() は sync_dependency_templates()
    （@undoable）を呼ぶため、抑止が無いとUndo直後にRedoできなくなる。
    今後追加するタブが同様の実装をしても壊れないことを担保する。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    writes = {"enabled": False}

    def write_during_restore():
        if writes["enabled"]:
            db.add_holiday("2099-12-31")

    manager, _ = _attach_dummy_undo_manager(db, on_restore=write_during_restore)

    db.add_team("チームA", 1)
    db.add_team("チームB", 1)
    writes["enabled"] = True

    manager.undo()
    assert manager.can_redo(), "適用中の書き込みでRedoスタックが破棄されている"
    assert len(manager._undo_stack) == 1

    manager.redo()
    assert [t["name"] for t in db.list_teams()] == ["チームA", "チームB"]
    db.close()


def test_partial_changes_before_an_exception_stay_undoable(tmp_path):
    """回帰テスト: 複合操作の途中で例外が起きても、そこまでにコミット済みの
    変更はUndoで取り消せること（取り消せない変更が残る方が有害なため、
    エントリを積む方針）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    with pytest.raises(RuntimeError):
        with db.undo_group("途中で失敗する複合操作"):
            db.add_team("チームX", 1)
            raise RuntimeError("途中で失敗")

    assert [t["name"] for t in db.list_teams()] == ["チームX"]
    assert manager.can_undo()
    manager.undo()
    assert db.list_teams() == []
    db.close()


def test_commit_is_allowed_while_undo_recording_is_suspended(tmp_path):
    """Undo/Redoの適用中は、@undoableの外側からの_commit()もガードに
    引っかからないこと（適用中はそもそも記録しない区間のため）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    _attach_dummy_undo_manager(db)

    with db.suspend_undo_recording():
        db._commit()  # 例外にならない
    with pytest.raises(AssertionError):
        db._commit()  # 抑止を抜けたらガードが復活する
    db.close()


def test_undo_stack_is_capped(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    from gui.undo_manager import _MAX_STACK_SIZE

    for i in range(_MAX_STACK_SIZE + 20):
        db.add_team(f"チーム{i}", 1)
    assert len(manager._undo_stack) == _MAX_STACK_SIZE
