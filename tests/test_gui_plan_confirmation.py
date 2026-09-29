"""
計画の確定と変更案（docs/roadmap.md §8）のGUIテスト。

状態帯（全タブの上）の表示とボタン、ガントのバーの装飾（未確定の斜線・確定して
いた位置の細線）、確定後のドラッグが変更案として記録されることを確かめる。
プロジェクトとウィンドウの用意は tests/test_gui_gantt_edit.py のものを使う。
"""

from datetime import timedelta
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pandas")

from test_gui_gantt_edit import (  # noqa: E402,F401  (gantt/qapp はフィクスチャ)
    _drag,
    _key,
    _override,
    _wait,
    _wait_recomputed,
    _zoom_to,
    gantt,
    qapp,
)

from gui.plan_confirmation import CONFIRMED, DRAFT, UNCONFIRMED  # noqa: E402

pytestmark = pytest.mark.gui


def _answer(tab, label):
    """確認ダイアログで、label のボタンを押したことにする。"""
    return patch.object(
        tab, "_exec_message_box",
        side_effect=lambda box: next(b for b in box.buttons() if b.text() == label),
    )


def _band_settled(qapp, w, status):
    return _wait(qapp, lambda: w.tab_gantt.cache.is_fresh() and w.plan_band.status == status)


def _confirm(qapp, w):
    w.plan_band.confirm_button.click()
    assert _band_settled(qapp, w, CONFIRMED)
    _wait_recomputed(qapp, w.tab_gantt)


def test_band_starts_unconfirmed_and_shows_buttons_only_on_the_gantt_tab(qapp, gantt):
    w, tab, _ids = gantt
    assert _band_settled(qapp, w, UNCONFIRMED)
    assert w.plan_band.isVisible()
    assert w.plan_band.title_label.text() == "未確定"
    assert w.plan_band.confirm_button.isVisible()
    w.tabs.setCurrentIndex(0)
    assert _wait(qapp, lambda: not w.plan_band.confirm_button.isVisible())
    assert w.plan_band.isVisible()  # 帯そのものは全タブに出す


def test_confirming_records_the_schedule_and_undo_returns_to_unconfirmed(qapp, gantt):
    w, tab, _ids = gantt
    _confirm(qapp, w)
    assert w.db.has_confirmation()
    assert len(w.db.list_confirmed_schedule()) == 4
    assert w.plan_band.clear_button.isVisible()
    assert not any(bar.unconfirmed for bar in tab.view.bars().values())
    assert w.undo_manager.undo_label() == "計画を確定"

    w.undo_manager.undo()
    assert _band_settled(qapp, w, UNCONFIRMED)
    assert not w.db.has_confirmation()


def test_dragging_after_confirming_makes_a_draft_with_a_baseline(qapp, gantt):
    w, tab, ids = gantt
    _confirm(qapp, w)
    key = _key(ids, "job1", "t1")
    bar = tab.view.bars()[key]
    confirmed_start = bar.start
    _zoom_to(tab, bar)
    body = tab.view.body
    start = body.mapFromScene(bar.bar_rect.center())
    end = body.mapFromScene(bar.bar_rect.center().x() + 7 * 10, bar.bar_rect.center().y())
    _drag(qapp, body, start, end)
    _wait_recomputed(qapp, tab)
    assert _band_settled(qapp, w, DRAFT)

    # 手動ピンにはせず、変更案の移動として記録する
    assert _override(w.db, ids["job1"], ids["t1"])["start_pin_date"] is None
    moves = w.db.list_draft_moves()
    assert [(m["job_id"], m["workflow_task_id"]) for m in moves] == [(ids["job1"], ids["t1"])]
    moved = tab.view.bars()[key]
    assert moved.start == tab._calendar.shift(confirmed_start, 5, moved.team_key)
    assert "確定:" in moved.toolTip()
    assert "変更 1件" in w.plan_band.detail_label.text()
    for button in (w.plan_band.confirm_draft_button, w.plan_band.confirm_selected_button,
                   w.plan_band.discard_button):
        assert button.isVisible()

    # 変更を破棄すると、確定した位置に戻る
    with _answer(tab, "変更を破棄"):
        w.plan_band.discard_button.click()
    assert _band_settled(qapp, w, CONFIRMED)
    _wait_recomputed(qapp, tab)
    assert tab.view.bars()[key].start == confirmed_start
    assert w.db.list_draft_moves() == []


def test_confirming_the_draft_moves_the_confirmed_dates(qapp, gantt):
    w, tab, ids = gantt
    _confirm(qapp, w)
    w.db.update_job_task_override_fields(ids["job1"], ids["t1"], override_days=8)
    tab.refresh_choices()  # 他のタブでの編集の後にガントタブを開いたときと同じ
    assert _band_settled(qapp, w, DRAFT)
    _wait_recomputed(qapp, tab)
    w.plan_band.confirm_draft_button.click()
    assert _band_settled(qapp, w, CONFIRMED)
    row = next(r for r in w.db.list_confirmed_schedule()
               if (r["job_id"], r["workflow_task_id"]) == (ids["job1"], ids["t1"]))
    assert row["days"] == 8


def test_new_job_after_confirming_is_drawn_unconfirmed(qapp, gantt):
    w, tab, ids = gantt
    _confirm(qapp, w)
    wf = w.db.list_jobs()[0]["workflow_id"]
    job3 = w.db.add_job("ジョブ3", wf, None, 3)
    tab.refresh_choices()
    assert _band_settled(qapp, w, DRAFT)
    _wait_recomputed(qapp, tab)
    bars = tab.view.bars()
    assert bars[_key(dict(ids, job3=job3), "job3", "t1")].unconfirmed
    assert not bars[_key(ids, "job1", "t1")].unconfirmed


def test_confirm_selected_is_grayed_out_unless_a_changed_task_is_selected(qapp, gantt):
    w, tab, ids = gantt
    _confirm(qapp, w)
    w.db.update_job_task_override_fields(ids["job1"], ids["t1"], override_days=8)
    tab.refresh_choices()  # 他のタブでの編集の後にガントタブを開いたときと同じ
    assert _band_settled(qapp, w, DRAFT)
    _wait_recomputed(qapp, tab)
    button = w.plan_band.confirm_selected_button

    tab.view.select_keys([])
    assert _wait(qapp, lambda: not button.isEnabled())
    # 変更も、変更の影響も無いタスクだけを選んでいる
    tab.view.select_keys([_key(ids, "job2", "t1")])
    assert _wait(qapp, lambda: not button.isEnabled())

    tab.view.select_keys([_key(ids, "job1", "t1")])
    assert _wait(qapp, button.isEnabled)
    button.click()
    assert _band_settled(qapp, w, CONFIRMED)
    assert w.undo_manager.undo_label() == "選択した変更を確定"


def test_clearing_the_confirmation_asks_first(qapp, gantt):
    w, _tab, _ids = gantt
    _confirm(qapp, w)
    with _answer(w.tab_gantt, "キャンセル"):
        w.plan_band.clear_button.click()
    assert w.db.has_confirmation()
    with _answer(w.tab_gantt, "未確定に戻す"):
        w.plan_band.clear_button.click()
    assert _band_settled(qapp, w, UNCONFIRMED)
    assert not w.db.has_confirmation()


# -- ジョブ作成タブ（§8-9） ---------------------------------------------------------


def _open_jobs_tab(qapp, w, job_id):
    jobs = w.tab_jobs
    w.tabs.setCurrentWidget(jobs)
    jobs.refresh_choices()
    jobs.jobs_section.table.setCurrentCell(jobs._row_by_job_id[job_id], 0)
    _wait(qapp, lambda: False, timeout=0.1)
    return jobs


def _override_row(jobs, task_id):
    from gui.widgets_common import row_id

    table = jobs.override_table
    return next(r for r in range(table.rowCount()) if row_id(table, r) == task_id)


def test_jobs_tab_hides_the_plan_columns_until_the_first_confirmation(qapp, gantt):
    from gui.tab_jobs import _JOB_PLAN_COLUMN, _OVERRIDE_PLAN_COLUMN

    w, _tab, ids = gantt
    jobs = _open_jobs_tab(qapp, w, ids["job1"])
    assert jobs.jobs_section.table.isColumnHidden(_JOB_PLAN_COLUMN)
    assert jobs.override_table.isColumnHidden(_OVERRIDE_PLAN_COLUMN)
    assert jobs.plan_filter.isHidden()


def test_jobs_tab_shows_confirmed_dates_and_marks_changed_inputs_in_red(qapp, gantt):
    from gui.tab_jobs import _JOB_PLAN_COLUMN, _OVERRIDE_PLAN_COLUMN

    w, tab, ids = gantt
    _confirm(qapp, w)
    confirmed = {(r["job_id"], r["workflow_task_id"]): r for r in w.db.list_confirmed_schedule()}
    jobs = _open_jobs_tab(qapp, w, ids["job1"])
    table = jobs.jobs_section.table
    assert not table.isColumnHidden(_JOB_PLAN_COLUMN)
    assert table.item(jobs._row_by_job_id[ids["job1"]], _JOB_PLAN_COLUMN).text() == "確定"
    row = _override_row(jobs, ids["t1"])
    plan_item = jobs.override_table.item(row, _OVERRIDE_PLAN_COLUMN)
    assert plan_item.text().startswith(confirmed[(ids["job1"], ids["t1"])]["start_date"])
    assert "5日" in plan_item.text()

    # 日数を変えると（表は作り直さずに）赤文字になり、確定値と今の値が並ぶ
    spin = jobs.override_table.cellWidget(row, 2)
    spin.setValue(8)
    _wait(qapp, lambda: False, timeout=0.1)
    assert jobs.override_table.cellWidget(row, 2) is spin
    assert "color" in spin.styleSheet()
    assert spin.toolTip() == "確定: 5日 → 変更案: 8日"
    assert "確定: 5日 → 変更案: 8日" in jobs.override_table.item(row, _OVERRIDE_PLAN_COLUMN).toolTip()
    assert table.item(jobs._row_by_job_id[ids["job1"]], _JOB_PLAN_COLUMN).text() == "変更 1"
    # 変更していない行は赤くしない
    other = jobs.override_table.cellWidget(_override_row(jobs, ids["t2"]), 2)
    assert other.styleSheet() == ""


def test_jobs_added_after_confirming_are_unconfirmed_and_can_be_filtered(qapp, gantt):
    from gui.plan_confirmation import JOB_CONFIRMED
    from gui.tab_jobs import _JOB_PLAN_COLUMN, _OVERRIDE_PLAN_COLUMN

    w, _tab, ids = gantt
    _confirm(qapp, w)
    wf = w.db.list_jobs()[0]["workflow_id"]
    job3 = w.db.add_job("ジョブ3", wf, None, 3)
    jobs = _open_jobs_tab(qapp, w, job3)
    table = jobs.jobs_section.table
    assert table.item(jobs._row_by_job_id[job3], _JOB_PLAN_COLUMN).text() == "未確定"
    assert jobs.override_table.item(0, _OVERRIDE_PLAN_COLUMN).text() == "未確定"

    jobs.plan_filter._checks[JOB_CONFIRMED].setChecked(False)
    assert set(jobs._row_by_job_id) == {job3}


def test_opening_a_file_expands_dependency_templates_without_marking_it_modified(qapp, tmp_path):
    """依存テンプレートから展開するジョブ間の依存は、ファイルを開いた時点で揃える
    （ジョブ作成タブを開いたときだけだと、確定の後に依存が増えて「変更あり」に
    なってしまうため）。揃えるだけなので未保存にはしない。"""
    import shiboken6

    from gui.app_settings import AppSettings
    from gui.db import ProjectDatabase
    from gui.main import MainWindow

    path = tmp_path / "tpl.pschedule"
    db = ProjectDatabase.create_new(str(path))
    db.set_project("P", "2026-04-06")
    team = db.add_team("チームA", 2)
    wf_a = db.add_workflow("WF_A")
    task_a = db.add_workflow_task(wf_a, "作業A", team, 3)
    wf_b = db.add_workflow("WF_B")
    task_b = db.add_workflow_task(wf_b, "作業B", team, 3)
    job_a = db.add_job("A", wf_a, None, 1)
    job_b = db.add_job("B", wf_b, None, 2)
    db.add_job_dependency_link(job_b, job_a)
    db.add_dependency_template(wf_b, task_b, wf_a, task_a)
    # テンプレートの展開が済んでいない古いファイルの状態を作る
    db._conn.execute("DELETE FROM job_external_dependencies")
    db._conn.commit()
    db.save()
    db.close()

    w = MainWindow(app_settings=AppSettings(str(tmp_path / "settings.ini")))
    try:
        w._open_database(ProjectDatabase.open_existing(str(path)))
        pairs = w.db._conn.execute(
            "SELECT job_id, workflow_task_id, depends_on_job_id, depends_on_workflow_task_id "
            "FROM job_external_dependencies"
        ).fetchall()
        assert [tuple(p) for p in pairs] == [(job_b, task_b, job_a, task_a)]
        assert not w.db.is_dirty()
        assert not w.undo_manager.can_undo()
    finally:
        w._shutdown_schedule_cache()
        w.db.on_change = None
        w.db.undo_manager = None
        w.db.close()
        shiboken6.delete(w)
        qapp.processEvents()


# -- 全面再計画（§8-6） --------------------------------------------------------------


def test_full_replan_becomes_a_draft_and_confirming_it_shows_the_new_plan(qapp, gantt):
    from datetime import date

    w, tab, ids = gantt
    _confirm(qapp, w)
    assert w.plan_band.replan_button.isVisible()
    base = date(2026, 6, 1)
    with patch.object(tab, "_exec_replan_dialog", return_value=base):
        w.plan_band.replan_button.click()
    assert _band_settled(qapp, w, DRAFT)
    _wait_recomputed(qapp, tab)
    assert "全面再計画（06/01 から）" in w.plan_band.detail_label.text()
    assert w.undo_manager.undo_label() == "全面再計画"
    # 全体として組み直した結果なので、一部だけは確定できない
    tab.view.select_keys([_key(ids, "job1", "t2")])
    assert _wait(qapp, lambda: not w.plan_band.confirm_selected_button.isEnabled())
    assert all(bar.start >= base for bar in tab.view.bars().values())

    w.plan_band.confirm_draft_button.click()
    assert _band_settled(qapp, w, CONFIRMED)
    today = date.today()
    assert w.plan_band.detail_label.text() == f"06/01 から新計画（{today:%m/%d} 再計画）"


def test_band_suggests_a_full_replan_when_the_draft_moves_many_tasks(qapp, gantt):
    w, tab, ids = gantt
    _confirm(qapp, w)
    tab.app_settings.set("full_replan_threshold_percent", 10)
    w.db.update_job_task_override_fields(ids["job1"], ids["t1"], override_days=8)
    tab.refresh_choices()
    assert _band_settled(qapp, w, DRAFT)
    _wait_recomputed(qapp, tab)
    assert _wait(qapp, lambda: "全面再計画を検討" in w.plan_band.detail_label.text())

    tab.app_settings.set("full_replan_threshold_percent", 100)
    w._apply_app_settings()  # オプション画面でOKを押したときと同じ
    assert _wait(qapp, lambda: "全面再計画を検討" not in w.plan_band.detail_label.text())


def test_replan_dialog_counts_the_tasks_and_warns_about_past_dates(qapp, gantt):
    from datetime import date

    from PySide6.QtCore import QDate

    from gui.replan_dialog import ReplanDialog

    w, tab, ids = gantt
    _confirm(qapp, w)
    starts = sorted(date.fromisoformat(r["start_date"]) for r in w.db.list_confirmed_schedule())
    today = starts[1]
    dialog = ReplanDialog(w.db, today=today)
    try:
        assert dialog.base_date() == today
        assert dialog.warning_label.isHidden()
        assert "置き直します" in dialog.preview_label.text()
        # 未来の基準日: 今日〜基準日の前日に始まる予定のタスクは残す
        future = starts[-1]
        dialog.date_edit.setDate(QDate(future.year, future.month, future.day))
        assert "今の確定のまま残します" in dialog.preview_label.text()
        # 過去の日付は選べるが警告する
        past = date(2026, 1, 5)
        dialog.date_edit.setDate(QDate(past.year, past.month, past.day))
        assert not dialog.warning_label.isHidden()
        assert "今日より前" in dialog.warning_label.text()
    finally:
        dialog.deleteLater()


# -- HTML出力（§8-9） ----------------------------------------------------------------


def test_generate_asks_which_schedule_only_during_a_draft(qapp, gantt, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    from gui.gantt_generator import PLAN_OUTPUT_CONFIRMED

    w, tab, ids = gantt
    out = tmp_path / "out"
    out.mkdir()
    html = out / "schedule_gantt.html"
    _confirm(qapp, w)
    with patch.object(w, "_ask_plan_output") as ask, \
            patch.object(w, "_ask_output_dir", return_value=str(out)), \
            patch.object(QMessageBox, "information"):
        w.on_generate_gantt()
    assert not ask.called  # 確定済み（変更案が無い）なら尋ねない
    assert "確定した日程" in html.read_text(encoding="utf-8")

    w.db.update_job_task_override_fields(ids["job1"], ids["t1"], override_days=8)
    html.unlink()
    with patch.object(w, "_ask_plan_output", return_value=None), \
            patch.object(w, "_ask_output_dir", return_value=str(out)) as ask_dir:
        w.on_generate_gantt()
    assert not ask_dir.called and not html.exists()  # キャンセルしたら何もしない

    with patch.object(w, "_ask_plan_output", return_value=PLAN_OUTPUT_CONFIRMED), \
            patch.object(w, "_ask_output_dir", return_value=str(out)), \
            patch.object(QMessageBox, "information"):
        w.on_generate_gantt()
    assert "確定した日程" in html.read_text(encoding="utf-8")


def test_band_height_does_not_change_with_the_buttons(qapp, gantt):
    """ボタンはガントチャートタブでだけ出すが、その有無で帯の高さが変わらないこと。"""
    w, _tab, _ids = gantt
    assert _band_settled(qapp, w, UNCONFIRMED)
    _wait(qapp, lambda: False, timeout=0.05)
    with_buttons = w.plan_band.height()
    w.tabs.setCurrentIndex(0)
    assert _wait(qapp, lambda: not w.plan_band.confirm_button.isVisible())
    _wait(qapp, lambda: False, timeout=0.05)
    assert w.plan_band.height() == with_buttons


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_band_follows_the_light_and_dark_theme(qapp, dark):
    """ダークモードでは暗い帯にし（明るい帯が浮かない）、ボタンの文字色も明示する
    （スタイル任せだと Windows 11 で白文字になり読めない）。起動後の切り替えにも追従する。"""
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

    from gui.plan_band import _COLORS, PlanStatusBand

    original = QApplication.palette()
    # 親の中に置く（スタイルシートを当てた子にはアプリのパレット変更が届かないため、
    # 単独のウィンドウで試すと実際の画面での不具合を見逃す）
    parent = QWidget()
    band = PlanStatusBand()
    QVBoxLayout(parent).addWidget(band)
    parent.show()
    qapp.processEvents()
    try:
        palette = QPalette(original)
        palette.setColor(QPalette.Window, QColor("#202020" if dark else "#f0f0f0"))
        QApplication.setPalette(palette)
        qapp.processEvents()
        background, _border, text = _COLORS[dark][UNCONFIRMED]
        sheet = band.styleSheet()
        assert f"background: {background};" in sheet
        assert "QPushButton {" in sheet and f"color: {text};" in sheet.split("QPushButton {", 1)[1]
        assert (QColor(background).lightness() < 128) == dark
    finally:
        QApplication.setPalette(original)
        parent.deleteLater()
        qapp.processEvents()


def test_marking_a_moved_task_in_progress_keeps_it_where_it_is(qapp, gantt):
    """回帰テスト: 変更案で動かしたタスクを右クリックで進行中にすると、確定した位置へ
    飛び戻っていた。表示中の位置のまま止まる（gui/main.py が表示中の日程を渡す）。"""
    from test_gui_gantt_edit import _choose_menu

    w, tab, ids = gantt
    _confirm(qapp, w)
    key = _key(ids, "job1", "t1")
    bar = tab.view.bars()[key]
    w.db.set_draft_move(ids["job1"], ids["t1"], (bar.start + timedelta(days=14)).isoformat())
    tab.refresh_choices()
    assert _band_settled(qapp, w, DRAFT)
    _wait_recomputed(qapp, tab)
    moved = (tab.view.bars()[key].start, tab.view.bars()[key].end)

    tab.view.select_keys([key])
    with _choose_menu(tab, "進行中"):
        tab._show_context_menu(None)
    _wait_recomputed(qapp, tab)
    assert (tab.view.bars()[key].start, tab.view.bars()[key].end) == moved
    assert tab.view.bars()[key].status == "in_progress"
