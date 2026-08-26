"""
Undo/Redo基盤（gui/db.py の @undoable / undo_group、gui/undo_manager.py）の
単体テスト。Qt非依存——ダミーのUI状態キャプチャ/復元callableを差し込んだ
ProjectDatabase + UndoManager の組み合わせだけで検証する。

Qtを介したGUIレベルの選択・フォーカス復元は tests/test_gui_undo_redo.py 参照。
"""

import sys
from pathlib import Path
from unittest.mock import patch

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


def test_duplicate_workflow_copies_tasks_dependencies_and_own_templates(tmp_path):
    """複製は、配下のタスク・タスク間依存・このワークフロー自身が持つ依存
    テンプレート（他ワークフローへの依存）をコピーする。ジョブや、他の
    ワークフローが複製元に依存しているテンプレートはコピーされないこと、
    名前の衝突は自動的に連番回避されること、Undo1回で全て元に戻ることを
    確認する。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    team_id = db.add_team("チームA", 1)
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    wf3 = db.add_workflow("WF3")
    a = db.add_workflow_task(wf1, "A", team_id, 1)
    b = db.add_workflow_task(wf1, "B", team_id, 2)
    db.update_task_position(a, 10, 20)
    db.update_task_position(b, 200, 20)
    db.add_task_dependency(wf1, a, b)

    other_task = db.add_workflow_task(wf2, "X", team_id, 1)
    third_task = db.add_workflow_task(wf3, "Y", team_id, 1)
    # WF1がWF2へ依存する側のテンプレート（複製に含まれるべき）。
    db.add_dependency_template(wf1, a, wf2, other_task)
    # WF3がWF1へ依存する側のテンプレート（複製に含まれてはいけない）。
    db.add_dependency_template(wf3, third_task, wf1, b)

    job_id = db.add_job("J1", wf1, None, 100)  # 複製に含まれてはいけない

    stack_size_before = len(manager._undo_stack)
    new_wf_id = db.duplicate_workflow(wf1)
    assert len(manager._undo_stack) == stack_size_before + 1  # 1つのUndo単位

    workflows = {w["name"]: w["id"] for w in db.list_workflows()}
    assert workflows["WF1のコピー"] == new_wf_id

    new_tasks = db.list_workflow_tasks(new_wf_id)
    assert sorted((t["name"], t["default_days"]) for t in new_tasks) == [("A", 1), ("B", 2)]
    new_a = next(t for t in new_tasks if t["name"] == "A")
    new_b = next(t for t in new_tasks if t["name"] == "B")
    assert (new_a["canvas_x"], new_a["canvas_y"]) == (10, 20)  # 座標も複製する
    assert new_a["id"] not in (a, b)  # 新規採番されている（元の行の使い回しではない）

    new_deps = db.list_task_dependencies(new_wf_id)
    assert len(new_deps) == 1
    assert new_deps[0]["predecessor_task_id"] == new_a["id"]
    assert new_deps[0]["successor_task_id"] == new_b["id"]

    new_templates = db.list_dependency_templates(new_wf_id)
    assert len(new_templates) == 1  # WF1側のテンプレートのみ複製される
    assert new_templates[0]["workflow_task_id"] == new_a["id"]
    assert new_templates[0]["depends_on_workflow_id"] == wf2
    assert new_templates[0]["depends_on_workflow_task_id"] == other_task

    # 他ワークフロー（WF3）が持つ、複製元WF1への依存テンプレートは複製されない。
    assert len(db.list_dependency_templates(wf3)) == 1

    assert [j["id"] for j in db.list_jobs()] == [job_id]  # ジョブは増えていない（複製元のジョブのみ）

    # 名前が衝突する場合は連番を付与する。
    new_wf_id2 = db.duplicate_workflow(wf1)
    workflows = {w["name"]: w["id"] for w in db.list_workflows()}
    assert "WF1のコピー (2)" in workflows
    assert workflows["WF1のコピー (2)"] == new_wf_id2

    manager.undo()
    assert "WF1のコピー (2)" not in {w["name"] for w in db.list_workflows()}
    manager.undo()
    assert db.list_workflows() == [
        {"id": wf1, "name": "WF1", "sort_order": 0},
        {"id": wf2, "name": "WF2", "sort_order": 1},
        {"id": wf3, "name": "WF3", "sort_order": 2},
    ]
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


def test_all_mutating_methods_are_marked_undoable():
    """変更系の命名を持つ公開メソッドすべてに @undoable が付いていること。

    _commit() の実行時ガードは「_commit() を経由する実装」しか検知できない
    （self._conn.commit() を直接呼ぶ実装をすり抜ける）ため、命名規約からの
    静的な検査も併用する。新しい変更系メソッドを追加してデコレータを付け
    忘れると、GUIを起動しなくてもこのテストが失敗する。"""
    mutating_prefixes = (
        "add_", "update_", "delete_", "set_", "upsert_",
        "clear_", "reorder_", "rename_", "sync_", "enforce_", "cascade_",
        "duplicate_",
    )
    # 変更系の命名だが、意図的にUndo対象外にしているもの。
    exempt = {
        # Undo/Redoの適用そのもの（gui/undo_manager.py から呼ばれる）。
        # これ自体をUndo記録の対象にすると無限に入れ子になる。
        "restore_state",
    }
    checked, missing = [], []
    for name in dir(ProjectDatabase):
        if name.startswith("_") or name in exempt:
            continue
        if not name.startswith(mutating_prefixes):
            continue
        checked.append(name)
        if not getattr(getattr(ProjectDatabase, name), "_is_undoable", False):
            missing.append(name)

    assert checked, "検査対象のメソッドが1件も見つかっていない（命名規約か検査条件の変更漏れ）"
    assert missing == [], f"@undoable が付いていない変更系メソッド: {missing}"


def test_save_failure_keeps_the_previously_saved_file(tmp_path):
    """回帰テスト: 保存が途中で失敗しても、保存済みのファイルが失われないこと。

    書き込み先を先に削除する実装だと、ディスク満杯・権限エラー等で保存済みの
    内容ごと消えてしまう。Undo履歴はメモリ上にしか無いため復旧手段が無い。"""
    import sqlite3

    path = tmp_path / "大事なプロジェクト.pschedule"
    db = ProjectDatabase.create_new(str(path))
    db.set_project("重要", "2026-01-01")
    db.add_team("チームA", 3)
    db.save()
    saved_size = path.stat().st_size

    db.add_team("チームB", 2)
    real_connect = sqlite3.connect

    def failing_connect(target, *args, **kwargs):
        # 一時ファイルへの書き込みが失敗する状況（ディスク満杯等）を再現する。
        if str(target).endswith(".tmp"):
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(target, *args, **kwargs)

    with patch("sqlite3.connect", failing_connect):
        with pytest.raises(sqlite3.OperationalError):
            db.save()

    assert path.exists(), "保存に失敗した結果、保存済みファイルが消えている"
    assert path.stat().st_size == saved_size
    # 失敗した保存の一時ファイルが残っていないこと
    assert list(tmp_path.glob("*.tmp")) == []

    reopened = ProjectDatabase.open_existing(str(path))
    assert [t["name"] for t in reopened.list_teams()] == ["チームA"]  # 失敗前の内容のまま
    reopened.close()
    db.close()


def test_open_ended_group_collapses_changes_until_it_is_closed(tmp_path):
    """begin_undo_group()/end_undo_group() の間の変更が、何回あっても1エントリに
    まとまること（スピンボックスの▲連打をフォーカス単位で1Undoにする仕組み）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)
    team_id = db.add_team("チームA", 1)

    steps_before = len(manager._undo_stack)
    db.begin_undo_group("チームの同時ライン数を変更")
    for lines in range(2, 7):
        db.update_team(team_id, "チームA", lines)
    assert db.list_teams()[0]["max_lines"] == 6  # DBは変更のたびに最新
    assert len(manager._undo_stack) == steps_before  # まだ記録されない

    changed = db.end_undo_group()
    assert changed is True  # gui/widgets_common.py の on_session_end 呼び出し判定に使う
    assert len(manager._undo_stack) == steps_before + 1
    assert manager.undo_label() == "チームの同時ライン数を変更"

    manager.undo()
    assert db.list_teams()[0]["max_lines"] == 1  # 1回で一気に戻る
    db.close()


def test_open_ended_group_records_nothing_when_the_value_is_unchanged(tmp_path):
    """フォーカスを出入りしただけ（値が変わっていない）なら記録しないこと。
    戻り値（False）は、gui/widgets_common.py の bind_undo_session が
    on_session_end を呼ぶかどうかの判定に使う（値が変わっていないのに
    並べ替え等の再構築が走ってしまうのを防ぐため）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)
    team_id = db.add_team("チームA", 1)

    steps_before = len(manager._undo_stack)
    db.begin_undo_group("チームの同時ライン数を変更")
    db.update_team(team_id, "チームA", 1)  # 同じ値に設定＝実質変化なし
    changed = db.end_undo_group()
    assert changed is False
    assert len(manager._undo_stack) == steps_before
    db.close()


def test_end_undo_group_returns_false_when_nothing_was_open(tmp_path):
    """begin_undo_group() を呼んでいない状態で end_undo_group() を呼んでも
    何もせず False を返すこと（bind_undo_sessionを付けていないウィジェットの
    フォーカス喪失や、二重にend_undo_groupが呼ばれるケースでの安全策）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    _attach_dummy_undo_manager(db)
    assert db.end_undo_group() is False
    db.close()


def test_open_ended_group_is_closed_by_undo_and_by_save(tmp_path):
    """編集途中のまま Undo や 保存 が行われた場合、その編集を先に1つの単位として
    確定させること（確定しないと、Undoが1つ前の操作を戻してしまう／保存済みの
    内容なのに後から未保存マークが付く）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)
    team_id = db.add_team("チームA", 1)

    db.begin_undo_group("チームの同時ライン数を変更")
    db.update_team(team_id, "チームA", 5)
    manager.undo()  # 編集途中でUndo
    assert db.list_teams()[0]["max_lines"] == 1  # 直前の編集が取り消される
    assert db._open_group is None

    db.begin_undo_group("チームの同時ライン数を変更")
    db.update_team(team_id, "チームA", 7)
    db.save()  # 編集途中で保存
    assert db._open_group is None
    assert not db.is_dirty()
    db.close()


def test_saved_state_is_tracked_across_undo_and_redo(tmp_path):
    """保存した時点までUndoで戻ったら未保存扱いが解除され、そこから離れると
    また未保存になること（変更→保存→変更→Undo で未保存マークが消える）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    assert db.is_dirty()  # 新規作成直後は未保存
    db.add_team("チームA", 1)
    db.save()
    assert not db.is_dirty()

    db.add_team("チームB", 1)
    assert db.is_dirty()

    manager.undo()  # 保存した時点へ戻る
    assert not db.is_dirty()

    manager.redo()
    assert db.is_dirty()

    manager.undo()
    manager.undo()  # 保存した時点より前へ
    assert db.is_dirty()

    manager.redo()  # 再び保存した時点へ
    assert not db.is_dirty()
    db.close()


def test_saved_state_is_forgotten_when_its_entry_is_discarded(tmp_path):
    """保存時点を指すエントリが上限超過で捨てられたら、以降は未保存扱いに
    倒すこと（もう「保存時と同じ内容か」を判定できないため）。"""
    from gui.undo_manager import _MAX_STACK_SIZE

    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)
    db.add_team("チームA", 1)
    db.save()
    assert not db.is_dirty()

    for i in range(_MAX_STACK_SIZE + 1):
        db.add_team(f"チーム{i}", 1)
    for _ in range(len(manager._undo_stack)):
        manager.undo()

    assert db.is_dirty()
    db.close()


def test_adjacent_entries_share_snapshots(tmp_path):
    """連続した操作では、直前のエントリの「後」と今回の「前」が同じbytes
    オブジェクトとして共有され、保持するスナップショット数がエントリ数+1に
    収まること（スナップショット方式のメモリ使用量を半減させる工夫）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    for i in range(5):
        db.add_team(f"チーム{i}", 1)

    stack = manager._undo_stack
    assert len(stack) == 5
    for previous, current in zip(stack, stack[1:]):
        assert current.before_db is previous.after_db

    held = {id(e.before_db) for e in stack} | {id(e.after_db) for e in stack}
    assert len(held) == len(stack) + 1
    db.close()


def test_undo_stack_is_capped(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    from gui.undo_manager import _MAX_STACK_SIZE

    for i in range(_MAX_STACK_SIZE + 20):
        db.add_team(f"チーム{i}", 1)
    assert len(manager._undo_stack) == _MAX_STACK_SIZE
