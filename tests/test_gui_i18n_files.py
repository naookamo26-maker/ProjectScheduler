"""
多言語対応（docs/roadmap.md §11）: どの表示言語で作ったプロジェクトファイルも、
別の表示言語で開いて同じように使えることを確かめる。

ファイルには表示用の文字列ではなく値（状態は in_progress／done、依存の種別は
FS／SS 等）を保存しているので、表示言語に依存しないはず。ここでは各言語で
画面から既定の名前でジョブを足し、その言語の名前でデータを作り、計画を確定して
保存したファイルを、他のすべての言語で開き直して、確定の状態・計算・編集・Undo・
保存・HTML出力が通ることを確かめる。
"""

import contextlib
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6")
pytest.importorskip("pandas")

import i18n  # noqa: E402
from gui.db import ProjectDatabase  # noqa: E402

pytestmark = pytest.mark.gui

LANGUAGES = [code for code, _label in i18n.LANGUAGES]

# 各言語で作るデータの名前（利用者が入力したデータとして、その言語の文字で作る）
NAMES = {
    "ja": ("設計", "実装", "チームA", "リリース", "開発"),
    "en": ("Design", "Build", "Team A", "Release", "Development"),
    "vi": ("Thiết kế", "Triển khai", "Nhóm Ă", "Phát hành", "Phát triển"),
    "zh_CN": ("设计", "实现", "团队甲", "发布", "开发"),
}


@pytest.fixture(autouse=True)
def restore_language():
    yield
    i18n.set_language("ja")


def _schedule(db):
    from gui.gantt_generator import build_frames, build_plan, compute_schedule_with_plan
    from gui.gantt_generator import compute_schedule_from_frames
    from gui.plan_confirmation import PlanState

    state = PlanState(db)
    plan = build_plan(db, state)
    ratio = db.get_project()["distribution_ratio"]
    with contextlib.redirect_stdout(io.StringIO()):
        if plan is None:
            return compute_schedule_from_frames(build_frames(db), verbose=False, distribution_ratio=ratio), state
        return compute_schedule_with_plan(build_frames(db), plan, verbose=False, distribution_ratio=ratio)[0], state


def _create_in(language, path, qapp):
    """その表示言語で、画面の操作（ジョブの追加）とその言語の名前でデータを作り、
    確定して保存する。"""
    from gui.plan_actions import confirm_all
    from gui.tab_jobs import JobsTab

    i18n.set_language(language)
    design, build, team_name, ms_name, wf_name = NAMES[language]
    db = ProjectDatabase.create_new(str(path))
    db.set_project(f"{wf_name} 2026", "2026-04-06")
    team = db.add_team(team_name, 2)
    wf = db.add_workflow(wf_name)
    t1 = db.add_workflow_task(wf, design, team, 3)
    t2 = db.add_workflow_task(wf, build, team, 4)
    db.add_task_dependency(wf, t1, t2)
    ms = db.add_milestone(ms_name, "2026-12-25")
    jobs_tab = JobsTab(db)
    jobs_tab._add_job()  # 既定の名前（表示言語の「新しいジョブ」）で足す
    jobs_tab._add_job()
    qapp.processEvents()
    for job in db.list_jobs():
        db.update_job(job["id"], job["name"], wf, ms, 1, f"{team_name}, {ms_name}")
    first = db.list_jobs()[0]["id"]
    db.update_job_task_override_fields(first, t1, status="done", tags=design)
    df, _state = _schedule(db)
    confirm_all(db, df)
    db.save()
    jobs_tab.deleteLater()
    db.close()


@pytest.mark.parametrize("created_in", LANGUAGES)
def test_files_created_in_one_language_work_in_every_language(qapp_i18n, tmp_path, created_in):
    from gui.gantt_generator import generate_gantt
    from gui.plan_confirmation import CONFIRMED, DRAFT

    path = tmp_path / f"{created_in}.pschedule"
    _create_in(created_in, path, qapp_i18n)
    design, build, team_name, ms_name, wf_name = NAMES[created_in]

    for opened_in in LANGUAGES:
        i18n.set_language(opened_in)
        db = ProjectDatabase.open_existing(str(path))
        try:
            # 利用者のデータ（その言語の文字）はそのまま戻る
            assert [t["name"] for t in db.list_teams()] == [team_name]
            jobs = db.list_jobs()
            assert len(jobs) == 2
            assert all(j["tags"] == f"{team_name}, {ms_name}" for j in jobs)
            # 確定した状態のまま開け、計算も確定と一致する
            df, state = _schedule(db)
            assert state.status == CONFIRMED
            assert len(df) == 4
            # 編集すると変更案になり、Undo 相当の破棄で戻る
            wf_task = db.list_job_tasks_with_overrides(jobs[1]["id"])[1]
            db.update_job_task_override_fields(jobs[1]["id"], wf_task["workflow_task_id"], override_days=6)
            assert _schedule(db)[1].status == DRAFT
            db.discard_draft()
            assert _schedule(db)[1].status == CONFIRMED
            # 状態（進捗）は値で保存されているので、どの言語で開いても「完了」
            done = [r for r in db.list_all_job_task_overrides() if r["status"] == "done"]
            assert len(done) == 1
            # HTML はその表示言語で出力できる
            html = tmp_path / f"{created_in}_{opened_in}.html"
            with contextlib.redirect_stdout(io.StringIO()):
                generate_gantt(db, plotly_output_path=str(html), verbose=False)
            assert team_name in html.read_text(encoding="utf-8")
            # 別の言語で開いたまま保存しても壊れない
            db.save()
        finally:
            db.close()


@pytest.fixture
def qapp_i18n():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication(sys.argv)


@pytest.mark.parametrize("language, expected", [
    ("ja", ["保存(&S)", "保存しない", "キャンセル", "はい(&Y)", "いいえ(&N)"]),
    ("en", ["&Save", "Don't save", "Cancel", "&Yes", "&No"]),
    ("vi", ["Lưu(&S)", "Không lưu", "Hủy", "Có(&Y)", "Không(&N)"]),
    ("zh_CN", ["保存(&S)", "不保存", "取消", "是(&Y)", "否(&N)"]),
])
def test_message_box_standard_buttons_follow_the_display_language(qapp_i18n, language, expected):
    """保存の確認（QMessageBox.question の 保存／破棄／キャンセル）・はい／いいえの
    ボタンが、表示言語の文言になる（Qt 同梱の翻訳の有無によらない）。"""
    from PySide6.QtWidgets import QMessageBox

    from gui.main import StandardButtonTranslator

    translator = StandardButtonTranslator()
    qapp_i18n.installTranslator(translator)
    try:
        i18n.set_language(language)
        box = QMessageBox(QMessageBox.Question, "t", "m",
                          QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        texts = [box.button(b).text() for b in (QMessageBox.Save, QMessageBox.Discard, QMessageBox.Cancel)]
        box = QMessageBox(QMessageBox.Question, "t", "m", QMessageBox.Yes | QMessageBox.No)
        texts += [box.button(b).text() for b in (QMessageBox.Yes, QMessageBox.No)]
        assert texts == expected
    finally:
        qapp_i18n.removeTranslator(translator)
