"""
GUI(gui/db.py, gui/gantt_generator.py) と project_scheduler.py の連携を
end-to-endで検証するスモークテスト。PySide6のQGraphicsシーン等、Qtの
イベントループが必要なウィジェット層は対象外（gui/db.py・gui/gantt_generator.py
はいずれもQt非依存であり、Qtを一切importせずに検証できる）。ウィジェット層の
動作確認は開発時にheadless Qt（QT_QPA_PLATFORM=offscreen）で個別に実施済み。

実行方法:
    pip install pytest
    pytest tests/test_gui_gantt_smoke.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.db import (  # noqa: E402
    DuplicateNameError,
    ProjectDatabase,
    ProjectDatabaseError,
    ReferencedEntityError,
)
from gui.gantt_generator import build_frames, generate_gantt, validate_for_generation  # noqa: E402
from project_scheduler import ResourceOverflowError, SchedulingError  # noqa: E402

SAMPLE_DB = Path(__file__).resolve().parent.parent / "data" / "Project_Schedule_Sample_GameDev_v22.pschedule"


# -- ProjectDatabase の基本CRUD・永続化 --------------------------------------------

def test_create_and_reopen_preserves_data(tmp_path):
    path = tmp_path / "project.pschedule"
    db = ProjectDatabase.create_new(str(path))
    db.set_project("テストプロジェクト", "2026-01-01")
    team_id = db.add_team("チームA", 2)
    db.save()  # 明示的に保存するまでファイルには書き込まれない
    db.close()

    db2 = ProjectDatabase.open_existing(str(path))
    assert db2.get_project() == {"project_name": "テストプロジェクト", "start_date": "2026-01-01"}
    assert db2.list_teams() == [{"id": team_id, "name": "チームA", "max_lines": 2}]
    db2.close()


def test_changes_are_not_written_until_save(tmp_path):
    """明示的にsave()するまで、ディスク上のファイルには変更が反映されないこと。"""
    path = tmp_path / "project.pschedule"
    db = ProjectDatabase.create_new(str(path))
    assert db.is_dirty()
    db.save()
    assert not db.is_dirty()
    assert path.exists()

    db.add_team("チームA", 1)
    assert db.is_dirty()

    db2 = ProjectDatabase.open_existing(str(path))
    assert db2.list_teams() == []  # まだ保存していないので反映されない
    db2.close()

    db.save()
    assert not db.is_dirty()
    db3 = ProjectDatabase.open_existing(str(path))
    assert len(db3.list_teams()) == 1
    db3.close()
    db.close()


def test_save_as_writes_to_new_path_and_updates_path(tmp_path):
    original = tmp_path / "original.pschedule"
    renamed = tmp_path / "renamed.pschedule"
    db = ProjectDatabase.create_new(str(original))
    db.add_team("チームA", 1)
    db.save_as(str(renamed))

    assert db.path == str(renamed)
    assert renamed.exists()
    assert not original.exists()
    assert not db.is_dirty()
    db.close()

    reopened = ProjectDatabase.open_existing(str(renamed))
    assert len(reopened.list_teams()) == 1
    reopened.close()


# -- workflows: 並び替え・旧スキーマからの自動マイグレーション -----------------------

def test_workflows_default_order_is_creation_order(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    wf3 = db.add_workflow("WF3")
    assert [w["id"] for w in db.list_workflows()] == [wf1, wf2, wf3]
    db.close()


def test_reorder_workflows_persists_across_reopen(tmp_path):
    path = tmp_path / "project.pschedule"
    db = ProjectDatabase.create_new(str(path))
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    wf3 = db.add_workflow("WF3")

    db.reorder_workflows([wf3, wf1, wf2])
    assert [w["id"] for w in db.list_workflows()] == [wf3, wf1, wf2]

    db.save()
    db.close()

    reopened = ProjectDatabase.open_existing(str(path))
    assert [w["id"] for w in reopened.list_workflows()] == [wf3, wf1, wf2]
    reopened.close()


def test_opening_pre_sort_order_schema_migrates_and_keeps_name_order(tmp_path):
    """sort_order列が存在しない旧バージョンの.pscheduleファイルを模して、
    open_existing()が自動的にマイグレーションし、既存の並び順（名前順）を
    保ったままsort_orderを振ることを確認する。"""
    import sqlite3

    path = tmp_path / "legacy.pschedule"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE project (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            project_name TEXT NOT NULL DEFAULT '',
            start_date TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE workflows (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
        """
    )
    conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1')")
    conn.execute("INSERT INTO project(id, project_name, start_date) VALUES (1, '', '')")
    conn.execute("INSERT INTO workflows(name) VALUES ('Zワークフロー')")
    conn.execute("INSERT INTO workflows(name) VALUES ('Aワークフロー')")
    conn.commit()
    conn.close()

    db = ProjectDatabase.open_existing(str(path))
    names = [w["name"] for w in db.list_workflows()]
    assert names == ["Aワークフロー", "Zワークフロー"]  # 旧仕様の名前順を踏襲

    new_id = db.add_workflow("新規ワークフロー")
    assert [w["name"] for w in db.list_workflows()][-1] == "新規ワークフロー"
    db.close()


def test_opening_pre_is_active_schema_migrates_and_defaults_to_active(tmp_path):
    """job_external_dependencies.is_active列が無い旧バージョン(v2)の
    .pscheduleファイルを開いた際、自動的に列が追加され、既存行はすべて
    有効（is_active=1）として扱われることを確認する。"""
    import sqlite3

    from gui.db import _SCHEMA_SQL

    path = tmp_path / "legacy_v2.pschedule"
    conn = sqlite3.connect(str(path))
    # 現行スキーマから is_active 列だけを取り除いた v2 相当のテーブルを再現する。
    legacy_sql = _SCHEMA_SQL.replace("    is_active INTEGER NOT NULL DEFAULT 1,\n", "")
    conn.executescript(legacy_sql)
    conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')")
    conn.execute("INSERT INTO project(id, project_name, start_date) VALUES (1, '', '')")
    team_id = conn.execute("INSERT INTO teams(name, max_lines) VALUES ('チームA', 1)").lastrowid
    wf1 = conn.execute("INSERT INTO workflows(name, sort_order) VALUES ('WF1', 0)").lastrowid
    wf2 = conn.execute("INSERT INTO workflows(name, sort_order) VALUES ('WF2', 1)").lastrowid
    t1 = conn.execute(
        "INSERT INTO workflow_tasks(workflow_id, name, team_id, default_days) VALUES (?, 'A', ?, 1)",
        (wf1, team_id),
    ).lastrowid
    t2 = conn.execute(
        "INSERT INTO workflow_tasks(workflow_id, name, team_id, default_days) VALUES (?, 'B', ?, 1)",
        (wf2, team_id),
    ).lastrowid
    j1 = conn.execute(
        "INSERT INTO jobs(name, workflow_id, priority) VALUES ('J1', ?, 100)", (wf1,)
    ).lastrowid
    j2 = conn.execute(
        "INSERT INTO jobs(name, workflow_id, priority) VALUES ('J2', ?, 100)", (wf2,)
    ).lastrowid
    conn.execute(
        "INSERT INTO job_external_dependencies"
        "(job_id, workflow_task_id, depends_on_job_id, depends_on_workflow_task_id) "
        "VALUES (?, ?, ?, ?)",
        (j1, t1, j2, t2),
    )
    conn.commit()
    conn.close()

    db = ProjectDatabase.open_existing(str(path))
    rows = db.list_external_dependencies(job_id=j1)
    assert len(rows) == 1
    assert rows[0]["is_active"] == 1
    db.close()


def test_opening_legacy_file_backfills_missing_dependency_links(tmp_path):
    """旧バージョンでは「依存先ジョブ」のリンクを作らずに個別のタスク依存だけを
    追加できたため、そのような孤立したjob_external_dependencies行がある
    .pscheduleファイルを開くと、対応するjob_dependency_links行が自動的に
    補完されることを確認する（新UIはリンク経由でしかタスク対応を表示しない
    ため、補完しないと既存データが見えなくなってしまう）。"""
    import sqlite3

    from gui.db import _SCHEMA_SQL

    path = tmp_path / "legacy_v4.pschedule"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA_SQL)
    conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '4')")
    conn.execute("INSERT INTO project(id, project_name, start_date) VALUES (1, '', '')")
    team_id = conn.execute("INSERT INTO teams(name, max_lines) VALUES ('チームA', 1)").lastrowid
    wf1 = conn.execute("INSERT INTO workflows(name, sort_order) VALUES ('WF1', 0)").lastrowid
    wf2 = conn.execute("INSERT INTO workflows(name, sort_order) VALUES ('WF2', 1)").lastrowid
    t1 = conn.execute(
        "INSERT INTO workflow_tasks(workflow_id, name, team_id, default_days) VALUES (?, 'A', ?, 1)",
        (wf1, team_id),
    ).lastrowid
    t2 = conn.execute(
        "INSERT INTO workflow_tasks(workflow_id, name, team_id, default_days) VALUES (?, 'B', ?, 1)",
        (wf2, team_id),
    ).lastrowid
    j1 = conn.execute(
        "INSERT INTO jobs(name, workflow_id, priority) VALUES ('J1', ?, 100)", (wf1,)
    ).lastrowid
    j2 = conn.execute(
        "INSERT INTO jobs(name, workflow_id, priority) VALUES ('J2', ?, 100)", (wf2,)
    ).lastrowid
    # job_dependency_links を経由しない「孤立した」個別のタスク依存（旧UIで作成可能だった）
    conn.execute(
        "INSERT INTO job_external_dependencies"
        "(job_id, workflow_task_id, depends_on_job_id, depends_on_workflow_task_id) "
        "VALUES (?, ?, ?, ?)",
        (j1, t1, j2, t2),
    )
    conn.commit()
    conn.close()

    assert sqlite3.connect(str(path)).execute(
        "SELECT COUNT(*) FROM job_dependency_links"
    ).fetchone()[0] == 0  # 補完前は0件であることの前提確認

    db = ProjectDatabase.open_existing(str(path))
    links = db.list_job_dependency_links(j1)
    assert len(links) == 1
    assert links[0]["depends_on_job_id"] == j2
    db.close()


def test_duplicate_names_are_rejected(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    db.add_team("チームA", 1)
    with pytest.raises(DuplicateNameError):
        db.add_team("チームA", 2)
    db.close()


def test_referenced_team_cannot_be_deleted(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    wf_id = db.add_workflow("WF1")
    db.add_workflow_task(wf_id, "タスク1", team_id, 3)
    with pytest.raises(ReferencedEntityError):
        db.delete_team(team_id)
    db.close()


# -- team_capacity_changes: チームの同時ライン数の期間変動 --------------------------

def test_team_capacity_changes_crud_and_ordering(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    assert db.list_team_capacity_changes(team_id) == []

    c2 = db.add_team_capacity_change(team_id, "2026-06-01", 4)
    c1 = db.add_team_capacity_change(team_id, "2026-03-01", 2)  # 後から早い日付を追加

    rows = db.list_team_capacity_changes(team_id)
    assert [r["id"] for r in rows] == [c1, c2]  # 開始日昇順で返る
    assert [r["lines"] for r in rows] == [2, 4]

    with pytest.raises(DuplicateNameError):
        db.add_team_capacity_change(team_id, "2026-03-01", 5)  # 同じ開始日は重複

    db.update_team_capacity_change(c1, "2026-03-01", 3)
    assert db.list_team_capacity_changes(team_id)[0]["lines"] == 3

    db.delete_team_capacity_change(c1)
    remaining = db.list_team_capacity_changes(team_id)
    assert [r["id"] for r in remaining] == [c2]
    db.close()


def test_deleting_team_cascades_capacity_changes(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    db.add_team_capacity_change(team_id, "2026-03-01", 2)
    db.delete_team(team_id)  # 参照なしなので削除でき、変更点もCASCADEで消える
    assert db.list_teams() == []
    db.close()


def test_job_task_override_is_diff_only(tmp_path):
    """既定値のままなら上書き行を作らず、既定に戻すと上書き行が消えることを確認する。"""
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    wf_id = db.add_workflow("WF1")
    task_id = db.add_workflow_task(wf_id, "タスク1", team_id, 3)
    job_id = db.add_job("ジョブ1", wf_id, None, 100)

    rows = db.list_job_tasks_with_overrides(job_id)
    assert len(rows) == 1
    assert rows[0]["override_id"] is None  # 既定のまま＝上書き行なし

    db.upsert_job_task_override(job_id, task_id, is_active=False)
    rows = db.list_job_tasks_with_overrides(job_id)
    assert rows[0]["override_id"] is not None
    assert rows[0]["is_active"] == 0

    db.clear_job_task_override(job_id, task_id)
    rows = db.list_job_tasks_with_overrides(job_id)
    assert rows[0]["override_id"] is None
    db.close()


def test_job_task_overrides_are_ordered_upstream_first(tmp_path):
    """タスクの登録順に関わらず、依存関係上の先行タスクが上に来ること。
    あえて依存とは逆順（後続タスクを先）に登録し、登録順のままでは並びが
    崩れることを確認したうえでチェックする。"""
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    wf_id = db.add_workflow("WF1")
    # 登録順: タスク3, タスク1, タスク2（IDもこの順）だが、依存はタスク1->タスク2->タスク3
    t3 = db.add_workflow_task(wf_id, "タスク3", team_id, 1)
    t1 = db.add_workflow_task(wf_id, "タスク1", team_id, 1)
    t2 = db.add_workflow_task(wf_id, "タスク2", team_id, 1)
    db.add_task_dependency(wf_id, t1, t2)
    db.add_task_dependency(wf_id, t2, t3)
    job_id = db.add_job("ジョブ1", wf_id, None, 100)

    rows = db.list_job_tasks_with_overrides(job_id)
    assert [r["workflow_task_id"] for r in rows] == [t1, t2, t3]
    db.close()


# -- workflow_dependency_templates: 編集・循環防止 -----------------------------------

def _build_three_workflows(db):
    team_id = db.add_team("チームA", 1)
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    wf3 = db.add_workflow("WF3")
    t1 = db.add_workflow_task(wf1, "A", team_id, 1)
    t2 = db.add_workflow_task(wf2, "B", team_id, 1)
    t3 = db.add_workflow_task(wf3, "C", team_id, 1)
    return (wf1, t1), (wf2, t2), (wf3, t3)


def test_dependency_template_can_be_updated_in_place(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    (wf1, t1), (wf2, t2), (wf3, t3) = _build_three_workflows(db)

    tpl_id = db.add_dependency_template(wf1, t1, wf2, t2)
    db.update_dependency_template(tpl_id, t1, wf3, t3)

    templates = db.list_dependency_templates(wf1)
    assert len(templates) == 1
    assert templates[0]["depends_on_workflow_id"] == wf3
    db.close()


def test_dependency_template_rejects_direct_and_transitive_cycles(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    (wf1, t1), (wf2, t2), (wf3, t3) = _build_three_workflows(db)

    db.add_dependency_template(wf1, t1, wf2, t2)  # WF1 depends on WF2
    tpl2 = db.add_dependency_template(wf2, t2, wf3, t3)  # WF2 depends on WF3

    with pytest.raises(ProjectDatabaseError):
        db.add_dependency_template(wf3, t3, wf1, t1)  # WF3 -> WF1 closes the loop

    with pytest.raises(ProjectDatabaseError):
        db.update_dependency_template(tpl2, t2, wf1, t1)  # editing into a cycle

    db.close()


def test_adding_template_after_link_upgrades_matching_manual_pair(tmp_path):
    """依存先ジョブのリンクを先に作り、タスク対応を手動で追加した後に、
    ワークフロー設計タブで同じタスク同士の依存テンプレートを追加すると、
    重複した行を作らず、既存の手動追加行を自動生成扱い（source_link_id設定）
    に昇格させることを確認する。"""
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    t1 = db.add_workflow_task(wf1, "A", team_id, 1)
    t2 = db.add_workflow_task(wf2, "B", team_id, 1)
    j1 = db.add_job("J1", wf1, None, 100)
    j2 = db.add_job("J2", wf2, None, 100)

    link_id = db.add_job_dependency_link(j1, j2)  # まだテンプレートは無い
    db.add_external_dependency(j1, t1, j2, t2)  # 手動でタスク対応を追加

    pairs_before = db.list_external_dependencies(job_id=j1)
    assert len(pairs_before) == 1
    assert pairs_before[0]["source_link_id"] is None  # 手動のまま

    db.add_dependency_template(wf1, t1, wf2, t2)  # 同じタスク対応をテンプレート化

    pairs_after = db.list_external_dependencies(job_id=j1)
    assert len(pairs_after) == 1  # 重複せず1件のまま
    assert pairs_after[0]["id"] == pairs_before[0]["id"]  # 同じ行が
    assert pairs_after[0]["source_link_id"] == link_id  # 自動生成扱いに昇格
    db.close()


def test_adding_template_after_link_expands_to_existing_links_without_manual_pair(tmp_path):
    """依存先ジョブのリンクだけがあり、まだタスク対応が無い状態でテンプレートを
    追加すると、既存のリンクにもそのタスク対応が自動的に展開されること。"""
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    t1 = db.add_workflow_task(wf1, "A", team_id, 1)
    t2 = db.add_workflow_task(wf2, "B", team_id, 1)
    j1 = db.add_job("J1", wf1, None, 100)
    j2 = db.add_job("J2", wf2, None, 100)

    link_id = db.add_job_dependency_link(j1, j2)
    assert db.list_external_dependencies(job_id=j1) == []

    db.add_dependency_template(wf1, t1, wf2, t2)

    pairs = db.list_external_dependencies(job_id=j1)
    assert len(pairs) == 1
    assert pairs[0]["source_link_id"] == link_id
    db.close()


def test_updating_template_resyncs_existing_links(tmp_path):
    """既存の依存テンプレートを編集（依存先ワークフロー/タスクを変更）すると、
    その場で追加した時と同じように、既存の「依存先ジョブ」リンクへ即座に
    反映されること。変更前のワークフローペア向けに自動生成されていた
    タスク対応は削除され、変更後のワークフローペアに合致するリンクへは
    新しく展開される。"""
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    (wf1, t1), (wf2, t2), (wf3, t3) = _build_three_workflows(db)
    j1 = db.add_job("J1", wf1, None, 100)
    j2 = db.add_job("J2", wf2, None, 100)
    j3 = db.add_job("J3", wf3, None, 100)

    tpl_id = db.add_dependency_template(wf1, t1, wf2, t2)
    db.add_job_dependency_link(j1, j2)  # WF1->WF2のテンプレートで自動展開される
    link_to_j3 = db.add_job_dependency_link(j1, j3)  # まだWF1->WF3のテンプレートは無い

    pairs_before = db.list_external_dependencies(job_id=j1)
    assert {p["depends_on_job_id"] for p in pairs_before} == {j2}

    db.update_dependency_template(tpl_id, t1, wf3, t3)  # 依存先をWF2からWF3へ変更

    pairs_after = db.list_external_dependencies(job_id=j1)
    assert {p["depends_on_job_id"] for p in pairs_after} == {j3}  # J2向けは消え、J3向けが追加
    assert pairs_after[0]["source_link_id"] == link_to_j3
    db.close()


def test_deleting_template_removes_stale_auto_pair_but_keeps_manual(tmp_path):
    """依存テンプレートを削除すると、そのテンプレートから自動生成されていた
    タスク対応は削除されるが、手動で追加した別のタスク対応には影響しない
    こと。"""
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    t1 = db.add_workflow_task(wf1, "A", team_id, 1)
    t2 = db.add_workflow_task(wf2, "B", team_id, 1)
    t3 = db.add_workflow_task(wf2, "C", team_id, 1)
    j1 = db.add_job("J1", wf1, None, 100)
    j2 = db.add_job("J2", wf2, None, 100)

    tpl_id = db.add_dependency_template(wf1, t1, wf2, t2)
    db.add_job_dependency_link(j1, j2)  # 自動: t1 -> t2
    db.add_external_dependency(j1, t1, j2, t3)  # 手動: t1 -> t3（別ペア）
    assert len(db.list_external_dependencies(job_id=j1)) == 2

    db.delete_dependency_template(tpl_id)

    pairs = db.list_external_dependencies(job_id=j1)
    assert len(pairs) == 1
    assert pairs[0]["depends_on_workflow_task_id"] == t3
    assert pairs[0]["source_link_id"] is None
    db.close()


def test_job_workflow_reassignment_resyncs_dependency_templates(tmp_path):
    """ジョブ作成タブでジョブのワークフローを再割当てすると、依存先ジョブの
    タスク対応が新しいワークフローの組み合わせに合わせて同期し直される
    こと（テンプレートが無い組み合わせになった場合は自動生成分が削除される）。"""
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    (wf1, t1), (wf2, t2), (_wf3, _t3) = _build_three_workflows(db)
    j1 = db.add_job("J1", wf1, None, 100)
    j2 = db.add_job("J2", wf2, None, 100)

    db.add_dependency_template(wf1, t1, wf2, t2)
    db.add_job_dependency_link(j1, j2)
    assert len(db.list_external_dependencies(job_id=j1)) == 1  # 自動展開済み

    job = next(j for j in db.list_jobs() if j["id"] == j1)
    wf3 = next(w["id"] for w in db.list_workflows() if w["id"] not in (wf1, wf2))
    db.update_job(j1, job["name"], wf3, job["default_milestone_id"], job["priority"])

    # WF3->WF2のテンプレートは存在しないため、自動生成分は削除される
    assert db.list_external_dependencies(job_id=j1) == []
    db.close()


# -- job_task_overrides: マイルストーンの整合性（先行/後続タスク間） -----------------

def _build_linear_workflow_job(db):
    """タスク1 -> タスク2 -> タスク3（この順に依存）を持つワークフローと、
    既定マイルストーンを中期MSに設定したジョブ1件を組み立てる。"""
    team_id = db.add_team("チームA", 1)
    ms_early = db.add_milestone("早期MS", "2026-01-31")
    ms_mid = db.add_milestone("中期MS", "2026-03-31")
    ms_late = db.add_milestone("後期MS", "2026-06-30")
    wf = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf, "タスク1", team_id, 3)
    t2 = db.add_workflow_task(wf, "タスク2", team_id, 3)
    t3 = db.add_workflow_task(wf, "タスク3", team_id, 3)
    db.add_task_dependency(wf, t1, t2)
    db.add_task_dependency(wf, t2, t3)
    job = db.add_job("ジョブ1", wf, ms_mid, 100)
    return {
        "job": job, "t1": t1, "t2": t2, "t3": t3,
        "ms_early": ms_early, "ms_mid": ms_mid, "ms_late": ms_late,
    }


def test_minimum_milestone_end_date_follows_predecessor(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    ids = _build_linear_workflow_job(db)

    # 先行タスクが無いタスク1には下限が無い
    assert db.minimum_milestone_end_date(ids["job"], ids["t1"]) is None

    # タスク2は先行タスク1（既定＝中期MS）に合わせて中期MS以降が下限になる
    assert db.minimum_milestone_end_date(ids["job"], ids["t2"]) == "2026-03-31"

    # タスク1を後期MSへ上書きすると、タスク2の下限もそれに追随する
    db.upsert_job_task_override(ids["job"], ids["t1"], milestone_id=ids["ms_late"])
    assert db.minimum_milestone_end_date(ids["job"], ids["t2"]) == "2026-06-30"
    db.close()


def test_cascade_milestone_to_successors_pushes_back_transitively(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    ids = _build_linear_workflow_job(db)

    db.upsert_job_task_override(ids["job"], ids["t1"], milestone_id=ids["ms_late"])
    changed = db.cascade_milestone_to_successors(ids["job"], ids["t1"])

    assert set(changed) == {ids["t2"], ids["t3"]}
    assert db.effective_milestone(ids["job"], ids["t2"])["milestone_id"] == ids["ms_late"]
    assert db.effective_milestone(ids["job"], ids["t3"])["milestone_id"] == ids["ms_late"]
    db.close()


def test_cascade_milestone_does_nothing_when_already_consistent(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    ids = _build_linear_workflow_job(db)

    # タスク1を早期MSに前倒ししても、後続（既定＝中期MS）はより遅いので調整不要
    db.upsert_job_task_override(ids["job"], ids["t1"], milestone_id=ids["ms_early"])
    changed = db.cascade_milestone_to_successors(ids["job"], ids["t1"])
    assert changed == []
    db.close()


def test_enforce_milestone_floor_fixes_reversal_when_successor_reverted_to_default(tmp_path):
    """バグ回帰テスト: タスク1を後期MSへ上書きすると、cascadeによりタスク2も
    後期MSへ自動的に合わせられる。その後タスク2の上書きを解除して既定
    （中期MS）に戻すと、上書きを再度クリアしただけでは先行タスク1（後期MS）
    より早いマイルストーンに逆転してしまう——enforce_milestone_floorが
    これを検出し、タスク2を先行タスクに合わせて再度引き上げること。"""
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    ids = _build_linear_workflow_job(db)

    db.upsert_job_task_override(ids["job"], ids["t1"], milestone_id=ids["ms_late"])
    db.cascade_milestone_to_successors(ids["job"], ids["t1"])
    assert db.effective_milestone(ids["job"], ids["t2"])["milestone_id"] == ids["ms_late"]

    # タスク2の上書きを解除（既定＝中期MSに戻す）。解除直後は先行タスク1
    # （後期MS）より早くなってしまっている。
    db.clear_job_task_override(ids["job"], ids["t2"])
    assert db.effective_milestone(ids["job"], ids["t2"])["milestone_id"] == ids["ms_mid"]

    raised = db.enforce_milestone_floor(ids["job"], ids["t2"])
    assert raised is True
    assert db.effective_milestone(ids["job"], ids["t2"])["milestone_id"] == ids["ms_late"]
    db.close()


def test_enforce_milestone_floor_noop_without_predecessors_or_when_consistent(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    ids = _build_linear_workflow_job(db)

    # タスク1には先行タスクが無いので常にFalse
    assert db.enforce_milestone_floor(ids["job"], ids["t1"]) is False

    # タスク2は既定のまま（中期MS）で先行タスク1の既定（中期MS）と同じなので調整不要
    assert db.enforce_milestone_floor(ids["job"], ids["t2"]) is False
    db.close()


# -- job_external_dependencies: 個別のタスク依存の編集 ---------------------------------

def test_external_dependency_can_be_updated_in_place(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    t1 = db.add_workflow_task(wf1, "A", team_id, 1)
    t2 = db.add_workflow_task(wf2, "B", team_id, 1)
    t3 = db.add_workflow_task(wf2, "C", team_id, 1)
    j1 = db.add_job("J1", wf1, None, 100)
    j2 = db.add_job("J2", wf2, None, 100)

    dep_id = db.add_external_dependency(j1, t1, j2, t2)
    db.update_external_dependency(dep_id, t1, j2, t3)

    rows = db.list_external_dependencies(job_id=j1)
    assert len(rows) == 1
    assert rows[0]["depends_on_workflow_task_id"] == t3
    db.close()


def test_external_dependency_update_rejects_self_dependency(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    wf1 = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf1, "A", team_id, 1)
    t2 = db.add_workflow_task(wf1, "B", team_id, 1)
    j1 = db.add_job("J1", wf1, None, 100)
    j2 = db.add_job("J2", wf1, None, 100)

    dep_id = db.add_external_dependency(j1, t1, j2, t2)
    with pytest.raises(ProjectDatabaseError):
        db.update_external_dependency(dep_id, t1, j1, t1)  # 自己依存になる変更
    db.close()


def test_external_dependency_can_be_toggled_active_without_deleting(tmp_path):
    """自動生成分は「依存先ジョブ」から削除するまで一覧から消えないため、
    一時的に外したいだけの場合は削除ではなく無効化で対応できること。
    無効化してもDB上の行（および依存先の指定）はそのまま残る。"""
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    t1 = db.add_workflow_task(wf1, "A", team_id, 1)
    t2 = db.add_workflow_task(wf2, "B", team_id, 1)
    j1 = db.add_job("J1", wf1, None, 100)
    j2 = db.add_job("J2", wf2, None, 100)

    dep_id = db.add_external_dependency(j1, t1, j2, t2)
    assert db.list_external_dependencies(job_id=j1)[0]["is_active"] == 1

    db.set_external_dependency_active(dep_id, False)
    rows = db.list_external_dependencies(job_id=j1)
    assert len(rows) == 1  # 削除されず残っている
    assert rows[0]["is_active"] == 0

    db.set_external_dependency_active(dep_id, True)
    assert db.list_external_dependencies(job_id=j1)[0]["is_active"] == 1
    db.close()


def test_build_frames_excludes_inactive_external_dependencies(tmp_path):
    """無効化した個別のタスク依存は、スケジューリングに渡すDataFrameから
    除外される（＝そのタスクの依存として扱われない）こと。"""
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    team_id = db.add_team("チームA", 1)
    wf1 = db.add_workflow("WF1")
    wf2 = db.add_workflow("WF2")
    t1 = db.add_workflow_task(wf1, "A", team_id, 1)
    t2 = db.add_workflow_task(wf2, "B", team_id, 1)
    j1 = db.add_job("J1", wf1, None, 100)
    j2 = db.add_job("J2", wf2, None, 100)

    dep_id = db.add_external_dependency(j1, t1, j2, t2)
    frames = build_frames(db)
    assert frames["external_dependencies"] is not None
    assert len(frames["external_dependencies"]) == 1

    db.set_external_dependency_active(dep_id, False)
    frames = build_frames(db)
    assert frames["external_dependencies"] is None
    db.close()


# -- gantt_generator: DB -> DataFrame -> project_scheduler.py -----------------------

def _build_minimal_project(db_path):
    """チーム1・マイルストーン1・依存ありタスク2つのワークフロー1・ジョブ1、
    という最小構成のプロジェクトを組み立てる。"""
    db = ProjectDatabase.create_new(str(db_path))
    db.set_project("スモークテストプロジェクト", "2026-01-01")
    team_id = db.add_team("チームA", 2)
    ms_id = db.add_milestone("マイルストーン1", "2026-03-01")
    wf_id = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf_id, "デザイン", team_id, 3)
    t2 = db.add_workflow_task(wf_id, "実装", team_id, 5)
    db.add_task_dependency(wf_id, t1, t2)
    db.add_job("ジョブ1", wf_id, ms_id, 1)
    return db


def test_validate_for_generation_blocks_empty_project(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    errors = validate_for_generation(db)
    assert errors  # 未完成なプロジェクトはエラーが1件以上ある
    db.close()


def test_validate_for_generation_passes_minimal_project(tmp_path):
    db = _build_minimal_project(tmp_path / "project.pschedule")
    assert validate_for_generation(db) == []
    db.close()


def test_generate_gantt_minimal_project(tmp_path):
    db = _build_minimal_project(tmp_path / "project.pschedule")
    md_path = tmp_path / "schedule_gantt.md"
    html_path = tmp_path / "schedule_gantt.html"

    result_df = generate_gantt(
        db, mermaid_output_path=str(md_path), plotly_output_path=str(html_path), verbose=False,
    )

    assert len(result_df) == 2
    assert md_path.exists() and md_path.stat().st_size > 0
    assert html_path.exists() and html_path.stat().st_size > 0
    assert "デザイン" in md_path.read_text(encoding="utf-8")
    db.close()


def test_generate_gantt_rejects_circular_dependency(tmp_path):
    db = ProjectDatabase.create_new(str(tmp_path / "project.pschedule"))
    db.set_project("循環依存テスト", "2026-01-01")
    team_id = db.add_team("チームA", 1)
    ms_id = db.add_milestone("マイルストーン1", "2026-03-01")
    wf_id = db.add_workflow("WF1")
    t1 = db.add_workflow_task(wf_id, "タスク1", team_id, 3)
    t2 = db.add_workflow_task(wf_id, "タスク2", team_id, 3)
    # ここでは gui/node_canvas.py の事前チェックを経由しないため、DB層は
    # 直接の循環（1<->2）以外なら書き込めてしまう。project_scheduler.py側の
    # CircularDependencyError検出を確認する。
    db.add_task_dependency(wf_id, t1, t2)
    db.add_task_dependency(wf_id, t2, t1)
    db.add_job("ジョブ1", wf_id, ms_id, 1)

    with pytest.raises(SchedulingError):
        generate_gantt(db, verbose=False)
    db.close()


def _build_tight_single_team_project(db_path, team_max_lines):
    """1チーム・独立した3ジョブ（各2日タスク1つ）・共通の厳しい締切（開始日を
    含む1週間）という構成を組み立てる。チームの同時ライン数が1のままでは
    3タスク×2日＝6team-daysを5営業日に収められず必ずResourceOverflowErrorに
    なるが、同時ライン数が十分（3以上）あれば並行実行でき、必ず間に合う。"""
    db = ProjectDatabase.create_new(str(db_path))
    db.set_project("同時ライン数変動テスト", "2026-01-05")  # 月曜（祝日等と重ならない週）
    team_id = db.add_team("チームA", team_max_lines)
    ms_id = db.add_milestone("マイルストーン1", "2026-01-09")  # 同じ週の金曜（5営業日）
    for i in range(3):
        wf_id = db.add_workflow(f"WF{i}")
        t_id = db.add_workflow_task(wf_id, "タスク", team_id, 2)
        db.add_job(f"ジョブ{i}", wf_id, ms_id, 100)
    return db, team_id


def test_constant_low_team_capacity_overflows_tight_deadline(tmp_path):
    db, _team_id = _build_tight_single_team_project(tmp_path / "project.pschedule", team_max_lines=1)
    with pytest.raises(ResourceOverflowError):
        generate_gantt(db, verbose=False)
    db.close()


def test_team_capacity_change_relieves_overflow_from_its_start_date(tmp_path):
    """同時ライン数が最初は1のままでも、開発開始日からライン数3に変更する
    team_capacity_changes を追加すれば、同じ締切でも間に合うようになること
    （＝project_scheduler.py側のスケジューリングが日付ごとのライン数を
    実際に考慮していることの回帰テスト）。"""
    db, team_id = _build_tight_single_team_project(tmp_path / "project.pschedule", team_max_lines=1)
    db.add_team_capacity_change(team_id, "2026-01-05", 3)

    result_df = generate_gantt(db, verbose=False)
    assert len(result_df) == 3
    db.close()


# -- 移行済みサンプルデータ（data/Project_Schedule_Sample_GameDev_v22.pschedule）--------

@pytest.mark.skipif(not SAMPLE_DB.exists(), reason="サンプル.pscheduleが見つかりません")
def test_sample_pschedule_generates_full_schedule(tmp_path):
    db = ProjectDatabase.open_existing(str(SAMPLE_DB))
    assert validate_for_generation(db) == []

    md_path = tmp_path / "schedule_gantt.md"
    html_path = tmp_path / "schedule_gantt.html"
    result_df = generate_gantt(
        db, mermaid_output_path=str(md_path), plotly_output_path=str(html_path), verbose=False,
    )

    # data/Project_Schedule_Sample_GameDev_v22.xlsx をCLIで直接実行した場合と
    # 同じ340行になることを確認する（DB移行・GUI側の変換で欠落/重複が無いこと）。
    assert len(result_df) == 340
    assert md_path.stat().st_size > 0
    assert html_path.stat().st_size > 0
    db.close()
