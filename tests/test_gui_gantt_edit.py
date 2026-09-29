"""
ガントチャートタブでのタスク編集（docs/roadmap.md §9）のGUIテスト。

headless Qt（offscreen）で MainWindow を作り、実際にマウス操作（Shift＋ドラッグ、
ジョブ名のクリック）を送って、DBへの書き込み・再計算・選択と表示位置の保持・
Undo を確かめる。再計算はワーカースレッドで走るため、結果が出るまで待つ。
"""

import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6")
pytest.importorskip("pandas")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QMouseEvent  # noqa: E402
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
    import shiboken6
    tab = w.tab_gantt  # テストの中で別のプロジェクトを開いた場合は、そのタブ
    if tab is not None and shiboken6.isValid(tab) and tab._editor is not None:
        tab._editor.close()
    w._shutdown_schedule_cache()
    w.db.on_change = None
    w.db.undo_manager = None
    w.db.close()
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


def _hover(qapp, view, pos, modifiers=Qt.NoModifier):
    viewport = view.viewport()
    move = QMouseEvent(QEvent.MouseMove, QPointF(pos), QPointF(viewport.mapToGlobal(pos)),
                       Qt.NoButton, Qt.NoButton, modifiers)
    QApplication.sendEvent(viewport, move)
    qapp.processEvents()


def test_the_right_edge_shows_a_resize_cursor_only_while_the_drag_key_is_held(qapp, gantt):
    """バーの右端は、指定キー（Shift）を押しているときだけカーソルが↔になり、伸縮できる。
    選んでいるバーでも、キーを押していなければ↔にも伸縮にもならない（移動と同じ）。"""
    w, tab, ids = gantt
    key = _key(ids, "job1", "t1")
    bar = tab.view.bars()[key]
    _zoom_to(tab, bar)
    body = tab.view.body
    edge = body.mapFromScene(bar.bar_rect.right() - 0.5, bar.bar_rect.center().y())
    edge.setX(edge.x() - 1)
    middle = body.mapFromScene(bar.bar_rect.center())
    tab.view.select_keys([key])

    _hover(qapp, body, edge)
    assert body.viewport().cursor().shape() != Qt.SizeHorCursor
    _hover(qapp, body, edge, Qt.ShiftModifier)
    assert body.viewport().cursor().shape() == Qt.SizeHorCursor
    _hover(qapp, body, middle, Qt.ShiftModifier)
    assert body.viewport().cursor().shape() == Qt.ArrowCursor

    end = body.mapFromScene(bar.bar_rect.right() + 7 * 10, bar.bar_rect.center().y())
    _drag(qapp, body, edge, end, modifiers=Qt.NoModifier)
    assert w.db.list_all_job_task_overrides() == []
    assert tab._pending_edit is None


def test_the_right_edge_can_be_grabbed_when_the_next_bar_touches_it(qapp, gantt):
    """右端に次のバーが接していると、その位置で一番上にあるのは次のバーのことがある。
    それでも選んだバーの右端を掴める（↔になる）。"""
    w, tab, ids = gantt
    key = _key(ids, "job2", "t1")
    bar = tab.view.bars()[key]
    neighbor = tab.view.bars()[_key(ids, "job2", "t2")]
    assert neighbor.bar_rect.left() == bar.bar_rect.right()  # 前提: 次のバーが接している
    _zoom_to(tab, bar)
    tab.view.select_keys([key])
    body = tab.view.body
    edge = body.mapFromScene(bar.bar_rect.right() + 0.5, bar.bar_rect.center().y())
    edge.setX(edge.x() + 1)  # 次のバーの上（左端のすぐ内側）
    _hover(qapp, body, edge, Qt.ShiftModifier)
    assert body.viewport().cursor().shape() == Qt.SizeHorCursor

    end = body.mapFromScene(bar.bar_rect.right() + 3 * 10, bar.bar_rect.center().y())
    _drag(qapp, body, edge, end)
    _wait_recomputed(qapp, tab)
    assert _override(w.db, ids["job2"], ids["t1"])["override_days"] is not None
    assert _override(w.db, ids["job2"], ids["t2"])["override_id"] is None


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

    # 問題の無い文言は画面下部（ファイル名の横）に出す
    assert "他に" in w.schedule_summary_label.text()
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


def test_editor_window_shows_tasks_without_overrides_as_active(qapp, gantt):
    """上書き行の無いタスク（is_active が NULL）も「有効」と表示する。以前は
    無効に見え、チェックを押すと有効なタスクを無効にしてしまうおそれがあった。"""
    w, tab, ids = gantt
    k1 = _key(ids, "job1", "t1")
    k2 = _key(ids, "job2", "t2")
    tab.view.select_keys([k2])
    tab.open_editor()
    editor = tab._editor
    assert editor.active_check.isChecked()

    # 上書き行のあるタスク（有効）と無いタスクを一緒に選んでも「混在」にしない
    w.db.update_job_task_override_fields(ids["job1"], ids["t1"], status="done")
    tab.refresh_choices()
    assert _wait(qapp, lambda: tab.cache.is_fresh())
    _wait(qapp, lambda: False, timeout=0.05)
    tab.view.select_keys([k1, k2])
    assert editor.pages.currentWidget() is editor.multi_page
    assert editor.multi_active_check.checkState() == Qt.Checked


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


def test_task_name_does_not_overlap_the_pin_marker(qapp, gantt):
    """📍を描いたバーでは、縦に余裕が無いときはタスク名を印の右へ避け、
    縦に余裕があるとき（印の下に文字が収まる）は中央のまま置く。"""
    from gui.gantt_view import _PIN_LABEL_RESERVE_PX
    from PySide6.QtGui import QTransform

    w, tab, ids = gantt
    key = _key(ids, "job1", "t2")
    tab.apply_task_fields([key], "pin", {"start_pin_date": tab.view.bars()[key].start.isoformat()})
    _wait_recomputed(qapp, tab)
    bar = tab.view.bars()[key]
    assert bar.pinned
    label = next(t for t in tab.view.body.scene().gantt_task_labels if t[-1] is bar)[0]
    body = tab.view.body

    def label_left_offset_px(sx, sy):
        body.setTransform(QTransform().scale(sx, sy))
        body.centerOn(bar.sceneBoundingRect().center())  # 画面外のラベルは整形されない
        tab.view._sync_panes()
        bar_left = body.mapFromScene(bar.bar_rect.topLeft()).x()
        return body.mapFromScene(label.pos()).x() - bar_left, label.isVisible()

    # 横に広く縦は標準（バーの高さ20px）: 印の右に避ける
    offset, visible = label_left_offset_px(8.0, 1.0)
    assert visible
    assert offset >= _PIN_LABEL_RESERVE_PX
    # 縦にも大きく拡大: 中央に置く（文字の左端はバーの中央付近）
    offset_tall, visible = label_left_offset_px(8.0, 6.0)
    bar_w_px = bar.bar_rect.width() * 8.0
    assert visible
    assert abs((offset_tall + label.boundingRect().width() / 2) - bar_w_px / 2) < 2


def _screen_x(view, scene_x):
    return view.viewport().mapToGlobal(view.mapFromScene(QPointF(scene_x, 0))).x()


def _screen_y(view, scene_y):
    return view.viewport().mapToGlobal(view.mapFromScene(QPointF(0, scene_y))).y()


@pytest.mark.parametrize("h_end, v_end", [("min", "min"), ("max", "max")])
def test_header_and_job_column_stay_aligned_with_the_body_at_the_scroll_ends(qapp, gantt, h_end, v_end):
    """拡大して本体を端までスクロールしても、見出し（日付・マイルストーンの線）と
    ジョブ名の列が本体とずれない。本体にはスクロールバーと枠があるので、見出し・列の
    表示幅をそれに合わせていないと、端では本体だけがスクロールバーの幅だけ先へ進む。"""
    w, tab, ids = gantt
    pane = tab.view
    body = pane.body
    body.scale(6.0, 6.0)
    body._clamp_scale()
    pane._sync_panes()
    qapp.processEvents()
    h_bar, v_bar = body.horizontalScrollBar(), body.verticalScrollBar()
    assert h_bar.isVisible() and v_bar.isVisible()
    h_bar.setValue(getattr(h_bar, h_end + "imum")())
    v_bar.setValue(getattr(v_bar, v_end + "imum")())
    qapp.processEvents()

    rect = body.scene().gantt_body_rect
    for scene_x in (rect.left() + 5, rect.center().x(), rect.right() - 5):
        if body.viewport().rect().contains(body.mapFromScene(QPointF(scene_x, rect.top()))):
            assert _screen_x(pane.header, scene_x) == _screen_x(body, scene_x)
    visible = body.mapToScene(body.viewport().rect()).boundingRect()
    assert _screen_x(pane.header, visible.left()) == _screen_x(body, visible.left())
    assert _screen_x(pane.header, visible.right()) == _screen_x(body, visible.right())
    assert _screen_y(pane.column, visible.top()) == _screen_y(body, visible.top())
    assert _screen_y(pane.column, visible.bottom()) == _screen_y(body, visible.bottom())


# -- 右クリック・編集ウィンドウの古い状態（操作中の不具合の回帰テスト） -----------------------


def test_right_clicking_a_bar_opens_the_context_menu(qapp, gantt):
    """回帰テスト: バーを右クリックすると contextMenuEvent が例外
    （QContextMenuEvent に position() が無い）になり、メニューが一度も開かなかった。
    既存のメニューのテストは _show_context_menu を直接呼んでいたため気付けなかった。
    ここでは実際の右クリックと同じイベントを送る。"""
    from PySide6.QtGui import QContextMenuEvent

    w, tab, ids = gantt
    key = _key(ids, "job1", "t2")
    body = tab.view.body
    bar = tab.view.bars()[key]
    body.ensureVisible(bar.sceneBoundingRect())
    qapp.processEvents()
    pos = body.mapFromScene(bar.sceneBoundingRect().center())
    shown = []
    with patch.object(tab, "_exec_menu", lambda menu, _pos: shown.append(menu) or None):
        event = QContextMenuEvent(QContextMenuEvent.Mouse, pos, body.viewport().mapToGlobal(pos))
        QApplication.sendEvent(body.viewport(), event)
    assert len(shown) == 1
    assert tab.view.selected_keys() == [key]


def test_editor_hides_with_the_tab_and_shows_current_values_when_back(qapp, gantt):
    """回帰テスト: 編集ウィンドウは独立したウィンドウなので、他のタブへ移っても出た
    ままになり、そこでの変更（同じタスクの日数・状態）が反映されない古い表示のまま
    編集できていた。タブと一緒に隠し、戻ったときに最新の内容で出し直す。"""
    w, tab, ids = gantt
    key = _key(ids, "job1", "t1")
    tab.view.select_keys([key])
    tab.open_editor()
    editor = tab._editor
    assert editor.isVisible()

    w.tabs.setCurrentWidget(w.tab_jobs)
    qapp.processEvents()
    assert not editor.isVisible()
    with w.db.undo_group("タスク上書きを変更"):
        w.db.upsert_job_task_override(ids["job1"], ids["t1"], override_days=7, status="done")

    w.tabs.setCurrentWidget(tab)
    qapp.processEvents()
    assert editor.isVisible()
    assert editor.days_spin.value() == 7
    assert editor.status_combo.currentData() == "done"


def test_editor_compares_with_current_values_not_the_displayed_ones(qapp, gantt):
    """回帰テスト: 表示した後に値が変わっていると（Undo等）、表示時点の値と比べて
    「変更なし」と判定され、入力が黙って無視されていた。書き込む直前の値と比べる。"""
    w, tab, ids = gantt
    key = _key(ids, "job1", "t1")
    tab.view.select_keys([key])
    tab.open_editor()
    editor = tab._editor
    assert editor.days_spin.value() == 0  # 既定
    # 編集ウィンドウに反映される前に、DBの日数が変わった
    with w.db.undo_group("タスク上書きを変更"):
        w.db.upsert_job_task_override(ids["job1"], ids["t1"], override_days=7)
    editor.days_spin.setValue(0)  # 「既定」に戻すつもりの入力
    editor.days_spin.editingFinished.emit()
    _wait_recomputed(qapp, tab)
    assert _override(w.db, ids["job1"], ids["t1"])["override_days"] is None


def test_editor_does_not_write_to_a_deleted_job(qapp, gantt):
    """回帰テスト: 編集ウィンドウを開いたまま他のタブでそのジョブを削除し、編集ウィンドウで
    値を変えると、存在しないジョブへの上書きを書こうとして外部キー制約の例外になっていた。"""
    w, tab, ids = gantt
    key = _key(ids, "job1", "t1")
    tab.view.select_keys([key])
    tab.open_editor()
    editor = tab._editor
    w.tabs.setCurrentWidget(w.tab_jobs)
    qapp.processEvents()
    w.db.delete_job(ids["job1"])
    before = w.db.serialize_state()

    tab.apply_task_fields([key], "タスクの状態を変更", {"status": "done"})  # 表示が古いまま書こうとしても
    assert w.db.serialize_state() == before
    assert not w.db._conn.in_transaction

    w.tabs.setCurrentWidget(tab)
    qapp.processEvents()
    assert editor.pages.currentWidget() is editor.empty_page


def test_editor_of_the_previous_project_does_not_outlive_it(qapp, gantt):
    """回帰テスト: 別のプロジェクトを開いた後も、前のプロジェクトの編集ウィンドウが
    表示されたまま残り、操作すると閉じたDBへ書こうとして変更が失われていた。
    旧タブ（と、その子の編集ウィンドウ）ごと破棄する。"""
    import shiboken6
    from PySide6.QtCore import QCoreApplication, QEvent

    w, tab, ids = gantt
    tab.view.select_keys([_key(ids, "job1", "t1")])
    tab.open_editor()
    editor = tab._editor
    old_db = w.db
    w._open_database(ProjectDatabase.create_new())
    assert not editor.isVisible()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()
    assert not shiboken6.isValid(editor)
    assert not shiboken6.isValid(tab)
    assert old_db is not w.db


# -- 見出し・ジョブ名の重なり ------------------------------------------------------------------


def _visible_label_rects(view, labels):
    rects = []
    for label in labels:
        if label.isVisible():
            # 画面上の矩形（縮小した名前は、その倍率も含めて）
            rect = label.deviceTransform(view.viewportTransform()).mapRect(label.boundingRect())
            # （ちょうど接している名前を、計算誤差で重なりと見なさないよう丸める）
            rects.append(tuple(round(v, 2) for v in (rect.left(), rect.top(), rect.right(), rect.bottom())))
    return rects


def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def test_job_names_do_not_overlap_when_the_chart_is_zoomed_out(qapp, gantt, tmp_path):
    """回帰テスト: 全体表示で縦に縮小すると行が文字より低くなり、左列のジョブ名同士が
    重なって読めなかった。重なるときは全行の名前をそろって縮小し、それでも読めないほど
    低いなら全行とも隠す（行によって出たり出なかったりしない）。拡大すれば全部現れる。"""
    w, tab, _ids = gantt
    path = tmp_path / "many.pschedule"
    db = ProjectDatabase.create_new(str(path))
    db.set_project("多数", "2026-04-06")
    team = db.add_team("チームA", 50)
    wf = db.add_workflow("WF")
    db.add_workflow_task(wf, "作業", team, 3)
    ms = db.add_milestone("リリース", "2026-12-25")
    for i in range(60):
        db.add_job(f"ジョブ{i:02d}", wf, ms)
    db.save()
    db.close()
    w._open_database(ProjectDatabase.open_existing(str(path)))
    w.tabs.setCurrentIndex(3)
    tab = w.tab_gantt
    assert _wait(qapp, lambda: tab.cache.is_fresh() and tab.view.scene() is not None)
    _wait(qapp, lambda: False, timeout=0.1)

    column = tab.view.column
    labels = [label for label, _swatch, _y in column.scene().gantt_job_labels]
    # 60行は収まらないので、全行とも隠している（一部だけ出すことはしない）
    assert not any(label.isVisible() for label in labels)

    # 少し拡大すると、全行の名前がそろって縮小して現れ、重ならない
    body = tab.view.body
    pitch = labels[0].boundingRect().height() * 0.8 / (column.scene().gantt_job_labels[1][2]
                                                        - column.scene().gantt_job_labels[0][2])
    body.scale(1.0, pitch / body.transform().m22())
    tab.view._sync_panes()
    assert all(label.isVisible() for label in labels)
    assert {round(label.scale(), 3) for label in labels} == {0.8}
    rects = _visible_label_rects(column, labels)
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            assert not _overlaps(a, b)

    body.scale(1.0, 20.0)
    tab.view._sync_panes()
    assert all(label.isVisible() and label.scale() == 1.0 for label in labels)


def test_header_labels_do_not_overlap_and_stay_inside_the_view(qapp, gantt, tmp_path):
    """回帰テスト: 開発開始日と「今日」、締切の近いマイルストーン同士のラベルが重なって
    読めなかった（「プロジ今日ト開始」）。右端のラベルは画面の外へはみ出して切れていた。"""
    from datetime import timedelta

    w, tab, _ids = gantt
    path = tmp_path / "labels.pschedule"
    db = ProjectDatabase.create_new(str(path))
    start = date.today() - timedelta(days=3)
    db.set_project("見出し", start.isoformat())
    team = db.add_team("チームA", 2)
    wf = db.add_workflow("WF")
    db.add_workflow_task(wf, "作業", team, 200)
    ms1 = db.add_milestone("アルファ版（コアアセット確定）", (start + timedelta(days=120)).isoformat())
    db.add_milestone("ベータ版（全カットシーン組み込み）", (start + timedelta(days=125)).isoformat())
    db.add_milestone("マスターアップ", (start + timedelta(days=400)).isoformat())
    db.add_job("ジョブ1", wf, ms1)
    db.save()
    db.close()
    w._open_database(ProjectDatabase.open_existing(str(path)))
    w.tabs.setCurrentIndex(3)
    tab = w.tab_gantt
    assert _wait(qapp, lambda: tab.cache.is_fresh() and tab.view.scene() is not None)
    _wait(qapp, lambda: False, timeout=0.1)

    header = tab.view.header
    entries = header.scene().gantt_milestone_labels
    rects = _visible_label_rects(header, [label for label, _x, _p in entries])
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            assert not _overlaps(a, b)
    viewport_width = header.viewport().width()
    for left, _top, right, _bottom in rects:
        assert left >= -1 and right <= viewport_width + 1
    # 締切（マイルストーン）のラベルは残り、最後のマイルストーンも見えている
    visible_texts = {label.text() for label, _x, _p in entries if label.isVisible()}
    assert "マスターアップ" in visible_texts
    assert "アルファ版（コアアセット確定）" in visible_texts


@pytest.mark.parametrize("on_bar", [True, False], ids=["on_selected_bar", "on_empty_area"])
def test_right_click_keeps_the_multi_selection(qapp, gantt, on_bar):
    """回帰テスト: 複数選択してから右クリックすると、右ボタンの押下がラバーバンド選択の
    開始として扱われて選択が解除され、メニューが1つのタスクにしか効かなかった。
    実際の右クリックと同じく、押下→離す→コンテキストメニューの順にイベントを送る。"""
    from PySide6.QtGui import QContextMenuEvent

    w, tab, ids = gantt
    keys = [_key(ids, "job1", "t1"), _key(ids, "job2", "t2")]
    tab.view.select_keys(keys)
    body = tab.view.body
    bar = tab.view.bars()[keys[0]]
    body.ensureVisible(bar.sceneBoundingRect())
    qapp.processEvents()
    if on_bar:
        pos = body.mapFromScene(bar.sceneBoundingRect().center())
    else:
        pos = body.mapFromScene(bar.sceneBoundingRect().bottomRight()) + QPoint(5, 30)
    viewport = body.viewport()
    shown = []
    with patch.object(tab, "_exec_menu", lambda menu, _pos: shown.append(menu) or None):
        QTest.mousePress(viewport, Qt.RightButton, Qt.NoModifier, pos)
        QTest.mouseRelease(viewport, Qt.RightButton, Qt.NoModifier, pos)
        event = QContextMenuEvent(QContextMenuEvent.Mouse, pos, viewport.mapToGlobal(pos))
        QApplication.sendEvent(viewport, event)
    assert len(shown) == 1
    assert sorted(tab.view.selected_keys()) == sorted(keys)


# -- 行の並び（gui/gantt_row_order.py） -----------------------------------------------


def _row_jobs(tab):
    return [job for _top, _bottom, job in tab.view.column.scene().gantt_job_rows]


def test_rows_keep_their_order_after_an_edit_until_re_sorted(qapp, gantt):
    """編集でジョブの開始日の順が入れ替わっても行は動かさず（対象を見失わないように）、
    並べ直すボタンを目立たせる。押すと選んだ並び順どおりに並べ直す。"""
    w, tab, ids = gantt
    job1, job2 = (f"JOB_{ids['job1']:03d}", f"JOB_{ids['job2']:03d}")
    assert _row_jobs(tab) == [job1, job2]
    assert not tab._row_order_stale

    # ジョブ2の最初のタスクを、ジョブ1より前へ固定する
    key = _key(ids, "job2", "t1")
    first_start = min(bar.start for bar in tab.view.bars().values())
    tab.view.select_keys([key])
    tab._on_move_requested([(key, first_start - timedelta(days=30))], -20)
    _wait_recomputed(qapp, tab)
    assert tab.view.bars()[key].start < tab.view.bars()[_key(ids, "job1", "t1")].start
    assert _row_jobs(tab) == [job1, job2]
    assert tab._row_order_stale
    assert tab.row_resort_button.styleSheet() != ""
    assert tab.view.selected_keys() == [key]

    tab.row_resort_button.click()
    qapp.processEvents()
    assert _row_jobs(tab) == [job2, job1]
    assert not tab._row_order_stale
    assert tab.row_resort_button.styleSheet() == ""
    assert tab.view.selected_keys() == [key]  # 並べ直しても選択は残る


def test_choosing_a_row_order_re_sorts_and_is_remembered(qapp, gantt, tmp_path):
    from gui.app_settings import AppSettings
    from gui.gantt_row_order import ORDER_PRIORITY

    w, tab, ids = gantt
    job1, job2 = (f"JOB_{ids['job1']:03d}", f"JOB_{ids['job2']:03d}")
    tab._select_combo(tab.row_order_combo, ORDER_PRIORITY)
    tab.row_descending_button.click()  # 降順＝優先度の低い（数値の大きい）ジョブ2が先
    qapp.processEvents()
    assert _row_jobs(tab) == [job2, job1]
    assert tab.row_descending_button.arrowType() == Qt.DownArrow

    settings = AppSettings(str(tmp_path / "settings.ini"))
    assert settings.get_ui_state("gantt_row_order") == ORDER_PRIORITY
    assert settings.get_ui_state("gantt_row_descending") == "1"


# -- ダークモードの赤字 ------------------------------------------------------------


def test_error_text_is_readable_in_both_themes_and_follows_a_theme_switch(qapp, gantt):
    """エラー・警告の赤字は、ダークでは明るめの赤にする（暗い赤は暗い背景に沈んで
    読めなかった）。起動後にテーマを切り替えても、ガント・分析タブの赤字が追従する。"""
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem

    from gui.widgets_common import alert_text_color

    w, tab, _ids = gantt
    light, dark = alert_text_color(dark=False).name(), alert_text_color(dark=True).name()
    assert QColor(light).lightness() < QColor(dark).lightness()
    analysis = w.tab_analysis
    tab._set_errors("エラー")
    analysis._show_status_only("エラー", is_error=True)  # 表は空になるので、この後に赤字の項目を置く
    table = analysis.findChildren(QTableWidget)[0]
    table.setRowCount(max(1, table.rowCount()))
    table.setColumnCount(max(1, table.columnCount()))
    item = QTableWidgetItem("超過")
    item.setForeground(QColor(light))
    table.setItem(0, 0, item)
    assert light in tab.error_label.styleSheet()

    original = QApplication.palette()
    try:
        palette = QPalette(original)
        palette.setColor(QPalette.Window, QColor("#202020"))
        QApplication.setPalette(palette)
        qapp.processEvents()
        assert dark in tab.error_label.styleSheet()
        assert dark in analysis.status_label.styleSheet()
        assert table.item(0, 0).foreground().color().name() == dark
    finally:
        QApplication.setPalette(original)
        qapp.processEvents()
    assert light in tab.error_label.styleSheet()
    assert table.item(0, 0).foreground().color().name() == light


# -- ジョブ間の依存の矢印 ------------------------------------------------------------


def _with_cross_job_dependency(qapp, w, tab, ids):
    """ジョブ2の設計（t1）が、ジョブ1の実装（t2）を待つ依存を足して計算し直す。"""
    w.db.add_external_dependency(ids["job2"], ids["t1"], ids["job1"], ids["t2"])
    tab.refresh_choices()
    _wait_recomputed(qapp, tab)
    return _key(ids, "job1", "t2"), _key(ids, "job2", "t1")


def _dep_paths(tab):
    """今描いている矢印の線（QPainterPath）と色。"""
    return [(path, color) for path, color, *_rest in tab.view.body.dependency_arrows()]


def test_dependency_arrows_follow_the_mode_and_either_end_of_the_selection(qapp, gantt):
    from PySide6.QtWidgets import QGraphicsItem

    from gui.gantt_view import DEPENDENCY_ALL, DEPENDENCY_OFF

    w, tab, ids = gantt
    pred, succ = _with_cross_job_dependency(qapp, w, tab, ids)
    assert tab.dependency_combo.currentData() == "selected"  # 既定は選択中のみ
    tab.view.select_keys([])
    qapp.processEvents()
    assert _dep_paths(tab) == []
    for key in (pred, succ):  # 依存される側・する側のどちらを選んでも出る
        tab.view.select_keys([key])
        qapp.processEvents()
        assert len(_dep_paths(tab)) == 1
    tab._select_combo(tab.dependency_combo, DEPENDENCY_OFF)
    assert _dep_paths(tab) == []
    tab.view.select_keys([])
    tab._select_combo(tab.dependency_combo, DEPENDENCY_ALL)
    assert len(_dep_paths(tab)) == 1
    # 矢印はクリック・範囲選択・ツールチップを奪わない（形を持たない1つの部品で描く）
    body = tab.view.body
    layer = body._dep_layer
    assert layer.acceptedMouseButtons() == Qt.NoButton and not layer.flags() & QGraphicsItem.ItemIsSelectable
    assert layer.shape().isEmpty()
    bar = tab.view.bars()[succ]
    assert body._bar_at(body.mapFromScene(bar.bar_rect.center())) is bar


def test_dependency_arrow_path_leaves_and_enters_the_bars_at_their_middle(qapp, gantt):
    from gui.gantt_view import DEPENDENCY_ALL

    w, tab, ids = gantt
    pred, succ = _with_cross_job_dependency(qapp, w, tab, ids)
    tab._select_combo(tab.dependency_combo, DEPENDENCY_ALL)
    a, b = tab.view.bars()[pred].bar_rect, tab.view.bars()[succ].bar_rect
    path = _dep_paths(tab)[0][0]
    start, end = path.elementAt(0), path.elementAt(path.elementCount() - 1)
    assert (start.x, start.y) == (a.right(), a.center().y())
    assert (end.x, end.y) == (b.left(), b.center().y())


def test_a_hidden_partner_is_shown_as_a_short_line_and_a_dot(qapp, gantt):
    from PySide6.QtWidgets import QGraphicsEllipseItem

    from gui.gantt_view import BAR_MARGIN, DEPENDENCY_ALL

    w, tab, ids = gantt
    pred, succ = _with_cross_job_dependency(qapp, w, tab, ids)
    tab._select_combo(tab.dependency_combo, DEPENDENCY_ALL)
    tab.search_edit.setText("ジョブ2")  # ジョブ1（待たれている側）を絞り込みで隠す
    tab._refresh_chart()
    assert pred not in tab.view.bars()
    dots = [i for i in tab.view.body.dependency_items() if isinstance(i, QGraphicsEllipseItem)]
    assert len(dots) == 1 and "ジョブ1" in dots[0].toolTip()
    # 丸はバーのすぐ上の段の隙間（上のジョブには入らない）
    rect = tab.view.bars()[succ].bar_rect
    assert dots[0].pos().y() == rect.top() - BAR_MARGIN


def test_a_broken_dependency_is_drawn_in_red(qapp, gantt):
    from gui.gantt_view import DEPENDENCY_ALL, _DEP_BROKEN_COLOR

    w, tab, ids = gantt
    pred, succ = _with_cross_job_dependency(qapp, w, tab, ids)
    # ジョブ2の設計を、ジョブ1の実装がどうやっても終わらない日（プロジェクト開始日）に固定する
    # （少し前に固定するだけなら、待たれている側が固定の手前へ寄って破綻しない）
    w.db.update_job_task_override_fields(ids["job2"], ids["t1"], start_pin_date="2026-04-06")
    tab.refresh_choices()
    _wait_recomputed(qapp, tab)
    tab._select_combo(tab.dependency_combo, DEPENDENCY_ALL)
    assert _dep_paths(tab)[0][1] == _DEP_BROKEN_COLOR
    assert tab.error_label.isVisible() and "開始固定日" in tab.error_label.text()


def test_dependency_arrows_follow_a_drag_and_are_redrawn_after_the_edit(qapp, gantt):
    w, tab, ids = gantt
    pred, succ = _with_cross_job_dependency(qapp, w, tab, ids)
    body = tab.view.body
    bar = tab.view.bars()[pred]
    _zoom_to(tab, bar)
    tab.view.select_keys([pred])
    qapp.processEvents()
    viewport = body.viewport()
    start = body.mapFromScene(bar.bar_rect.center())
    end = start + QPoint(80, 0)
    QTest.mousePress(viewport, Qt.LeftButton, Qt.ShiftModifier, start)
    move = QMouseEvent(QEvent.MouseMove, QPointF(end), QPointF(viewport.mapToGlobal(end)),
                       Qt.NoButton, Qt.LeftButton, Qt.ShiftModifier)
    QApplication.sendEvent(viewport, move)
    ghost = body._drag["ghosts"][pred].path().boundingRect()
    first = _dep_paths(tab)[0][0].elementAt(0)
    assert first.x == ghost.right()  # 影の右端から出ている（置いて行かれない）
    QTest.mouseRelease(viewport, Qt.LeftButton, Qt.ShiftModifier, end)
    _wait_recomputed(qapp, tab)
    new_bar = tab.view.bars()[pred]
    assert tab.view.body._dep_layer.scene() is tab.view.body.scene()
    assert _dep_paths(tab)[0][0].elementAt(0).x == new_bar.bar_rect.right()


def test_messages_without_problems_go_to_the_status_bar(qapp, gantt):
    w, tab, _ids = gantt
    assert "件のタスクを生成しました" in w.schedule_summary_label.text()
    assert tab.error_label.isHidden()


# -- 利用者視点の調査で見つかった問題の回帰テスト --------------------------------------


def test_switching_tabs_keeps_the_chart_and_its_view(qapp, gantt):
    """回帰テスト: タブを行き来しただけ（データの変更なし）でもチャートを作り直し、
    表示位置が全体表示に戻っていた（大きな計画では毎回数秒固まっていた）。"""
    w, tab, ids = gantt
    _zoom_to(tab, tab.view.bars()[_key(ids, "job1", "t1")])
    tab.view.body._clamp_scale()  # 操作でズームしたときと同じく、縮尺の下限を効かせておく
    tab.view._sync_panes()
    before, scene = tab.view.view_state(), tab.view.scene()
    w.tabs.setCurrentIndex(2)
    qapp.processEvents()
    w.tabs.setCurrentIndex(3)
    _wait(qapp, lambda: False, timeout=0.1)
    assert tab.view.scene() is scene
    assert tuple(tab.view.view_state())[:4] == tuple(before)[:4]


def test_a_recalculation_keeps_the_zoom_and_the_visible_dates(qapp, gantt):
    """回帰テスト: 配置の変更・他のタブでの変更による再計算で、表示位置が全体表示に戻っていた。"""
    w, tab, ids = gantt
    _zoom_to(tab, tab.view.bars()[_key(ids, "job1", "t1")])
    before = tab.view.view_state()
    assert not before.fit_x
    w.db.set_distribution_ratio(0.3)
    tab.refresh_choices()
    _wait_recomputed(qapp, tab)
    after = tab.view.view_state()
    assert after.sx == pytest.approx(before.sx)
    assert after.left_day == pytest.approx(before.left_day, abs=0.5)


def test_leaving_the_search_box_after_it_was_applied_does_not_rebuild_the_chart(qapp, gantt):
    """回帰テスト: 検索で絞り込まれた後にチャートをクリックしてフォーカスが外れると、
    同じ文字列でチャートを作り直し、表示位置が全体表示に戻っていた。"""
    _w, tab, _ids = gantt
    tab.search_edit.setText("ジョブ1")
    tab._apply_search()
    scene = tab.view.scene()
    tab.search_edit.editingFinished.emit()
    assert tab.view.scene() is scene


def test_a_filter_keeps_the_whole_chart_in_view_when_it_was_fitted(qapp, gantt):
    """全体表示のままなら、絞り込みで行が増減しても全体表示のまま（行が増えて見切れない）。"""
    _w, tab, _ids = gantt
    tab.view.fit_all()
    tab.search_edit.setText("ジョブ1")
    tab._apply_search()
    tab.search_edit.setText("")
    tab._apply_search()
    _wait(qapp, lambda: False, timeout=0.05)
    state = tab.view.view_state()
    assert state.fit_x and state.fit_y


def test_the_status_line_is_updated_while_the_tab_is_hidden(qapp, gantt):
    """回帰テスト: 編集の直後に他のタブへ移ると、計算が終わっても画面下部が
    「計算中です...」のまま残っていた。"""
    w, tab, ids = gantt
    key = _key(ids, "job1", "t1")
    tab.view.select_keys([key])
    tab.shift_tasks([key], 2)
    assert "計算中" in w.schedule_summary_label.text()
    w.tabs.setCurrentIndex(2)
    assert _wait(qapp, lambda: tab.cache.is_fresh())
    qapp.processEvents()
    assert "件のタスクを生成しました" in w.schedule_summary_label.text()


def _wheel(qapp, view, pos, dy=120, dx=0, modifiers=Qt.NoModifier):
    from PySide6.QtGui import QWheelEvent

    viewport = view.viewport()
    event = QWheelEvent(QPointF(pos), QPointF(viewport.mapToGlobal(pos)), QPoint(0, 0), QPoint(dx, dy),
                        Qt.NoButton, modifiers, Qt.NoScrollPhase, False)
    QApplication.sendEvent(viewport, event)
    qapp.processEvents()


def test_the_wheel_over_the_job_column_keeps_names_aligned_with_the_rows(qapp, gantt):
    """回帰テスト: 左列（ジョブ名）の上でホイールを回すと左列だけがスクロールして、
    ジョブ名が本体の別の行の横に並んでいた。見出し・左列の上でも本体と同じ操作にする。"""
    _w, tab, ids = gantt
    body, column, header = tab.view.body, tab.view.column, tab.view.header
    _zoom_to(tab, tab.view.bars()[_key(ids, "job1", "t1")])
    for view in (column, header):
        _wheel(qapp, view, QPoint(20, 20), dy=-120)
        assert column.verticalScrollBar().value() == body.verticalScrollBar().value()
        assert column.transform().m22() == pytest.approx(body.transform().m22())
        assert header.horizontalScrollBar().value() == body.horizontalScrollBar().value()
        assert header.transform().m11() == pytest.approx(body.transform().m11())


def test_the_wheel_zooms_under_the_cursor_in_proportion_and_a_sideways_wheel_scrolls(qapp, gantt):
    """回帰テスト: 横ホイール（チルト・横スワイプ）で縮小していた。タッチパッドの細かい
    スクロールでも1回ごとに1ノッチ分拡大して、少し動かすだけで何倍にもなっていた。"""
    _w, tab, ids = gantt
    body = tab.view.body
    _zoom_to(tab, tab.view.bars()[_key(ids, "job1", "t1")])
    sx = body.transform().m11()
    h = body.horizontalScrollBar().value()
    _wheel(qapp, body, QPoint(100, 100), dy=0, dx=-120)
    assert body.transform().m11() == pytest.approx(sx)
    assert body.horizontalScrollBar().value() != h
    # 細かいスクロール15回（合わせて1ノッチ）でも拡大は1ノッチ分だけ
    sx = body.transform().m11()
    for _ in range(15):
        _wheel(qapp, body, QPoint(100, 100), dy=8)
    assert body.transform().m11() == pytest.approx(sx * 1.15, rel=0.02)
    # マウスの下の日付・行は動かない
    point = QPoint(body.viewport().width() - 80, body.viewport().height() // 2)
    anchor = body.mapToScene(point)
    _wheel(qapp, body, point, dy=120)
    after = body.mapToScene(point)
    assert abs(after.x() - anchor.x()) < 2 / body.transform().m11()
    assert abs(after.y() - anchor.y()) < 2 / body.transform().m22()


def test_the_placement_slider_moved_with_keys_is_committed(qapp, gantt):
    """回帰テスト: 配置スライダーを溝のクリックやキーで動かすと、表示だけ変わって確定されず、
    スライダーの値と実際のチャートが食い違ったままになっていた。"""
    w, tab, _ids = gantt
    slider = tab.placement_slider
    start = slider.value()
    slider.setFocus()
    QTest.keyClick(slider, Qt.Key_Left)
    QTest.keyClick(slider, Qt.Key_Left)
    assert _wait(qapp, lambda: w.db.get_project()["distribution_ratio"] == pytest.approx((start - 2) / 100))
    assert w.undo_manager.undo_label() == "配置を変更"


def test_an_empty_filter_explains_why_the_chart_is_empty(qapp, gantt):
    """回帰テスト: 絞り込みで0件になると、理由が出ずに空白になり、前のチャートのスクロール
    バーが残っていた。ダークモードでは本体だけ暗く、明るい見出し・左列とL字に食い違っていた。"""
    from PySide6.QtGui import QPalette

    from gui.gantt_view import _PANE_BG

    w, tab, _ids = gantt
    tab.search_edit.setText("該当しないジョブ名")
    tab._apply_search()
    view = tab.view
    assert view.scene() is None
    assert view.placeholder.isVisible() and "絞り込み" in view.placeholder.text()
    assert view.body.horizontalScrollBar().maximum() == 0 and view.body.verticalScrollBar().maximum() == 0
    viewport = view.body.viewport()
    assert viewport.autoFillBackground()
    assert viewport.palette().color(viewport.backgroundRole()) == _PANE_BG
    assert viewport.palette().color(QPalette.Base) == _PANE_BG
    # 畳んだ見出しと画面下部にも、絞り込みで何件出ているかを出す
    assert "0件" in tab.filters_section._toggle_btn.text()
    assert "絞り込みで0件" in w.schedule_summary_label.text()
    tab.search_edit.setText("")
    tab._apply_search()
    assert view.scene() is not None and not view.placeholder.isVisible()
    assert tab.filters_section._toggle_btn.text() == "絞り込み"


def test_filter_check_boxes_wrap_instead_of_widening_the_window(qapp):
    """回帰テスト: チェックボックスを1行に並べていたため、チームが多いと絞り込みを開いた
    ときの最小幅が伸び（15チームで約2,000px）、ウィンドウが画面からはみ出していた。"""
    from gui.widgets_common import ChoiceFilterGroup

    group = ChoiceFilterGroup("チーム")
    group.rebuild([(i, f"チーム{i:02d}") for i in range(30)], colors={i: "#888888" for i in range(30)})
    assert group.minimumSizeHint().width() < 600
    layout = group.layout()
    assert layout.hasHeightForWidth()
    assert layout.heightForWidth(500) > layout.heightForWidth(4000)


def _render_bar(bar, width_px, height_px):
    """bar（シーンに載せたもの）を、その形がちょうど width_px × height_px になる縮尺で描く。"""
    from PySide6.QtGui import QImage, QPainter

    image = QImage(width_px, height_px, QImage.Format_ARGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    bar.scene().render(painter, QRectF(0, 0, width_px, height_px), bar.bar_rect, Qt.IgnoreAspectRatio)
    painter.end()
    return image


def _bar_in_scene(color="#ff0000"):
    """(シーン, 枠線が黒1pxのバー)。シーンは呼び出し側で持っておく（捨てるとバーも消える）。"""
    from datetime import date as _date

    from PySide6.QtGui import QBrush, QPainterPath, QPen
    from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene

    from gui.gantt_view import TaskBarItem

    rect = QRectF(0, 0, 100, 20)
    path = QPainterPath()
    path.addRect(rect)
    bar = TaskBarItem(path, rect, "JOB_001", "T_001", "TEAM_001", _date(2026, 4, 6), _date(2026, 4, 10))
    bar.setBrush(QBrush(QColor(color)))
    pen = QPen(QColor("#0b0b0b"), 1)
    pen.setCosmetic(True)
    bar.setPen(pen)
    bar.setFlag(QGraphicsItem.ItemIsSelectable, True)
    scene = QGraphicsScene()
    scene.addItem(bar)
    return scene, bar


def test_small_bars_are_drawn_without_the_black_border(qapp):
    """回帰テスト: 大きな計画を全体表示すると、1pxの黒い枠線がバーの塗りを覆って
    チャートが真っ黒になり、チームの色分けが見えなかった。"""
    _scene, bar = _bar_in_scene()
    image = _render_bar(bar, 100, 2)  # 高さ2pxまで縮めたバー
    for y in range(2):
        pixel = QColor(image.pixel(50, y))
        assert pixel.red() > 200 and pixel.green() < 60  # 黒い枠ではなく、バーの赤が見える


def test_a_selected_bar_is_drawn_with_a_blue_frame(qapp):
    """回帰テスト: 選択の印が Qt 既定の細い点線だけで、全体表示ではほとんど見分けられなかった。"""
    from gui.gantt_view import _SELECTION_COLOR

    _scene, bar = _bar_in_scene("#ffffff")
    image = _render_bar(bar, 200, 40)
    assert QColor(image.pixel(100, 2)).name() == "#ffffff"  # 未選択は白いまま
    bar.setSelected(True)
    image = _render_bar(bar, 200, 40)
    edge = QColor(image.pixel(100, 2))
    assert abs(edge.red() - _SELECTION_COLOR.red()) < 40 and abs(edge.blue() - _SELECTION_COLOR.blue()) < 40
    # 小さいバーは、選択するとバー全体が青くなる
    image = _render_bar(bar, 100, 3)
    assert QColor(image.pixel(50, 1)).red() < 120


def test_the_year_stays_visible_when_zoomed_into_the_middle_of_a_year(qapp, gantt):
    """回帰テスト: 年のラベルを年の中央に固定していたため、拡大して年の途中を見ていると
    年がどこにも出ず、何年か分からなかった。"""
    _w, tab, ids = gantt
    _zoom_to(tab, tab.view.bars()[_key(ids, "job1", "t2")])
    header = tab.view.header
    sx = header.transform().m11()
    visible = header.mapToScene(header.viewport().rect()).boundingRect()
    shown = [label for label, _l, _r in header.scene().gantt_year_labels if label.isVisible()]
    assert any(visible.left() <= label.x() and label.x() + label.boundingRect().width() / sx <= visible.right()
               for label in shown)


def test_date_tick_labels_neither_overlap_nor_get_cut_at_the_left_edge(qapp, gantt):
    """回帰テスト: 軸の左端の目盛りラベルが半分切れ、月初めが続くと隣と重なっていた（「8 9」）。"""
    _w, tab, _ids = gantt
    tab.view.fit_all()
    header = tab.view.header
    sx = header.transform().m11()
    left_limit = header.scene().gantt_header_rect.left()
    spans = sorted(
        (label.x(), label.x() + label.boundingRect().width() / sx)
        for label, _x in header.scene().gantt_tick_labels if label.isVisible()
    )
    assert spans and spans[0][0] >= left_limit - 1e-6
    for (_l1, r1), (l2, _r2) in zip(spans, spans[1:]):
        assert r1 <= l2


def test_a_dependency_arrow_enters_from_above_when_there_is_no_room_on_the_left(qapp):
    """回帰テスト: 後のタスクが前のタスクの終わった直後に始まる（依存でいちばん多い）と、
    後のバーの手前へ戻る小さなS字の折れになり、矢じりが線より長く窮屈に見えていた。"""
    from gui.gantt_view import HEAD_DOWN, HEAD_RIGHT, HEAD_UP, dependency_path

    a = QRectF(0, 0, 100, 20)
    path, head, direction = dependency_path(a, QRectF(100, 60, 80, 20), 6)
    assert direction == HEAD_DOWN and (head.x(), head.y()) == (106, 60)
    assert path.elementCount() == 3  # 出る横線 → 縦線 の1回だけ折れる
    _path, head, direction = dependency_path(a, QRectF(100, -60, 80, 20), 6)
    assert direction == HEAD_UP and (head.x(), head.y()) == (106, -40)
    # 十分右にあれば従来どおり左端の中央へ横から入る
    _path, head, direction = dependency_path(a, QRectF(200, 60, 80, 20), 6)
    assert direction == HEAD_RIGHT and (head.x(), head.y()) == (200, 70)
    # 後のバーが短すぎて縦にも入れないときだけ、手前へ戻る
    _path, head, direction = dependency_path(a, QRectF(96, 60, 8, 20), 6)
    assert direction == HEAD_RIGHT and (head.x(), head.y()) == (96, 70)


def test_dependency_arrows_are_drawn_under_the_task_names(qapp):
    """回帰テスト: 矢印の線がタスク名の上を横切り、文字が読めなくなっていた。"""
    from gui.gantt_view import _DEP_Z, _OVERRUN_BAR_Z, _TASK_LABEL_Z

    assert _OVERRUN_BAR_Z < _DEP_Z < _TASK_LABEL_Z


def test_filtering_does_not_mark_the_rows_as_out_of_order(qapp, gantt):
    """回帰テスト: 「依存のつながり」で絞り込むだけで、編集していないのに並べ直すボタンが
    「選んだ順どおりではない」と目立っていた（絞り込んだ一部で並べ直していたため）。"""
    from gui.gantt_row_order import GROUP_DEPENDENCY

    w, tab, ids = gantt
    job1 = next(j for j in w.db.list_jobs() if j["id"] == ids["job1"])
    w.db.update_job(job1["id"], job1["name"], job1["workflow_id"], job1["default_milestone_id"],
                    job1["priority"], "隠す")
    job3 = w.db.add_job("ジョブ3", job1["workflow_id"], job1["default_milestone_id"], 3)
    w.db.add_external_dependency(job3, ids["t1"], ids["job1"], ids["t2"])
    tab.refresh_choices()
    _wait_recomputed(qapp, tab)
    tab._select_combo(tab.row_grouping_combo, GROUP_DEPENDENCY)
    assert _row_jobs(tab) == ["JOB_001", f"JOB_{job3:03d}", "JOB_002"]
    tab.tag_filter._checks["隠す"].setChecked(False)  # ジョブ1（待たれている側）を隠す
    assert _row_jobs(tab) == [f"JOB_{job3:03d}", "JOB_002"]
    assert not tab._row_order_stale


def test_escape_cancels_a_drag_and_otherwise_clears_the_selection(qapp, gantt):
    w, tab, ids = gantt
    key = _key(ids, "job1", "t1")
    bar = tab.view.bars()[key]
    _zoom_to(tab, bar)
    body = tab.view.body
    viewport = body.viewport()
    start = body.mapFromScene(bar.bar_rect.center())
    end = start + QPoint(70, 0)
    QTest.mousePress(viewport, Qt.LeftButton, Qt.ShiftModifier, start)
    QApplication.sendEvent(viewport, QMouseEvent(QEvent.MouseMove, QPointF(end), QPointF(viewport.mapToGlobal(end)),
                                                 Qt.NoButton, Qt.LeftButton, Qt.ShiftModifier))
    assert body._drag is not None and body._drag["started"]
    QTest.keyClick(viewport, Qt.Key_Escape)
    QTest.keyClick(body, Qt.Key_Escape, Qt.ShiftModifier)
    QTest.mouseRelease(viewport, Qt.LeftButton, Qt.ShiftModifier, end)
    qapp.processEvents()
    assert body._drag is None and tab.cache.is_fresh()
    assert _override(w.db, ids["job1"], ids["t1"])["start_pin_date"] is None
    tab.view.select_keys([key])
    QTest.keyClick(body, Qt.Key_Escape)
    assert tab.view.selected_keys() == []


def test_ctrl_a_selects_every_task_and_a_alone_still_fits(qapp, gantt):
    _w, tab, _ids = gantt
    body = tab.view.body
    QTest.keyClick(body, Qt.Key_A, Qt.ControlModifier)
    assert sorted(tab.view.selected_keys()) == sorted(tab.view.bars())


def test_shift_click_adds_the_bar_to_the_selection(qapp, gantt):
    """回帰テスト: 複数選んでから Shift＋クリックすると、押した瞬間に選択を外して1件に戻っていた。"""
    _w, tab, ids = gantt
    keys = [_key(ids, "job1", "t1"), _key(ids, "job2", "t1")]
    tab.view.select_keys(keys)
    body = tab.view.body
    other = tab.view.bars()[_key(ids, "job1", "t2")]
    body.ensureVisible(other.sceneBoundingRect())
    qapp.processEvents()
    pos = body.mapFromScene(other.bar_rect.center())
    QTest.mouseClick(body.viewport(), Qt.LeftButton, Qt.ShiftModifier, pos)
    qapp.processEvents()
    assert sorted(tab.view.selected_keys()) == sorted(keys + [other.key])
    assert tab.cache.is_fresh()  # 動かしていないので書き込まない


def test_ctrl_clicking_a_selected_job_name_deselects_it_and_the_chart_keeps_the_focus(qapp, gantt):
    w, tab, _ids = gantt
    column = tab.view.column
    rows = column.scene().gantt_job_rows
    y_top, y_bottom, job = rows[0]
    pos = column.mapFromScene(50, (y_top + y_bottom) / 2)
    QTest.mouseClick(column.viewport(), Qt.LeftButton, Qt.NoModifier, pos)
    assert {k[0] for k in tab.view.selected_keys()} == {job}
    # 左列はフォーカスを取らず、本体に渡す（ジョブを選んだ直後に F・A が効くように）
    assert column.focusPolicy() == Qt.NoFocus
    assert w.focusWidget() is tab.view.body
    QTest.mouseClick(column.viewport(), Qt.LeftButton, Qt.ControlModifier, pos)
    assert tab.view.selected_keys() == []


def test_the_context_menu_names_its_target_and_checks_the_current_values(qapp, gantt):
    w, tab, ids = gantt
    key = _key(ids, "job1", "t1")
    w.db.update_job_task_override_fields(ids["job1"], ids["t1"], status="in_progress", team_id=ids["team_b"])
    tab.refresh_choices()
    _wait_recomputed(qapp, tab)
    shown = []

    def fake_exec(menu, _pos):
        shown.append(menu)
        return None

    with patch.object(tab, "_exec_menu", fake_exec):
        tab.view.select_keys([key])
        tab._show_context_menu(QPoint(0, 0))
        tab.view.select_keys([key, _key(ids, "job2", "t1")])
        tab._show_context_menu(QPoint(0, 0))
    single, multi = shown
    title = single.actions()[0]
    assert title.text() == "ジョブ1 / 設計" and not title.isEnabled()
    assert multi.actions()[0].text() == "選択中の2件のタスク"

    def checked(menu, name):
        sub = next(a.menu() for a in menu.actions() if a.menu() is not None and a.text() == name)
        return [a.text() for a in sub.actions() if a.isChecked()]
    assert checked(single, "状態") == ["進行中"]
    assert checked(single, "チーム") == ["チームB"]
    assert checked(multi, "状態") == []  # 値が揃っていなければ印を付けない


def test_a_shortened_job_name_shows_the_full_name_as_a_tooltip(qapp, gantt):
    w, tab, ids = gantt
    job1 = next(j for j in w.db.list_jobs() if j["id"] == ids["job1"])
    long_name = "とても長いジョブの名前" * 4
    w.db.update_job(job1["id"], long_name, job1["workflow_id"], job1["default_milestone_id"],
                    job1["priority"], job1["tags"])
    tab.refresh_choices()
    _wait_recomputed(qapp, tab)
    label = next(label for label, _s, _y in tab.view.column.scene().gantt_job_labels if label.text().endswith("…"))
    assert label.toolTip() == long_name


def test_the_project_path_in_the_status_bar_is_shortened_in_the_middle(qapp, gantt):
    """回帰テスト: パスが途中で切れ、右の計算結果の文言と区切りなくつながって見えていた。"""
    w, _tab, _ids = gantt
    label = w.project_path_label
    full = label.full_text()
    assert str(w.db.path) in full
    label.resize(120, label.height())
    assert "…" in label.text() and label.toolTip() == full
