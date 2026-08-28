"""
タスク上書き（job_task_overrides）の保持に関する回帰テスト。

`ProjectDatabase.upsert_job_task_override()` は行ごと置き換えるAPIで、
呼び出し元が渡さなかった列は既定値（start_pin_date=None, tags=""）で
上書きされる。マイルストーンの自動調整（enforce_milestone_floor /
cascade_milestone_to_successors / apply_milestone_consistency_repair）は
マイルストーン以外を変えるつもりが無いため、全列版を使うと開始固定日と
タスク タグが黙って消えてしまう。この3経路が
`_set_override_milestone()`（マイルストーン列だけのUPDATE）を通り続けることを
固定する。

分類は core（gui/db.py の変更で `pytest -m core` に必ず含まれるようにする。
Qt・pandas いずれにも依存しない）。マイルストーン整合性そのものの検証は
tests/test_gui_gantt_smoke.py 側にある。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.db import ProjectDatabase  # noqa: E402

pytestmark = pytest.mark.core


def _build_chain_job(db):
    """設計 → 実装 の2タスク。「早い」「遅い」2つのマイルストーンを用意する。"""
    team_id = db.add_team("チームA", 2)
    wf_id = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf_id, "設計", team_id, 3)
    t2 = db.add_workflow_task(wf_id, "実装", team_id, 5)
    db.add_task_dependency(wf_id, t1, t2)
    ms_early = db.add_milestone("早いMS", "2026-03-31")
    ms_late = db.add_milestone("遅いMS", "2026-09-30")
    job_id = db.add_job("ジョブ1", wf_id, ms_early, 1)
    return {"job": job_id, "wf": wf_id, "team": team_id,
            "t1": t1, "t2": t2, "ms_early": ms_early, "ms_late": ms_late}


def _override(db, job_id, workflow_task_id):
    return next(
        r for r in db.list_job_tasks_with_overrides(job_id)
        if r["workflow_task_id"] == workflow_task_id
    )


def _assert_untouched_columns_survived(db, ids):
    """マイルストーン以外の上書き列が、調整前のまま残っていること。"""
    row = _override(db, ids["job"], ids["t2"])
    assert row["start_pin_date"] == "2026-05-01"
    assert row["tags"] == "確定, 外注"
    assert row["override_days"] == 9
    assert row["override_team_id"] == ids["team"]
    # 目的であるマイルストーンの引き上げ自体は起きていること
    assert db.effective_milestone(ids["job"], ids["t2"])["milestone_id"] == ids["ms_late"]


def _pin_successor(db, ids):
    """後続タスクに、マイルストーン以外の上書きをひととおり設定する。"""
    db.upsert_job_task_override(
        ids["job"], ids["t2"], is_active=True, override_days=9,
        milestone_id=ids["ms_early"], team_id=ids["team"],
        start_pin_date="2026-05-01", tags="確定, 外注",
    )


def test_enforce_milestone_floor_keeps_start_pin_date_and_tags(tmp_path):
    """回帰テスト: ジョブタブで開始固定日を入力した操作の中で
    enforce_milestone_floor() が発火すると、入力したばかりの固定日が
    その場で消えていた（gui/tab_jobs.py の _on_override_changed は
    upsert の直後にこれを呼ぶ）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    ids = _build_chain_job(db)
    db.upsert_job_task_override(ids["job"], ids["t1"], milestone_id=ids["ms_late"])
    _pin_successor(db, ids)

    assert db.enforce_milestone_floor(ids["job"], ids["t2"]) is True
    _assert_untouched_columns_survived(db, ids)
    db.close()


def test_cascade_milestone_to_successors_keeps_start_pin_date_and_tags(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    ids = _build_chain_job(db)
    _pin_successor(db, ids)
    db.upsert_job_task_override(ids["job"], ids["t1"], milestone_id=ids["ms_late"])

    assert db.cascade_milestone_to_successors(ids["job"], ids["t1"]) == [ids["t2"]]
    _assert_untouched_columns_survived(db, ids)
    db.close()


def test_milestone_consistency_repair_keeps_start_pin_date_and_tags(tmp_path):
    """一括再調整は複数ジョブの上書き行をまとめて書き換えるため、消える場合の
    被害が最も大きい経路。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    ids = _build_chain_job(db)
    _pin_successor(db, ids)
    db.upsert_job_task_override(ids["job"], ids["t1"], milestone_id=ids["ms_late"])
    # tab3を経由しない崩れ方（先行タスクのマイルストーンだけが後ろへ動く）を作る
    plan = db.plan_milestone_consistency_repair()
    assert [item["workflow_task_id"] for item in plan] == [ids["t2"]]

    db.apply_milestone_consistency_repair(plan)
    _assert_untouched_columns_survived(db, ids)
    assert db.plan_milestone_consistency_repair() == []
    db.close()


def test_milestone_adjustment_creates_a_minimal_row_when_none_exists(tmp_path):
    """上書き行がまだ無いタスクを調整する場合は、マイルストーンだけを持つ行を
    作る（他の列は「上書きなし」のまま）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    ids = _build_chain_job(db)
    db.upsert_job_task_override(ids["job"], ids["t1"], milestone_id=ids["ms_late"])

    assert db.enforce_milestone_floor(ids["job"], ids["t2"]) is True
    row = _override(db, ids["job"], ids["t2"])
    assert row["start_pin_date"] is None
    assert row["tags"] == ""
    assert row["override_days"] is None
    assert row["override_team_id"] is None
    assert bool(row["is_active"]) is True
    assert db.effective_milestone(ids["job"], ids["t2"])["milestone_id"] == ids["ms_late"]
    db.close()


def test_milestone_adjustment_does_not_reactivate_a_disabled_task(tmp_path):
    """無効化してあるタスクのマイルストーンを調整しても、無効のままであること
    （全列版のupsertは is_active の指定漏れで勝手に有効化してしまう）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    ids = _build_chain_job(db)
    db.upsert_job_task_override(ids["job"], ids["t2"], is_active=False,
                                milestone_id=ids["ms_early"])
    db.upsert_job_task_override(ids["job"], ids["t1"], milestone_id=ids["ms_late"])

    db.enforce_milestone_floor(ids["job"], ids["t2"])
    assert bool(_override(db, ids["job"], ids["t2"])["is_active"]) is False
    db.close()
