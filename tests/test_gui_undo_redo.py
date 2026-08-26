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
