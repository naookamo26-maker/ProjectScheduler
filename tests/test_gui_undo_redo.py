"""
Undo/Redoの、Qtを介したGUIレベルの選択・フォーカス復元テスト。

headless Qt（QT_QPA_PLATFORM=offscreen）で実際に MainWindow を構築し、
PySide6が別途インストールされていない環境ではスキップする（開発時の
手動確認用スクリプトと同じ考え方、docs/packaging.md参照）。

DB層のUndo/Redoの記録タイミング・粒度そのものは tests/test_undo_redo.py で
Qt非依存に検証済みのため、ここでは「GUI操作の結果、選択行・フォーカスが
実際に画面上のウィジェットへ復元されるか」に絞って確認する。
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6")
pd = pytest.importorskip("pandas")

# 分類: gui（PySide6 + offscreen QApplication が必要。最も重い）
pytestmark = pytest.mark.gui

from PySide6.QtCore import QDate, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402

from gui.db import ProjectDatabase  # noqa: E402
from gui.tab_gantt import GanttTab  # noqa: E402
from gui.widgets_common import row_id, select_row_by_id  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def window(qapp):
    # ここでインポートするとPySide6のimportがモジュール読み込み時に発生し、
    # PySide6未インストール環境でのcollectionを壊すため、fixture内で遅延させる。
    from gui.main import MainWindow

    w = MainWindow()
    w.show()
    qapp.processEvents()
    w._open_database(ProjectDatabase.create_new())
    qapp.processEvents()
    yield w
    # closeEvent は未保存の変更があるとモーダルダイアログで確認を求める
    # （gui/main.py の _confirm_discard_unsaved）。テストではダイアログを
    # 操作できず無限にブロックしてしまうため、後片付けはダイアログを経由
    # しない形で行う。
    w._shutdown_schedule_cache()
    w.db.on_change = None
    w.db.undo_manager = None
    w.db.close()
    # ウィンドウをPython GC任せにせず、この時点でC++側の実体ごと即座に破棄する。
    # GC任せだと（特に多数のテストを連続実行した場合）破棄がpytestプロセス
    # 終了時まで遅延することがあり、その際にQtが子ウィジェット（ワークフロー
    # 一覧等）の破棄に伴うシグナルを発火させ、既に閉じたDBへアクセスして
    # 例外になることがあるため。
    import shiboken6
    w.hide()
    shiboken6.delete(w)
    qapp.processEvents()


def test_team_add_undo_redo_restores_selection(window, qapp):
    """内容と、ツリーの選択（チーム項目）が復元されること。"""
    bi = window.tab_basic_info
    new_id = window.db.add_team("チームテスト", 2)
    bi.refresh_teams()
    bi._select_team_tree_item(new_id)
    bi.teams_tree.setFocus()
    qapp.processEvents()

    window.on_undo()
    qapp.processEvents()
    assert window.db.list_teams() == []
    assert window.undo_manager.can_redo()

    window.on_redo()
    qapp.processEvents()
    assert [t["name"] for t in window.db.list_teams()] == ["チームテスト"]

    item = bi.teams_tree.currentItem()
    assert item is not None
    assert item.data(0, Qt.UserRole) == {"kind": "team", "team_id": new_id}


def test_spinbox_edits_collapse_into_one_undo_step_at_focus_out(window, qapp):
    """回帰テスト: スピンボックスを連続して変更しても、フォーカスが外れるまでは
    Undoが分かれず、外れた時点で1エントリにまとまること。

    DBは変更のたびに更新される（値を変えた直後に保存しても取りこぼさない）が、
    Undoの単位は「フォーカスを得てから外れるまで」でまとめる。"""
    bi = window.tab_basic_info
    window.db.add_team("チームA", 1)
    bi.refresh_all()
    qapp.processEvents()
    top = bi.teams_tree.topLevelItem(0)
    default_child = top.child(0)  # 既定値（開発開始日からの同時ライン数）の行
    spin = bi.teams_tree.itemWidget(default_child, 1)

    steps_before = len(window.undo_manager._undo_stack)
    spin.setFocus()
    qapp.processEvents()
    for _ in range(5):
        spin.stepBy(1)
        qapp.processEvents()

    assert window.db.list_teams()[0]["max_lines"] == 6  # DBは即座に最新
    assert len(window.undo_manager._undo_stack) == steps_before  # まだ記録されない

    bi.project_name_edit.setFocus()  # フォーカスを外して編集を確定
    qapp.processEvents()
    assert len(window.undo_manager._undo_stack) == steps_before + 1

    window.on_undo()
    qapp.processEvents()
    assert window.db.list_teams()[0]["max_lines"] == 1  # 1回のUndoで一気に戻る


def test_focus_is_never_moved_into_cell_widgets_by_undo(window, qapp):
    """回帰テスト: Undo/Redoの復元で、スピンボックスや日付欄へフォーカスが
    移らないこと。

    これらはCtrl+Zを自分のものとして横取りする（QAbstractSpinBox/QLineEditが
    ShortcutOverrideを受け取る）ため、フォーカスが入ると次のCtrl+Zがメニューまで
    届かず、Undoが効かなくなったように見える。"""
    bi = window.tab_basic_info
    window.db.add_team("チームA", 1)
    bi.refresh_all()
    qapp.processEvents()

    top = bi.teams_tree.topLevelItem(0)
    default_child = top.child(0)  # 既定値（開発開始日からの同時ライン数）の行
    spin = bi.teams_tree.itemWidget(default_child, 1)
    spin.setFocus()
    qapp.processEvents()
    spin.stepBy(1)
    qapp.processEvents()
    bi.project_name_edit.setFocus()  # 編集を確定し、フォーカスをツリーの外へ
    qapp.processEvents()

    window.on_undo()
    qapp.processEvents()
    focused = QApplication.focusWidget()
    assert focused is bi.project_name_edit, f"フォーカスが移動している: {type(focused).__name__}"


def test_teams_tree_shows_default_capacity_as_first_undeletable_child(window, qapp):
    """開発開始日からの既定値（teams.max_lines）は、他の変動点と同じ見た目の
    最初の子行として表示され、日付は開発開始日で固定（編集不可）・ライン数は
    インライン編集可能（OptionalSpinBox）で、削除ボタンでは削除できないこと。"""
    bi = window.tab_basic_info
    window.tabs.setCurrentWidget(bi)
    window.db.set_project("P", "2026-01-01")
    team_id = window.db.add_team("チームA", 2)
    bi.refresh_all()
    qapp.processEvents()

    top = bi.teams_tree.topLevelItem(0)
    assert top.childCount() == 1
    default_child = top.child(0)
    assert default_child.text(0) == "2026-01-01"
    assert not (default_child.flags() & Qt.ItemIsEditable)
    assert default_child.data(0, Qt.UserRole) == {"kind": "default_capacity", "team_id": team_id}
    spin = bi.teams_tree.itemWidget(default_child, 1)
    assert spin.value() == 2

    from PySide6.QtWidgets import QMessageBox

    bi.teams_tree.setCurrentItem(default_child)
    qapp.processEvents()
    with patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
        bi._delete_capacity_change_selected()
    assert bi.teams_tree.topLevelItem(0).childCount() == 1  # 既定値は削除されない


def test_teams_tree_shows_unspecified_lines_as_special_value(window, qapp):
    """ライン数「指定なし」（max_lines=None）のチームは、ツリー上のスピン
    ボックスが特殊値表示（specialValueText）になり、optional_value()が
    Noneを返すこと。"""
    bi = window.tab_basic_info
    window.tabs.setCurrentWidget(bi)
    window.db.set_project("P", "2026-01-01")
    window.db.add_team("チームA", None)
    bi.refresh_all()
    qapp.processEvents()

    top = bi.teams_tree.topLevelItem(0)
    default_child = top.child(0)
    spin = bi.teams_tree.itemWidget(default_child, 1)
    assert spin.value() == spin.minimum()
    assert spin.optional_value() is None
    assert spin.text() == "指定なし"


def test_add_team_dialog_defaults_lines_to_unspecified(window, qapp):
    """「＋チーム」ダイアログの既定ライン数は「指定なし」（NULL）であること
    （docs/project_analysis_tab_design.md「新規チームの既定を『指定なし』に」）。"""
    from gui.tab_basic_info import AddTeamDialog

    dialog = AddTeamDialog("新しいチーム", window)
    assert dialog.values() == ("新しいチーム", None)
    dialog.deleteLater()
    qapp.processEvents()


def test_teams_tree_capacity_changes_are_inline_editable_children(window, qapp):
    """期間中の変動点は、チーム項目の第2子以降として表示され、日付・ライン数
    ともにツリー上で直接編集できること（専用ダイアログは廃止）。"""
    bi = window.tab_basic_info
    window.tabs.setCurrentWidget(bi)
    team_id = window.db.add_team("チームA", 2)
    change_id = window.db.add_team_capacity_change(team_id, "2026-03-01", 5)
    bi.refresh_all()
    qapp.processEvents()

    top = bi.teams_tree.topLevelItem(0)
    assert top.childCount() == 2
    child = top.child(1)
    assert child.data(0, Qt.UserRole) == {
        "kind": "capacity_change", "team_id": team_id, "change_id": change_id,
    }
    date_edit = bi.teams_tree.itemWidget(child, 0)
    lines_spin = bi.teams_tree.itemWidget(child, 1)
    assert date_edit is not None and lines_spin is not None
    assert lines_spin.value() == 5

    lines_spin.setValue(9)
    qapp.processEvents()
    assert window.db.list_team_capacity_changes(team_id)[0]["lines"] == 9


def test_add_capacity_change_opens_dialog_for_date_and_lines(window, qapp):
    """「＋ 変動点」は、以前は既定日を自動提案してすぐ追加していたが、
    ダイアログを開いて適用開始日とライン数を入力させてから追加すること。"""
    from gui.tab_basic_info import AddCapacityChangeDialog

    bi = window.tab_basic_info
    window.tabs.setCurrentWidget(bi)
    team_id = window.db.add_team("チームA", 2)
    bi.refresh_all()
    qapp.processEvents()

    bi._select_team_tree_item(team_id)
    qapp.processEvents()

    with patch.object(AddCapacityChangeDialog, "exec", return_value=QDialog.Accepted), \
         patch.object(AddCapacityChangeDialog, "values", return_value=("2026-03-01", 7)):
        bi._add_capacity_change_selected()
    qapp.processEvents()

    changes = window.db.list_team_capacity_changes(team_id)
    assert len(changes) == 1
    assert changes[0]["start_date"] == "2026-03-01"
    assert changes[0]["lines"] == 7


def test_add_capacity_change_selects_new_row_visibly_and_keeps_it_editable(window, qapp):
    """回帰テスト: 「＋ 変動点」追加直後、(1) 余計なツリー再構築が走らず
    （選択・スクロール位置を失わない）、(2) 追加した行が画面内に見えており、
    (3) その場で適用開始日をすぐ変更できること。以前は、追加直後の選択操作
    （setCurrentItem）がセルウィジェットへのフォーカス出入りを誘発し、
    「実際には何も変わっていないのに」並べ替え用の再構築
    （_resort_team_capacity_changes_later）が走ってしまい、選択・スクロール
    位置を失う（＝追加した行を見失う）とともに、直後の日付編集が不安定に
    なる不具合があった。"""
    from gui.tab_basic_info import AddCapacityChangeDialog

    bi = window.tab_basic_info
    window.tabs.setCurrentWidget(bi)
    team_id = window.db.add_team("チームA", 2)
    for i in range(1, 15):
        window.db.add_team_capacity_change(team_id, f"2026-01-{i + 1:02d}", i + 1)
    bi.refresh_all()
    qapp.processEvents()

    bi._select_team_tree_item(team_id)
    qapp.processEvents()
    bi.teams_tree.verticalScrollBar().setValue(bi.teams_tree.verticalScrollBar().maximum())
    qapp.processEvents()

    refresh_calls = []
    original_refresh = bi.refresh_teams
    def counting_refresh():
        refresh_calls.append(1)
        original_refresh()
    bi.refresh_teams = counting_refresh
    try:
        with patch.object(AddCapacityChangeDialog, "exec", return_value=QDialog.Accepted), \
             patch.object(AddCapacityChangeDialog, "values", return_value=("2026-03-01", 9)):
            bi._add_capacity_change_selected()
        qapp.processEvents()
        qapp.processEvents()  # 余計な再構築があれば、その QTimer.singleShot(0, ...) も処理させる

        assert len(refresh_calls) == 1, "追加直後に余計なツリー再構築が走っている"
    finally:
        bi.refresh_teams = original_refresh

    selected = bi.teams_tree.selectedItems()
    assert selected, "追加した変動点が選択されていない"
    data = selected[0].data(0, Qt.UserRole)
    assert data["kind"] == "capacity_change"

    rect = bi.teams_tree.visualItemRect(selected[0])
    viewport_height = bi.teams_tree.viewport().height()
    assert 0 <= rect.top() and rect.bottom() <= viewport_height, "追加直後の項目が画面外（見失っている）"

    # 追加直後、その場で適用開始日を変更できること。
    date_edit = bi.teams_tree.itemWidget(selected[0], 0)
    date_edit.setDate(QDate(2026, 3, 5))
    qapp.processEvents()
    changed = next(c for c in window.db.list_team_capacity_changes(team_id) if c["id"] == data["change_id"])
    assert changed["start_date"] == "2026-03-05"


def test_override_combo_keeps_an_inconsistent_milestone_instead_of_wiping_it(window, qapp):
    """回帰テスト: 設定済みのマイルストーンが「先行タスクより早い締切」に
    なってしまった場合でも、コンボの選択肢から外さないこと。

    外すと make_fk_combo の findData が -1 になり、コンボは先頭の「（既定）」に
    フォールバックする。その結果 (1) DBの実際の値と違うものを表示し、
    (2) 同じ行の別の欄（日数・チーム）を触った瞬間に _on_override_changed が
    その表示値(None)を書き戻し、マイルストーン上書きを無言で消してしまう。"""
    jt = window.tab_jobs
    window.tabs.setCurrentWidget(jt)
    db = window.db

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

    # タブ1側の操作を模して、後期MSを中期MSより前へ動かす（不整合を作る）
    db.update_milestone(ms_late, "後期MS", "2026-02-15")
    jt.refresh_jobs(select_id=job)
    qapp.processEvents()

    table = jt.override_table
    row_of_t2 = next(r for r in range(table.rowCount())
                     if table.item(r, 0) and table.item(r, 0).text() == "タスク2")
    ms_combo = table.cellWidget(row_of_t2, 3)

    # DBの実際の値がそのまま選ばれている（「（既定）」へフォールバックしない）
    assert ms_combo.currentData() == ms_late
    assert "後期MS" in ms_combo.currentText()

    # この行の日数だけを変えても、マイルストーン上書きが None で潰されない。
    # 正しい値が読み戻されるため、既存の enforce_milestone_floor が働いて
    # 先行タスクの中期MSまで引き上げられる（通知ダイアログ付き）。
    from PySide6.QtWidgets import QMessageBox
    with patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
        table.cellWidget(row_of_t2, 2).setValue(7)
        qapp.processEvents()
    stored = db._conn.execute(
        "SELECT milestone_id FROM job_task_overrides WHERE job_id=? AND workflow_task_id=?",
        (job, t2),
    ).fetchone()
    assert stored["milestone_id"] is not None, "マイルストーン上書きが無言で消えている"
    assert stored["milestone_id"] == ms_mid  # 先行タスクに合わせて引き上げ


def test_selecting_a_capacity_change_child_resolves_to_its_parent_team(window, qapp):
    """回帰テスト: 変動点（子）を選択した状態で「－ チーム」「＋ 変動点」を
    押しても、親のチームを対象として動作すること（tab_jobs.pyのdep_treeと
    同じ「子は親へ辿る」考え方）。ここでは _resolve_team_item の結果と、
    その選択状態が capture_ui_state/restore_ui_state で往復することを確認する。"""
    bi = window.tab_basic_info
    window.tabs.setCurrentWidget(bi)
    team_id = window.db.add_team("チームA", 2)
    window.db.add_team_capacity_change(team_id, "2026-03-01", 5)
    bi.refresh_all()
    qapp.processEvents()

    child = bi.teams_tree.topLevelItem(0).child(1)  # 第2子＝実際の変動点（第1子は既定値）
    bi.teams_tree.setCurrentItem(child)
    qapp.processEvents()

    _item, data = bi._resolve_team_item(bi.teams_tree.currentItem())
    assert data == {"kind": "team", "team_id": team_id}

    state = bi.capture_ui_state()
    assert state["team_selected"]["kind"] == "capacity_change"
    bi.teams_tree.setCurrentItem(None)
    bi.restore_ui_state(state)
    qapp.processEvents()
    restored = bi.teams_tree.currentItem()
    assert restored is not None
    assert restored.data(0, Qt.UserRole) == state["team_selected"]

    # 変動点行の日付欄にフォーカスが残ったままだと、Undoセッション
    # （focusInEventで開始）が閉じられないまま終わってしまうため、
    # 明示的にフォーカスを外して確定させる（他のテストと同じ後始末）。
    bi.project_name_edit.setFocus()
    qapp.processEvents()


def test_workflow_task_add_is_single_undo_step_and_restores_canvas_selection(window, qapp):
    wf_tab = window.tab_workflows
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    qapp.processEvents()

    scene = wf_tab.current_scene
    assert scene is not None

    stack_size_before = len(window.undo_manager._undo_stack)
    node = scene.add_task("タスクA", team_id, 3, 0, 0)
    node.setSelected(True)
    qapp.processEvents()

    # add_task呼び出し全体（DB書き込みはadd_workflow_task1件のみ。
    # auto_arrangeは座標をQt側で更新するだけでDBには書き込まない）が
    # Undo1件にまとまっていること。
    assert len(window.undo_manager._undo_stack) == stack_size_before + 1
    assert window.db.list_workflow_tasks(wf_id) != []

    window.on_undo()
    qapp.processEvents()
    assert window.db.list_workflow_tasks(wf_id) == []

    window.on_redo()
    qapp.processEvents()
    tasks = window.db.list_workflow_tasks(wf_id)
    assert len(tasks) == 1
    # ノードの選択（視覚的なハイライト）は復元される。フォーカス（キーボード
    # 入力の宛先）はUndo/Redoの対象外——スピンボックス等と異なりビューは
    # Ctrl+Zを横取りしないため実害は無いが、設計として意図的に外している。
    restored_scene = wf_tab.current_scene
    restored_node = restored_scene.nodes[tasks[0]["id"]]
    assert restored_node.isSelected()


def test_canvas_selection_survives_switching_tabs_away_and_back(window, qapp):
    """回帰テスト: Undo/Redoとは無関係に、単に別のタブへ移って戻ってきただけでも
    ノードの選択が保たれること。

    ワークフローを選択するたびに WorkflowGraphScene を作り直すため
    （_on_selection_changed）、タブ切り替え時の refresh_choices() が選択状態を
    明示的に持ち回らないと、ノードをクリックしただけで選んだ選択が、他のタブを
    見て戻ってくるたびに消えてしまう。"""
    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    node = wf_tab.current_scene.add_task("タスクA", team_id, 3, 0, 0)
    qapp.processEvents()
    scene = wf_tab.current_scene
    scene.nodes[node.workflow_task_id].setSelected(True)
    qapp.processEvents()

    window.tabs.setCurrentWidget(window.tab_jobs)
    qapp.processEvents()
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    restored_scene = wf_tab.current_scene
    assert restored_scene.nodes[node.workflow_task_id].isSelected()


def test_undo_and_redo_from_another_tab_return_to_the_edited_tab(window, qapp):
    """回帰テスト: 操作したタブとは別のタブに移動してからUndo/Redoしても、
    どちらも「操作を行ったタブ」へ戻り、その時の選択を復元すること。

    Redo側は、Ctrl+Zを押した瞬間の画面ではなく操作直後の画面を復元する必要が
    ある（gui/undo_manager.py の before/after 対称モデル）。"""
    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    first = wf_tab.current_scene.add_task("タスク1", team_id, 1, 0, 0)
    qapp.processEvents()
    # 「タスク1を選択した状態」で次の操作を行う＝これがUndoで戻るべき画面。
    first.setSelected(True)
    wf_tab.view.setFocus()
    qapp.processEvents()
    wf_tab.current_scene.add_task("タスク2", team_id, 1, 200, 0)
    qapp.processEvents()

    workflows_index = window.tabs.indexOf(wf_tab)
    window.tabs.setCurrentWidget(window.tab_jobs)
    qapp.processEvents()

    window.on_undo()
    qapp.processEvents()
    assert window.tabs.currentIndex() == workflows_index
    assert len(window.db.list_workflow_tasks(wf_id)) == 1
    selected = [tid for tid, n in wf_tab.current_scene.nodes.items() if n.isSelected()]
    assert selected == [first.workflow_task_id]

    window.tabs.setCurrentWidget(window.tab_jobs)
    qapp.processEvents()
    window.on_redo()
    qapp.processEvents()
    assert window.tabs.currentIndex() == workflows_index
    assert len(window.db.list_workflow_tasks(wf_id)) == 2


def test_tab_changed_is_connected_only_once_across_reopens(window, qapp):
    """回帰テスト: プロジェクトを開き直すたびに currentChanged の接続が
    累積しないこと。累積すると、タブ切り替え1回でジョブタブの
    refresh_choices()（DBへ書き込む sync_dependency_templates を含む）が
    開いた回数だけ走ってしまう。"""
    from gui.main import MainWindow

    calls = {"n": 0}
    original = MainWindow._on_tab_changed

    def spy(self, index):
        calls["n"] += 1
        return original(self, index)

    with patch.object(MainWindow, "_on_tab_changed", spy):
        for _ in range(3):
            window._open_database(ProjectDatabase.create_new())
            qapp.processEvents()
            calls["n"] = 0
            window.tabs.setCurrentIndex(1)
            qapp.processEvents()
            assert calls["n"] == 1
            window.tabs.setCurrentIndex(0)
            qapp.processEvents()


def test_multi_select_delete_shows_a_single_aggregated_confirmation(window, qapp):
    """回帰テスト: 参照されているタスクを複数選択してDeleteした場合、ノードの
    数だけ確認ダイアログが繰り返されず、1回だけ・合計件数をまとめた文面で
    出ること。単一削除（右クリック「削除」相当）は従来通り単数形の文面のまま
    であることも確認する。"""
    from PySide6.QtWidgets import QMessageBox

    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    n1 = scene.add_task("タスク1", team_id, 1, 0, 0)
    qapp.processEvents()
    n2 = wf_tab.current_scene.add_task("タスク2", team_id, 1, 200, 0)
    qapp.processEvents()
    n3 = wf_tab.current_scene.add_task("タスク3", team_id, 1, 400, 0)
    qapp.processEvents()
    job_id = window.db.add_job("J1", wf_id, None, 100)
    # タスク1・タスク2を参照ありの状態にする（タスク3は参照なしのまま）。
    window.db.upsert_job_task_override(job_id, n1.workflow_task_id, is_active=False)
    window.db.upsert_job_task_override(job_id, n2.workflow_task_id, is_active=False)

    # -- 単一削除（右クリック「削除」相当）: 従来通り単数形・1回だけ ------------------
    scene = wf_tab.current_scene
    messages = []
    with patch.object(QMessageBox, "question",
                       side_effect=lambda *a, **k: (messages.append(a[2]), QMessageBox.Yes)[1]):
        scene.delete_node(scene.nodes[n1.workflow_task_id])
    qapp.processEvents()
    assert len(messages) == 1
    assert messages[0].startswith("このタスクは ")

    # -- 複数選択削除: 参照ありのタスク2＋参照なしのタスク3をまとめてDelete ----------
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    scene = wf_tab.current_scene
    scene.nodes[n2.workflow_task_id].setSelected(True)
    scene.nodes[n3.workflow_task_id].setSelected(True)
    qapp.processEvents()

    messages2 = []
    with patch.object(QMessageBox, "question",
                       side_effect=lambda *a, **k: (messages2.append(a[2]), QMessageBox.Yes)[1]):
        wf_tab.view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
    qapp.processEvents()

    assert len(messages2) == 1, "選択ノードの数だけダイアログが繰り返されている"
    assert "選択した2件のタスクは" in messages2[0]
    assert window.db.list_workflow_tasks(wf_id) == []


def test_deleting_a_task_bridges_its_predecessors_and_successors(window, qapp):
    """タスクA→B→Cという流れでBを削除すると、A→Cの依存関係が新設され、
    前後関係が保たれること。Undo1回で橋渡し込みの状態が元に戻ることも確認する。"""
    from PySide6.QtWidgets import QMessageBox

    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    task_a = scene.add_task("A", team_id, 1, 0, 0)
    qapp.processEvents()
    task_b = wf_tab.current_scene.add_task("B", team_id, 1, 200, 0)
    qapp.processEvents()
    task_c = wf_tab.current_scene.add_task("C", team_id, 1, 400, 0)
    qapp.processEvents()
    scene = wf_tab.current_scene
    scene.try_add_edge(scene.nodes[task_a.workflow_task_id], scene.nodes[task_b.workflow_task_id])
    qapp.processEvents()
    scene = wf_tab.current_scene
    scene.try_add_edge(scene.nodes[task_b.workflow_task_id], scene.nodes[task_c.workflow_task_id])
    qapp.processEvents()

    def pairs():
        id_to_name = {t["id"]: t["name"] for t in window.db.list_workflow_tasks(wf_id)}
        return sorted(
            (id_to_name.get(d["predecessor_task_id"]), id_to_name.get(d["successor_task_id"]))
            for d in window.db.list_task_dependencies(wf_id)
        )

    assert pairs() == [("A", "B"), ("B", "C")]

    scene = wf_tab.current_scene
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        scene.delete_node(scene.nodes[task_b.workflow_task_id])
    qapp.processEvents()

    assert pairs() == [("A", "C")]  # Bが消え、A→Cへ橋渡しされる
    assert len(window.db.list_workflow_tasks(wf_id)) == 2

    window.on_undo()
    qapp.processEvents()
    assert pairs() == [("A", "B"), ("B", "C")]  # 橋渡し込みで1回のUndoで復元


def test_deleting_a_task_does_not_duplicate_an_existing_bridge(window, qapp):
    """A→B→D、A→C→D、かつA→Dも直接存在する状態でBを削除しても、既に存在する
    A→Dへ重複した依存関係を作ろうとしてエラーにならないこと。"""
    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    nodes = {}
    for name in ("A", "B", "C", "D"):
        nodes[name] = wf_tab.current_scene.add_task(name, team_id, 1, 0, 0)
        qapp.processEvents()
    scene = wf_tab.current_scene
    for pred, succ in [("A", "B"), ("B", "D"), ("A", "C"), ("C", "D"), ("A", "D")]:
        scene = wf_tab.current_scene
        scene.try_add_edge(scene.nodes[nodes[pred].workflow_task_id], scene.nodes[nodes[succ].workflow_task_id])
        qapp.processEvents()

    from PySide6.QtWidgets import QMessageBox
    scene = wf_tab.current_scene
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        scene.delete_node(scene.nodes[nodes["B"].workflow_task_id])
    qapp.processEvents()

    id_to_name = {t["id"]: t["name"] for t in window.db.list_workflow_tasks(wf_id)}
    result = sorted(
        (id_to_name.get(d["predecessor_task_id"]), id_to_name.get(d["successor_task_id"]))
        for d in window.db.list_task_dependencies(wf_id)
    )
    assert result == [("A", "C"), ("A", "D"), ("C", "D")]


def test_multi_select_delete_bridges_across_chained_deletions(window, qapp):
    """A→B→C→D→EからB・Dをまとめて削除すると、連鎖的にA→C→Eへ橋渡しされること
    （Bの処理でA→Cが繋がり、続くDの処理はその時点の後続関係C→Eを見るため）。"""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QMessageBox

    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    nodes = {}
    for name in ("A", "B", "C", "D", "E"):
        nodes[name] = wf_tab.current_scene.add_task(name, team_id, 1, 0, 0)
        qapp.processEvents()
    for pred, succ in [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")]:
        scene = wf_tab.current_scene
        scene.try_add_edge(scene.nodes[nodes[pred].workflow_task_id], scene.nodes[nodes[succ].workflow_task_id])
        qapp.processEvents()

    scene = wf_tab.current_scene
    scene.nodes[nodes["B"].workflow_task_id].setSelected(True)
    scene.nodes[nodes["D"].workflow_task_id].setSelected(True)
    qapp.processEvents()
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        wf_tab.view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
    qapp.processEvents()

    id_to_name = {t["id"]: t["name"] for t in window.db.list_workflow_tasks(wf_id)}
    result = sorted(
        (id_to_name.get(d["predecessor_task_id"]), id_to_name.get(d["successor_task_id"]))
        for d in window.db.list_task_dependencies(wf_id)
    )
    assert result == [("A", "C"), ("C", "E")]


def test_multi_select_delete_on_canvas_is_a_single_undo_step(window, qapp):
    """回帰テスト: キャンバスで複数選択してDeleteした場合、選択項目ごとに
    Undoが分かれず1回のUndoで全部戻ること。あわせて、タスクとその依存線を
    同時に選択しても、同じ依存線を二重に削除して落ちないこと。"""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QMessageBox

    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    first = wf_tab.current_scene.add_task("タスク1", team_id, 1, 0, 0)
    qapp.processEvents()
    second = wf_tab.current_scene.add_task("タスク2", team_id, 1, 200, 0)
    qapp.processEvents()
    scene = wf_tab.current_scene
    scene.try_add_edge(scene.nodes[first.workflow_task_id], scene.nodes[second.workflow_task_id])
    qapp.processEvents()
    assert len(window.db.list_workflow_tasks(wf_id)) == 2
    assert len(window.db.list_task_dependencies(wf_id)) == 1

    scene = wf_tab.current_scene
    for node in scene.nodes.values():
        node.setSelected(True)
    for edge in scene.edges.values():  # タスクとその依存線を同時に選択する
        edge.setSelected(True)
    qapp.processEvents()

    steps_before = len(window.undo_manager._undo_stack)
    # 参照があるタスクの削除は確認ダイアログを出すため、常に「はい」を返させる。
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        wf_tab.view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
    qapp.processEvents()

    assert window.db.list_workflow_tasks(wf_id) == []
    assert len(window.undo_manager._undo_stack) - steps_before == 1

    window.on_undo()
    qapp.processEvents()
    assert len(window.db.list_workflow_tasks(wf_id)) == 2
    assert len(window.db.list_task_dependencies(wf_id)) == 1


def test_opening_another_project_while_editing_does_not_touch_a_closed_db(window, qapp):
    """回帰テスト: 入力欄にフォーカスがある状態で別プロジェクトを開いても、
    閉じたDBへの書き込みが発生しないこと。

    タブの差し替えでフォーカスが外れると editingFinished 等が発火し、旧タブが
    自分の持つDBへ書き込もうとする。旧DBを先に閉じていると
    「Cannot operate on a closed database」になる（Qtがスロット内の例外を
    握りつぶすため、画面上は無害に見えてしまう）。"""
    errors = []

    def record(exc_type, exc_value, _tb):
        errors.append(f"{exc_type.__name__}: {exc_value}")

    name_edit = window.tab_basic_info.project_name_edit
    name_edit.setFocus()
    name_edit.setText("編集中のプロジェクト名")
    qapp.processEvents()

    original_hook = sys.excepthook
    sys.excepthook = record
    try:
        window._open_database(ProjectDatabase.create_new())
        qapp.processEvents()
    finally:
        sys.excepthook = original_hook

    assert errors == []


def test_override_days_edit_keeps_its_spinbox_alive(window, qapp):
    """回帰テスト: ジョブのタスク上書きで日数を変えても、表全体が作り直されない
    こと。

    作り直すと、▲で連続操作している最中にスピンボックスごと差し替わって
    フォーカスが飛び、続けて操作できないうえ、フォーカス単位でまとめている
    Undoの区切りも途切れてしまう。他の行に影響するマイルストーンの変更時だけ
    作り直す。"""
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    window.db.add_workflow_task(wf_id, "タスク1", team_id, 3)
    job_id = window.db.add_job("J1", wf_id, None, 100)
    window.tabs.setCurrentWidget(window.tab_jobs)
    window.tab_jobs.refresh_jobs(select_id=job_id)
    qapp.processEvents()

    spin = window.tab_jobs.override_table.cellWidget(0, 2)
    spin.setFocus()
    qapp.processEvents()
    steps_before = len(window.undo_manager._undo_stack)
    for _ in range(3):
        spin.stepBy(1)
        qapp.processEvents()

    assert window.tab_jobs.override_table.cellWidget(0, 2) is spin, "スピンボックスが作り直されている"
    assert QApplication.focusWidget() is spin, "編集中にフォーカスが外れている"
    assert len(window.undo_manager._undo_stack) == steps_before  # 編集中は記録しない

    window.tab_jobs.jobs_section.table.setFocus()  # 編集を確定
    qapp.processEvents()
    assert len(window.undo_manager._undo_stack) == steps_before + 1

    window.on_undo()
    qapp.processEvents()
    rows = window.db.list_job_tasks_with_overrides(job_id)
    assert rows[0]["override_days"] is None  # 1回のUndoで上書きが消える


def test_task_tag_edit_persists_and_filters_jobs(window, qapp):
    """タスク上書き表の「タグ」列（8列目、タスク タグ）を編集すると
    job_task_overrides.tags に保存され、ジョブ タグと同じ正規化（前後の
    空白除去・重複排除・「, 」区切り）を経て表示に書き戻ること。また、
    「絞り込み」のタスク タグフィルタで、そのタグを持つタスクを含む
    ジョブだけに絞り込めること（他の項目を上書きしていないジョブでも
    タスク タグだけで上書き行が作られ、絞り込み対象になる）。"""
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    window.db.add_workflow_task(wf_id, "タスク1", team_id, 3)
    job1 = window.db.add_job("J1", wf_id, None, 100)
    job2 = window.db.add_job("J2", wf_id, None, 100)  # タスク タグを付けない対照
    window.tabs.setCurrentWidget(window.tab_jobs)
    window.tab_jobs.refresh_jobs(select_id=job1)
    qapp.processEvents()

    table = window.tab_jobs.override_table
    assert table.horizontalHeaderItem(7).text() == "タグ"
    table.item(0, 7).setText(" 確認 ,レビュー,, 確認")
    qapp.processEvents()

    rows = window.db.list_job_tasks_with_overrides(job1)
    assert rows[0]["tags"] == "確認, レビュー"
    assert table.item(0, 7).text() == "確認, レビュー"  # 正規化した表記へ書き戻る

    # 絞り込みの選択肢は（ジョブ タグと同様）refresh_jobs() 時にDBから
    # 作り直される。タグ編集直後は即座には反映されないため、タブの
    # 切り替え等で自然に起きる再読み込みを明示的に呼ぶ。
    window.tab_jobs.refresh_jobs(select_id=job1)
    qapp.processEvents()

    task_tag_filter = window.tab_jobs.task_tag_filter
    assert set(task_tag_filter._checks.keys()) == {"確認", "レビュー", None}
    task_tag_filter._checks[None].setChecked(False)  # 「（タスク タグなし）」を外す
    qapp.processEvents()

    jobs_table = window.tab_jobs.jobs_section.table
    visible_job_ids = {row_id(jobs_table, row) for row in range(jobs_table.rowCount())}
    assert visible_job_ids == {job1}
    assert job2 not in visible_job_ids


def test_gantt_tab_reports_validation_errors_without_a_modal(window, qapp):
    """未完成なプロジェクトでガントチャートタブに切り替えても、モーダル
    ダイアログではなくタブ内の表示でエラーを知らせること。

    refresh_choices() は「タブが表示されるたび」「Undo/Redoで表示を作り直す
    たび」に自動的に呼ばれるため、ダイアログにするとプロジェクトが未完成な
    間ずっと操作に割り込むことになる（headlessテストでは応答できず停止する）。"""
    window.tabs.setCurrentWidget(window.tab_gantt)
    qapp.processEvents()

    assert window.tab_gantt._result_df is None
    assert "解決してください" in window.tab_gantt.error_label.text()


def _add_many_jobs(db, count):
    """ジョブ一覧が画面に収まりきらない件数のプロジェクトを作る。"""
    db.set_project("大量ジョブテスト", "2026-01-05")
    team_id = db.add_team("チームA", 3)
    ms_id = db.add_milestone("マイルストーン1", "2027-12-31")
    wf_id = db.add_workflow("WF1")
    db.add_workflow_task(wf_id, "タスク", team_id, 3)
    other_wf_id = db.add_workflow("WF2")
    db.add_workflow_task(other_wf_id, "タスク", team_id, 3)
    for i in range(count):
        db.add_job(f"ジョブ{i:04d}", wf_id, ms_id, 100)
    return wf_id, other_wf_id, ms_id


def test_job_table_only_builds_widgets_for_visible_rows(window, qapp):
    """ジョブ一覧のセルウィジェットは、画面に見えている行の分だけ作ること。

    全行に QComboBox / QSpinBox を実体として置くと、行数に比例してタブを開く
    のに時間がかかる（1,916ジョブで約19秒かかっていた）。見えていない行は
    読み取り専用のテキスト表示で代替する。"""
    _add_many_jobs(window.db, 120)
    window.tabs.setCurrentWidget(window.tab_jobs)
    qapp.processEvents()

    tab = window.tab_jobs
    table = tab.jobs_section.table
    assert table.rowCount() == 120
    assert len(tab._materialized_rows) < table.rowCount(), "全行にウィジェットを作っている"

    # 可視範囲の行にはウィジェットがあり、範囲外の行には無いこと
    first, last = tab._visible_job_row_range()
    for row in range(first, last + 1):
        assert table.cellWidget(row, 1) is not None
        assert table.cellWidget(row, 3) is not None
    for row in tab._materialized_rows:
        assert first <= row <= last


def test_job_table_shows_the_same_values_with_and_without_widgets(window, qapp):
    """セルウィジェットが無い行も、あるときと同じ値を表示していること。"""
    _add_many_jobs(window.db, 120)
    window.tabs.setCurrentWidget(window.tab_jobs)
    qapp.processEvents()

    tab = window.tab_jobs
    table = tab.jobs_section.table
    workflow_names = {w["id"]: w["name"] for w in window.db.list_workflows()}
    jobs = {j["id"]: j for j in window.db.list_jobs()}

    checked_with_widget = checked_without_widget = 0
    for row in range(table.rowCount()):
        job = jobs[row_id(table, row)]
        combo = table.cellWidget(row, 1)
        if combo is not None:
            assert combo.currentData() == job["workflow_id"]
            assert table.cellWidget(row, 3).value() == job["priority"]
            checked_with_widget += 1
        else:
            assert table.item(row, 1).text() == workflow_names[job["workflow_id"]]
            assert table.item(row, 3).text() == str(job["priority"])
            checked_without_widget += 1
    assert checked_with_widget and checked_without_widget


def test_job_table_builds_widgets_when_scrolled_into_view(window, qapp):
    """スクロールで見えるようになった行には、その時点でウィジェットを作ること。"""
    _add_many_jobs(window.db, 120)
    window.tabs.setCurrentWidget(window.tab_jobs)
    qapp.processEvents()

    tab = window.tab_jobs
    table = tab.jobs_section.table
    scrollbar = table.verticalScrollBar()
    assert scrollbar.maximum() > 0, "スクロールできる件数になっていない"

    last_row = table.rowCount() - 1
    assert table.cellWidget(last_row, 1) is None  # まだ見えていない

    scrollbar.setValue(scrollbar.maximum())
    qapp.processEvents()
    assert table.cellWidget(last_row, 1) is not None

    first, last = tab._visible_job_row_range()
    for row in range(first, last + 1):
        assert table.cellWidget(row, 1) is not None


def test_editing_a_job_survives_scrolling_out_of_view(window, qapp):
    """編集した行を画面外へスクロールして戻しても、編集後の値が表示されること
    （ウィジェットを作り直す際に古い値を使ってしまわないこと）。"""
    wf_id, other_wf_id, _ms_id = _add_many_jobs(window.db, 120)
    window.tabs.setCurrentWidget(window.tab_jobs)
    qapp.processEvents()

    tab = window.tab_jobs
    table = tab.jobs_section.table
    job_id = row_id(table, 0)
    combo = table.cellWidget(0, 1)
    combo.setCurrentIndex(combo.findData(other_wf_id))
    qapp.processEvents()

    assert {j["id"]: j["workflow_id"] for j in window.db.list_jobs()}[job_id] == other_wf_id

    scrollbar = table.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum()); qapp.processEvents()
    scrollbar.setValue(0); qapp.processEvents()

    assert table.cellWidget(0, 1).currentData() == other_wf_id


def test_focused_job_row_keeps_its_widgets_while_scrolling(window, qapp):
    """入力フォーカスのある行のウィジェットは、画面外へ出ても外さないこと。

    bind_undo_session はフォーカスの出入りでUndo単位を開閉するため、単位を
    開いたままウィジェットを破棄すると、閉じられないまま残ってしまう。"""
    _add_many_jobs(window.db, 120)
    window.tabs.setCurrentWidget(window.tab_jobs)
    qapp.processEvents()

    tab = window.tab_jobs
    table = tab.jobs_section.table
    priority_spin = table.cellWidget(0, 3)
    priority_spin.setFocus()
    qapp.processEvents()
    assert priority_spin.hasFocus()

    scrollbar = table.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
    qapp.processEvents()

    assert table.cellWidget(0, 3) is priority_spin, "編集中の行のウィジェットが破棄された"

    # フォーカスを持ったままDBを閉じると、bind_undo_session が開いたUndo単位を
    # 閉じられないまま破棄されることになる（docs/architecture.md 参照）。
    # 後片付けでそこへ入らないよう、テストの側でフォーカスを外しておく。
    priority_spin.clearFocus()
    qapp.processEvents()


def _build_schedulable_project(db):
    """ガントチャートを生成できる最小構成（1チーム・1ワークフロー・1ジョブ）。"""
    db.set_project("スケジュールテスト", "2026-01-05")
    team_id = db.add_team("チームA", 2)
    ms_id = db.add_milestone("マイルストーン1", "2026-06-30")
    wf_id = db.add_workflow("WF1")
    db.add_workflow_task(wf_id, "タスク", team_id, 3)
    db.add_job("ジョブ1", wf_id, ms_id, 100)
    return team_id, ms_id, wf_id


def _wait_for_schedule(window, qapp, timeout_sec=15.0):
    """ワーカースレッドのスケジューリング完了までイベントを回して待つ。"""
    deadline = time.monotonic() + timeout_sec
    while window.tab_gantt._result_df is None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()


def _wait_for_search_debounce(window, qapp, timeout_sec=2.0):
    """ジョブ名検索欄のデバウンスタイマー（gui/tab_gantt.py の
    _search_debounce_timer）が発火するまでイベントを回して待つ。"""
    timer = window.tab_gantt._search_debounce_timer
    deadline = time.monotonic() + timeout_sec
    while timer.isActive() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()


def test_gantt_tab_computes_schedule_in_background(window, qapp):
    """スケジューリングはワーカースレッドで実行され、完了後に結果が反映されること。

    タスク数が増えるとスケジューリングは数秒かかるため、GUIスレッドで同期実行
    すると、タブを開くたびにその間ウィンドウ全体が固まってしまう。"""
    _build_schedulable_project(window.db)

    window.tabs.setCurrentWidget(window.tab_gantt)
    # まだイベントを回していないので、この時点では結果は返ってきていない
    assert window.tab_gantt._result_df is None
    assert "計算中" in window.schedule_summary_label.text()

    _wait_for_schedule(window, qapp)
    assert window.tab_gantt._result_df is not None
    assert len(window.tab_gantt._result_df) == 1
    assert "件のタスクを生成しました" in window.schedule_summary_label.text()


def test_gantt_tab_reuses_result_until_the_db_changes(window, qapp):
    """DBの内容が変わっていない間は、タブを開き直してもスケジューリングを
    やり直さないこと（タブを行き来するだけで毎回数秒かかるのを防ぐ）。"""
    _build_schedulable_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)

    seq_before = window.schedule_cache._request_seq
    window.tab_gantt.refresh_choices()
    qapp.processEvents()
    assert window.schedule_cache._request_seq == seq_before  # 再計算していない
    assert window.tab_gantt._result_df is not None

    window.db.add_team("チームB", 1)  # 内容が変わったら計算し直す
    window.tab_gantt.refresh_choices()
    assert window.schedule_cache._request_seq == seq_before + 1
    _wait_for_schedule(window, qapp)
    assert window.tab_gantt._result_df is not None


def test_switching_to_jobs_tab_and_back_does_not_force_a_recomputation(window, qapp):
    """回帰テスト: ジョブタブに切り替えるたびに sync_dependency_templates() が
    呼ばれる（gui/tab_jobs.py の refresh_choices）。この関数は以前、実際には
    何も変わらなくても無条件に _commit() していたため、DB内容を一切変えて
    いないのに db.revision だけが進んでいた。ガントチャートタブはその
    revision が変わったことを「内容が変わった」と誤認し、ジョブタブへ
    寄り道するだけで毎回スケジューリングをやり直していた
    （gui/db.py の sync_dependency_templates 参照）。"""
    _build_schedulable_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)

    seq_before = window.schedule_cache._request_seq
    revision_before = window.db.revision

    window.tabs.setCurrentWidget(window.tab_jobs)
    qapp.processEvents()
    assert window.db.revision == revision_before  # 何も変わっていない

    window.tabs.setCurrentWidget(window.tab_gantt)
    qapp.processEvents()
    assert window.schedule_cache._request_seq == seq_before  # 再計算していない
    assert window.tab_gantt._result_df is not None


def test_schedule_completing_after_a_concurrent_edit_does_not_mask_it(window, qapp):
    """回帰テスト: 計算がワーカースレッドで走っている間に他タブでDBを編集すると、
    完了した結果には「要求した時点」のrevisionを刻むこと。

    以前は計算が完了した時点の db.revision を刻んでいたため、計算中の編集で
    revision が先に進んでいると、まだ反映されていない編集を「反映済み」と
    誤認していた——次にこのタブへ切り替えても再計算がスキップされ、編集前の
    古い結果がそのまま「最新」として画面に残り続けていた。"""
    _build_schedulable_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    # ここではまだイベントを一度も回していない（＝ワーカースレッドはまだ
    # 起動しただけで、Pythonコードとしてはまだ何も実行していない）ため、
    # 次の編集は確実に「要求後・完了前」に割り込む。
    assert window.tab_gantt._result_df is None
    requested_revision = window.schedule_cache._pending_revision
    assert requested_revision == window.db.revision

    window.db.add_team("後から追加したチーム", 1)
    assert window.db.revision != requested_revision

    _wait_for_schedule(window, qapp)
    # 結果には要求時点のrevisionが刻まれ、完了時点（編集後）の値ではないこと。
    assert window.schedule_cache._computed_revision == requested_revision
    assert window.schedule_cache._computed_revision != window.db.revision

    # 食い違いが検知され、次に切り替えたときは再計算されること。
    seq_before = window.schedule_cache._request_seq
    window.tab_gantt.refresh_choices()
    assert window.schedule_cache._request_seq == seq_before + 1
    _wait_for_schedule(window, qapp)
    assert window.schedule_cache._computed_revision == window.db.revision


def test_shutdown_waits_for_every_in_flight_worker_thread(window, qapp):
    """回帰テスト: 配置コントロールを連続して操作する等、結果が返る前に
    次々と新しい要求を出すと、複数のワーカースレッドが同時に実行中の状態に
    なり得る（_start_worker は古いスレッドを止めずに放置する方針のため）。

    以前は self._thread に最後の1本しか保持していなかったため、shutdown()
    （ウィンドウを閉じる際に呼ばれる）がそれより前の実行中スレッドを待たずに
    戻ってしまい、"QThread: Destroyed while thread is still running" という
    形でクラッシュしうる状態だった。このワーカー管理は ScheduleCache
    （gui/schedule_cache.py）へ移した。"""
    _build_schedulable_project(window.db)
    tab = window.tab_gantt
    cache = window.schedule_cache

    # イベントを一度も回さずに複数回 refresh_choices() を呼び、前の要求が
    # 完了する前に次の要求を出す（DBを毎回変えて再計算の対象にする）。
    for i in range(3):
        window.db.add_team(f"チーム{i}", 1)
        tab.refresh_choices()
    assert len(cache._threads) == 3  # 3本とも実行中（または実行待ち）として追跡されている

    cache.shutdown()
    assert cache._threads == []


def test_gantt_chart_draws_overrun_tasks_with_a_red_border(window, qapp):
    """締切に間に合わないタスクのバーは、赤く太い枠線で描かれること。

    塗りつぶしはチーム／ワークフローの色分けをそのまま残し、枠線だけで
    「間に合っていない」を重ねて表す。"""
    from gui.gantt_view import _NORMAL_BORDER_WIDTH, _OVERRUN_BORDER_COLOR, _OVERRUN_BORDER_WIDTH

    window.db.set_project("枠線テスト", "2026-01-05")
    team_id = window.db.add_team("チームA", 1)  # 同時1本のみ
    ms_id = window.db.add_milestone("マイルストーン1", "2026-01-09")  # 同じ週の金曜
    wf_id = window.db.add_workflow("WF")
    window.db.add_workflow_task(wf_id, "タスク", team_id, 2)
    for i in range(3):
        window.db.add_job(f"ジョブ{i}", wf_id, ms_id, 100)

    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)

    df = window.tab_gantt._result_df
    assert (df["Deadline_Overrun_Days"] > 0).any()

    # 描画されたバーを、ツールチップの内容から超過あり/なしに振り分ける
    body_scene = window.tab_gantt.view.scene()  # 本体ペインのシーンを返す
    pens = {"overrun": [], "normal": []}
    for item in body_scene.items():
        tooltip = item.toolTip()
        if not tooltip or not hasattr(item, "pen"):
            continue
        key = "overrun" if "締切を" in tooltip else "normal"
        pens[key].append(item.pen())

    assert pens["overrun"], "超過タスクのバーが描かれていない"
    for pen in pens["overrun"]:
        assert pen.color() == _OVERRUN_BORDER_COLOR
        assert pen.widthF() == _OVERRUN_BORDER_WIDTH
    for pen in pens["normal"]:
        assert pen.color() != _OVERRUN_BORDER_COLOR
        assert pen.widthF() == _NORMAL_BORDER_WIDTH


def test_gantt_tab_reports_deadline_overrun_in_status(window, qapp):
    """締切に間に合わないタスクがある場合、結果は表示したうえで、その件数を
    状況表示で知らせること（締切超過はエラーではなく結果として返るため、
    明示しないと見過ごされる）。"""
    window.db.set_project("締切超過テスト", "2026-01-05")
    team_id = window.db.add_team("チームA", 1)  # 同時1本のみ
    ms_id = window.db.add_milestone("マイルストーン1", "2026-01-09")  # 同じ週の金曜
    for i in range(3):
        wf_id = window.db.add_workflow(f"WF{i}")
        window.db.add_workflow_task(wf_id, "タスク", team_id, 2)
        window.db.add_job(f"ジョブ{i}", wf_id, ms_id, 100)

    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)

    assert window.tab_gantt._result_df is not None  # 例外にせず結果は出す
    assert "締切に間に合いません" in window.tab_gantt.error_label.text()


def test_hidden_gantt_tab_is_not_refreshed_during_undo(window, qapp):
    """ガントチャートタブの refresh_choices() はスケジューリングを実行し直す
    重い処理なので、見えていない間はUndo/Redoのたびに走らせない
    （アクティブなタブだけを更新し、非表示タブはタブ切り替え時に最新化する）。"""
    window.tabs.setCurrentWidget(window.tab_basic_info)
    qapp.processEvents()
    assert window.tabs.currentWidget() is not window.tab_gantt

    window.db.add_team("チームB", 1)  # プロジェクトは依然未完成（検証エラーになる状態）

    with patch.object(GanttTab, "refresh_choices") as mocked:
        window.on_undo()
        qapp.processEvents()
        mocked.assert_not_called()


# -- ガントチャートタブ: 絞り込み（ワークフロー／チーム／タグ・文字列検索・間に合わないジョブ） --

def _build_multi_workflow_project(db):
    """複数ワークフロー・複数チームが混在するプロジェクト（絞り込みのテスト用）。
    ジョブ2（WF2）だけが1本のチームで詰まり、締切に間に合わなくなる。"""
    db.set_project("絞り込みテスト", "2026-01-05")
    team_a = db.add_team("チームA", 2)
    team_b = db.add_team("チームB", 1)
    ms_id = db.add_milestone("MS1", "2026-01-09")  # 同じ週の金曜（間に合わせづらい締切）
    wf1 = db.add_workflow("WF1")
    db.add_workflow_task(wf1, "タスク", team_a, 1)
    wf2 = db.add_workflow("WF2")
    db.add_workflow_task(wf2, "タスク", team_b, 5)  # 締切に間に合わない所要日数
    job1 = db.add_job("案件アルファ", wf1, ms_id, 100, "緊急")
    job2 = db.add_job("案件ベータ", wf2, ms_id, 100, "通常")
    return job1, job2, team_a, team_b, wf1, wf2


def test_gantt_filters_narrow_the_displayed_jobs(window, qapp):
    """ワークフロー／チーム／タグのチェックを外すと、該当するジョブのタスクが
    チャート（本体シーン）から消えること（スケジューリング結果自体は変わらない）。"""
    job1, job2, _team_a, _team_b, wf1, wf2 = _build_multi_workflow_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)

    def job_names_in_body_scene():
        body_scene = window.tab_gantt.view.scene()
        names = set()
        for item in body_scene.items():
            tooltip = item.toolTip()
            if tooltip and hasattr(item, "pen"):
                names.add(tooltip.split(" / ")[0])
        return names

    assert job_names_in_body_scene() == {"案件アルファ", "案件ベータ"}

    wf1_str = window.tab_gantt._display["workflow_names"]
    wf1_id = next(k for k, v in wf1_str.items() if v == "WF1")
    window.tab_gantt.workflow_filter._checks[wf1_id].setChecked(False)
    qapp.processEvents()
    assert job_names_in_body_scene() == {"案件ベータ"}
    window.tab_gantt.workflow_filter._checks[wf1_id].setChecked(True)
    qapp.processEvents()

    tag_filter = window.tab_gantt.tag_filter
    tag_filter._checks["緊急"].setChecked(False)
    qapp.processEvents()
    assert job_names_in_body_scene() == {"案件ベータ"}


def test_gantt_task_tag_filter_narrows_the_displayed_jobs(window, qapp):
    """タスク タグのチェックを外すと、そのタグを持つタスクを含まないジョブが
    チャート（本体シーン）から消えること。"""
    job1, _job2, _team_a, _team_b, wf1, _wf2 = _build_multi_workflow_project(window.db)
    task1 = window.db.list_workflow_tasks(wf1)[0]["id"]
    window.db.upsert_job_task_override(job1, task1, tags="要確認")

    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)

    def job_names_in_body_scene():
        body_scene = window.tab_gantt.view.scene()
        names = set()
        for item in body_scene.items():
            tooltip = item.toolTip()
            if tooltip and hasattr(item, "pen"):
                names.add(tooltip.split(" / ")[0])
        return names

    assert job_names_in_body_scene() == {"案件アルファ", "案件ベータ"}

    task_tag_filter = window.tab_gantt.task_tag_filter
    assert set(task_tag_filter._checks.keys()) == {"要確認", None}
    task_tag_filter._checks[None].setChecked(False)  # 「（タスク タグなし）」を外す
    qapp.processEvents()
    assert job_names_in_body_scene() == {"案件アルファ"}


def test_gantt_search_box_filters_by_job_name(window, qapp):
    """ジョブ名の文字列検索で、一致しないジョブが表示から除外されること。"""
    _build_multi_workflow_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)

    window.tab_gantt.search_edit.setText("アルファ")
    _wait_for_search_debounce(window, qapp)

    body_scene = window.tab_gantt.view.scene()
    tooltips = [item.toolTip() for item in body_scene.items() if item.toolTip()]
    assert any("案件アルファ" in t for t in tooltips)
    assert not any("案件ベータ" in t for t in tooltips)


def test_gantt_overrun_only_checkbox_filters_to_late_jobs(window, qapp):
    """「間に合わないジョブのみ表示」をオンにすると、締切に間に合うジョブが
    チャートから消え、間に合わないジョブだけが残ること。"""
    _build_multi_workflow_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)

    df = window.tab_gantt._result_df
    assert (df["Deadline_Overrun_Days"] > 0).any()  # 前提: 実際に超過するジョブがある

    window.tab_gantt.overrun_only_checkbox.setChecked(True)
    qapp.processEvents()

    body_scene = window.tab_gantt.view.scene()
    tooltips = [item.toolTip() for item in body_scene.items() if item.toolTip()]
    assert any("案件ベータ" in t for t in tooltips)
    assert not any("案件アルファ" in t for t in tooltips)


def test_gantt_column_shows_workflow_color_swatch_next_to_job_name(window, qapp):
    """左列のジョブ名の左に、ワークフロー別の色スペースが表示されること
    （複数ワークフローが混在する表示で、どのワークフローのジョブか見分ける
    ため）。"""
    from PySide6.QtWidgets import QGraphicsRectItem

    from gui.gantt_view import _JOB_SWATCH_WIDTH

    _build_multi_workflow_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)

    column_scene = window.tab_gantt.view.column.scene()
    swatches = {
        item.toolTip(): item.brush().color().name()
        for item in column_scene.items()
        if isinstance(item, QGraphicsRectItem) and item.rect().width() == _JOB_SWATCH_WIDTH
    }
    assert "ワークフロー: WF1" in swatches
    assert "ワークフロー: WF2" in swatches
    assert swatches["ワークフロー: WF1"] != swatches["ワークフロー: WF2"]


# -- プロジェクト分析タブ（gui/tab_analysis.py） ---------------------------------------

def test_analysis_tab_shows_reason_instead_of_a_dialog_when_no_schedule_result(window, qapp):
    """ガントチャートタブで一度も計算していなくても、プロジェクト分析タブ自身が
    ScheduleCache に計算を要求する（gui/schedule_cache.py）。プロジェクトが
    未完成で検証エラーになる場合、モーダルダイアログではなくタブ内に赤字で
    理由を表示すること（ガントチャートタブと同じ方針）。"""
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    assert "解決してください" in window.tab_analysis.status_label.text()
    assert window.tab_analysis.kpi_scale.value_label.text() == "—"
    assert window.tab_analysis.milestone_table.rowCount() == 0


def test_analysis_tab_computes_its_own_result_without_opening_gantt_tab(window, qapp):
    """ガントチャートタブを一度も開かなくても、プロジェクト分析タブを開けば
    共有の ScheduleCache 経由で自分から計算を起動できること
    （docs/project_analysis_tab_design.md §5）。"""
    _build_schedulable_project(window.db)
    assert not hasattr(window, "tab_gantt") or window.tab_gantt._result_df is None

    window.tabs.setCurrentWidget(window.tab_analysis)
    # まだイベントを回していないので、この時点では計算中の表示のはず。
    assert "計算中" in window.tab_analysis.status_label.text()

    deadline = time.monotonic() + 15.0
    while window.tab_analysis.kpi_scale.value_label.text() == "—" and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()

    assert window.tab_analysis.kpi_scale.value_label.text() == "1 ジョブ"
    assert window.tab_analysis.milestone_table.rowCount() == 1
    # ガントチャートタブも同じキャッシュを見るので、後から開いても再計算しない。
    seq_before = window.schedule_cache._request_seq
    window.tabs.setCurrentWidget(window.tab_gantt)
    qapp.processEvents()
    assert window.schedule_cache._request_seq == seq_before
    assert window.tab_gantt._result_df is not None


def test_analysis_tab_renders_kpi_and_milestone_table_from_gantt_result(window, qapp):
    """ガントチャートタブの計算結果をもとに、KPIタイルとマイルストーン別表の
    基本列が埋まること。自分では計算を起こさない（共有の ScheduleCache が
    持つ結果をそのまま集計する）。"""
    _build_schedulable_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)

    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    assert window.tab_analysis.kpi_scale.value_label.text() == "1 ジョブ"
    assert window.tab_analysis.milestone_table.rowCount() == 1
    assert window.tab_analysis.milestone_table.item(0, 0).text() == "マイルストーン1"
    # マイルストーンが1件だけなので、全タスクがそこに属し進捗は必ず100%。
    assert window.tab_analysis.milestone_table.item(0, 1).text() == "100%"


def test_analysis_tab_milestone_progress_column_is_cumulative_across_milestones(window, qapp):
    """「進捗」列はタスクの完了状態と無関係の計画上の指標で、締切順に
    マイルストーンをまたいで累積し、最後のマイルストーンで必ず100%になる
    こと（gui/summary_metrics.compute_milestone_rows参照）。"""
    db = window.db
    db.set_project("進捗列テスト", "2026-01-05")
    team_id = db.add_team("チームA", 2)
    ms1 = db.add_milestone("マイルストーン1", "2026-03-01")
    ms2 = db.add_milestone("マイルストーン2", "2026-06-01")
    wf_id = db.add_workflow("WF1")
    db.add_workflow_task(wf_id, "タスク", team_id, 3)
    db.add_job("ジョブA", wf_id, ms1, 100)  # MS1に1タスク
    db.add_job("ジョブB", wf_id, ms2, 100)  # MS2に1タスク
    db.add_job("ジョブC", wf_id, ms2, 100)  # MS2に1タスク（MS2は計2タスク）

    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    table = window.tab_analysis.milestone_table
    assert table.rowCount() == 2
    # 全体3タスク中、MS1に1件・MS2に2件 -> 累積は 1/3=33%, 3/3=100%。
    assert table.item(0, 1).text() == "33%"
    assert table.item(1, 1).text() == "100%"


def test_analysis_tab_status_kpi_reflects_recorded_status_not_a_date_guess(window, qapp):
    """「タスクの状態」は日付からの推測ではなく、job_task_overrides.statusに
    実際に記録された値を使うこと。今日を挟む長い所要日数のタスクでも、
    statusを記録していなければ（日付上は「進行中」に見えても）未着手のまま
    集計され、記録して初めて反映されることを確認する。"""
    window.db.set_project("状態テスト", "2020-01-01")
    team_id = window.db.add_team("チームA", 2)
    ms_id = window.db.add_milestone("マイルストーン1", "2040-06-30")
    wf_id = window.db.add_workflow("WF1")
    # 2020年から約20年（5000営業日）と、今日をまたぐ長い所要日数にする——
    # 日付だけで判定していれば「進行中」に見えるはずの状況を作る。
    task_id = window.db.add_workflow_task(wf_id, "タスク", team_id, 5000)
    job_id = window.db.add_job("ジョブ1", wf_id, ms_id, 100)

    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    # statusを記録していないので、日付上は今日を挟んでいても「未着手」のまま。
    assert window.tab_analysis.kpi_status.value_label.text() == "0 完了"
    assert "未着手 1" in window.tab_analysis.kpi_status.sub_label.text()

    window.db.upsert_job_task_override(job_id, task_id, status="in_progress")
    window.tabs.setCurrentWidget(window.tab_gantt)
    qapp.processEvents()
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    assert "進行中 1" in window.tab_analysis.kpi_status.sub_label.text()
    assert "未着手 0" in window.tab_analysis.kpi_status.sub_label.text()


def test_task_status_combo_in_override_table_persists_to_db(window, qapp):
    """タスク上書き表の「状態」列（7列目）でコンボを選ぶと
    job_task_overrides.status に保存されること。"""
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    window.db.add_workflow_task(wf_id, "タスク1", team_id, 3)
    job_id = window.db.add_job("J1", wf_id, None, 100)
    window.tabs.setCurrentWidget(window.tab_jobs)
    window.tab_jobs.refresh_jobs(select_id=job_id)
    qapp.processEvents()

    table = window.tab_jobs.override_table
    assert table.horizontalHeaderItem(6).text() == "状態"
    status_combo = table.cellWidget(0, 6)
    assert status_combo.currentData() is None  # 既定は未着手

    idx = status_combo.findData("done")
    status_combo.setCurrentIndex(idx)
    qapp.processEvents()

    rows = window.db.list_job_tasks_with_overrides(job_id)
    assert rows[0]["status"] == "done"


def _build_two_team_project(db):
    """チームAに2タスク・チームBに1タスクが乗る、同一マイルストーンの
    プロジェクト（「チーム別」内訳の選択切り替えを件数で見分けるため、
    チームごとの件数をわざと変えてある）。"""
    db.set_project("チーム別テスト", "2026-01-05")
    team_a = db.add_team("チームA", 2)
    team_b = db.add_team("チームB", 1)
    ms_id = db.add_milestone("マイルストーン1", "2026-06-30")
    wf1 = db.add_workflow("WF1")
    db.add_workflow_task(wf1, "タスク", team_a, 3)
    wf2 = db.add_workflow("WF2")
    db.add_workflow_task(wf2, "タスク", team_b, 3)
    db.add_job("ジョブA1", wf1, ms_id, 100)
    db.add_job("ジョブA2", wf1, ms_id, 100)
    db.add_job("ジョブB1", wf2, ms_id, 100)
    return team_a, team_b, wf1, wf2


def test_analysis_tab_breakdown_dimension_selector_filters_to_one_team_at_a_time(window, qapp):
    """「チーム別」「ワークフロー別」は全チーム/全ワークフローを列に並べるの
    ではなく、コンボで選んだ1件だけを「全体」と同じ列構成（ジョブ/タスク/
    完了/進行中/未着手）で表示すること。"""
    _build_two_team_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    tab = window.tab_analysis
    headers = ["マイルストーン", "進捗", "締切日", "最終終了日", "スラック", "超過",
               "ジョブ", "タスク", "完了", "進行中", "未着手"]
    assert [tab.milestone_table.horizontalHeaderItem(i).text() for i in range(len(headers))] == headers
    assert not tab._dimension_combo.isVisible()

    tab._breakdown_buttons["team"].click()
    qapp.processEvents()
    assert tab._dimension_combo.isVisible()
    assert tab._dimension_combo.currentText() == "チームA"
    assert tab.milestone_table.item(0, 7).text() == "2"  # タスク: チームAの2件

    idx_team_b = tab._dimension_combo.findText("チームB")
    tab._dimension_combo.setCurrentIndex(idx_team_b)
    qapp.processEvents()
    assert tab.milestone_table.item(0, 7).text() == "1"  # タスク: チームBの1件

    tab._breakdown_buttons["workflow"].click()
    qapp.processEvents()
    assert tab._dimension_combo.currentText() == "WF1"
    assert tab.milestone_table.item(0, 7).text() == "2"  # タスク: WF1の2件


def test_analysis_tab_milestone_progress_uses_the_selected_breakdown_target_as_the_denominator(
    window, qapp,
):
    """「チーム別」「ワークフロー別」表示中は、「進捗」列の基準（分母）が
    プロジェクト全体ではなく選択中の対象の件数になること。"""
    db = window.db
    db.set_project("進捗内訳テスト", "2026-01-05")
    team_a = db.add_team("チームA", 2)
    team_b = db.add_team("チームB", 1)
    ms1 = db.add_milestone("マイルストーン1", "2026-03-01")
    ms2 = db.add_milestone("マイルストーン2", "2026-06-01")
    wf_id = db.add_workflow("WF1")
    db.add_workflow_task(wf_id, "タスク", team_a, 3)
    db.add_workflow_task(wf_id, "タスク2", team_b, 3)
    # チームAはMS1に1件・MS2に1件（計2件）。チームBはMS1に1件のみ（計1件）。
    job1 = db.add_job("ジョブA1", wf_id, ms1, 100)
    db.upsert_job_task_override(job1, db.list_workflow_tasks(wf_id)[1]["id"], is_active=False)
    job2 = db.add_job("ジョブA2", wf_id, ms2, 100)
    db.upsert_job_task_override(job2, db.list_workflow_tasks(wf_id)[1]["id"], is_active=False)
    job3 = db.add_job("ジョブB1", wf_id, ms1, 100)
    db.upsert_job_task_override(job3, db.list_workflow_tasks(wf_id)[0]["id"], is_active=False)

    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    tab = window.tab_analysis
    tab._breakdown_buttons["team"].click()
    qapp.processEvents()
    assert tab._dimension_combo.currentText() == "チームA"
    # チームAはMS1・MS2に1件ずつ（計2件）なので累積は 1/2=50%, 2/2=100%。
    assert tab.milestone_table.item(0, 1).text() == "50%"
    assert tab.milestone_table.item(1, 1).text() == "100%"

    idx_team_b = tab._dimension_combo.findText("チームB")
    tab._dimension_combo.setCurrentIndex(idx_team_b)
    qapp.processEvents()
    # チームBはMS1に1件のみ（計1件）なので、MS1時点で既に100%。
    assert tab.milestone_table.item(0, 1).text() == "100%"
    assert tab.milestone_table.item(1, 1).text() == "100%"


def test_analysis_tab_milestone_last_end_slack_and_overrun_use_the_selected_breakdown_target(
    window, qapp,
):
    """「チーム別」「ワークフロー別」表示中は、最終終了日・スラック・超過件数も
    「進捗」列・内訳列と同じくプロジェクト全体ではなく選択中の対象で計算する
    こと（以前はプロジェクト全体固定だったが、対象を切り替えても値が変わらず
    個別の状況が読めないという指摘を受けて変更した）。"""
    db = window.db
    db.set_project("スラック内訳テスト", "2026-01-05")
    team_a = db.add_team("チームA", 2)
    team_b = db.add_team("チームB", 1)
    ms_id = db.add_milestone("マイルストーン1", "2026-01-20")
    wf_id = db.add_workflow("WF1")
    db.add_workflow_task(wf_id, "タスクA", team_a, 10)  # 長い方（10日）
    db.add_workflow_task(wf_id, "タスクB", team_b, 3)   # 短い方（3日）
    # 各ジョブは片方のタスクだけを有効にし、チームAは10日タスクのみ、
    # チームBは3日タスクのみを持つようにする（それぞれのチームの終了日を
    # はっきり分ける）。
    job_a = db.add_job("ジョブA1", wf_id, ms_id, 100)
    db.upsert_job_task_override(job_a, db.list_workflow_tasks(wf_id)[1]["id"], is_active=False)
    job_b = db.add_job("ジョブB1", wf_id, ms_id, 100)
    db.upsert_job_task_override(job_b, db.list_workflow_tasks(wf_id)[0]["id"], is_active=False)

    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    tab = window.tab_analysis
    # 「全体」表示中は、プロジェクト全体の最終終了日（=長い方のチームAの
    # 10日タスク、締切ちょうどでスラック+0日）。
    assert tab.milestone_table.item(0, 3).text() == "2026-01-20"
    assert tab.milestone_table.item(0, 4).text() == "+0日"

    tab._breakdown_buttons["team"].click()
    qapp.processEvents()
    assert tab._dimension_combo.currentText() == "チームA"
    assert tab.milestone_table.item(0, 3).text() == "2026-01-20"
    assert tab.milestone_table.item(0, 4).text() == "+0日"

    idx_team_b = tab._dimension_combo.findText("チームB")
    tab._dimension_combo.setCurrentIndex(idx_team_b)
    qapp.processEvents()
    # チームBは3日タスクのみで、全体より早く終わりスラックも大きくなる
    # ——「全体」表示中の値のままではないこと（＝選択対象で計算し直している
    # こと）を確認する。
    assert tab.milestone_table.item(0, 3).text() == "2026-01-17"
    assert tab.milestone_table.item(0, 4).text() == "+3日"


def test_analysis_tab_milestone_table_handles_a_workflow_with_no_tasks(window, qapp):
    """ワークフロー別の絞り込み対象に「タスクを1件も持たないワークフロー」を
    選んでも、進捗・内訳が0扱いになるだけでクラッシュしないこと
    （プロジェクト全体としては別のワークフローにタスクがあるため、
    スケジューリング自体は成立する）。"""
    db = window.db
    db.set_project("空ワークフローテスト", "2026-01-05")
    team_id = db.add_team("チームA", 2)
    ms_id = db.add_milestone("マイルストーン1", "2026-06-30")
    wf_used = db.add_workflow("使用中WF")
    db.add_workflow_task(wf_used, "タスク", team_id, 3)
    db.add_job("ジョブ1", wf_used, ms_id, 100)
    db.add_workflow("未使用WF")  # タスクを1件も持たない

    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    tab = window.tab_analysis
    tab._breakdown_buttons["workflow"].click()
    qapp.processEvents()
    idx_unused = tab._dimension_combo.findText("未使用WF")
    assert idx_unused >= 0
    tab._dimension_combo.setCurrentIndex(idx_unused)
    qapp.processEvents()

    assert tab.milestone_table.item(0, 1).text() == "0%"
    assert tab.milestone_table.item(0, 7).text() == "0"  # タスク
    assert "タスクがありません" in tab.breakdown_hint_label.text()


def test_analysis_tab_capture_and_restore_breakdown_mode(window, qapp):
    _build_two_team_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    window.tab_analysis._breakdown_buttons["team"].click()
    qapp.processEvents()
    idx_team_b = window.tab_analysis._dimension_combo.findText("チームB")
    window.tab_analysis._dimension_combo.setCurrentIndex(idx_team_b)
    qapp.processEvents()

    state = window.tab_analysis.capture_ui_state()
    assert state["breakdown_mode"] == "team"
    assert state["selected_team_id"] is not None

    window.tab_analysis._breakdown_buttons["all"].click()
    qapp.processEvents()
    window.tab_analysis.restore_ui_state(state)
    qapp.processEvents()
    assert window.tab_analysis._breakdown_mode == "team"
    assert window.tab_analysis._breakdown_buttons["team"].isChecked()
    assert window.tab_analysis._dimension_combo.currentText() == "チームB"


# -- チーム別サマリー（gui/analysis_charts.py） ---------------------------------------

def test_team_summary_table_starts_with_an_all_teams_row_driving_the_stacked_chart(window, qapp):
    """表の先頭は「全チーム」行で、既定でそれが選択されていること。グラフは
    1枚に統合してあり、この行のときは積み上げ（凡例あり）になる。"""
    from gui.tab_analysis import _ALL_TEAMS_KEY

    _build_two_team_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    tab = window.tab_analysis
    table = tab.team_table
    assert table.item(0, 0).text() == "全チーム"
    assert table.item(0, 0).data(Qt.UserRole) == _ALL_TEAMS_KEY
    assert table.rowCount() == 3  # 全チーム + チームA + チームB
    assert tab._selected_team_summary_id == _ALL_TEAMS_KEY
    assert "積み上げ" in tab.team_chart_label.text()
    assert tab.team_legend_label.text()  # 積み上げのときだけ凡例を出す


def test_team_summary_chart_switches_to_the_selected_teams_detail(window, qapp):
    """個別チームの行を選ぶと、同じ1枚のグラフがそのチームの詳細に切り替わり、
    凡例（1色なので不要）が消えること。"""
    _build_two_team_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    tab = window.tab_analysis
    tab.team_table.selectRow(1)  # 「全チーム」の次＝最初の個別チーム
    qapp.processEvents()

    assert tab._selected_team_summary_id is not None
    assert "チームA" in tab.team_chart_label.text()
    assert "破線＝設定上限" in tab.team_chart_label.text()
    assert tab.team_legend_label.text() == ""


def test_capacity_line_merges_consecutive_weeks_so_the_dash_pattern_is_visible():
    """回帰テスト: 設定上限の破線は、同じ値が続く週をまとめて1本にすること。

    週ごと（9px）の細切れで描くと、QtのDashLineはダッシュ長を線幅の倍数で
    決めるため1ダッシュも入りきらず、実線にしか見えなくなっていた。"""
    from gui.analysis_charts import _merge_runs

    # 同じ値が続く区間は1本にまとまる。
    assert _merge_runs([2, 2, 2]) == [(0, 2, 2)]
    # 値が変わったところで切れる（階段になる）。
    assert _merge_runs([1, 1, 3, 3]) == [(0, 1, 1), (2, 3, 3)]
    # None（＝上限「指定なし」）は区間を作らず、そこで切る。
    assert _merge_runs([1, 1, None, 1]) == [(0, 1, 1), (3, 3, 1)]
    assert _merge_runs([None, None]) == []
    assert _merge_runs([]) == []


# -- サマリーのサブタブ・ワークフロー別サマリー ------------------------------------------

def test_analysis_tab_splits_the_summaries_into_sub_tabs_with_the_kpi_tiles_outside(window, qapp):
    """サマリーはサブタブに分け、KPIタイルはその外に常時表示すること
    （全部を縦積みにするとウインドウの高さが足りないときに窮屈になるため）。"""
    _build_two_team_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    tab = window.tab_analysis
    labels = [tab.section_tabs.tabText(i) for i in range(tab.section_tabs.count())]
    assert labels == ["マイルストーン", "チーム", "ワークフロー"]
    # KPIタイルはサブタブの中ではなく、タブの外（＝どのサブタブでも見える）。
    assert tab.kpi_scale.parent() is tab
    assert not tab.section_tabs.isAncestorOf(tab.kpi_scale)
    assert tab.section_tabs.isAncestorOf(tab.milestone_table)
    assert tab.section_tabs.isAncestorOf(tab.workflow_table)


def test_analysis_tab_workflow_table_lists_each_workflow_with_its_job_and_task_counts(window, qapp):
    """ワークフロー別サマリーの表に、先頭の「全ワークフロー」行に続けて
    ワークフローごとのジョブ件数・タスク件数・ジョブ所要期間の中央値・
    超過件数が並ぶこと（チーム別サマリーの「全チーム」行と同じ構成）。"""
    _build_two_team_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    tab = window.tab_analysis
    headers = ["ワークフロー", "ジョブ件数", "タスク件数", "ジョブ所要期間の中央値", "超過件数"]
    assert [
        tab.workflow_table.horizontalHeaderItem(i).text() for i in range(len(headers))
    ] == headers
    assert tab.workflow_table.rowCount() == 3
    assert tab.workflow_table.item(0, 0).text() == "全ワークフロー"
    assert tab.workflow_table.item(0, 1).text() == "3"  # 全ワークフロー合算のジョブ3件
    assert tab.workflow_table.item(1, 0).text() == "WF1"
    assert tab.workflow_table.item(1, 1).text() == "2"  # WF1のジョブ2件
    assert tab.workflow_table.item(2, 1).text() == "1"  # WF2のジョブ1件
    # 既定は「全ワークフロー」行が選択され、積み上げグラフと、色とワークフロー名を
    # 対応させる凡例が出ている。
    assert tab.workflow_table.currentRow() == 0
    assert tab.workflow_chart_view.scene() is not None
    assert "WF1" in tab.workflow_legend_label.text()


def test_analysis_tab_workflow_row_selection_switches_chart_to_that_workflow_alone(window, qapp):
    """表の行を選択すると、チーム別サマリーと同じくグラフがその1件だけの
    表示に切り替わり、凡例（1色なので不要）は隠れること。"""
    _build_two_team_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    tab = window.tab_analysis
    tab.workflow_table.selectRow(1)  # WF1
    qapp.processEvents()

    assert tab.workflow_chart_view.scene() is not None
    assert "WF1" in tab.workflow_chart_label.text()
    assert tab.workflow_legend_label.text() == ""

    tab.workflow_table.selectRow(0)  # 全ワークフローに戻す
    qapp.processEvents()
    assert "WF1" in tab.workflow_legend_label.text()


def test_analysis_tab_workflow_chart_granularity_switch_changes_the_number_of_points(window, qapp):
    """粒度（月次/週次/日次）を切り替えるとグラフを引き直すこと。粒度が細かい
    ほど点が増えるので、シーンの横幅で見分けられる。"""
    _build_two_team_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    tab = window.tab_analysis
    widths = {}
    for key in ("month", "week", "day"):
        tab._granularity_buttons[key].click()
        qapp.processEvents()
        assert tab._granularity == key
        widths[key] = tab.workflow_chart_view.scene().itemsBoundingRect().width()
    assert widths["month"] < widths["week"] < widths["day"]


def test_analysis_tab_restores_the_active_sub_tab_and_granularity(window, qapp):
    """Undo/Redoでの復元対象に、サマリーのサブタブと粒度も含まれること
    （CLAUDE.md「内容・選択・アクティブタブを復元」）。"""
    _build_two_team_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)
    window.tabs.setCurrentWidget(window.tab_analysis)
    qapp.processEvents()

    tab = window.tab_analysis
    tab.section_tabs.setCurrentIndex(2)  # ワークフロー
    tab._granularity_buttons["month"].click()
    qapp.processEvents()
    state = tab.capture_ui_state()

    tab.section_tabs.setCurrentIndex(0)
    tab._granularity_buttons["day"].click()
    qapp.processEvents()

    tab.restore_ui_state(state)
    qapp.processEvents()
    assert tab.section_tabs.currentIndex() == 2
    assert tab._granularity == "month"
    assert tab._granularity_buttons["month"].isChecked()


# -- ガントチャート描画（gui/gantt_view.py）: 「今日」の縦線・1行飛ばしの行背景 ------------
#
# build_gantt_scenes() はDB/スケジューラーを介さずDataFrameだけで呼べるため、
# ワーカースレッドでの計算を待つ window フィクスチャより軽量な qapp フィクスチャ
# だけで直接検証する。

_MINIMAL_GANTT_DISPLAY = {
    "team_names": {"TEAM_001": "チームA"},
    "team_colors": {"TEAM_001": "#cccccc"},
    "workflow_names": {"WF_001": "WF1"},
    "workflow_colors": {"WF_001": "#dddddd"},
    "milestone_markers": [],
    "common_holiday_dates": set(),
    "holidays_by_team": {},
    "job_tags": {},
}


def _gantt_task_row(job_id, job_name, start, end):
    return {
        "Job_ID": job_id, "Job_Name": job_name, "Task_Name": "タスク1",
        "Workflow_ID": "WF_001", "Team_ID": "TEAM_001",
        "Start_Date": start, "End_Date": end,
        "Deadline_Overrun_Days": 0, "Constraint_Violation": "", "Resource_Adjusted": False,
    }


def test_gantt_chart_shows_today_line_when_within_display_range(qapp):
    """表示範囲内に今日が含まれる場合、マイルストーンと同じコズメティック
    ペインの「今日」の縦線が描かれること。"""
    from gui.gantt_view import _TODAY_LINE_COLOR, build_gantt_scenes

    today = pd.Timestamp.today().normalize()
    df = pd.DataFrame([_gantt_task_row(
        "JOB_001", "ジョブA", today - pd.Timedelta(days=10), today + pd.Timedelta(days=10),
    )])
    scenes = build_gantt_scenes(df, _MINIMAL_GANTT_DISPLAY, color_by="team")
    assert scenes is not None

    today_lines = [
        item for item in scenes.body.items()
        if hasattr(item, "pen") and item.pen().color() == _TODAY_LINE_COLOR
    ]
    assert today_lines, "今日の縦線が描かれていない"
    assert today_lines[0].pen().isCosmetic()


def test_gantt_chart_hides_today_line_when_outside_display_range(qapp):
    """今日が表示範囲の外（プロジェクトが過去のみ）にある場合、今日の縦線を
    描かない（軸を無理に広げて表示範囲を歪めないため）。"""
    from gui.gantt_view import _TODAY_LINE_COLOR, build_gantt_scenes

    df = pd.DataFrame([_gantt_task_row(
        "JOB_001", "ジョブA", pd.Timestamp("2000-01-01"), pd.Timestamp("2000-01-10"),
    )])
    scenes = build_gantt_scenes(df, _MINIMAL_GANTT_DISPLAY, color_by="team")
    today_lines = [
        item for item in scenes.body.items()
        if hasattr(item, "pen") and item.pen().color() == _TODAY_LINE_COLOR
    ]
    assert not today_lines


def test_gantt_chart_stripes_every_other_job_row(qapp):
    """ジョブの行は1行飛ばしで背面が薄い灰色になること（左列・本体の両方）。"""
    from gui.gantt_view import _ROW_STRIPE_COLOR, build_gantt_scenes

    base = pd.Timestamp("2026-01-05")
    df = pd.DataFrame([
        _gantt_task_row(f"JOB_{i:03d}", f"ジョブ{i}", base + pd.Timedelta(days=i * 3),
                         base + pd.Timedelta(days=i * 3 + 2))
        for i in range(4)
    ])
    scenes = build_gantt_scenes(df, _MINIMAL_GANTT_DISPLAY, color_by="team")

    def stripe_count(scene):
        return sum(
            1 for item in scene.items()
            if hasattr(item, "brush") and item.brush().color() == _ROW_STRIPE_COLOR
        )

    assert stripe_count(scenes.body) == 2  # 4行中、奇数インデックス(1,3)の2行分
    assert stripe_count(scenes.column) == 2


# -- ワークフロー設計タブ: ノードビュー／テーブルビューの切り替え ---------------------------


def test_view_tabs_default_to_node_view(window, qapp):
    """デフォルトはノードビューであり、テーブルビューへの切り替えタブが
    用意されていること。"""
    wf_tab = window.tab_workflows
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    assert wf_tab.view_tabs.currentIndex() == 0
    assert wf_tab.view_tabs.tabText(0) == "ノードビュー"
    assert wf_tab.view_tabs.tabText(1) == "テーブルビュー"
    assert wf_tab.view_tabs.widget(0) is wf_tab.view


def test_task_table_orders_tasks_upstream_to_downstream(window, qapp):
    """テーブルビューのタスク表は、ノードビューの自動整列と同じ「依存の深さ→
    同じ深さ内は名前順」で、上流を上・下流を下に並べること。"""
    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    nodes = {}
    # わざと深さ順とは逆のアルファベット順で追加し、並び替えが名前ではなく
    # 深さに基づいていることを確認する。
    for name in ("D", "C", "B", "A"):
        nodes[name] = scene.add_task(name, team_id, 1, 0, 0)
        qapp.processEvents()
        scene = wf_tab.current_scene
    # A -> B -> C（直列）、Dは独立（深さ0）。
    for pred, succ in [("A", "B"), ("B", "C")]:
        scene = wf_tab.current_scene
        scene.try_add_edge(scene.nodes[nodes[pred].workflow_task_id], scene.nodes[nodes[succ].workflow_task_id])
        qapp.processEvents()

    wf_tab.refresh_task_table()
    table = wf_tab.task_section.table
    names = [table.item(row, 0).text() for row in range(table.rowCount())]
    # 深さ0（A, D）が先、深さ1（B）、深さ2（C）の順。同じ深さ内は名前順。
    assert names == ["A", "D", "B", "C"]

    b_row = names.index("B")
    assert table.item(b_row, 3).text() == "A"
    c_row = names.index("C")
    assert table.item(c_row, 3).text() == "B"


def test_compute_workflow_min_duration_follows_critical_path():
    """FS/SSの依存種別とラグを踏まえた最短完了日数（クリティカルパス）を
    正しく計算すること。チームの同時ライン数・休業日は一切考慮しない
    （純粋にタスクの所要日数と内部依存だけで決まる下限）。"""
    from gui.node_canvas import compute_workflow_min_duration

    tasks = [
        {"id": 1, "default_days": 5},  # A
        {"id": 2, "default_days": 3},  # B: Aの完了(FS)+2日後に開始
        {"id": 3, "default_days": 4},  # C: Aの開始(SS)+1日後に開始
    ]
    deps = [
        {"predecessor_task_id": 1, "successor_task_id": 2, "dep_type": "FS", "lag_days": 2},
        {"predecessor_task_id": 1, "successor_task_id": 3, "dep_type": "SS", "lag_days": 1},
    ]
    # A: 0〜5。B: FSなのでAの終了(5)+2=7に開始、3日で10に終了。
    # C: SSなのでAの開始(0)+1=1に開始、4日で5に終了。全体は最も遅いBの10。
    assert compute_workflow_min_duration(tasks, deps) == 10


def test_compute_workflow_min_duration_clamps_negative_lead_to_zero():
    """リード（負のラグ）で計算上マイナスの開始日になっても、0未満には
    しない（プロジェクト開始日より前には遡れないため）。"""
    from gui.node_canvas import compute_workflow_min_duration

    tasks = [{"id": 1, "default_days": 2}, {"id": 2, "default_days": 3}]
    deps = [{"predecessor_task_id": 1, "successor_task_id": 2, "dep_type": "FS", "lag_days": -100}]
    assert compute_workflow_min_duration(tasks, deps) == 3  # 開始日は0に丸められ、3日タスクで終了


def test_compute_workflow_min_duration_returns_zero_for_no_tasks():
    from gui.node_canvas import compute_workflow_min_duration

    assert compute_workflow_min_duration([], []) == 0


def test_node_canvas_shows_duration_marker_above_all_nodes(window, qapp):
    """ノードビュー上部に、ワークフロー全体の最短完了日数を示す目盛り
    （｜←-- 最短N日 --→｜）が表示され、常に全ノードより上（yが小さい）に
    位置すること。"""
    from PySide6.QtWidgets import QGraphicsSimpleTextItem

    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    a = scene.add_task("A", team_id, 5, 0, 0)
    qapp.processEvents()
    scene = wf_tab.current_scene
    b = scene.add_task("B", team_id, 3, 0, 0)
    qapp.processEvents()
    scene = wf_tab.current_scene
    scene.try_add_edge(scene.nodes[a.workflow_task_id], scene.nodes[b.workflow_task_id])
    qapp.processEvents()
    scene = wf_tab.current_scene

    labels = [
        item for item in scene.items()
        if isinstance(item, QGraphicsSimpleTextItem) and item.text().startswith("最短")
    ]
    assert len(labels) == 1
    assert labels[0].text() == "最短8日"  # A(5日)→B(3日、FS+0)の直列で8日

    marker_y = labels[0].y()
    node_tops = [node.y() for node in scene.nodes.values()]
    assert marker_y < min(node_tops)


def test_task_table_edit_and_delete_reuse_the_node_view_dialog_flow(window, qapp):
    """テーブルビューの「編集...」「削除」ボタンは、ノードビューと同じ
    scene操作（Undo・確認ダイアログ込み）を経由すること。"""
    from gui.widgets_common import row_id as _row_id

    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    scene.add_task("タスクA", team_id, 3, 0, 0)
    qapp.processEvents()
    wf_tab.refresh_task_table()

    row = 0
    task_id = _row_id(wf_tab.task_section.table, row)
    stack_before = len(window.undo_manager._undo_stack)
    wf_tab._delete_task_row(row)
    qapp.processEvents()

    assert len(window.undo_manager._undo_stack) == stack_before + 1
    assert window.db.list_workflow_tasks(wf_id) == []
    assert task_id not in wf_tab.current_scene.nodes


def test_add_dependency_template_creates_a_visually_distinct_pseudo_node(window, qapp):
    """ノードビューに依存テンプレートを追加すると、タスクノードとは別スタイルの
    疑似ノード・接続線として表示され、テーブルビューの依存テンプレート欄にも
    反映されること。"""
    from gui.node_canvas import TEMPLATE_EDGE_COLOR, TemplateDependencyNodeItem

    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf1_id = window.db.add_workflow("WF1")
    wf2_id = window.db.add_workflow("WF2")
    task_a = window.db.add_workflow_task(wf1_id, "A", team_id, 1)
    task_x = window.db.add_workflow_task(wf2_id, "X", team_id, 1)

    wf_tab.refresh_workflows(select_id=wf1_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    scene.add_dependency_template_node(task_a, wf2_id, task_x)
    qapp.processEvents()

    assert len(scene.template_nodes) == 1
    template_id, tnode = next(iter(scene.template_nodes.items()))
    assert isinstance(tnode, TemplateDependencyNodeItem)
    assert tnode.target_task_id == task_a
    assert tnode.pen().color().name() == TEMPLATE_EDGE_COLOR
    assert tnode.pen().style() == Qt.DashLine

    edge = scene.template_edges[template_id]
    assert edge.pen().color().name() == TEMPLATE_EDGE_COLOR
    assert edge.pen().style() == Qt.DashLine

    table = wf_tab.template_section.table
    assert table.rowCount() == 1
    assert table.item(0, 0).text() == "A"
    assert table.item(0, 1).text() == "WF2"
    assert table.item(0, 2).text() == "X"


def test_edit_and_delete_dependency_template_via_scene(window, qapp):
    """依存テンプレートの変更・削除も、ノードビューの疑似ノード・テーブル
    ビュー双方に即座に反映されること。"""
    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf1_id = window.db.add_workflow("WF1")
    wf2_id = window.db.add_workflow("WF2")
    task_a = window.db.add_workflow_task(wf1_id, "A", team_id, 1)
    task_x = window.db.add_workflow_task(wf2_id, "X", team_id, 1)
    task_y = window.db.add_workflow_task(wf2_id, "Y", team_id, 1)

    wf_tab.refresh_workflows(select_id=wf1_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    scene.add_dependency_template_node(task_a, wf2_id, task_x)
    qapp.processEvents()
    template_id = next(iter(scene.template_nodes))

    scene.update_dependency_template_node(template_id, task_a, wf2_id, task_y)
    qapp.processEvents()
    assert scene.template_nodes[template_id].target_task_id == task_a
    assert wf_tab.template_section.table.item(0, 2).text() == "Y"

    scene.delete_dependency_template_node(template_id)
    qapp.processEvents()
    assert scene.template_nodes == {}
    assert wf_tab.template_section.table.rowCount() == 0
    assert window.db.list_dependency_templates(wf1_id) == []


def test_deleting_a_task_cascades_its_dependency_template_in_one_undo_step(window, qapp):
    """依存テンプレートの対象タスクを削除すると、DBの外部キー制約で
    テンプレートも連鎖削除される。この連鎖削除はUndoスナップショットに
    自動的に含まれるため、タスク削除と同じ1回のUndoで両方が復元されること。
    キャンバス上の疑似ノードも明示的に取り除かれること。"""
    from PySide6.QtWidgets import QMessageBox

    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf1_id = window.db.add_workflow("WF1")
    wf2_id = window.db.add_workflow("WF2")
    task_x = window.db.add_workflow_task(wf2_id, "X", team_id, 1)

    wf_tab.refresh_workflows(select_id=wf1_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    node_a = scene.add_task("A", team_id, 1, 0, 0)
    qapp.processEvents()
    scene = wf_tab.current_scene
    scene.add_dependency_template_node(node_a.workflow_task_id, wf2_id, task_x)
    qapp.processEvents()
    assert len(scene.template_nodes) == 1

    stack_before = len(window.undo_manager._undo_stack)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        scene.delete_node(scene.nodes[node_a.workflow_task_id])
    qapp.processEvents()

    assert len(window.undo_manager._undo_stack) == stack_before + 1
    assert window.db.list_workflow_tasks(wf1_id) == []
    assert window.db.list_dependency_templates(wf1_id) == []
    assert wf_tab.current_scene.template_nodes == {}

    window.on_undo()
    qapp.processEvents()
    assert len(window.db.list_workflow_tasks(wf1_id)) == 1
    assert len(window.db.list_dependency_templates(wf1_id)) == 1
    assert len(wf_tab.current_scene.template_nodes) == 1


def test_mixed_select_delete_of_task_and_unrelated_template_is_one_undo_step(window, qapp):
    """タスクと、それとは無関係な依存テンプレートの疑似ノードを同時に選択して
    Deleteしても、1回のUndoでまとめて元に戻ること（無関係なので連鎖削除は
    起きず、_delete_selected 側の個別処理が両方を担当する）。"""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf1_id = window.db.add_workflow("WF1")
    wf2_id = window.db.add_workflow("WF2")
    task_x = window.db.add_workflow_task(wf2_id, "X", team_id, 1)

    wf_tab.refresh_workflows(select_id=wf1_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    node_a = scene.add_task("A", team_id, 1, 0, 0)
    qapp.processEvents()
    scene = wf_tab.current_scene
    node_b = scene.add_task("B", team_id, 1, 200, 0)
    qapp.processEvents()
    scene = wf_tab.current_scene
    # テンプレートはBに付ける（Aとは無関係）。
    scene.add_dependency_template_node(node_b.workflow_task_id, wf2_id, task_x)
    qapp.processEvents()

    scene = wf_tab.current_scene
    template_id = next(iter(scene.template_nodes))
    scene.nodes[node_a.workflow_task_id].setSelected(True)
    scene.template_nodes[template_id].setSelected(True)
    qapp.processEvents()

    stack_before = len(window.undo_manager._undo_stack)
    wf_tab.view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
    qapp.processEvents()

    assert len(window.undo_manager._undo_stack) == stack_before + 1
    remaining = {t["name"] for t in window.db.list_workflow_tasks(wf1_id)}
    assert remaining == {"B"}
    assert window.db.list_dependency_templates(wf1_id) == []
    assert wf_tab.current_scene.template_nodes == {}

    window.on_undo()
    qapp.processEvents()
    remaining = {t["name"] for t in window.db.list_workflow_tasks(wf1_id)}
    assert remaining == {"A", "B"}
    assert len(window.db.list_dependency_templates(wf1_id)) == 1
    assert len(wf_tab.current_scene.template_nodes) == 1


def test_multiple_templates_on_the_same_task_stack_without_overlapping_or_moving_tasks(window, qapp):
    """回帰テスト: 同じタスクに複数の依存テンプレートが設定されている場合、
    疑似ノードは同じ列（対象タスクの1つ上流）に重ならず積み上げられ、
    タスク側の座標（自動整列）には一切影響しないこと。1件削除すると、
    残りは詰め直され、タスクの位置はそのまま。"""
    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf1_id = window.db.add_workflow("WF1")
    wf2_id = window.db.add_workflow("WF2")
    dep_a = window.db.add_workflow_task(wf2_id, "依存先A", team_id, 1)
    dep_b = window.db.add_workflow_task(wf2_id, "依存先B", team_id, 1)
    dep_c = window.db.add_workflow_task(wf2_id, "依存先C", team_id, 1)

    wf_tab.refresh_workflows(select_id=wf1_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    node = scene.add_task("対象タスク", team_id, 1, 0, 0)
    qapp.processEvents()
    task_pos_before = wf_tab.current_scene.nodes[node.workflow_task_id].pos()

    scene = wf_tab.current_scene
    scene.add_dependency_template_node(node.workflow_task_id, wf2_id, dep_a)
    qapp.processEvents()
    scene = wf_tab.current_scene
    scene.add_dependency_template_node(node.workflow_task_id, wf2_id, dep_b)
    qapp.processEvents()
    scene = wf_tab.current_scene
    scene.add_dependency_template_node(node.workflow_task_id, wf2_id, dep_c)
    qapp.processEvents()

    scene = wf_tab.current_scene
    assert len(scene.template_nodes) == 3
    # タスク自身の位置は、テンプレートを3件追加しても変わらない。
    assert scene.nodes[node.workflow_task_id].pos() == task_pos_before

    positions = [tnode.pos() for tnode in scene.template_nodes.values()]
    xs = {p.x() for p in positions}
    ys = [p.y() for p in positions]
    assert len(xs) == 1, "同じ対象タスクなので同じ列（同じx）に揃うはず"
    assert len(set(ys)) == 3, "3件とも異なる高さに積み上がっているはず（重ならない）"
    # タスク群の最上段（ここでは対象タスク1件のみ）より、明確な余白を空けて上に配置される。
    assert max(ys) < task_pos_before.y()

    # 1件削除しても、タスクの位置は変わらず、残り2件は重ならず詰め直される。
    removed_id = next(iter(scene.template_nodes))
    scene.delete_dependency_template_node(removed_id)
    qapp.processEvents()
    scene = wf_tab.current_scene
    assert len(scene.template_nodes) == 2
    assert scene.nodes[node.workflow_task_id].pos() == task_pos_before
    remaining_ys = {tnode.pos().y() for tnode in scene.template_nodes.values()}
    assert len(remaining_ys) == 2


def test_reload_positions_dependency_template_nodes_without_overlapping_tasks(window, qapp):
    """回帰テスト: 依存テンプレート追加時は正しく配置されるが、ファイルを
    開いた際（WorkflowGraphScene.reload）は疑似ノードの座標が計算し直されず、
    既定位置(0,0)のままタスクノードと重なってしまっていた（当時はタスクの
    座標だけDB保存済みの値を使い、疑似ノードだけ毎回計算し直す非対称な設計
    だったため）。現在はタスク・疑似ノードとも座標をDBに保存せず、reload()の
    たびに両方をcompute_combined_layoutで計算し直す設計に統一したので、
    この非対称性自体が起こり得ない。reload()を呼んだ後も、テンプレートの
    疑似ノードがタスクの1列上流に正しく配置され、タスクの座標も
    （入力が変わっていないので）計算結果が変わらないことを確認する。"""
    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf1_id = window.db.add_workflow("WF1")
    wf2_id = window.db.add_workflow("WF2")
    task_x = window.db.add_workflow_task(wf2_id, "X", team_id, 1)

    wf_tab.refresh_workflows(select_id=wf1_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    node = scene.add_task("A", team_id, 1, 0, 0)
    qapp.processEvents()
    scene = wf_tab.current_scene
    scene.add_dependency_template_node(node.workflow_task_id, wf2_id, task_x)
    qapp.processEvents()
    task_pos_before = scene.nodes[node.workflow_task_id].pos()

    # reload()はファイルを開いた時・ワークフローを選び直した時に呼ばれる。
    # ここでは直接呼んで同じ状況を再現する。
    scene.reload()
    qapp.processEvents()

    task_node = scene.nodes[node.workflow_task_id]
    template_id = next(iter(scene.template_nodes))
    template_node = scene.template_nodes[template_id]

    assert task_node.pos() == task_pos_before  # 入力が同じなので計算結果も変わらない
    assert (template_node.pos().x(), template_node.pos().y()) != (0, 0)
    assert template_node.pos().x() < task_node.pos().x()  # タスクの1列上流


# -- ワークフロー設計タブ: 複製・名前変更（ダブルクリック） ---------------------------


def test_workflow_list_has_no_dedicated_rename_button_and_double_click_opens_rename_dialog(window, qapp):
    """回帰テスト: 「名前変更」専用ボタンは廃止され、一覧の項目をダブル
    クリックすると同じ名前変更ダイアログが開くこと。"""
    from PySide6.QtWidgets import QInputDialog, QPushButton

    wf_tab = window.tab_workflows
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    button_labels = {b.text() for b in wf_tab.findChildren(QPushButton)}
    assert "名前変更" not in button_labels
    assert "複製" in button_labels

    item = wf_tab.workflow_list.currentItem()
    assert item is not None
    with patch.object(QInputDialog, "getText", return_value=("WF1改", True)):
        wf_tab.workflow_list.itemDoubleClicked.emit(item)
    qapp.processEvents()

    assert [w["name"] for w in window.db.list_workflows()] == ["WF1改"]


def test_duplicate_workflow_copies_content_and_selects_the_copy_as_one_undo_step(window, qapp):
    """「複製」ボタンは、タスク・依存関係を含めて複製し、複製先を選択状態に
    する。複製全体が1回のUndoで元に戻り、選択も複製元へ戻ること。"""
    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    scene.add_task("タスクA", team_id, 3, 0, 0)
    qapp.processEvents()

    stack_before = len(window.undo_manager._undo_stack)
    wf_tab._duplicate_workflow()
    qapp.processEvents()

    assert len(window.undo_manager._undo_stack) == stack_before + 1
    names = [w["name"] for w in window.db.list_workflows()]
    assert names == ["WF1", "WF1のコピー"]

    current_item = wf_tab.workflow_list.currentItem()
    assert current_item is not None and current_item.text() == "WF1のコピー"
    new_wf_id = current_item.data(Qt.UserRole)
    assert [t["name"] for t in window.db.list_workflow_tasks(new_wf_id)] == ["タスクA"]

    window.on_undo()
    qapp.processEvents()
    assert [w["name"] for w in window.db.list_workflows()] == ["WF1"]
    restored_item = wf_tab.workflow_list.currentItem()
    assert restored_item is not None and restored_item.data(Qt.UserRole) == wf_id


def test_edit_dependency_kind_updates_edge_label_and_table(window, qapp):
    """依存関係の種別・ラグを変更すると、ノードビューのエッジラベルと
    テーブルビューの「先行タスク」欄の双方に反映され、Undoで戻ること。
    既定（FS・ラグ0）のエッジにはラベルを出さない。"""
    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    task_a = window.db.add_workflow_task(wf_id, "A", team_id, 1)
    task_b = window.db.add_workflow_task(wf_id, "B", team_id, 1)
    dep_id = window.db.add_task_dependency(wf_id, task_a, task_b)

    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    scene = wf_tab.current_scene
    edge = scene.edges[dep_id]
    assert not edge.label_bg.isVisible()  # 既定はラベル無し

    def pred_cell_text():
        table = wf_tab.task_section.table
        for row in range(table.rowCount()):
            if table.item(row, 0).text() == "B":
                return table.item(row, 3).text()
        return None

    assert pred_cell_text() == "A"

    scene.update_edge_kind(dep_id, "SS", 2)
    qapp.processEvents()
    assert edge.label_bg.isVisible()
    assert edge.label_item.text() == "SS+2"
    assert pred_cell_text() == "A（SS+2）"

    window.undo_manager.undo()
    qapp.processEvents()
    dep = window.db.get_task_dependency(dep_id)
    assert (dep["dep_type"], dep["lag_days"]) == ("FS", 0)
    assert not wf_tab.current_scene.edges[dep_id].label_bg.isVisible()
    assert pred_cell_text() == "A"


def test_dependency_kind_dialog_reports_the_setting_in_words(window, qapp):
    """FS/SS・正負のラグの意味を、ダイアログが日本語の一文で言い直すこと
    （記号だけだと取り違えやすいため）。"""
    from gui.node_canvas import DependencyKindDialog

    dialog = DependencyKindDialog("A", "B", "FS", 0, window)
    assert dialog.values() == ("FS", 0)
    assert "完了後に後続タスクを開始" in dialog.hint.text()

    dialog.lag_spin.setValue(2)
    assert "2 営業日空けて" in dialog.hint.text()

    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("SS"))
    dialog.lag_spin.setValue(-3)
    assert dialog.values() == ("SS", -3)
    assert "3 営業日早く" in dialog.hint.text()
    dialog.deleteLater()


def test_dependency_edge_is_clickable_along_the_curve(window, qapp):
    """依存関係の線をダブルクリック/右クリックで編集できるようにするには、
    2pxのベジェ曲線に当たり判定が付いている必要がある。曲線上および
    その近傍でエッジとして解決でき、ノードの上では誤認しないこと。"""
    wf_tab = window.tab_workflows
    team_id = window.db.add_team("チームA", 1)
    wf_id = window.db.add_workflow("WF1")
    task_a = window.db.add_workflow_task(wf_id, "A", team_id, 1)
    task_b = window.db.add_workflow_task(wf_id, "B", team_id, 1)
    dep_id = window.db.add_task_dependency(wf_id, task_a, task_b)

    wf_tab.refresh_workflows(select_id=wf_id)
    window.tabs.setCurrentWidget(wf_tab)
    qapp.processEvents()

    view, scene = wf_tab.view, wf_tab.current_scene
    # タスクをDBへ直接追加したためノードの座標は既定(0,0)のまま重なっている。
    # 実際の操作（キャンバスからの追加・編集）と同じ配置にしてから判定する。
    scene.auto_arrange()
    qapp.processEvents()
    edge = scene.edges[dep_id]

    for pct in (0.4, 0.5, 0.6):
        point = edge.path().pointAtPercent(pct)
        assert view._resolve_edge_hit(scene.itemAt(point, view.transform())) is edge

    near = edge.path().pointAtPercent(0.5)
    near.setY(near.y() - 5)   # 線から少し外れた位置でも掴める
    assert view._resolve_edge_hit(scene.itemAt(near, view.transform())) is edge

    node_center = scene.nodes[task_a].sceneBoundingRect().center()
    assert view._resolve_edge_hit(scene.itemAt(node_center, view.transform())) is None

    # 線の端は出力アンカーの丸と重なる。そこは依存関係を引くドラッグの
    # 起点なので、エッジではなくアンカーが勝つ（当たり判定を広げても
    # ドラッグでの依存追加を潰さないこと）。
    from gui.node_canvas import AnchorItem

    anchor_point = scene.nodes[task_a].output_anchor_scene_pos()
    assert isinstance(scene.itemAt(anchor_point, view.transform()), AnchorItem)



def test_start_pin_date_column_edits_the_override_and_is_undoable(window, qapp):
    """タスク上書き表の「開始固定日」列（OptionalDateEdit）で日付を選ぶと
    job_task_overrides.start_pin_date に反映され、Undo1回で元に戻ること。
    Deleteキーで固定を解除できることも確認する。"""
    from PySide6.QtCore import QDate

    jobs_tab = window.tab_jobs
    team_id = window.db.add_team("チームA", 1)
    ms_id = window.db.add_milestone("MS1", "2026-06-30")
    wf_id = window.db.add_workflow("WF1")
    task_id = window.db.add_workflow_task(wf_id, "タスク1", team_id, 3)
    job_id = window.db.add_job("ジョブ1", wf_id, ms_id, 1)

    window.tabs.setCurrentWidget(jobs_tab)
    jobs_tab.refresh_jobs(select_id=job_id)
    qapp.processEvents()

    table = jobs_tab.override_table
    pin_edit = table.cellWidget(0, 5)
    assert pin_edit.value() is None
    assert pin_edit.text() == "（固定なし）"

    pin_edit.setDate(QDate(2026, 2, 2))
    qapp.processEvents()
    assert window.db.list_job_tasks_with_overrides(job_id)[0]["start_pin_date"] == "2026-02-02"

    window.undo_manager.undo()
    qapp.processEvents()
    assert window.db.list_job_tasks_with_overrides(job_id)[0]["start_pin_date"] is None
    assert jobs_tab.override_table.cellWidget(0, 5).value() is None

    window.undo_manager.redo()
    qapp.processEvents()
    pin_edit = jobs_tab.override_table.cellWidget(0, 5)
    assert pin_edit.value() == "2026-02-02"

    # Deleteキーで固定を解除できる（カレンダーを未設定の特殊値まで戻す必要が無い）。
    # keyPressEvent()を直接呼ぶだけなのでQtの実フォーカスは動かさない——
    # setFocus()すると bind_undo_session の Undo単位が開いたままになり、
    # フィクスチャ側でDBを閉じた後にフォーカス喪失イベントが発火してクラッシュする。
    from PySide6.QtCore import QEvent, Qt as QtCore_Qt
    from PySide6.QtGui import QKeyEvent
    pin_edit.keyPressEvent(QKeyEvent(QEvent.KeyPress, QtCore_Qt.Key_Delete, QtCore_Qt.NoModifier))
    qapp.processEvents()
    assert pin_edit.value() is None


def test_start_pin_date_calendar_popup_opens_near_today_not_year_2000(window, qapp):
    """回帰テスト: 「開始固定日」が未設定（特殊値の2000-01-01）のままカレンダーを
    開くと、表示ページが2000年になってしまい現在の年まで大きくスクロールする
    必要があった。gui/widgets_common.py の _sync_calendar_page_to_today は
    以前から存在したが、mousePressEvent内でsuper()を呼ぶ「前」に効かせようと
    していたため、QDateTimeEdit自身がポップアップを開く際に自分の日付
    （＝2000-01-01）へ表示ページを上書きし直す処理が後から効いてしまい、
    実際には直っていなかった（QTest.mouseClickで実際にポップアップを開いて
    確認しないと検出できない——setDate()を直接呼ぶだけのテストでは再現しない）。"""
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    jobs_tab = window.tab_jobs
    team_id = window.db.add_team("チームA", 1)
    ms_id = window.db.add_milestone("MS1", "2026-06-30")
    wf_id = window.db.add_workflow("WF1")
    window.db.add_workflow_task(wf_id, "タスク1", team_id, 3)
    job_id = window.db.add_job("ジョブ1", wf_id, ms_id, 1)

    window.tabs.setCurrentWidget(jobs_tab)
    jobs_tab.refresh_jobs(select_id=job_id)
    qapp.processEvents()

    pin_edit = jobs_tab.override_table.cellWidget(0, 5)
    assert pin_edit.value() is None  # 未設定（特殊値の2000-01-01）から始める

    # ドロップダウンの矢印ボタンは右端にある。ウィジェット全体へのクリックとして
    # 実際にQtのイベントを流し、QDateTimeEdit本体のポップアップ表示処理まで
    # 走らせる（内部実装への依存を避けるため、正確なボタン矩形は問わない）。
    QTest.mouseClick(
        pin_edit, Qt.LeftButton, Qt.NoModifier, QPoint(pin_edit.width() - 10, pin_edit.height() // 2),
    )
    qapp.processEvents()

    calendar = pin_edit.calendarWidget()
    today = QDate.currentDate()
    assert (calendar.yearShown(), calendar.monthShown()) == (today.year(), today.month())
    # ポップアップを開いただけでは値そのものは変えない。
    assert pin_edit.value() is None


def _fresh_pin_edit(window, qapp, name_suffix=""):
    """ジョブタブのタスク上書き表を1行だけ用意し、その「開始固定日」欄を返す。"""
    team_id = window.db.add_team(f"チームA{name_suffix}", 1)
    ms_id = window.db.add_milestone(f"MS1{name_suffix}", "2026-06-30")
    wf_id = window.db.add_workflow(f"WF1{name_suffix}")
    window.db.add_workflow_task(wf_id, "タスク1", team_id, 3)
    job_id = window.db.add_job(f"ジョブ1{name_suffix}", wf_id, ms_id, 1)

    window.tabs.setCurrentWidget(window.tab_jobs)
    window.tab_jobs.refresh_jobs(select_id=job_id)
    qapp.processEvents()

    pin_edit = window.tab_jobs.override_table.cellWidget(0, 5)
    assert pin_edit.value() is None  # 未設定（特殊値の2000-01-01）から始める
    return pin_edit, job_id


def test_start_pin_date_steps_from_today_not_from_the_year_2000_sentinel(window, qapp):
    """回帰テスト: 未設定（特殊値の2000-01-01）の開始固定日を▲キー・スピンの
    矢印・ホイールで動かすと、Qtの既定動作では最小値の**年セクション**が1つ
    進んで 2001-01-01 になってしまい、「日付を変えようとすると2000年代に
    なる」という状態だった。

    未設定から動かした1歩目は今日を起点にする（DefaultAwareSpinBoxが0から
    既定値を起点に増減するのと同じ考え方。gui/widgets_common.py の
    OptionalDateEdit.stepBy 参照）。カレンダーの表示ページだけを直した以前の
    修正では、この経路には効いていなかった。"""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    today_iso = QDate.currentDate().toString("yyyy-MM-dd")

    # 経路1: スピンの矢印ボタン相当（stepBy）。
    pin_edit, job_id = _fresh_pin_edit(window, qapp)
    pin_edit.stepBy(1)
    qapp.processEvents()
    assert pin_edit.value() == today_iso
    assert window.db.list_job_tasks_with_overrides(job_id)[0]["start_pin_date"] == today_iso

    # 経路2: ▲キー（QDateEdit内部でstepByを呼ぶ）。
    pin_edit2, _ = _fresh_pin_edit(window, qapp, name_suffix="_b")
    pin_edit2.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Up, Qt.NoModifier))
    qapp.processEvents()
    assert pin_edit2.value() == today_iso

    # 2歩目以降は通常どおりカーソル位置のセクションを増減する（今日に張り付かない）。
    pin_edit2.stepBy(1)
    qapp.processEvents()
    assert pin_edit2.value() != today_iso
    assert pin_edit2.value() is not None


def test_start_pin_date_typing_a_digit_does_not_corrupt_the_special_value_text(window, qapp):
    """回帰テスト: 未設定の間は表示が「（固定なし）」という特殊テキストのため、
    そこへ数字を打つとQtはセクションを更新できず、'（固定なし）0260415' の
    ような壊れた表示になったうえ値も入らなかった。数字の打鍵で入力を
    始めた場合は、先に今日を入れて通常の日付表示にしてから打鍵を適用する。"""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    pin_edit, _ = _fresh_pin_edit(window, qapp)
    pin_edit.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_2, Qt.NoModifier, "2"))
    qapp.processEvents()

    # 特殊テキストが残った壊れた表示になっていないこと。
    assert "（固定なし）" not in pin_edit.text()
    # 値として読める日付になっていること（2000年代の番兵ではない）。
    assert pin_edit.value() is not None
    assert pin_edit.date().year() >= QDate.currentDate().year()


def _collapsible_header_color(section):
    """折りたたみセクションの見出しに指定されている文字色（'#rrggbb'）を取り出す。"""
    import re

    match = re.search(r"color:\s*(#[0-9a-fA-F]{6})", section._toggle_btn.styleSheet())
    assert match is not None, f"見出しに文字色が指定されていない: {section._toggle_btn.styleSheet()!r}"
    return match.group(1).lower()


def test_collapsible_section_header_color_follows_the_palette(window, qapp):
    """回帰テスト: 「絞り込み」の見出し（CollapsibleSection）は文字色をスタイル
    任せにしていたため、Windows 11のライトモードで白く描かれ、背景と同化して
    読めなくなっていた（利用者からの報告。Linuxのスタイルでは再現しないため、
    ここでは「パレットのWindowTextを明示的に使っているか」を検証する）。

    ハードコードした色にするとダークモードで逆に見えなくなるので、
    ライト/ダークそれぞれのパレットに追従することを確認する。"""
    from PySide6.QtGui import QColor, QPalette

    from gui.widgets_common import CollapsibleSection

    def palette_with_text(window_color, text_color):
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(window_color))
        palette.setColor(QPalette.WindowText, QColor(text_color))
        return palette

    # ライト相当（黒文字）のパレットで作れば黒、ダーク相当なら白になる。
    for window_color, text_color in (("#f0f0f0", "#000000"), ("#202020", "#ffffff")):
        holder = QWidget()
        holder.setPalette(palette_with_text(window_color, text_color))
        section = CollapsibleSection("絞り込み", holder)
        assert _collapsible_header_color(section) == text_color

    # 起動後にOSのテーマが切り替わった場合も追従する（固定色で取り残されない）。
    holder = QWidget()
    holder.setPalette(palette_with_text("#f0f0f0", "#000000"))
    section = CollapsibleSection("絞り込み", holder)
    assert _collapsible_header_color(section) == "#000000"

    holder.setPalette(palette_with_text("#202020", "#ffffff"))
    qapp.processEvents()
    assert _collapsible_header_color(section) == "#ffffff"


def test_filters_section_headers_are_readable_in_jobs_and_gantt_tabs(window, qapp):
    """利用者が報告した2箇所——ジョブ作成タブとガントチャートタブの「絞り込み」
    ——の見出しに、実際に文字色が入っていること（両タブとも同じ
    CollapsibleSectionを使っているので、片方だけ直り残しになっていないか）。"""
    for tab in (window.tab_jobs, window.tab_gantt):
        color = _collapsible_header_color(tab.filters_section)
        assert color == window.palette().color(QPalette.WindowText).name().lower()


def _focused_placement_spinbox(window, qapp):
    """ガントチャートタブを開き、配置コントロールの数値入力欄にフォーカスを当てる。"""
    _build_schedulable_project(window.db)
    window.tabs.setCurrentWidget(window.tab_gantt)
    _wait_for_schedule(window, qapp)

    spin = window.tab_gantt.placement_spinbox
    spin.setFocus()
    qapp.processEvents()
    assert spin.hasFocus()
    return spin


def test_placement_spinbox_arrows_do_not_recompute_until_focus_leaves(window, qapp):
    """回帰テスト: 配置コントロールの▲▼を押すたびにDB書き込み＋再計算が走って
    いた（重いうえ、Undoが1押しずつ積み上がってしまう）。値の変更は表示だけに
    留め、確定はアクティブ状態が外れた時（またはEnter）に1回だけ行う。"""
    spin = _focused_placement_spinbox(window, qapp)
    tab = window.tab_gantt

    revision_before = window.db.revision
    seq_before = window.schedule_cache._request_seq
    start_value = spin.value()

    for _ in range(3):
        spin.stepBy(1)
        qapp.processEvents()

    # 表示（スピンボックスとスライダー）は追従するが、DBも再計算も動かない。
    assert spin.value() == start_value + 3
    assert tab.placement_slider.value() == start_value + 3
    assert window.db.revision == revision_before
    assert window.schedule_cache._request_seq == seq_before

    # アクティブ状態が外れて初めて、1回だけ確定・再計算される。
    spin.clearFocus()
    qapp.processEvents()
    assert window.db.revision != revision_before
    assert window.schedule_cache._request_seq == seq_before + 1
    assert window.db.get_project()["distribution_ratio"] == (start_value + 3) / 100.0


def test_placement_spinbox_arrows_collapse_into_a_single_undo_entry(window, qapp):
    """▲を複数回押してからフォーカスを外すまでが1つのUndo単位であること。

    このUndoのまとめ自体は bind_undo_session（フォーカスの出入りで単位を
    開閉する）が以前から担っており、確定を遅らせる変更の前後で壊れていない
    ことを守るためのテスト。確定をフォーカスアウトへ移した際、DB書き込みが
    Undo単位の「外」に出てしまうと（gui/widgets_common.py の
    _UndoSessionMixin.focusOutEvent が end_undo_group を呼んだ後に書き込むと）
    ここが壊れる。"""
    spin = _focused_placement_spinbox(window, qapp)
    original_ratio = window.db.get_project()["distribution_ratio"]

    for _ in range(3):
        spin.stepBy(1)
        qapp.processEvents()
    spin.clearFocus()
    qapp.processEvents()

    changed_ratio = window.db.get_project()["distribution_ratio"]
    assert changed_ratio != original_ratio

    # Undo1回で、3回ぶんの▲がまとめて元に戻る。
    window.undo_manager.undo()
    qapp.processEvents()
    assert window.db.get_project()["distribution_ratio"] == original_ratio


def test_placement_spinbox_enter_commits_once_and_focus_out_does_not_repeat_it(window, qapp):
    """Enterでの確定は即座に反映してよいが、その後フォーカスが外れた際に
    同じ内容の確定・再計算が二重に走らないこと。

    確定をフォーカスアウトへ移したことで新たに生じる危険を守るためのテスト
    ——editingFinished は Enter と フォーカスアウトの両方で飛ぶため、
    _commit_distribution_ratio の「値が変わっていなければ何もしない」判定を
    外すと、Enterで確定した直後にフォーカスを外しただけで同じ再計算が
    もう一度走ってしまう。"""
    spin = _focused_placement_spinbox(window, qapp)

    spin.stepBy(1)
    qapp.processEvents()

    QTest.keyClick(spin, Qt.Key_Return)
    qapp.processEvents()
    revision_after_enter = window.db.revision
    seq_after_enter = window.schedule_cache._request_seq
    ratio_after_enter = window.db.get_project()["distribution_ratio"]
    assert ratio_after_enter == spin.value() / 100.0

    spin.clearFocus()
    qapp.processEvents()
    assert window.db.revision == revision_after_enter
    assert window.schedule_cache._request_seq == seq_after_enter


def test_placement_spinbox_loses_focus_when_clicking_elsewhere_in_the_tab(window, qapp):
    """配置コントロールの外（並びの枠・枠の余白・タブの空き領域）を
    クリックしたら、数値入力欄のアクティブ状態が外れること。以前はこれらが
    どれもフォーカスを受け取らないため、クリックしてもアクティブなままだった
    （チャート本体をクリックした場合だけ外れていた）。"""
    tab = window.tab_gantt
    spin = _focused_placement_spinbox(window, qapp)

    for target in (tab.row_resort_button.parentWidget(), tab.placement_group, tab):
        spin.setFocus()
        qapp.processEvents()
        assert spin.hasFocus()

        QTest.mouseClick(target, Qt.LeftButton, Qt.NoModifier, QPoint(3, 3))
        qapp.processEvents()
        assert not spin.hasFocus(), f"{type(target).__name__} のクリックでフォーカスが外れていない"


def test_gantt_tab_reports_unsatisfiable_pin_in_the_status_line(window, qapp):
    """満たせない開始固定日は例外ではなく結果として返るため、状況表示で
    件数を出さないと気付けない。"""
    import time

    team_id = window.db.add_team("チームA", 1)
    ms_id = window.db.add_milestone("MS1", "2026-06-30")
    wf_id = window.db.add_workflow("WF1")
    task_a = window.db.add_workflow_task(wf_id, "タスクA", team_id, 3)
    task_b = window.db.add_workflow_task(wf_id, "タスクB", team_id, 3)
    window.db.add_task_dependency(wf_id, task_a, task_b)
    job_id = window.db.add_job("ジョブ1", wf_id, ms_id, 1)
    window.db.set_project("固定日テスト", "2026-01-05")
    # 依存元(タスクA)より前にタスクBを固定する＝矛盾
    window.db.upsert_job_task_override(job_id, task_b, start_pin_date="2026-01-06")

    window.tabs.setCurrentWidget(window.tab_gantt)
    for _ in range(100):
        qapp.processEvents()
        if window.tab_gantt._result_df is not None:
            break
        time.sleep(0.05)

    summary, errors = window.tab_gantt._result_summary()
    assert "件のタスクを生成しました" in summary
    # エラーはタブ内のエラーの段に出す（件数は画面下部）
    assert "開始固定日どおりに配置できません" in errors
    assert "依存タスクの着手可能日" in errors
    assert window.tab_gantt.error_label.text() == errors
    assert not window.tab_gantt.error_label.isHidden()


# -- 名前を付けて保存：拡張子自動付与時の上書き確認（gui/main.py の on_save_as） ----------
#
# QFileDialogの上書き確認は、ユーザーがダイアログで実際に選んだパス（拡張子を
# 補う前）に対してのもの。on_save_as() が拡張子を補った結果の実際の書き込み先が
# 別の既存ファイルと衝突する場合、ユーザーが目にしていないファイルを無確認で
# 上書きしてしまわないよう、on_save_as() 側で改めて確認することを検証する。

def test_save_as_without_extension_confirms_before_overwriting_existing_file(window, qapp, tmp_path):
    """回帰テスト: ダイアログで 'foo.txt' を選ぶと、その名前の上書き確認だけを
    経て、実際の書き込み先は 'foo.txt.pschedule' になる。この時
    'foo.txt.pschedule' が既に存在すれば、以前は無確認で上書きしていた。"""
    window.db.set_project("上書き確認テスト", "2026-01-05")

    from PySide6.QtWidgets import QMessageBox

    existing = tmp_path / "foo.txt.pschedule"
    existing.write_bytes(b"as-is: should not be overwritten without confirmation")
    picked = str(tmp_path / "foo.txt")  # ユーザーはこの名前（拡張子なし）を選ぶ

    with patch("gui.main.QFileDialog.getSaveFileName", return_value=(picked, "")), \
         patch("gui.main.QMessageBox.question", return_value=QMessageBox.No) as mock_confirm:
        result = window.on_save_as()

    assert mock_confirm.called  # 補った後のパスに対して確認したこと
    assert result is False
    assert existing.read_bytes() == b"as-is: should not be overwritten without confirmation"
    assert window.db.path != str(existing)  # 保存先として確定していないこと


def test_save_as_without_extension_overwrites_after_confirmation(window, qapp, tmp_path):
    """確認で「はい」を選べば、従来どおり拡張子を補って保存されること。"""
    window.db.set_project("上書き確認テスト", "2026-01-05")

    from PySide6.QtWidgets import QMessageBox

    existing = tmp_path / "foo.txt.pschedule"
    existing.write_bytes(b"old content")
    picked = str(tmp_path / "foo.txt")

    with patch("gui.main.QFileDialog.getSaveFileName", return_value=(picked, "")), \
         patch("gui.main.QMessageBox.question", return_value=QMessageBox.Yes) as mock_confirm:
        result = window.on_save_as()

    assert mock_confirm.called
    assert result is True
    assert window.db.path == str(existing)
    assert existing.read_bytes() != b"old content"  # 実際に上書きされている


def test_save_as_with_extension_already_typed_does_not_prompt_again(window, qapp, tmp_path):
    """既に'.pschedule'付きで選んだ場合は、拡張子を補う分岐そのものを通らない
    ため、ここでの二重確認は発生しない（QFileDialog自身の確認に任せる）。"""
    window.db.set_project("上書き確認テスト", "2026-01-05")

    existing = tmp_path / "bar.pschedule"
    existing.write_bytes(b"old content")
    picked = str(existing)

    with patch("gui.main.QFileDialog.getSaveFileName", return_value=(picked, "")), \
         patch("gui.main.QMessageBox.question") as mock_confirm:
        result = window.on_save_as()

    assert not mock_confirm.called
    assert result is True
    assert window.db.path == str(existing)
