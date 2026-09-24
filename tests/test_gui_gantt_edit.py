"""
ガントチャートタブでのタスク編集（docs/roadmap.md §9）のGUIテスト。

headless Qt（offscreen）で MainWindow を作り、実際にマウス操作（Shift＋ドラッグ、
ジョブ名のクリック）を送って、DBへの書き込み・再計算・選択と表示位置の保持・
Undo を確かめる。再計算はワーカースレッドで走るため、結果が出るまで待つ。
"""

import os
import sys
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6")
pytest.importorskip("pandas")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui.db import ProjectDatabase  # noqa: E402

# 分類: gui（PySide6 + offscreen QApplication が必要）
pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _wait(qapp, condition, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def _build_project(path):
    """チームA（2ライン）に、設計→実装 のワークフローのジョブを2つ。"""
    db = ProjectDatabase.create_new(str(path))
    db.set_project("編集テスト", "2026-04-06")
    team = db.add_team("チームA", 2)
    team_b = db.add_team("チームB", 2)
    wf = db.add_workflow("WF")
    t1 = db.add_workflow_task(wf, "設計", team, 5)
    t2 = db.add_workflow_task(wf, "実装", team, 5)
    db.add_task_dependency(wf, t1, t2)
    ms = db.add_milestone("リリース", "2026-12-25")
    job1 = db.add_job("ジョブ1", wf, ms, 1)
    job2 = db.add_job("ジョブ2", wf, ms, 2)
    db.save()
    db.close()
    return {"team": team, "team_b": team_b, "t1": t1, "t2": t2, "job1": job1, "job2": job2}


@pytest.fixture
def gantt(qapp, tmp_path):
    from gui.main import MainWindow

    from gui.app_settings import AppSettings

    ids = _build_project(tmp_path / "edit.pschedule")
    # オプション（ドラッグのキー等）がテスト間で漏れないよう、テストごとに別の設定ファイル
    w = MainWindow(app_settings=AppSettings(str(tmp_path / "settings.ini")))
    w.resize(1400, 800)
    w.show()
    w._open_database(ProjectDatabase.open_existing(str(tmp_path / "edit.pschedule")))
    w.tabs.setCurrentIndex(3)
    tab = w.tab_gantt
    assert _wait(qapp, lambda: tab.cache.is_fresh() and tab.view.scene() is not None)
    _wait(qapp, lambda: False, timeout=0.1)  # fit_all（次のイベントループ）を済ませる
    yield w, tab, ids
    if tab._editor is not None:
        tab._editor.close()
    w._shutdown_schedule_cache()
    w.db.on_change = None
    w.db.undo_manager = None
    w.db.close()
    import shiboken6
    w.hide()
    shiboken6.delete(w)
    qapp.processEvents()


def _key(ids, job, task):
    return (f"JOB_{ids[job]:03d}", f"T_{ids[task]:03d}")


def _override(db, job_id, task_id):
    return next(r for r in db.list_job_tasks_with_overrides(job_id) if r["workflow_task_id"] == task_id)


def _drag(qapp, view, start, end, modifiers=Qt.ShiftModifier):
    viewport = view.viewport()
    QTest.mousePress(viewport, Qt.LeftButton, modifiers, start)
    move = QMouseEvent(QEvent.MouseMove, QPointF(end), QPointF(viewport.mapToGlobal(end)),
                       Qt.NoButton, Qt.LeftButton, modifiers)
    QApplication.sendEvent(viewport, move)
    QTest.mouseRelease(viewport, Qt.LeftButton, modifiers, end)
    qapp.processEvents()


def _wait_recomputed(qapp, tab):
    assert _wait(qapp, lambda: tab.cache.is_fresh() and tab._pending_edit is None)
    _wait(qapp, lambda: False, timeout=0.05)


def _zoom_to(tab, bar):
    tab.view.body.fitInView(bar.sceneBoundingRect().adjusted(-200, -60, 200, 60))
    tab.view._sync_panes()


def test_shift_drag_pins_the_new_start_and_keeps_selection_and_view(qapp, gantt):
    w, tab, ids = gantt
    key = _key(ids, "job1", "t1")
    bar = tab.view.bars()[key]
    _zoom_to(tab, bar)
    view_before = tab.view.view_state()
    body = tab.view.body
    start = body.mapFromScene(bar.bar_rect.center())
    end = body.mapFromScene(bar.bar_rect.center().x() + 7 * 10, bar.bar_rect.center().y())

    _drag(qapp, body, start, end)
    _wait_recomputed(qapp, tab)

    pin = _override(w.db, ids["job1"], ids["t1"])["start_pin_date"]
    expected = tab._calendar.shift(bar.start, 5, bar.team_key)  # 7暦日＝5営業日
    assert pin == expected.isoformat()
    moved = tab.view.bars()[key]
    assert moved.start == expected
    assert moved.pinned
    assert tab.view.selected_keys() == [key]
    # 時間軸の縮尺は保つ（fit_all で全体表示に戻さない）。縦の縮尺は、チャートの
    # 高さが変わると下限（_clamp_scale）で補正されうるので比べない
    assert tab.view.view_state()[0] == pytest.approx(view_before[0])
    assert w.undo_manager.undo_label() == "ガントでタスクを移動"


def test_drag_without_the_modifier_key_does_not_move(qapp, gantt):
    w, tab, ids = gantt
    bar = tab.view.bars()[_key(ids, "job1", "t1")]
    _zoom_to(tab, bar)
    body = tab.view.body
    start = body.mapFromScene(bar.bar_rect.center())
    end = body.mapFromScene(bar.bar_rect.center().x() + 70, bar.bar_rect.center().y())

    _drag(qapp, body, start, end, modifiers=Qt.NoModifier)

    assert w.db.list_all_job_task_overrides() == []
    assert tab._pending_edit is None


def test_the_drag_key_follows_the_option(qapp, gantt):
    w, tab, ids = gantt
    w.app_settings.set("gantt_drag_modifier", "alt")
    w._apply_app_settings()
    bar = tab.view.bars()[_key(ids, "job1", "t1")]
    _zoom_to(tab, bar)
    body = tab.view.body
    start = body.mapFromScene(bar.bar_rect.center())
    end = body.mapFromScene(bar.bar_rect.center().x() + 70, bar.bar_rect.center().y())

    _drag(qapp, body, start, end, modifiers=Qt.ShiftModifier)
    assert w.db.list_all_job_task_overrides() == []

    # 1回目（Shift）は通常の範囲選択として扱われ、表示がずれうるので座標を取り直す
    _zoom_to(tab, bar)
    start = body.mapFromScene(bar.bar_rect.center())
    end = body.mapFromScene(bar.bar_rect.center().x() + 70, bar.bar_rect.center().y())
    _drag(qapp, body, start, end, modifiers=Qt.AltModifier)
    _wait_recomputed(qapp, tab)
    assert _override(w.db, ids["job1"], ids["t1"])["start_pin_date"] is not None


def test_dragging_the_right_edge_changes_the_duration(qapp, gantt):
    w, tab, ids = gantt
    key = _key(ids, "job1", "t1")
    bar = tab.view.bars()[key]
    _zoom_to(tab, bar)
    body = tab.view.body
    edge = body.mapFromScene(bar.bar_rect.right() - 0.5, bar.bar_rect.center().y())
    edge.setX(edge.x() - 1)
    end = body.mapFromScene(bar.bar_rect.right() + 7 * 10, bar.bar_rect.center().y())

    _drag(qapp, body, edge, end)
    _wait_recomputed(qapp, tab)

    assert _override(w.db, ids["job1"], ids["t1"])["override_days"] == 10  # 5日 + 5営業日
    assert _override(w.db, ids["job1"], ids["t1"])["start_pin_date"] is None
    assert w.undo_manager.undo_label() == "ガントでタスクの期間を変更"


def test_resizing_back_to_the_default_duration_removes_the_override(qapp, gantt):
    w, tab, ids = gantt
    key = _key(ids, "job1", "t1")
    tab._on_resize_requested(key, 8)
    _wait_recomputed(qapp, tab)
    assert _override(w.db, ids["job1"], ids["t1"])["override_days"] == 8
    tab._on_resize_requested(key, 5)
    _wait_recomputed(qapp, tab)
    assert _override(w.db, ids["job1"], ids["t1"])["override_id"] is None


def test_multi_selection_moves_all_selected_tasks_by_the_same_working_days(qapp, gantt):
    w, tab, ids = gantt
    k1 = _key(ids, "job1", "t1")
    k2 = _key(ids, "job2", "t1")
    bars = tab.view.bars()
    tab.view.select_keys([k1, k2])
    _zoom_to(tab, bars[k1])
    body = tab.view.body
    start = body.mapFromScene(bars[k1].bar_rect.center())
    end = body.mapFromScene(bars[k1].bar_rect.center().x() + 7 * 10, bars[k1].bar_rect.center().y())
    before = {k: bars[k].start for k in (k1, k2)}
    teams = {k: bars[k].team_key for k in (k1, k2)}

    _drag(qapp, body, start, end)
    _wait_recomputed(qapp, tab)

    for k, (job, task) in ((k1, ("job1", "t1")), (k2, ("job2", "t1"))):
        pin = _override(w.db, ids[job], ids[task])["start_pin_date"]
        assert pin == tab._calendar.shift(before[k], 5, teams[k]).isoformat()
    assert sorted(tab.view.selected_keys()) == sorted([k1, k2])
    # 2件の書き込みは1回のUndoで戻る
    w.undo_manager.undo()
    assert w.db.list_all_job_task_overrides() == []


def test_clicking_a_job_name_selects_all_its_tasks(qapp, gantt):
    w, tab, ids = gantt
    column = tab.view.column
    rows = column.scene().gantt_job_rows
    job1_row = next(r for r in rows if r[2] == f"JOB_{ids['job1']:03d}")
    job2_row = next(r for r in rows if r[2] == f"JOB_{ids['job2']:03d}")

    def click(row, modifiers=Qt.NoModifier):
        pos = column.mapFromScene(20, (row[0] + row[1]) / 2)
        QTest.mouseClick(column.viewport(), Qt.LeftButton, modifiers, pos)

    click(job1_row)
    assert sorted(tab.view.selected_keys()) == sorted(
        [_key(ids, "job1", "t1"), _key(ids, "job1", "t2")]
    )
    click(job2_row, Qt.ControlModifier)
    assert len(tab.view.selected_keys()) == 4


def test_moved_note_and_highlight_show_other_tasks_that_moved(qapp, gantt):
    """設計を後ろへずらすと、依存している実装も動く。実装は「他に動いた」に数えられ、
    次のクリックまで強調される。"""
    w, tab, ids = gantt
    design = _key(ids, "job1", "t1")
    impl = _key(ids, "job1", "t2")
    new_start = tab._calendar.shift(tab.view.bars()[design].start, 10, "TEAM_001")

    tab._on_move_requested([(design, new_start)], 10)
    _wait_recomputed(qapp, tab)

    assert "他に" in tab.status_label.text()
    assert impl in tab._highlighted_keys
    assert design not in tab._highlighted_keys
    assert tab.view.bars()[impl].highlighted

    tab.view.body.pressed.emit()
    assert tab._highlighted_keys == []
    assert not tab.view.bars()[impl].highlighted


def test_move_hint_warns_only_when_placed_before_the_predecessor(qapp, gantt):
    w, tab, ids = gantt
    design = tab.view.bars()[_key(ids, "job1", "t1")]
    impl_key = _key(ids, "job1", "t2")
    assert "先行タスク「設計」の完了より前" in tab._move_hint(impl_key, design.start)
    assert tab._move_hint(impl_key, design.end) is None
    assert tab._move_hint(_key(ids, "job1", "t1"), date(2026, 1, 1)) is None


def _choose_menu(tab, text):
    """メニューの表示を差し替え、指定した文言のアクション（サブメニュー内も探す）を選ぶ。"""
    def fake_exec(menu, *_args):
        def find(m):
            for action in m.actions():
                if action.menu() is not None:
                    found = find(action.menu())
                    if found is not None:
                        return found
                elif action.text() == text:
                    return action
            return None
        return find(menu)
    return patch.object(tab, "_exec_menu", fake_exec)


def test_context_menu_pins_sets_status_and_team(qapp, gantt):
    w, tab, ids = gantt
    key = _key(ids, "job1", "t2")
    tab.view.select_keys([key])
    start = tab.view.bars()[key].start

    with _choose_menu(tab, "開始日を固定"):
        tab._show_context_menu(None)
    _wait_recomputed(qapp, tab)
    assert _override(w.db, ids["job1"], ids["t2"])["start_pin_date"] == start.isoformat()

    with _choose_menu(tab, "完了"):
        tab._show_context_menu(None)
    _wait_recomputed(qapp, tab)
    assert _override(w.db, ids["job1"], ids["t2"])["status"] == "done"

    with _choose_menu(tab, "チームB"):
        tab._show_context_menu(None)
    _wait_recomputed(qapp, tab)
    assert _override(w.db, ids["job1"], ids["t2"])["override_team_id"] == ids["team_b"]

    with _choose_menu(tab, "固定を解除"):
        tab._show_context_menu(None)
    _wait_recomputed(qapp, tab)
    row = _override(w.db, ids["job1"], ids["t2"])
    assert row["start_pin_date"] is None
    assert row["status"] == "done"  # 他の列は消えない


def test_editor_window_single_selection_edits_status_and_days(qapp, gantt):
    w, tab, ids = gantt
    key = _key(ids, "job1", "t1")
    tab.view.select_keys([key])
    tab.open_editor()
    editor = tab._editor
    assert editor.pages.currentWidget() is editor.single_page
    assert "ジョブ1 / 設計" in editor.title_label.text()
    assert editor.days_spin.specialValueText() == "既定（5日）"

    editor.status_combo.setCurrentIndex(editor.status_combo.findData("in_progress"))
    editor.status_combo.activated.emit(editor.status_combo.currentIndex())
    _wait_recomputed(qapp, tab)
    assert _override(w.db, ids["job1"], ids["t1"])["status"] == "in_progress"
    # 再計算の後も編集ウィンドウは同じタスクを表示し続ける
    assert editor.status_combo.currentData() == "in_progress"

    editor.days_spin.setValue(7)
    editor.days_spin.editingFinished.emit()
    _wait_recomputed(qapp, tab)
    assert _override(w.db, ids["job1"], ids["t1"])["override_days"] == 7


def test_editor_window_multi_selection_shows_mixed_values_and_edits_all(qapp, gantt):
    w, tab, ids = gantt
    k1 = _key(ids, "job1", "t1")
    k2 = _key(ids, "job2", "t2")
    w.db.update_job_task_override_fields(ids["job1"], ids["t1"], status="done")
    tab.refresh_choices()
    assert _wait(qapp, lambda: tab.cache.is_fresh())
    _wait(qapp, lambda: False, timeout=0.05)
    tab.view.select_keys([k1, k2])
    tab.open_editor()
    editor = tab._editor
    assert editor.pages.currentWidget() is editor.multi_page
    assert editor.multi_status_combo.currentText() == "（複数の値）"

    combo = editor.multi_team_combo
    combo.setCurrentIndex(combo.findData(ids["team_b"]))
    combo.activated.emit(combo.currentIndex())
    _wait_recomputed(qapp, tab)
    assert _override(w.db, ids["job1"], ids["t1"])["override_team_id"] == ids["team_b"]
    assert _override(w.db, ids["job2"], ids["t2"])["override_team_id"] == ids["team_b"]
    # 触っていない項目（状態）は変えない
    assert _override(w.db, ids["job1"], ids["t1"])["status"] == "done"
    assert _override(w.db, ids["job2"], ids["t2"])["status"] is None


def test_editor_window_follows_the_selection(qapp, gantt):
    w, tab, ids = gantt
    tab.view.select_keys([_key(ids, "job1", "t1")])
    tab.open_editor()
    editor = tab._editor
    tab.view.select_keys([_key(ids, "job2", "t2")])
    assert "ジョブ2 / 実装" in editor.title_label.text()


def test_undo_of_a_gantt_edit_restores_the_selection(qapp, gantt):
    w, tab, ids = gantt
    key = _key(ids, "job1", "t1")
    tab.view.select_keys([key])
    new_start = tab._calendar.shift(tab.view.bars()[key].start, 3, "TEAM_001")
    tab._on_move_requested([(key, new_start)], 3)
    _wait_recomputed(qapp, tab)
    tab.view.select_keys([])

    w.undo_manager.undo()
    assert _wait(qapp, lambda: tab.cache.is_fresh() and tab.view.selected_keys() == [key])
    assert w.db.list_all_job_task_overrides() == []
