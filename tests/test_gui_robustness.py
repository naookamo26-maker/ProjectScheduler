"""
利用者の操作でクラッシュ・表示の食い違い・データの不整合が起きないことの、GUIレベルの
回帰テスト（ガントチャートタブの分は tests/test_gui_gantt_edit.py）。

headless Qt（offscreen）で MainWindow を作り、各タブのハンドラを実際に呼ぶ。
確認ダイアログ・メッセージは差し替える。
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6")
pytest.importorskip("pandas")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from gui.db import MAX_SUPPORTED_DATE, ProjectDatabase  # noqa: E402
from gui.widgets_common import row_id  # noqa: E402

# 分類: gui（PySide6 + offscreen QApplication が必要）
pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def window(qapp):
    from gui.main import MainWindow

    w = MainWindow()
    w.show()
    qapp.processEvents()
    w._open_database(ProjectDatabase.create_new())
    qapp.processEvents()
    yield w
    # 予約済みの処理（Undoの「操作直後のUI状態」の記録など）を、ウィンドウを破棄する
    # 前に済ませる（破棄後に走ると、破棄済みのオブジェクトを触って例外になる）
    qapp.processEvents()
    w._shutdown_schedule_cache()
    w.db.on_change = None
    w.db.undo_manager = None
    w.db.close()
    import shiboken6
    w.hide()
    shiboken6.delete(w)
    qapp.processEvents()


def _repair_dialog_returns(result):
    """マイルストーンの整合性の確認ダイアログを差し替え、呼ばれた回数を数える。"""
    calls = []

    def fake_exec(self):
        calls.append(self)
        return result
    return patch("gui.widgets_common.MilestoneRepairConfirmDialog.exec", fake_exec), calls


def _chain_project(db):
    """A→B の依存を持つワークフローと、締切の違う3つのマイルストーン。"""
    with db.undo_group("準備"):
        db.set_project("P", "2026-01-05")
        team = db.add_team("チームA", 1)
        early = db.add_milestone("早い", "2026-03-31")
        middle = db.add_milestone("中間", "2026-06-30")
        late = db.add_milestone("遅い", "2026-12-25")
        wf = db.add_workflow("WF")
        a = db.add_workflow_task(wf, "A", team, 2)
        b = db.add_workflow_task(wf, "B", team, 2)
        db.add_task_dependency(wf, a, b)
    return {"team": team, "early": early, "middle": middle, "late": late, "wf": wf, "a": a, "b": b}


# -- プロジェクトを開き直したときの旧タブ ---------------------------------------------------


def test_reopening_a_project_disposes_of_the_previous_tabs(qapp, window):
    """回帰テスト: 開き直しても旧タブ一式が破棄されずに残り、開くたびにメモリが
    増え続けていた（大規模サンプルで1回あたり約200MB）。"""
    from gui.tab_jobs import JobsTab

    for _ in range(3):
        window._open_database(ProjectDatabase.create_new())
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()
    assert len(window.tabs.findChildren(JobsTab)) == 1


# -- ワークフロー設計タブ -------------------------------------------------------------------


def test_adding_a_task_with_a_duplicate_name_warns_and_keeps_the_input(qapp, window):
    """回帰テスト: 同じ名前のタスクを追加すると例外が捕まえられず、何も表示されずに
    失敗していた（さらにトランザクションが開いたまま残り、直後の保存が固まった）。"""
    from gui import node_canvas

    db = window.db
    ids = _chain_project(db)
    tab = window.tab_workflows
    window.tabs.setCurrentWidget(tab)
    tab.refresh_workflows(select_id=ids["wf"])
    qapp.processEvents()

    results = iter([QDialog.Accepted, QDialog.Rejected])
    shown_names = []

    def fake_exec(dialog):
        dialog.name_edit.setText("A") if not shown_names else None
        shown_names.append(dialog.name_edit.text())
        return next(results)

    with patch.object(node_canvas.TaskNodeEditDialog, "exec", fake_exec), \
            patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as warning:
        node_canvas.add_task_via_dialog(tab.current_scene, tab)
    assert warning.call_count == 1
    assert shown_names == ["A", "A"]  # 2回目も入力内容が残っている
    assert [t["name"] for t in db.list_workflow_tasks(ids["wf"])] == ["A", "B"]
    assert not db._conn.in_transaction


def test_cancelling_a_new_team_keeps_the_previously_selected_team(qapp, window):
    """回帰テスト: チーム欄で「＋ 新しいチームを追加...」を選んでキャンセルすると、
    先頭のチームに戻ってしまい、気付かずにOKすると別のチームで登録されていた。"""
    from gui.node_canvas import _ADD_TEAM_SENTINEL, TaskNodeEditDialog

    db = window.db
    with db.undo_group("準備"):
        db.add_team("チームA", 1)
        team_b = db.add_team("チームB", 1)
    dialog = TaskNodeEditDialog(db, "タスクを編集", name="X", team_id=team_b)
    sentinel = dialog.team_combo.findData(_ADD_TEAM_SENTINEL)
    dialog.team_combo.setCurrentIndex(sentinel)
    with patch("gui.node_canvas.QInputDialog.getText", return_value=("", False)):
        dialog.team_combo.activated.emit(sentinel)
    assert dialog.team_combo.currentData() == team_b
    dialog.deleteLater()


def test_deleting_a_task_bridges_dependencies_without_asking_about_other_jobs(qapp, window):
    """回帰テスト: タスクを削除したときの前後の橋渡し（A→B→C の B を消して A→C）で、
    プロジェクト内の無関係な不整合について確認が出て、キャンセルすると橋渡しが作られず
    前後関係が黙って失われていた。"""
    db = window.db
    with db.undo_group("準備"):
        db.set_project("P", "2026-01-05")
        team = db.add_team("T", 1)
        early = db.add_milestone("早い", "2026-03-31")
        late = db.add_milestone("遅い", "2026-12-25")
        wf = db.add_workflow("WF")
        a = db.add_workflow_task(wf, "A", team, 2)
        b = db.add_workflow_task(wf, "B", team, 2)
        c = db.add_workflow_task(wf, "C", team, 2)
        db.add_task_dependency(wf, a, b)
        db.add_task_dependency(wf, b, c)
        # 別のワークフローのジョブに、既に不整合がある（古いファイル等）
        wf2 = db.add_workflow("WF2")
        x = db.add_workflow_task(wf2, "X", team, 1)
        y = db.add_workflow_task(wf2, "Y", team, 1)
        db.add_task_dependency(wf2, x, y)
        job = db.add_job("J", wf2, early)
        db.upsert_job_task_override(job, x, milestone_id=late)
    assert db.plan_milestone_consistency_repair()
    tab = window.tab_workflows
    window.tabs.setCurrentWidget(tab)
    tab.refresh_workflows(select_id=wf)
    qapp.processEvents()

    repair_patch, calls = _repair_dialog_returns(QDialog.Rejected)
    with repair_patch, patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        tab.current_scene.delete_node(tab.current_scene.nodes[b])
    assert calls == []
    assert [(d["predecessor_task_id"], d["successor_task_id"]) for d in db.list_task_dependencies(wf)] == [(a, c)]


# -- ジョブ作成タブ -------------------------------------------------------------------------


def _select_job_row(tab, job_id):
    table = tab.jobs_section.table
    row = next(r for r in range(table.rowCount()) if row_id(table, r) == job_id)
    table.setCurrentCell(row, 0)
    tab._sync_job_row_widgets()
    return row


def test_changing_job_default_milestone_asks_to_repair_and_cancel_reverts(qapp, window):
    """回帰テスト: ジョブ一覧で既定マイルストーンを早めると、上書きを持つ先行タスクより
    後続タスクの締切が早くなる不整合が、確認なしに生じていた。締切日の変更と同じく
    確認し、キャンセルなら変更を取り消す（Undoにも積まない）。"""
    db = window.db
    ids = _chain_project(db)
    with db.undo_group("準備"):
        job = db.add_job("J", ids["wf"], ids["late"])
        db.upsert_job_task_override(job, ids["a"], milestone_id=ids["late"])
    tab = window.tab_jobs
    window.tabs.setCurrentWidget(tab)
    qapp.processEvents()
    row = _select_job_row(tab, job)
    steps = len(window.undo_manager._undo_stack)

    combo = tab.jobs_section.table.cellWidget(row, 2)
    repair_patch, calls = _repair_dialog_returns(QDialog.Rejected)
    with repair_patch:
        combo.setCurrentIndex(combo.findData(ids["early"]))
    qapp.processEvents()
    assert len(calls) == 1
    assert next(j for j in db.list_jobs() if j["id"] == job)["default_milestone_id"] == ids["late"]
    assert len(window.undo_manager._undo_stack) == steps
    row = _select_job_row(tab, job)
    assert tab.jobs_section.table.cellWidget(row, 2).currentData() == ids["late"]

    combo = tab.jobs_section.table.cellWidget(row, 2)
    repair_patch, calls = _repair_dialog_returns(QDialog.Accepted)
    with repair_patch:
        combo.setCurrentIndex(combo.findData(ids["early"]))
    qapp.processEvents()
    assert len(calls) == 1
    assert db.plan_milestone_consistency_repair() == []
    assert len(window.undo_manager._undo_stack) == steps + 1  # 変更と再調整で1回のUndo


def test_deleting_a_milestone_asks_to_repair_and_cancel_keeps_it(qapp, window):
    """回帰テスト: 後続タスクの上書きが参照するマイルストーンを削除すると、その
    タスクがジョブの既定（早い締切）に戻り、先行タスクより早い締切になる不整合が
    確認なしに生じていた。"""
    db = window.db
    ids = _chain_project(db)
    with db.undo_group("準備"):
        job = db.add_job("J", ids["wf"], ids["early"])
        db.upsert_job_task_override(job, ids["a"], milestone_id=ids["middle"])
        db.upsert_job_task_override(job, ids["b"], milestone_id=ids["late"])
    tab = window.tab_basic_info
    window.tabs.setCurrentWidget(tab)
    tab.refresh_milestones()
    table = tab.milestones_section.table
    row = next(r for r in range(table.rowCount()) if row_id(table, r) == ids["late"])
    steps = len(window.undo_manager._undo_stack)

    repair_patch, calls = _repair_dialog_returns(QDialog.Rejected)
    with repair_patch, patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        tab._delete_milestone(row)
    assert len(calls) == 1
    assert db.get_milestone(ids["late"]) is not None
    assert len(window.undo_manager._undo_stack) == steps

    repair_patch, calls = _repair_dialog_returns(QDialog.Accepted)
    with repair_patch, patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        tab._delete_milestone(row)
    assert db.get_milestone(ids["late"]) is None
    assert db.plan_milestone_consistency_repair() == []
    window.on_undo()  # 削除と再調整が1回で戻る
    assert db.get_milestone(ids["late"]) is not None


def test_changing_job_workflow_refreshes_the_dependency_tree(qapp, window):
    """回帰テスト: ジョブのワークフローを変えても「依存先ジョブ」欄が作り直されず、
    DBでは消えたタスク対応が表示されたままだった。"""
    db = window.db
    with db.undo_group("準備"):
        team = db.add_team("T", 1)
        wf_a = db.add_workflow("WF_A")
        wf_b = db.add_workflow("WF_B")
        wf_c = db.add_workflow("WF_C")
        a1 = db.add_workflow_task(wf_a, "A1", team, 1)
        b1 = db.add_workflow_task(wf_b, "B1", team, 1)
        db.add_workflow_task(wf_c, "C1", team, 1)
        job = db.add_job("J", wf_b, None)
        target = db.add_job("依存先", wf_a, None)
        db.add_dependency_template(wf_b, b1, wf_a, a1)
        db.add_job_dependency_link(job, target)
    tab = window.tab_jobs
    window.tabs.setCurrentWidget(tab)
    qapp.processEvents()
    row = _select_job_row(tab, job)
    qapp.processEvents()
    assert tab.dep_tree.topLevelItem(0).childCount() == 1

    combo = tab.jobs_section.table.cellWidget(row, 1)
    combo.setCurrentIndex(combo.findData(wf_c))
    qapp.processEvents()
    assert db.list_external_dependencies(job_id=job) == []
    assert tab.dep_tree.topLevelItem(0).childCount() == 0


def test_blank_job_name_is_rejected_in_the_jobs_table(qapp, window):
    """回帰テスト: ジョブ名を空欄にでき、選択肢や絞り込みに何も表示されない項目ができた。"""
    db = window.db
    with db.undo_group("準備"):
        team = db.add_team("T", 1)
        wf = db.add_workflow("WF")
        db.add_workflow_task(wf, "A", team, 1)
        job = db.add_job("ジョブ1", wf, None)
    tab = window.tab_jobs
    window.tabs.setCurrentWidget(tab)
    qapp.processEvents()
    row = _select_job_row(tab, job)
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as warning:
        tab.jobs_section.table.item(row, 0).setText("   ")
    assert warning.call_count == 1
    assert [j["name"] for j in db.list_jobs()] == ["ジョブ1"]
    row = _select_job_row(tab, job)
    tab.jobs_section.table.item(row, 0).setText("  ジョブ2 ")
    assert [j["name"] for j in db.list_jobs()] == ["ジョブ2"]
    assert tab.jobs_section.table.item(row, 0).text() == "ジョブ2"


def test_plan_column_is_hidden_before_a_job_is_selected(qapp, window):
    """回帰テスト: 未確定のプロジェクトで、ジョブを選ぶ前のタスク上書き表に「確定日程」列の
    見出しだけが出ていた。"""
    from gui.tab_jobs import _OVERRIDE_PLAN_COLUMN

    tab = window.tab_jobs
    window.tabs.setCurrentWidget(tab)
    qapp.processEvents()
    assert tab.current_job_id is None
    assert tab.override_table.isColumnHidden(_OVERRIDE_PLAN_COLUMN)


# -- 基本情報設定タブ -----------------------------------------------------------------------


def test_team_name_spaces_are_trimmed_and_blank_names_rejected(qapp, window):
    db = window.db
    with db.undo_group("準備"):
        team = db.add_team("チームA", 1)
    tab = window.tab_basic_info
    window.tabs.setCurrentWidget(tab)
    tab.refresh_teams()
    item = tab._find_team_tree_item(team)
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as warning:
        item.setText(0, "  ")
    assert warning.call_count == 1
    assert [t["name"] for t in db.list_teams()] == ["チームA"]
    item = tab._find_team_tree_item(team)
    item.setText(0, " チームB ")
    assert [t["name"] for t in db.list_teams()] == ["チームB"]
    assert tab._find_team_tree_item(team).text(0) == "チームB"


def test_date_inputs_cannot_go_past_the_supported_limit(qapp, window):
    """回帰テスト: 日付欄に9999年まで入力でき、スケジューラが扱えない日付（2262年以降）で
    計算が想定外のエラーになっていた。"""
    from gui.replan_dialog import ReplanDialog
    from gui.widgets_common import NoWheelDateEdit, OptionalDateEdit

    limit = MAX_SUPPORTED_DATE
    for edit in (NoWheelDateEdit(), OptionalDateEdit()):
        assert edit.maximumDate().toString("yyyy-MM-dd") == limit
        edit.deleteLater()
    tab = window.tab_basic_info
    assert tab.start_date_edit.maximumDate().toString("yyyy-MM-dd") == limit
    dialog = ReplanDialog(window.db)
    assert dialog.date_edit.maximumDate().toString("yyyy-MM-dd") == limit
    dialog.deleteLater()


# -- ファイルメニュー -------------------------------------------------------------------------


def test_generate_gantt_reports_unexpected_errors(qapp, window, tmp_path):
    """回帰テスト: 「ガントチャートを生成」は SchedulingError しか捕まえておらず、想定外の
    例外（扱えない日付での OverflowError 等）では何も表示されずに終わっていた。"""
    with patch("gui.main.validate_for_generation", return_value=[]), \
            patch.object(window, "_ask_output_dir", return_value=str(tmp_path)), \
            patch("gui.main.generate_gantt", side_effect=OverflowError("boom")), \
            patch("gui.main.QMessageBox.critical") as critical:
        window.on_generate_gantt()
    assert critical.call_count == 1
    assert "boom" in critical.call_args[0][2]
