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

from gui.db import ProjectDatabase, ProjectDatabaseError  # noqa: E402
from gui.undo_manager import UndoManager  # noqa: E402

# 分類: core（Qt非依存・pandas非依存。pytestだけで動く）
pytestmark = pytest.mark.core


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


def test_job_tags_are_normalized_on_add_and_update(tmp_path):
    """タグはカンマ区切りの1文字列として保持し、前後の空白除去・空要素除去・
    重複排除・「, 」区切りへの整形を保存時に行う。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    team = db.add_team("チームA", 1)
    wf = db.add_workflow("WF1")
    db.add_workflow_task(wf, "A", team, 1)

    job_id = db.add_job("J1", wf, None, 100, " 緊急 ,顧客A,, 緊急")
    job = next(j for j in db.list_jobs() if j["id"] == job_id)
    assert job["tags"] == "緊急, 顧客A"

    db.update_job(job_id, "J1", wf, None, 100, "顧客B")
    job = next(j for j in db.list_jobs() if j["id"] == job_id)
    assert job["tags"] == "顧客B"

    # タグ未指定（既定の""）の場合はタグ無しとして扱う。
    job_id2 = db.add_job("J2", wf, None, 100)
    job2 = next(j for j in db.list_jobs() if j["id"] == job_id2)
    assert job2["tags"] == ""
    db.close()


def test_task_tags_are_normalized_and_default_free_rows_are_cleared(tmp_path):
    """タスク タグ（job_task_overrides.tags）はジョブ タグ（jobs.tags）と同じ
    仕様で、カンマ区切りの正規化（前後の空白除去・空要素除去・重複排除・
    「, 」区切り）を行う。タグ以外の項目が既定のままでも、タグを持てば
    行が作られ（差分のみ保持）、タグも既定（空）に戻せば
    clear_job_task_override で行ごと削除できる。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    team = db.add_team("チームA", 1)
    wf = db.add_workflow("WF1")
    task = db.add_workflow_task(wf, "A", team, 1)
    job_id = db.add_job("J1", wf, None, 100)

    # 他の項目は既定のまま、タスク タグだけを設定しても上書き行が作られる。
    db.upsert_job_task_override(job_id, task, tags=" 確認 ,レビュー,, 確認")
    row = db.list_job_tasks_with_overrides(job_id)[0]
    assert row["tags"] == "確認, レビュー"
    assert (row["is_active"], row["override_days"]) == (1, None)

    all_overrides = db.list_all_job_task_overrides()
    assert len(all_overrides) == 1
    assert all_overrides[0]["tags"] == "確認, レビュー"

    # タグを空に戻す（他の項目も既定のまま）と、差分が無くなるため呼び出し側は
    # clear_job_task_override を呼ぶ想定——ここでは実際に削除されることを確認する。
    db.clear_job_task_override(job_id, task)
    row = db.list_job_tasks_with_overrides(job_id)[0]
    assert row["tags"] == ""
    assert row["override_id"] is None
    db.close()


def test_job_priority_can_be_left_unspecified(tmp_path):
    """優先度は「未指定」（NULL）を許容する。新規ジョブは既定でこの状態になり、
    スケジューリング側で自動的に最低優先として扱われる
    （project_scheduler.DEFAULT_LOW_PRIORITY）。明示的に数値を設定した後、
    再び未指定へ戻すこともできる。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    team = db.add_team("チームA", 1)
    wf = db.add_workflow("WF1")
    db.add_workflow_task(wf, "A", team, 1)

    job_id = db.add_job("J1", wf, None)  # priority省略＝未指定
    job = next(j for j in db.list_jobs() if j["id"] == job_id)
    assert job["priority"] is None

    db.update_job(job_id, "J1", wf, None, 5, "")
    job = next(j for j in db.list_jobs() if j["id"] == job_id)
    assert job["priority"] == 5

    db.update_job(job_id, "J1", wf, None, None, "")
    job = next(j for j in db.list_jobs() if j["id"] == job_id)
    assert job["priority"] is None
    db.close()


def test_duplicate_job_copies_task_overrides_and_outgoing_dependencies(tmp_path):
    """ジョブの複製は、タスク上書き（タスク タグを含む）・依存先ジョブ
    （ジョブ間依存、テンプレート由来のタスク対応・手動追加のタスク対応の
    両方）を引き継ぐ。一方、他ジョブが
    このジョブに依存している側（依存されている側）は引き継がない（複製先へ
    他ジョブが勝手に依存する状態になる副作用を避けるため）。名前の衝突は
    自動的に連番回避されること、Undo1回で全て元に戻ることも確認する。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    team = db.add_team("チームA", 1)
    ms = db.add_milestone("MS1", "2026-06-30")
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    task_a = db.add_workflow_task(wf1, "A", team, 1)
    task_b = db.add_workflow_task(wf1, "B", team, 1)
    task_x = db.add_workflow_task(wf2, "X", team, 1)
    task_y = db.add_workflow_task(wf2, "Y", team, 1)
    db.add_dependency_template(wf1, task_a, wf2, task_x)  # WF1のA -> WF2のX

    job_id = db.add_job("J1", wf1, ms, 50, "緊急, 顧客A")
    other_job = db.add_job("J2", wf2, None, 100)
    third_job = db.add_job("J3", wf1, None, 100)
    db.upsert_job_task_override(job_id, task_a, is_active=False, override_days=3,
                                 tags=" 緊急タスク ,確認")

    db.add_job_dependency_link(job_id, other_job)  # テンプレート由来のタスク対応が自動生成される
    db.add_external_dependency(job_id, task_b, other_job, task_y)  # 手動追加分
    db.add_job_dependency_link(third_job, job_id)  # J3がJ1に依存する側（複製で引き継がれてはいけない）

    stack_size_before = len(manager._undo_stack)
    new_job_id = db.duplicate_job(job_id)
    assert len(manager._undo_stack) == stack_size_before + 1  # 1つのUndo単位

    jobs = {j["name"]: j for j in db.list_jobs()}
    assert "J1のコピー" in jobs
    new_job = jobs["J1のコピー"]
    assert new_job["id"] == new_job_id
    assert new_job["workflow_id"] == wf1
    assert new_job["default_milestone_id"] == ms
    assert new_job["priority"] == 50
    assert new_job["tags"] == "緊急, 顧客A"

    new_overrides = db.list_job_tasks_with_overrides(new_job_id)
    override = next(o for o in new_overrides if o["task_name"] == "A")
    assert (override["is_active"], override["override_days"]) == (0, 3)
    assert override["tags"] == "緊急タスク, 確認"  # タスク タグも複製される

    # 依存先ジョブ（このジョブ→他ジョブ）は複製される。
    new_links = db.list_job_dependency_links(new_job_id)
    assert [l["depends_on_job_id"] for l in new_links] == [other_job]
    new_link_id = new_links[0]["id"]

    new_ext_deps = {
        (d["workflow_task_id"], d["depends_on_workflow_task_id"]): d
        for d in db.list_external_dependencies(job_id=new_job_id)
    }
    assert set(new_ext_deps) == {(task_a, task_x), (task_b, task_y)}
    assert new_ext_deps[(task_a, task_x)]["source_link_id"] == new_link_id  # 付け替え済み
    assert new_ext_deps[(task_b, task_y)]["source_link_id"] is None  # 手動追加分はNoneのまま

    # このジョブに依存している側（J3→J1）は複製先には引き継がれない。
    assert [l["depends_on_job_id"] for l in db.list_job_dependency_links(third_job)] == [job_id]

    # 名前が衝突する場合は連番を付与する。
    new_job_id2 = db.duplicate_job(job_id)
    jobs = {j["name"]: j["id"] for j in db.list_jobs()}
    assert jobs["J1のコピー (2)"] == new_job_id2

    manager.undo()
    assert "J1のコピー (2)" not in {j["name"] for j in db.list_jobs()}
    manager.undo()
    assert {j["name"] for j in db.list_jobs()} == {"J1", "J2", "J3"}
    db.close()


def test_team_lines_can_be_zero(tmp_path):
    """途中で合流する・早めに引き上げるチームを表現するため、チームの同時
    ライン数（既定値・変更点のいずれも）に0を設定できること。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    team_id = db.add_team("チームA", 0)
    assert db.list_teams()[0]["max_lines"] == 0

    db.update_team(team_id, "チームA", 0)
    assert db.list_teams()[0]["max_lines"] == 0

    change_id = db.add_team_capacity_change(team_id, "2026-02-01", 0)
    assert db.list_team_capacity_changes(team_id)[0]["lines"] == 0

    db.update_team_capacity_change(change_id, "2026-02-01", 2)
    assert db.list_team_capacity_changes(team_id)[0]["lines"] == 2
    db.close()


def test_team_lines_can_be_null_meaning_unspecified(tmp_path):
    """ライン数上限は「指定なし」（NULL＝上限を設けない）も設定できること。
    0（その期間は稼働なし）とは別の値として区別される
    （docs/project_analysis_tab_design.md参照）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    team_id = db.add_team("チームA", None)
    assert db.list_teams()[0]["max_lines"] is None

    db.update_team(team_id, "チームA", 3)
    assert db.list_teams()[0]["max_lines"] == 3
    db.update_team(team_id, "チームA", None)
    assert db.list_teams()[0]["max_lines"] is None

    change_id = db.add_team_capacity_change(team_id, "2026-02-01", None)
    assert db.list_team_capacity_changes(team_id)[0]["lines"] is None
    db.update_team_capacity_change(change_id, "2026-02-01", 5)
    assert db.list_team_capacity_changes(team_id)[0]["lines"] == 5
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
        "duplicate_", "apply_",
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


def test_milestone_change_and_consistency_repair_collapse_into_one_undo_step(tmp_path):
    """マイルストーンの締切変更と、それに伴うジョブ側タスク上書きの再調整が、
    1回のUndoでまとめて戻ること（GUI側は編集セッションのUndo単位がまだ開いて
    いるうちに再調整を走らせる——gui/widgets_common.py の on_before_commit）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    team = db.add_team("チームA", 1)
    ms_mid = db.add_milestone("中期MS", "2026-03-31")
    ms_late = db.add_milestone("後期MS", "2026-06-30")
    wf = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf, "タスク1", team, 3)
    t2 = db.add_workflow_task(wf, "タスク2", team, 3)
    db.add_task_dependency(wf, t1, t2)
    job = db.add_job("ジョブ1", wf, ms_mid, 100)
    db.upsert_job_task_override(job, t2, is_active=True, override_days=None,
                                milestone_id=ms_late, team_id=None)

    before = (db.get_milestone(ms_late)["end_date"],
              db.effective_milestone(job, t2)["end_date"])
    steps_before = len(manager._undo_stack)

    with db.undo_group("マイルストーンの締切日を変更"):
        db.update_milestone(ms_late, "後期MS", "2026-02-15")
        plan = db.plan_milestone_consistency_repair()
        assert plan, "この変更で不整合が生じるはず"
        db.apply_milestone_consistency_repair(plan)

    assert len(manager._undo_stack) == steps_before + 1  # 2件に分かれない
    after = (db.get_milestone(ms_late)["end_date"],
             db.effective_milestone(job, t2)["end_date"])
    assert after == ("2026-02-15", "2026-03-31")

    manager.undo()
    assert (db.get_milestone(ms_late)["end_date"],
            db.effective_milestone(job, t2)["end_date"]) == before  # 1回で両方戻る

    manager.redo()
    assert (db.get_milestone(ms_late)["end_date"],
            db.effective_milestone(job, t2)["end_date"]) == after
    db.close()


def test_cancelled_milestone_repair_leaves_no_undo_entry(tmp_path):
    """確認ダイアログでキャンセルした場合、GUI側は同じUndo単位の中で締切日を
    元に戻す。差し引きゼロなのでUndoエントリも積まれないこと。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)

    ms = db.add_milestone("中期MS", "2026-03-31")
    steps_before = len(manager._undo_stack)

    with db.undo_group("マイルストーンの締切日を変更"):
        db.update_milestone(ms, "中期MS", "2026-02-15")
        db.update_milestone(ms, "中期MS", "2026-03-31")  # キャンセル＝元に戻す

    assert len(manager._undo_stack) == steps_before
    assert db.get_milestone(ms)["end_date"] == "2026-03-31"
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


# -- 依存関係の種別（FS/SS）とラグ ---------------------------------------------------

def test_task_dependency_defaults_to_finish_to_start_without_lag(tmp_path):
    """種別・ラグを指定せずに追加した依存は FS・0（＝この機能が入る前と
    まったく同じ意味）になること。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    team = db.add_team("チームA", 1)
    wf = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf, "タスク1", team, 3)
    t2 = db.add_workflow_task(wf, "タスク2", team, 3)
    dep_id = db.add_task_dependency(wf, t1, t2)

    dep = db.get_task_dependency(dep_id)
    assert (dep["dep_type"], dep["lag_days"]) == ("FS", 0)
    assert db.list_task_dependencies(wf)[0]["dep_type"] == "FS"
    db.close()


def test_update_task_dependency_kind_is_undoable(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)
    team = db.add_team("チームA", 1)
    wf = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf, "タスク1", team, 3)
    t2 = db.add_workflow_task(wf, "タスク2", team, 3)
    dep_id = db.add_task_dependency(wf, t1, t2)

    db.update_task_dependency(dep_id, "SS", 2)
    assert db.get_task_dependency(dep_id)["dep_type"] == "SS"
    assert db.get_task_dependency(dep_id)["lag_days"] == 2

    manager.undo()
    dep = db.get_task_dependency(dep_id)
    assert (dep["dep_type"], dep["lag_days"]) == ("FS", 0)

    manager.redo()
    dep = db.get_task_dependency(dep_id)
    assert (dep["dep_type"], dep["lag_days"]) == ("SS", 2)
    db.close()


def test_task_dependency_rejects_unknown_kind_and_out_of_range_lag(tmp_path):
    """ALTER TABLEで後から足した列にはCHECK制約を付けられないため、
    不正な値は書き込み経路（gui/db.py）で弾かれること。"""
    from gui.db import MAX_LAG_DAYS, ProjectDatabaseError

    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    team = db.add_team("チームA", 1)
    wf = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf, "タスク1", team, 3)
    t2 = db.add_workflow_task(wf, "タスク2", team, 3)

    with pytest.raises(ProjectDatabaseError):
        db.add_task_dependency(wf, t1, t2, dep_type="FF")

    dep_id = db.add_task_dependency(wf, t1, t2)
    with pytest.raises(ProjectDatabaseError):
        db.update_task_dependency(dep_id, "SS", MAX_LAG_DAYS + 1)
    with pytest.raises(ProjectDatabaseError):
        db.update_task_dependency(dep_id, "SS", "2日")
    # 弾かれた場合は元の値のまま
    assert db.get_task_dependency(dep_id)["dep_type"] == "FS"
    db.close()


def test_duplicating_workflow_copies_dependency_kind_and_lag(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    team = db.add_team("チームA", 1)
    wf = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf, "タスク1", team, 3)
    t2 = db.add_workflow_task(wf, "タスク2", team, 3)
    dep_id = db.add_task_dependency(wf, t1, t2, dep_type="SS", lag_days=3)
    assert db.get_task_dependency(dep_id)["lag_days"] == 3

    copy_id = db.duplicate_workflow(wf)
    copied = db.list_task_dependencies(copy_id)
    assert len(copied) == 1
    assert (copied[0]["dep_type"], copied[0]["lag_days"]) == ("SS", 3)
    db.close()


def test_opening_pre_dependency_lag_schema_migrates_to_finish_to_start(tmp_path):
    """dep_type/lag_days列が無い旧バージョン(v6)の.pscheduleを開いた際、
    列が追加され、既存の依存はすべて FS・0（＝従来の唯一の挙動）として
    扱われること。"""
    import sqlite3

    path = tmp_path / "legacy.pschedule"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE project (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            project_name TEXT NOT NULL DEFAULT '',
            start_date TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE workflows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE task_dependencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id INTEGER NOT NULL,
            predecessor_task_id INTEGER NOT NULL,
            successor_task_id INTEGER NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '6')")
    conn.execute("INSERT INTO project(id, project_name, start_date) VALUES (1, '', '')")
    conn.execute("INSERT INTO workflows(name) VALUES ('WF1')")
    conn.execute(
        "INSERT INTO task_dependencies(workflow_id, predecessor_task_id, successor_task_id) "
        "VALUES (1, 10, 20)"
    )
    conn.commit()
    conn.close()

    db = ProjectDatabase.open_existing(str(path))
    deps = db.list_task_dependencies(1)
    assert len(deps) == 1
    assert (deps[0]["dep_type"], deps[0]["lag_days"]) == ("FS", 0)
    db.close()




# -- 開始固定日（job_task_overrides.start_pin_date） -----------------------------------

def _build_job_with_task(db):
    team = db.add_team("チームA", 1)
    ms = db.add_milestone("MS1", "2026-06-30")
    wf = db.add_workflow("WF1")
    task = db.add_workflow_task(wf, "タスク1", team, 3)
    job = db.add_job("ジョブ1", wf, ms, 100)
    return job, task


def test_start_pin_date_is_stored_as_a_diff_only_override(tmp_path):
    """開始固定日は job_task_overrides の他の列（override_days等）と同じ
    「差分のみ保持」に乗る。全項目が既定に戻ると行ごと消える。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    job, task = _build_job_with_task(db)

    db.upsert_job_task_override(job, task, start_pin_date="2026-02-02")
    row = db.list_job_tasks_with_overrides(job)[0]
    assert row["start_pin_date"] == "2026-02-02"

    db.upsert_job_task_override(job, task, start_pin_date=None)
    row = db.list_job_tasks_with_overrides(job)[0]
    assert row["start_pin_date"] is None
    db.close()


def test_start_pin_date_change_is_undoable(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager, _ = _attach_dummy_undo_manager(db)
    job, task = _build_job_with_task(db)

    db.upsert_job_task_override(job, task, start_pin_date="2026-02-02")
    assert db.list_job_tasks_with_overrides(job)[0]["start_pin_date"] == "2026-02-02"

    manager.undo()
    rows = db.list_job_tasks_with_overrides(job)
    assert rows[0]["start_pin_date"] is None
    db.close()


def test_start_pin_date_rejects_malformed_value(tmp_path):
    """壊れた日付を保存できてしまうと、スケジューラ側で黙って NaT になり
    「固定が無かったこと」になる。書き込み経路で弾くこと。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    job, task = _build_job_with_task(db)

    with pytest.raises(ProjectDatabaseError):
        db.upsert_job_task_override(job, task, start_pin_date="2026/02/02")
    with pytest.raises(ProjectDatabaseError):
        db.upsert_job_task_override(job, task, start_pin_date="2026-13-45")
    assert db.list_job_tasks_with_overrides(job)[0]["start_pin_date"] is None
    db.close()


def test_opening_pre_start_pin_schema_migrates_existing_start_on_rows(tmp_path):
    """start_pin_date列もtask_constraintsテーブルも無い旧バージョン(v8)の
    .pscheduleを開いた際、列が追加され、旧task_constraintsのSTART_ON行が
    job_task_overrides.start_pin_dateへ引き継がれ、テーブル自体は削除される
    こと（SNET/SNLT/FNLTやジョブ全体の制約は撤回した機能のデータとして
    引き継がない）。"""
    import sqlite3

    path = tmp_path / "legacy.pschedule"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE project (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            project_name TEXT NOT NULL DEFAULT '',
            start_date TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
        CREATE TABLE workflow_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
        CREATE TABLE job_task_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL, workflow_task_id INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            override_days INTEGER, milestone_id INTEGER, team_id INTEGER
        );
        CREATE TABLE task_constraints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL, workflow_task_id INTEGER,
            kind TEXT NOT NULL, date TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '8')")
    conn.execute("INSERT INTO project(id, project_name, start_date) VALUES (1, '', '')")
    conn.execute("INSERT INTO jobs(name) VALUES ('ジョブ1')")
    conn.execute("INSERT INTO jobs(name) VALUES ('ジョブ2')")
    conn.execute("INSERT INTO workflow_tasks(name) VALUES ('タスク1')")
    conn.execute("INSERT INTO workflow_tasks(name) VALUES ('タスク2')")
    # ジョブ1・タスク1: 既存の上書き行にSTART_ONが追記されるケース
    conn.execute(
        "INSERT INTO job_task_overrides(job_id, workflow_task_id, is_active, override_days) "
        "VALUES (1, 1, 1, 5)"
    )
    conn.execute(
        "INSERT INTO task_constraints(job_id, workflow_task_id, kind, date) "
        "VALUES (1, 1, 'START_ON', '2026-02-02')"
    )
    # ジョブ2・タスク2: 上書き行が無い状態からSTART_ONだけで新規行ができるケース
    conn.execute(
        "INSERT INTO task_constraints(job_id, workflow_task_id, kind, date) "
        "VALUES (2, 2, 'START_ON', '2026-03-03')"
    )
    # 撤回した機能のデータ（ジョブ全体・SNET等）は引き継がれないことも確認する
    conn.execute(
        "INSERT INTO task_constraints(job_id, workflow_task_id, kind, date) "
        "VALUES (1, NULL, 'SNET', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO task_constraints(job_id, workflow_task_id, kind, date) "
        "VALUES (1, 1, 'FNLT', '2026-04-01')"
    )
    conn.commit()
    conn.close()

    db = ProjectDatabase.open_existing(str(path))
    # list_job_tasks_with_overrides() は jobs.workflow_id 等フル構成の
    # スキーマを要求するため、ここでは生SQLで移行結果だけを確認する。
    rows = {
        (r["job_id"], r["workflow_task_id"]): (r["start_pin_date"], r["override_days"])
        for r in db._conn.execute(
            "SELECT job_id, workflow_task_id, start_pin_date, override_days "
            "FROM job_task_overrides"
        ).fetchall()
    }
    assert rows[(1, 1)] == ("2026-02-02", 5)   # 既存の上書きは保持される
    assert rows[(2, 2)] == ("2026-03-03", None)
    tables = {
        r["name"] for r in
        db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "task_constraints" not in tables
    db.close()


# -- ノードグラフの座標（保存しない設計への移行） ------------------------------------

def test_workflow_task_rows_no_longer_have_canvas_columns(tmp_path):
    """workflow_tasks.canvas_x/canvas_yは撤去済み。座標は表示のたびに
    依存の深さから計算し直す方式に統一し、保存する意味のないデータを
    schema・保存ファイルの双方から無くした。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    team = db.add_team("チームA", 1)
    wf = db.add_workflow("WF1")
    db.add_workflow_task(wf, "タスク1", team, 3)

    cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(workflow_tasks)").fetchall()}
    assert "canvas_x" not in cols
    assert "canvas_y" not in cols
    assert not hasattr(db, "update_task_position")
    db.close()


def test_opening_pre_canvas_removal_schema_drops_canvas_columns(tmp_path):
    """canvas_x/canvas_y列がまだ残っている旧バージョン(v9)の.pscheduleを
    開いた際、列が削除され、他のデータ（タスク名・所要日数等）は保持される
    こと。"""
    import sqlite3

    path = tmp_path / "legacy.pschedule"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE project (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            project_name TEXT NOT NULL DEFAULT '',
            start_date TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE teams (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
        CREATE TABLE workflows (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
        CREATE TABLE workflow_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id INTEGER NOT NULL, name TEXT NOT NULL,
            team_id INTEGER NOT NULL, default_days INTEGER NOT NULL,
            canvas_x REAL NOT NULL DEFAULT 0, canvas_y REAL NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '9')")
    conn.execute("INSERT INTO project(id, project_name, start_date) VALUES (1, '', '')")
    conn.execute("INSERT INTO teams(name) VALUES ('チームA')")
    conn.execute("INSERT INTO workflows(name) VALUES ('WF1')")
    conn.execute(
        "INSERT INTO workflow_tasks(workflow_id, name, team_id, default_days, canvas_x, canvas_y) "
        "VALUES (1, 'タスク1', 1, 3, 123.0, 456.0)"
    )
    conn.commit()
    conn.close()

    db = ProjectDatabase.open_existing(str(path))
    cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(workflow_tasks)").fetchall()}
    assert "canvas_x" not in cols
    assert "canvas_y" not in cols
    tasks = db.list_workflow_tasks(1)
    assert len(tasks) == 1
    assert (tasks[0]["name"], tasks[0]["default_days"]) == ("タスク1", 3)
    db.close()


# -- jobs.tags の追加 / teams・team_capacity_changesのCHECK緩和（v11） -----------------

def test_opening_pre_tags_schema_adds_tags_column_and_allows_zero_lines(tmp_path):
    """tags列が無く、max_lines/linesのCHECKが「1以上」だった旧バージョン(v10)の
    .pscheduleを開いた際、jobs.tagsが追加され既存ジョブは空文字になること、
    かつteams.max_lines/team_capacity_changes.linesに0を保存できるようになる
    こと（テーブルを作り直すため、既存データは保持されること）。"""
    import sqlite3

    path = tmp_path / "legacy.pschedule"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE project (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            project_name TEXT NOT NULL DEFAULT '',
            start_date TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            max_lines INTEGER NOT NULL CHECK (max_lines >= 1)
        );
        CREATE TABLE team_capacity_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            start_date TEXT NOT NULL,
            lines INTEGER NOT NULL CHECK (lines >= 1),
            UNIQUE(team_id, start_date)
        );
        CREATE TABLE workflows (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
        CREATE TABLE milestones (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            end_date TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            workflow_id INTEGER NOT NULL,
            default_milestone_id INTEGER,
            priority INTEGER NOT NULL DEFAULT 100
        );
        """
    )
    conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '10')")
    conn.execute("INSERT INTO project(id, project_name, start_date) VALUES (1, '', '')")
    conn.execute("INSERT INTO teams(name, max_lines) VALUES ('チームA', 2)")
    conn.execute(
        "INSERT INTO team_capacity_changes(team_id, start_date, lines) VALUES (1, '2026-02-01', 3)"
    )
    conn.execute("INSERT INTO workflows(name) VALUES ('WF1')")
    conn.execute("INSERT INTO jobs(name, workflow_id, priority) VALUES ('J1', 1, 100)")
    conn.commit()
    conn.close()

    db = ProjectDatabase.open_existing(str(path))

    job_cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "tags" in job_cols
    assert db.list_jobs()[0]["tags"] == ""

    # 既存データ（チーム・変更点）は保持される。
    assert db.list_teams() == [{"id": 1, "name": "チームA", "max_lines": 2}]
    assert db.list_team_capacity_changes(1) == [
        {"id": 1, "team_id": 1, "start_date": "2026-02-01", "lines": 3}
    ]

    # CHECKが緩和され、0を保存できる。
    db.update_team(1, "チームA", 0)
    assert db.list_teams()[0]["max_lines"] == 0
    db.update_team_capacity_change(1, "2026-02-01", 0)
    assert db.list_team_capacity_changes(1)[0]["lines"] == 0
    db.close()


def test_opening_pre_optional_priority_schema_preserves_values_and_allows_null(tmp_path):
    """jobs.priorityがNOT NULL DEFAULT 100だった旧バージョン(v11)の.pscheduleを
    開いた際、既存ジョブの値（100を含む）はそのまま保持され、かつ以後は
    NULL（未指定）を保存できるようになること（テーブルを作り直すため）。"""
    import sqlite3

    path = tmp_path / "legacy.pschedule"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE project (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            project_name TEXT NOT NULL DEFAULT '',
            start_date TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE workflows (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
        CREATE TABLE milestones (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            end_date TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            workflow_id INTEGER NOT NULL,
            default_milestone_id INTEGER,
            priority INTEGER NOT NULL DEFAULT 100 CHECK (priority >= 1),
            tags TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '11')")
    conn.execute("INSERT INTO project(id, project_name, start_date) VALUES (1, '', '')")
    conn.execute("INSERT INTO workflows(name) VALUES ('WF1')")
    conn.execute("INSERT INTO jobs(name, workflow_id, priority, tags) VALUES ('J1', 1, 100, '')")
    conn.execute("INSERT INTO jobs(name, workflow_id, priority, tags) VALUES ('J2', 1, 3, '緊急')")
    conn.commit()
    conn.close()

    db = ProjectDatabase.open_existing(str(path))

    jobs = {j["name"]: j for j in db.list_jobs()}
    assert jobs["J1"]["priority"] == 100  # 既存の値は勝手にNULLへ書き換えない
    assert jobs["J2"]["priority"] == 3
    assert jobs["J2"]["tags"] == "緊急"

    # 以後は未指定（NULL）を保存できる。
    new_id = db.add_job("J3", 1, None, None)
    assert db.list_jobs()[[j["id"] for j in db.list_jobs()].index(new_id)]["priority"] is None
    db.close()


def test_opening_pre_optional_lines_schema_preserves_values_and_allows_null(tmp_path):
    """teams.max_lines/team_capacity_changes.linesがNOT NULLだった旧バージョン
    (v14)の.pscheduleを開いた際、既存の値はそのまま保持され、かつ以後は
    NULL（指定なし＝上限を設けない）を保存できるようになること
    （テーブルを作り直すため）。"""
    import sqlite3

    path = tmp_path / "legacy.pschedule"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE project (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            project_name TEXT NOT NULL DEFAULT '',
            start_date TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            max_lines INTEGER NOT NULL CHECK (max_lines >= 0)
        );
        CREATE TABLE team_capacity_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            start_date TEXT NOT NULL,
            lines INTEGER NOT NULL CHECK (lines >= 0),
            UNIQUE(team_id, start_date)
        );
        CREATE TABLE workflows (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
        CREATE TABLE milestones (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            end_date TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '14')")
    conn.execute("INSERT INTO project(id, project_name, start_date) VALUES (1, '', '')")
    conn.execute("INSERT INTO teams(name, max_lines) VALUES ('チームA', 2)")
    conn.execute(
        "INSERT INTO team_capacity_changes(team_id, start_date, lines) VALUES (1, '2026-02-01', 3)"
    )
    conn.commit()
    conn.close()

    db = ProjectDatabase.open_existing(str(path))

    # 既存の値は勝手にNULLへ書き換えない。
    assert db.list_teams() == [{"id": 1, "name": "チームA", "max_lines": 2}]
    assert db.list_team_capacity_changes(1) == [
        {"id": 1, "team_id": 1, "start_date": "2026-02-01", "lines": 3}
    ]

    # 以後はNULL（指定なし）を保存できる。
    new_id = db.add_team("チームB", None)
    assert db.list_teams()[1]["max_lines"] is None
    db.add_team_capacity_change(new_id, "2026-03-01", None)
    assert db.list_team_capacity_changes(new_id)[0]["lines"] is None
    db.close()


# -- ファイルを開く際の検証（gui/db_schema.py の check_openable） -----------------------
#
# open_existing() が、素のsqlite3例外をそのまま伝播させず、原因が分かる
# ProjectDatabaseError にして送出することを確認する（GUI側の「プロジェクトを
# 開けませんでした」ダイアログにそのまま表示されるため）。

def test_opening_a_future_schema_version_is_rejected_with_a_clear_message(tmp_path):
    """回帰テスト: より新しいバージョンのProjectSchedulerで作成されたファイルを
    開こうとすると、警告もエラーも無く開けてしまっていた（未知のカラム・
    テーブルは保持されるが、GUI側は古いスキーマの理解のまま動き続け、保存すると
    新しいバージョンの意味を持つデータが壊れる恐れがあった）。"""
    path = tmp_path / "future.pschedule"
    db = ProjectDatabase.create_new(str(path))
    db.set_project("P", "2025-01-01")
    db.save()
    db.close()

    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE schema_meta SET value = '99' WHERE key = 'schema_version'")
    conn.execute("ALTER TABLE project ADD COLUMN future_col TEXT")
    conn.commit()
    conn.close()

    with pytest.raises(ProjectDatabaseError, match="新しいバージョン"):
        ProjectDatabase.open_existing(str(path))


def test_opening_the_current_schema_version_still_works(tmp_path):
    """境界値: このアプリがまさに書き出すバージョンそのものは、引き続き
    問題なく開けること（>ではなく>=でない一方向の比較を確認する）。"""
    path = tmp_path / "current.pschedule"
    db = ProjectDatabase.create_new(str(path))
    db.save()
    db.close()

    reopened = ProjectDatabase.open_existing(str(path))
    reopened.close()


def test_opening_a_file_without_schema_meta_table_is_rejected_with_a_clear_message(tmp_path):
    """回帰テスト: ProjectSchedulerのファイルではない（別アプリのSQLiteファイル・
    0バイトのファイル等）場合、以前は 'no such table: schema_meta' という
    素のsqlite3例外がそのまま「プロジェクトを開けませんでした」ダイアログに
    出ていた。原因がファイルの中身の問題だと伝わる文言にする。"""
    import sqlite3

    # 別アプリのSQLiteファイル
    other = tmp_path / "other.pschedule"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE foo(x)")
    conn.commit()
    conn.close()
    with pytest.raises(ProjectDatabaseError, match="ProjectScheduler"):
        ProjectDatabase.open_existing(str(other))

    # 0バイトのファイル（sqlite3的には有効な空DBとして開けてしまう）
    empty = tmp_path / "empty.pschedule"
    empty.write_bytes(b"")
    with pytest.raises(ProjectDatabaseError, match="ProjectScheduler"):
        ProjectDatabase.open_existing(str(empty))


def test_opening_a_non_sqlite_file_is_rejected_with_a_clear_message(tmp_path):
    """回帰テスト: 中身がSQLiteですらないファイル（例: テキストファイルに
    .pscheduleの拡張子だけ付けたもの）は、以前は sqlite3.DatabaseError
    ('file is not a database') がそのまま伝播していた。"""
    path = tmp_path / "note.pschedule"
    path.write_text("これはプロジェクトファイルではありません")

    with pytest.raises(ProjectDatabaseError, match="読み込めませんでした"):
        ProjectDatabase.open_existing(str(path))


def test_opening_a_file_with_schema_meta_table_but_no_version_row_is_rejected(tmp_path):
    """回帰テスト: schema_metaテーブルはあるがschema_versionの行が無いファイルは、
    以前は「バージョン1」として扱われ、全migrationがv1から適用されていた。
    現行スキーマの列に対して重複してALTER TABLEしようとして
    'duplicate column name' で例外になっていた（本来のバージョンが1でない
    限り、この巻き戻しは常に壊れる）。"""
    import sqlite3

    path = tmp_path / "norow.pschedule"
    db = ProjectDatabase.create_new(str(path))
    db.save()
    db.close()

    conn = sqlite3.connect(str(path))
    conn.execute("DELETE FROM schema_meta")
    conn.commit()
    conn.close()

    with pytest.raises(ProjectDatabaseError, match="壊れています"):
        ProjectDatabase.open_existing(str(path))
