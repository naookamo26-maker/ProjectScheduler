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
