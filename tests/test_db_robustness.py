"""
DB層（gui/db.py）・Undo管理（gui/undo_manager.py）の、利用者の操作でデータが壊れたり
アプリが固まったりしないことの回帰テスト。Qt非依存。

- 失敗した操作・同期だけで終わった操作がトランザクションを開いたまま残さない
  （残ると保存の backup() が終わらず、アプリが固まっていた）
- 名前の検証（空欄・空白だけを拒否し、前後の空白を落とす）
- ジョブのワークフローを変えたとき、旧ワークフローのタスクを指す個別の依存を残さない
- 履歴が上限で削られた後に、保存時点へ戻ったと誤判定しない
"""

import faulthandler
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.db import (  # noqa: E402
    DuplicateNameError,
    InvalidNameError,
    ProjectDatabase,
    ProjectDatabaseError,
)
from gui.undo_manager import UndoManager  # noqa: E402

# 分類: core（Qt非依存・pandas非依存。pytestだけで動く）
pytestmark = pytest.mark.core


def _attach_undo(db):
    manager = UndoManager(
        db=db,
        capture_ui_state=lambda: None,
        restore_ui_state=lambda _state: None,
        apply_db_state=db.restore_state,
    )
    db.undo_manager = manager
    return manager


@contextmanager
def _fail_instead_of_hanging(seconds=60):
    """保存が固まる不具合が戻った場合に、テスト全体が止まり続けないよう、
    一定時間でスタックを出して終了させる。"""
    faulthandler.dump_traceback_later(seconds, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


def _project(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    _attach_undo(db)
    team = db.add_team("チームA", 1)
    wf_a = db.add_workflow("WF_A")
    wf_b = db.add_workflow("WF_B")
    ids = {
        "team": team, "wf_a": wf_a, "wf_b": wf_b,
        "a1": db.add_workflow_task(wf_a, "A1", team, 1),
        "a2": db.add_workflow_task(wf_a, "A2", team, 1),
        "b1": db.add_workflow_task(wf_b, "B1", team, 1),
    }
    ids["job1"] = db.add_job("ジョブ1", wf_a, None)
    ids["job2"] = db.add_job("ジョブ2", wf_a, None)
    return db, ids


# -- トランザクションを開いたまま残さない -------------------------------------------------


def test_save_right_after_a_duplicate_name_error_does_not_hang(tmp_path):
    """回帰テスト: 名前の重複で失敗した直後に保存すると、アプリが固まっていた。

    sqlite3 は UPDATE の前に暗黙にトランザクションを始めるため、文が一意制約違反で
    失敗してもトランザクションだけが開いたまま残る。その状態で backup() を呼ぶと
    SQLITE_LOCKED が返り続け、Python が無限に再試行していた。"""
    db, ids = _project(tmp_path)
    db.add_team("チームB", 1)
    with pytest.raises(DuplicateNameError):
        db.update_team(ids["team"], "チームB", 1)
    assert not db._conn.in_transaction
    with _fail_instead_of_hanging():
        db.save()
    reopened = ProjectDatabase.open_existing(str(tmp_path / "p.pschedule"))
    assert sorted(t["name"] for t in reopened.list_teams()) == ["チームA", "チームB"]
    reopened.close()
    db.close()


def test_link_and_template_changes_are_committed_even_when_nothing_is_expanded(tmp_path):
    """回帰テスト: 依存先ジョブの追加（当てはまるテンプレートが無い）・依存テンプレートの
    追加・変更・削除（当てはまるリンクが無い）は、展開する対応が無いと同期処理が
    コミットせずに戻るため、自分の変更までコミットされずトランザクションが開いたまま
    残っていた（直後の保存が固まる。revision も進まず、再計算・未保存マークが漏れる）。"""
    db, ids = _project(tmp_path)
    steps = [
        lambda: db.add_job_dependency_link(ids["job1"], ids["job2"]),
        lambda: ids.__setitem__("tpl", db.add_dependency_template(ids["wf_b"], ids["b1"], ids["wf_a"], ids["a1"])),
        lambda: db.update_dependency_template(ids["tpl"], ids["b1"], ids["wf_a"], ids["a2"]),
        lambda: db.delete_dependency_template(ids["tpl"]),
    ]
    for step in steps:
        revision = db.revision
        step()
        assert not db._conn.in_transaction
        assert db.revision != revision
    with _fail_instead_of_hanging():
        db.save()
    db.close()


def test_failed_operation_leaves_neither_changes_nor_an_undo_entry(tmp_path):
    """失敗した操作の書きかけは巻き戻し、Undoにも積まない。同じUndo単位の中で
    先に成功した操作（コミット済み）はそのまま残る。"""
    db, ids = _project(tmp_path)
    manager = db.undo_manager
    db.add_job_dependency_link(ids["job1"], ids["job2"])
    steps = len(manager._undo_stack)
    before = db.serialize_state()
    with pytest.raises(ProjectDatabaseError):
        db.add_job_dependency_link(ids["job1"], ids["job2"])  # 既に登録済み
    assert not db._conn.in_transaction
    assert db.serialize_state() == before
    assert len(manager._undo_stack) == steps

    job3 = db.add_job("ジョブ3", ids["wf_a"], None)
    errors = []
    with db.undo_group("依存先ジョブを追加"):
        for target in (job3, ids["job2"]):
            try:
                db.add_job_dependency_link(ids["job1"], target)
            except ProjectDatabaseError as e:
                errors.append(str(e))
    assert len(errors) == 1
    assert not db._conn.in_transaction
    assert {link["depends_on_job_id"] for link in db.list_job_dependency_links(ids["job1"])} == {job3, ids["job2"]}
    db.close()


def test_save_commits_a_pending_transaction_instead_of_hanging(tmp_path):
    """最後の砦: 変更系メソッドの外で書きかけのまま残った変更があっても、保存は
    固まらず、画面に見えている（メモリ上の）内容をそのまま保存する。"""
    db, _ids = _project(tmp_path)
    db._conn.execute("UPDATE project SET project_name = '書きかけ' WHERE id = 1")
    assert db._conn.in_transaction
    with _fail_instead_of_hanging():
        db.save()
    reopened = ProjectDatabase.open_existing(str(tmp_path / "p.pschedule"))
    assert reopened.get_project()["project_name"] == "書きかけ"
    reopened.close()
    db.close()


def test_end_undo_group_after_the_undo_manager_is_detached_just_closes(tmp_path):
    """別のプロジェクトへ切り替える途中（旧DBからUndo管理を外した後）に、旧タブの
    入力欄からフォーカスが外れて単位を閉じようとしても、例外にならない。"""
    db, _ids = _project(tmp_path)
    db.begin_undo_group("開発開始日を変更")
    db.set_project("P", "2026-01-05")
    db.undo_manager = None
    assert db.end_undo_group() is False
    assert db._open_group is None
    db.close()


# -- 名前の検証 --------------------------------------------------------------------------


def test_blank_names_are_rejected_for_every_kind_of_entity(tmp_path):
    """回帰テスト: マイルストーン・チーム・ジョブの名前は空欄でも登録できていた
    （選択肢や絞り込みに何も表示されない項目ができる）。"""
    db, ids = _project(tmp_path)
    ms = db.add_milestone("M1", "2026-12-25")
    attempts = [
        lambda n: db.add_milestone(n, "2026-12-25"),
        lambda n: db.update_milestone(ms, n, "2026-12-25"),
        lambda n: db.add_team(n, 1),
        lambda n: db.update_team(ids["team"], n, 1),
        lambda n: db.add_workflow(n),
        lambda n: db.rename_workflow(ids["wf_a"], n),
        lambda n: db.add_workflow_task(ids["wf_a"], n, ids["team"], 1),
        lambda n: db.update_workflow_task(ids["a1"], n, ids["team"], 1),
        lambda n: db.add_job(n, ids["wf_a"], None),
        lambda n: db.update_job(ids["job1"], n, ids["wf_a"], None, None, ""),
    ]
    before = db.serialize_state()
    for attempt in attempts:
        for blank in ("", "   ", None):
            with pytest.raises(InvalidNameError):
                attempt(blank)
            assert not db._conn.in_transaction
    assert db.serialize_state() == before
    db.close()


def test_names_are_stored_without_surrounding_spaces(tmp_path):
    """前後の空白だけが違う名前（"A" と "A "）は見分けられないので、落として保存する
    （既存の名前と空白だけが違う名前は重複として扱う）。"""
    db, ids = _project(tmp_path)
    ms = db.add_milestone("  締切  ", "2026-12-25")
    assert db.get_milestone(ms)["name"] == "締切"
    db.update_team(ids["team"], " チームA2 ", 1)
    assert [t["name"] for t in db.list_teams()] == ["チームA2"]
    with pytest.raises(DuplicateNameError):
        db.add_team("チームA2 ", 1)
    db.update_job(ids["job1"], " ジョブX ", ids["wf_a"], None, None, "")
    assert "ジョブX" in {j["name"] for j in db.list_jobs()}
    db.close()


def test_duplicate_name_error_is_an_invalid_name_error():
    """GUIは名前の欄で InvalidNameError をまとめて捕まえる（重複も空欄も同じ扱い）。"""
    assert issubclass(DuplicateNameError, InvalidNameError)
    assert issubclass(InvalidNameError, ProjectDatabaseError)


# -- ジョブのワークフローの変更 ---------------------------------------------------------------


def test_changing_job_workflow_drops_manual_task_pairs_that_point_at_old_tasks(tmp_path):
    """回帰テスト: ジョブのワークフローを変えても、手動で追加した個別のタスク依存が
    旧ワークフローのタスクを指したまま残っていた（スケジューラは黙って無視するため、
    画面に出ているのに効いていない依存になる）。このジョブ側・このジョブに依存する
    他ジョブ側の両方を消し、関係の無い依存は残す。"""
    db, ids = _project(tmp_path)
    job3 = db.add_job("ジョブ3", ids["wf_b"], None)
    own = db.add_external_dependency(ids["job1"], ids["a2"], job3, ids["b1"])
    incoming = db.add_external_dependency(ids["job2"], ids["a1"], ids["job1"], ids["a2"])
    unrelated = db.add_external_dependency(ids["job2"], ids["a2"], job3, ids["b1"])

    db.update_job(ids["job1"], "ジョブ1", ids["wf_b"], None, None, "")

    remaining = {d["id"] for d in db.list_external_dependencies()}
    assert own not in remaining
    assert incoming not in remaining
    assert unrelated in remaining
    db.close()


def test_changing_job_workflow_keeps_manual_task_pairs_that_still_apply(tmp_path):
    """ワークフローを変えない更新（名前・優先度等）では、個別のタスク依存に触れない。"""
    db, ids = _project(tmp_path)
    dep = db.add_external_dependency(ids["job1"], ids["a2"], ids["job2"], ids["a1"])
    db.update_job(ids["job1"], "ジョブ1改", ids["wf_a"], None, 3, "")
    assert dep in {d["id"] for d in db.list_external_dependencies()}
    db.close()


# -- 保存済みの判定（gui/undo_manager.py） ---------------------------------------------------


def test_undoing_everything_after_history_trim_is_not_mistaken_for_saved(tmp_path):
    """回帰テスト: 履歴が空の時点で保存（または開いた直後）→ 上限を超えて操作して最古の
    エントリが捨てられた後に、すべてUndoすると、内容は保存時と違う（捨てたエントリの
    変更が残っている）のに「保存済み」と判定され、閉じても確認が出なかった。"""
    from gui import undo_manager as um

    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager = _attach_undo(db)
    db.save()
    assert not db.is_dirty()
    for i in range(um._MAX_STACK_SIZE + 1):
        db.add_team(f"T{i}", 1)
    while manager.can_undo():
        manager.undo()
    assert db.list_teams() != []  # 最古の操作は戻せないまま残っている
    assert db.is_dirty()
    db.close()


def test_undoing_back_to_the_saved_state_is_still_clean_without_trim(tmp_path):
    """上限に達していなければ、従来どおり保存時点まで戻せば「保存済み」になる。"""
    db = ProjectDatabase.create_new(str(tmp_path / "p.pschedule"))
    manager = _attach_undo(db)
    db.save()
    db.add_team("T", 1)
    assert db.is_dirty()
    manager.undo()
    assert not db.is_dirty()
    db.close()


# -- 変更案の破棄（discard_draft） -----------------------------------------------------------


def _confirmed_row(job_id, task_id, team_id, signature="sig"):
    return {
        "job_id": job_id, "workflow_task_id": task_id, "start_date": "2026-04-06",
        "end_date": "2026-04-08", "days": 2, "team_id": team_id, "input_signature": signature,
    }


def test_discarding_after_partially_committing_a_task_that_uses_new_milestone_and_team(tmp_path):
    """回帰テスト: 確定した後に追加したマイルストーン・チームをタスクに使い、そのタスクだけ
    「選択した変更を確定」してから「変更を破棄」すると、最後に確定した時点（そのマイルス
    トーン・チームがまだ無い）へ戻した上に上書き・確定行を入れ直そうとして外部キー制約で
    失敗し、破棄が途中で止まっていた。確定したタスクが使うマイルストーン・チームは
    スナップショットにも写し、確定した入力のまま残す（「未設定」に戻すと、チームを変えて
    確定したタスクが破棄の直後から「変更あり」になるため）。使っていないものは破棄する。"""
    db, ids = _project(tmp_path)
    db.add_milestone("M1", "2026-12-25")
    db.replace_confirmed_schedule(
        [_confirmed_row(ids["job1"], t, ids["team"]) for t in (ids["a1"], ids["a2"])],
        "2026-04-01T09:00:00", "global",
    )
    new_ms = db.add_milestone("確定後の締切", "2027-03-31")
    new_team = db.add_team("確定後のチーム", 1)
    db.add_milestone("確定後の使わない締切", "2027-06-30")
    db.upsert_job_task_override(ids["job1"], ids["a1"], milestone_id=new_ms, team_id=new_team,
                                override_days=3, status="in_progress")
    db.update_confirmed_rows([_confirmed_row(ids["job1"], ids["a1"], new_team, "sig2")], [],
                             "2026-04-02T09:00:00")

    db.discard_draft(restore_statuses=True)

    assert not db._conn.in_transaction
    assert [m["name"] for m in db.list_milestones()] == ["M1", "確定後の締切"]
    assert "確定後のチーム" in [t["name"] for t in db.list_teams()]
    override = next(r for r in db.list_all_job_task_overrides()
                    if (r["job_id"], r["workflow_task_id"]) == (ids["job1"], ids["a1"]))
    assert override["override_milestone_id"] == new_ms
    assert override["override_team_id"] == new_team
    assert override["override_days"] == 3        # 一部確定した入力は残る
    assert override["status"] == "in_progress"   # 一部確定した時点の状態は残る
    confirmed = {(r["job_id"], r["workflow_task_id"]): r for r in db.list_confirmed_schedule()}
    assert confirmed[(ids["job1"], ids["a1"])]["input_signature"] == "sig2"
    assert confirmed[(ids["job1"], ids["a1"])]["team_id"] == new_team
    db.close()


def test_a_discard_that_fails_midway_leaves_the_project_as_it_was(tmp_path):
    """破棄は「最後に確定した時点」のスナップショットを読み込んでから、スナップショット
    自身を入れ直す。その途中で失敗しても、半端に戻った状態を残さず、破棄する前の状態に
    戻す（スナップショットの読み込みはトランザクションの巻き戻しでは戻らない）。"""
    from unittest.mock import patch

    db, ids = _project(tmp_path)
    db.replace_confirmed_schedule([_confirmed_row(ids["job1"], ids["a1"], ids["team"])],
                                  "2026-04-01T09:00:00", "global")
    db.add_team("確定後のチーム", 1)
    before = db.serialize_state()
    with patch.object(ProjectDatabase, "_restore_kept_changes_after_discard",
                      side_effect=RuntimeError("途中で失敗")):
        with pytest.raises(RuntimeError):
            db.discard_draft()
    assert db.serialize_state() == before
    assert not db._conn.in_transaction
    db.close()
