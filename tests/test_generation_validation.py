"""
ガントチャート生成前の検査（gui/gantt_generator.py の validate_for_generation）のうち、
扱える範囲を超えた日付の回帰テスト。

pandas の日付は 2262-04-11 までしか表せず、それより後の締切日などがあると計算が
想定外のエラー（OverflowError 等）で止まっていた。GUIの日付欄には上限
（gui/db.py の MAX_SUPPORTED_DATE）を設けたが、上限を設ける前に保存したファイルには
残りうるため、生成前にどこの日付かが分かる文言で止める。
"""

import contextlib
import io
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("pandas")

from gui.db import MAX_SUPPORTED_DATE, ProjectDatabase  # noqa: E402
from gui.gantt_generator import build_frames, compute_schedule_from_frames, validate_for_generation  # noqa: E402

# 分類: scheduler（Qt非依存だが pandas/numpy が必要）
pytestmark = pytest.mark.scheduler

_TOO_LATE = "2500-12-31"


@pytest.fixture
def project():
    db = ProjectDatabase.create_new()
    db.set_project("P", "2026-04-06")
    team = db.add_team("チームA", 1)
    ms = db.add_milestone("リリース", "2026-12-25")
    wf = db.add_workflow("WF")
    task = db.add_workflow_task(wf, "設計", team, 3)
    job = db.add_job("ジョブ1", wf, ms)
    ids = {"team": team, "ms": ms, "wf": wf, "task": task, "job": job}
    yield db, ids
    db.close()


def test_no_error_when_all_dates_are_within_range(project):
    db, _ids = project
    assert validate_for_generation(db) == []


@pytest.mark.parametrize("where, set_date, expected", [
    ("milestone", lambda db, ids: db.update_milestone(ids["ms"], "リリース", _TOO_LATE),
     "マイルストーン「リリース」の締切日"),
    ("start", lambda db, ids: db.set_project("P", _TOO_LATE), "開発開始日"),
    ("holiday", lambda db, ids: db.add_holiday(_TOO_LATE), f"休業日（{_TOO_LATE}）"),
    ("capacity", lambda db, ids: db.add_team_capacity_change(ids["team"], _TOO_LATE, 2),
     f"チーム「チームA」の同時ライン数の変動点（{_TOO_LATE}）"),
    ("pin", lambda db, ids: db.upsert_job_task_override(ids["job"], ids["task"], start_pin_date=_TOO_LATE),
     f"ジョブ「ジョブ1」のタスクの開始固定日（{_TOO_LATE}）"),
])
def test_dates_after_the_supported_limit_are_reported_before_scheduling(project, where, set_date, expected):
    db, ids = project
    set_date(db, ids)
    errors = validate_for_generation(db)
    assert any(expected in e and MAX_SUPPORTED_DATE in e for e in errors), errors


def test_a_deadline_at_the_supported_limit_can_still_be_scheduled(project):
    """上限ちょうどの日付は、実際にスケジューリングできる（上限は pandas の範囲・
    祝日の計算式が成り立つ範囲の内側にある）。"""
    db, ids = project
    db.update_milestone(ids["ms"], "リリース", MAX_SUPPORTED_DATE)
    assert validate_for_generation(db) == []
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = compute_schedule_from_frames(build_frames(db), verbose=False, distribution_ratio=0.7)
    finally:
        logging.disable(logging.NOTSET)
    assert len(result) == 1
