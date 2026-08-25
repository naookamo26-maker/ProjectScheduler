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
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

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
    w.db.on_change = None
    w.db.undo_manager = None
    w.db.close()


def test_team_add_undo_redo_restores_selection_and_focus(window, qapp):
    bi = window.tab_basic_info
    new_id = window.db.add_team("チームテスト", 2)
    bi.refresh_teams()
    select_row_by_id(bi.teams_section.table, new_id)
    bi.teams_section.table.setFocus()
    qapp.processEvents()

    window.on_undo()
    qapp.processEvents()
    assert window.db.list_teams() == []
    assert window.undo_manager.can_redo()

    window.on_redo()
    qapp.processEvents()
    assert [t["name"] for t in window.db.list_teams()] == ["チームテスト"]

    row = bi.teams_section.table.currentRow()
    assert row >= 0
    assert row_id(bi.teams_section.table, row) == new_id
    assert QApplication.focusWidget() is bi.teams_section.table


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
    wf_tab.view.setFocus()
    qapp.processEvents()

    # add_task内部でauto_arrangeが複数のupdate_task_positionを呼んでも、
    # 呼び出し全体でUndo1件にまとまっていること。
    assert len(window.undo_manager._undo_stack) == stack_size_before + 1
    assert window.db.list_workflow_tasks(wf_id) != []

    window.on_undo()
    qapp.processEvents()
    assert window.db.list_workflow_tasks(wf_id) == []

    window.on_redo()
    qapp.processEvents()
    tasks = window.db.list_workflow_tasks(wf_id)
    assert len(tasks) == 1
    restored_scene = wf_tab.current_scene
    restored_node = restored_scene.nodes[tasks[0]["id"]]
    assert restored_node.isSelected()
    assert wf_tab.view.hasFocus()


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


def test_hidden_gantt_tab_is_not_refreshed_during_undo(window, qapp):
    """ガントチャートタブは、未完成なプロジェクトに対してrefresh_choices()を
    呼ぶと検証エラーのモーダルダイアログを表示する仕様のため、非表示のまま
    裏側でUndo/Redoのたびに自動的に呼ばれてしまうと、無関係なダイアログで
    アプリがフリーズしてしまう（実装中に実際に踏んだ回帰）。アクティブでない
    タブのrefresh_choices()が呼ばれないことをスパイで確認する。"""
    window.tabs.setCurrentWidget(window.tab_basic_info)
    qapp.processEvents()
    assert window.tabs.currentWidget() is not window.tab_gantt

    window.db.add_team("チームB", 1)  # プロジェクトは依然未完成（検証エラーになる状態）

    with patch.object(GanttTab, "refresh_choices") as mocked:
        window.on_undo()
        qapp.processEvents()
        mocked.assert_not_called()
