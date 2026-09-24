"""
計画の確定と変更案（docs/roadmap.md §8）のGUIテスト。

状態帯（全タブの上）の表示とボタン、ガントのバーの装飾（未確定の斜線・確定して
いた位置の細線）、確定後のドラッグが変更案として記録されることを確かめる。
プロジェクトとウィンドウの用意は tests/test_gui_gantt_edit.py のものを使う。
"""

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
