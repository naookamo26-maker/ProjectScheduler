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


def test_team_add_undo_redo_restores_selection(window, qapp):
    """内容と、表の選択（行・列）が復元されること。"""
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


def test_spinbox_edits_collapse_into_one_undo_step_at_focus_out(window, qapp):
    """回帰テスト: スピンボックスを連続して変更しても、フォーカスが外れるまでは
    Undoが分かれず、外れた時点で1エントリにまとまること。

    DBは変更のたびに更新される（値を変えた直後に保存しても取りこぼさない）が、
    Undoの単位は「フォーカスを得てから外れるまで」でまとめる。"""
    bi = window.tab_basic_info
    window.db.add_team("チームA", 1)
    bi.refresh_all()
    qapp.processEvents()
    spin = bi.teams_section.table.cellWidget(0, 1)

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
    届かず、Undoが効かなくなったように見える。現在セルの復元は、その列に
    セルウィジェットがあるとフォーカスを移してしまうので、選択だけを戻す。"""
    bi = window.tab_basic_info
    window.db.add_team("チームA", 1)
    bi.refresh_all()
    qapp.processEvents()

    spin = bi.teams_section.table.cellWidget(0, 1)
    spin.setFocus()
    qapp.processEvents()
    spin.stepBy(1)
    qapp.processEvents()
    bi.project_name_edit.setFocus()  # 編集を確定し、フォーカスを表の外へ
    qapp.processEvents()

    window.on_undo()
    qapp.processEvents()
    focused = QApplication.focusWidget()
    assert focused is bi.project_name_edit, f"フォーカスが移動している: {type(focused).__name__}"
    # 選択自体は復元されている
    assert bi.teams_section.table.currentColumn() == 1


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


def test_gantt_tab_reports_validation_errors_without_a_modal(window, qapp):
    """未完成なプロジェクトでガントチャートタブに切り替えても、モーダル
    ダイアログではなくタブ内の表示でエラーを知らせること。

    refresh_choices() は「タブが表示されるたび」「Undo/Redoで表示を作り直す
    たび」に自動的に呼ばれるため、ダイアログにするとプロジェクトが未完成な
    間ずっと操作に割り込むことになる（headlessテストでは応答できず停止する）。"""
    window.tabs.setCurrentWidget(window.tab_gantt)
    qapp.processEvents()

    assert window.tab_gantt._result_df is None
    assert "解決してください" in window.tab_gantt.status_label.text()


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
