"""
プロジェクトデータの永続化層（SQLite）。

Qt（PySide6）に一切依存しない。GUIのタブ実装やテストから直接呼び出せる
CRUD一式を ProjectDatabase クラスとして提供する。保存ファイルの拡張子は
`.pschedule` を推奨するが、実体は標準的なSQLiteファイルであり、中身は
このモジュールが定義するスキーマそのもの（一般的なSQLiteビューアでも
開ける）。

永続化のタイミング: すべてのCRUDメソッドはメモリ上のSQLite接続
（`:memory:`）に対してのみ即座にコミットする。実際のファイル（`self.path`）
への書き込みは `save()` / `save_as()` を明示的に呼んだ時にのみ行う
（GUI側では Ctrl+S / Ctrl+Shift+S に割り当てる）。`is_dirty()` で
未保存の変更があるかどうかを確認できる。

ID方針: 全テーブルは整数の自動採番PKを持ち、GUI上はこのIDを一切表示しない
（常に名前で参照する）。project_scheduler.py が期待する文字列ID
（"WF_001" 等）への変換は gantt_generator.py 側でガントチャート生成の
直前にのみ行う。
"""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1"

_SCHEMA_SQL = """
CREATE TABLE schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE project (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    project_name TEXT NOT NULL DEFAULT '',
    start_date   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    end_date TEXT NOT NULL
);

CREATE TABLE teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    max_lines INTEGER NOT NULL CHECK (max_lines >= 1)
);

CREATE TABLE holidays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    team_id INTEGER REFERENCES teams(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX ux_holidays_team ON holidays(date, team_id) WHERE team_id IS NOT NULL;

CREATE TABLE workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE workflow_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
    default_days INTEGER NOT NULL CHECK (default_days >= 1),
    canvas_x REAL NOT NULL DEFAULT 0,
    canvas_y REAL NOT NULL DEFAULT 0,
    UNIQUE(workflow_id, name)
);

CREATE TABLE task_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    predecessor_task_id INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    successor_task_id   INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    UNIQUE(predecessor_task_id, successor_task_id),
    CHECK (predecessor_task_id != successor_task_id)
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
    default_milestone_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL,
    priority INTEGER NOT NULL DEFAULT 100 CHECK (priority >= 1)
);

CREATE TABLE job_task_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    workflow_task_id INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    is_active INTEGER NOT NULL DEFAULT 1,
    override_days INTEGER,
    milestone_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL,
    team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    UNIQUE(job_id, workflow_task_id)
);

CREATE TABLE job_dependency_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    depends_on_job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    UNIQUE(job_id, depends_on_job_id),
    CHECK (job_id != depends_on_job_id)
);

CREATE TABLE job_external_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    workflow_task_id INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    depends_on_job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    depends_on_workflow_task_id INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    source_link_id INTEGER REFERENCES job_dependency_links(id) ON DELETE CASCADE,
    UNIQUE(job_id, workflow_task_id, depends_on_job_id, depends_on_workflow_task_id)
);

CREATE TABLE workflow_dependency_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    workflow_task_id INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    depends_on_workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    depends_on_workflow_task_id INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    UNIQUE(workflow_id, workflow_task_id, depends_on_workflow_id, depends_on_workflow_task_id),
    CHECK (workflow_id != depends_on_workflow_id)
);
"""


class ProjectDatabaseError(Exception):
    """DB層で検出した業務エラー（一意性違反・不正な削除など）の基底クラス"""


class DuplicateNameError(ProjectDatabaseError):
    pass


class ReferencedEntityError(ProjectDatabaseError):
    """参照が残っている行を削除しようとした場合"""


class ProjectDatabase:
    def __init__(self, path):
        self.path = str(path)
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._dirty = False
        # GUI側から差し込む変更通知フック（タイトルバーの未保存マーク更新等に使う）。
        # 引数なしで呼ばれる callable、または None。
        self.on_change = None

    def close(self):
        self._conn.close()

    # -- 生成/オープン -----------------------------------------------------

    @classmethod
    def create_new(cls, path):
        """新規プロジェクトを（メモリ上に）作成する。この時点ではまだファイルへの
        書き込みは行わない——ユーザーが保存（Ctrl+S）するまでディスク上には
        何も生成されない。"""
        db = cls(path)
        db._conn.executescript(_SCHEMA_SQL)
        db._conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        db._conn.execute(
            "INSERT INTO project(id, project_name, start_date) VALUES (1, '', '')"
        )
        db._conn.commit()
        db._dirty = True  # 新規作成時点でまだ未保存
        return db

    @classmethod
    def open_existing(cls, path):
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"プロジェクトファイルが見つかりません: {path}")
        disk_conn = sqlite3.connect(str(p))
        try:
            db = cls(path)
            disk_conn.backup(db._conn)
        finally:
            disk_conn.close()
        db._dirty = False
        return db

    # -- 保存（明示的） -----------------------------------------------------

    def is_dirty(self):
        """前回の保存（またはオープン/新規作成）以降に変更があれば True。"""
        return self._dirty

    def save(self):
        """メモリ上の内容を self.path のファイルへ書き出す。"""
        self._write_to(self.path)
        self._dirty = False
        self._notify_change()

    def save_as(self, new_path):
        """メモリ上の内容を new_path へ書き出し、以降そのパスを対象とする。"""
        self._write_to(new_path)
        self.path = str(new_path)
        self._dirty = False
        self._notify_change()

    def _write_to(self, path):
        p = Path(path)
        if p.exists():
            p.unlink()
        dest_conn = sqlite3.connect(str(p))
        try:
            self._conn.backup(dest_conn)
        finally:
            dest_conn.close()

    def _commit(self):
        """変更系メソッドの末尾から呼ぶ内部コミット。メモリ上のトランザクションを
        確定し、未保存フラグを立てて変更通知フックを呼ぶ（ファイルへの書き込みは
        行わない——それは save()/save_as() の役目）。"""
        self._conn.commit()
        self._dirty = True
        self._notify_change()

    def _notify_change(self):
        if self.on_change is not None:
            self.on_change()

    # -- project（単一行） --------------------------------------------------

    def get_project(self):
        row = self._conn.execute(
            "SELECT project_name, start_date FROM project WHERE id = 1"
        ).fetchone()
        return {"project_name": row["project_name"], "start_date": row["start_date"]}

    def set_project(self, project_name, start_date):
        self._conn.execute(
            "UPDATE project SET project_name = ?, start_date = ? WHERE id = 1",
            (project_name, start_date),
        )
        self._commit()

    # -- milestones ---------------------------------------------------------

    def list_milestones(self):
        rows = self._conn.execute(
            "SELECT id, name, end_date FROM milestones ORDER BY end_date, name"
        ).fetchall()
        return [dict(r) for r in rows]

    def add_milestone(self, name, end_date):
        try:
            cur = self._conn.execute(
                "INSERT INTO milestones(name, end_date) VALUES (?, ?)", (name, end_date)
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"マイルストーン名 '{name}' は既に使用されています") from e
        self._commit()
        return cur.lastrowid

    def update_milestone(self, milestone_id, name, end_date):
        try:
            self._conn.execute(
                "UPDATE milestones SET name = ?, end_date = ? WHERE id = ?",
                (name, end_date, milestone_id),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"マイルストーン名 '{name}' は既に使用されています") from e
        self._commit()

    def milestone_usage_count(self, milestone_id):
        row = self._conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM jobs WHERE default_milestone_id = ?) + "
            "(SELECT COUNT(*) FROM job_task_overrides WHERE milestone_id = ?) AS n",
            (milestone_id, milestone_id),
        ).fetchone()
        return row["n"]

    def delete_milestone(self, milestone_id):
        self._conn.execute("DELETE FROM milestones WHERE id = ?", (milestone_id,))
        self._commit()

    # -- teams ---------------------------------------------------------------

    def list_teams(self):
        rows = self._conn.execute(
            "SELECT id, name, max_lines FROM teams ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def add_team(self, name, max_lines):
        try:
            cur = self._conn.execute(
                "INSERT INTO teams(name, max_lines) VALUES (?, ?)", (name, max_lines)
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"チーム名 '{name}' は既に使用されています") from e
        self._commit()
        return cur.lastrowid

    def update_team(self, team_id, name, max_lines):
        try:
            self._conn.execute(
                "UPDATE teams SET name = ?, max_lines = ? WHERE id = ?",
                (name, max_lines, team_id),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"チーム名 '{name}' は既に使用されています") from e
        self._commit()

    def team_usage_count(self, team_id):
        row = self._conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM workflow_tasks WHERE team_id = ?) + "
            "(SELECT COUNT(*) FROM job_task_overrides WHERE team_id = ?) AS n",
            (team_id, team_id),
        ).fetchone()
        return row["n"]

    def delete_team(self, team_id):
        if self.team_usage_count(team_id) > 0:
            raise ReferencedEntityError("このチームはタスクに使用されているため削除できません")
        self._conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        self._commit()

    # -- holidays --------------------------------------------------------------

    def list_holidays(self):
        rows = self._conn.execute(
            "SELECT h.id, h.date, h.team_id, t.name AS team_name "
            "FROM holidays h LEFT JOIN teams t ON t.id = h.team_id "
            "ORDER BY h.date"
        ).fetchall()
        return [dict(r) for r in rows]

    def add_holiday(self, date, team_id=None):
        exists = self._conn.execute(
            "SELECT 1 FROM holidays WHERE date = ? AND team_id IS ?", (date, team_id)
        ).fetchone()
        if exists:
            raise DuplicateNameError("同じ日付・チームの休業日が既に登録されています")
        cur = self._conn.execute(
            "INSERT INTO holidays(date, team_id) VALUES (?, ?)", (date, team_id)
        )
        self._commit()
        return cur.lastrowid

    def update_holiday(self, holiday_id, date, team_id=None):
        exists = self._conn.execute(
            "SELECT 1 FROM holidays WHERE date = ? AND team_id IS ? AND id != ?",
            (date, team_id, holiday_id),
        ).fetchone()
        if exists:
            raise DuplicateNameError("同じ日付・チームの休業日が既に登録されています")
        self._conn.execute(
            "UPDATE holidays SET date = ?, team_id = ? WHERE id = ?",
            (date, team_id, holiday_id),
        )
        self._commit()

    def delete_holiday(self, holiday_id):
        self._conn.execute("DELETE FROM holidays WHERE id = ?", (holiday_id,))
        self._commit()

    # -- workflows ----------------------------------------------------------

    def list_workflows(self):
        rows = self._conn.execute(
            "SELECT id, name FROM workflows ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def add_workflow(self, name):
        try:
            cur = self._conn.execute("INSERT INTO workflows(name) VALUES (?)", (name,))
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"ワークフロー名 '{name}' は既に使用されています") from e
        self._commit()
        return cur.lastrowid

    def rename_workflow(self, workflow_id, name):
        try:
            self._conn.execute(
                "UPDATE workflows SET name = ? WHERE id = ?", (name, workflow_id)
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"ワークフロー名 '{name}' は既に使用されています") from e
        self._commit()

    def workflow_usage_count(self, workflow_id):
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        return row["n"]

    def delete_workflow(self, workflow_id):
        if self.workflow_usage_count(workflow_id) > 0:
            raise ReferencedEntityError(
                "このワークフローは既存のジョブに使用されているため削除できません"
            )
        self._conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        self._commit()

    # -- workflow_tasks -------------------------------------------------------

    def list_workflow_tasks(self, workflow_id):
        rows = self._conn.execute(
            "SELECT wt.id, wt.workflow_id, wt.name, wt.team_id, t.name AS team_name, "
            "wt.default_days, wt.canvas_x, wt.canvas_y "
            "FROM workflow_tasks wt JOIN teams t ON t.id = wt.team_id "
            "WHERE wt.workflow_id = ? ORDER BY wt.id",
            (workflow_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_workflow_task(self, workflow_id, name, team_id, default_days, x=0.0, y=0.0):
        try:
            cur = self._conn.execute(
                "INSERT INTO workflow_tasks(workflow_id, name, team_id, default_days, "
                "canvas_x, canvas_y) VALUES (?, ?, ?, ?, ?, ?)",
                (workflow_id, name, team_id, default_days, x, y),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(
                f"タスク名 '{name}' はこのワークフロー内で既に使用されています"
            ) from e
        self._commit()
        return cur.lastrowid

    def update_workflow_task(self, task_id, name, team_id, default_days):
        try:
            self._conn.execute(
                "UPDATE workflow_tasks SET name = ?, team_id = ?, default_days = ? "
                "WHERE id = ?",
                (name, team_id, default_days, task_id),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(
                f"タスク名 '{name}' はこのワークフロー内で既に使用されています"
            ) from e
        self._commit()

    def update_task_position(self, task_id, x, y):
        self._conn.execute(
            "UPDATE workflow_tasks SET canvas_x = ?, canvas_y = ? WHERE id = ?",
            (x, y, task_id),
        )
        self._commit()

    def workflow_task_usage_count(self, task_id):
        row = self._conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM job_task_overrides WHERE workflow_task_id = ?) + "
            "(SELECT COUNT(*) FROM job_external_dependencies WHERE workflow_task_id = ? "
            " OR depends_on_workflow_task_id = ?) + "
            "(SELECT COUNT(*) FROM task_dependencies WHERE predecessor_task_id = ? "
            " OR successor_task_id = ?) + "
            "(SELECT COUNT(*) FROM workflow_dependency_templates WHERE workflow_task_id = ? "
            " OR depends_on_workflow_task_id = ?) AS n",
            (task_id, task_id, task_id, task_id, task_id, task_id, task_id),
        ).fetchone()
        return row["n"]

    def delete_workflow_task(self, task_id):
        self._conn.execute("DELETE FROM workflow_tasks WHERE id = ?", (task_id,))
        self._commit()

    # -- task_dependencies（ワークフロー内の依存＝Internal_Depends） -----------

    def list_task_dependencies(self, workflow_id):
        rows = self._conn.execute(
            "SELECT id, workflow_id, predecessor_task_id, successor_task_id "
            "FROM task_dependencies WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_task_dependency(self, workflow_id, predecessor_task_id, successor_task_id):
        """循環依存のチェックは呼び出し側（gui/node_canvas.py）が事前に行う想定。
        ここでは構造的制約（自己参照禁止・重複禁止）のみDB制約で守る。"""
        try:
            cur = self._conn.execute(
                "INSERT INTO task_dependencies(workflow_id, predecessor_task_id, "
                "successor_task_id) VALUES (?, ?, ?)",
                (workflow_id, predecessor_task_id, successor_task_id),
            )
        except sqlite3.IntegrityError as e:
            raise ProjectDatabaseError("この依存関係は既に存在するか、不正です") from e
        self._commit()
        return cur.lastrowid

    def delete_task_dependency(self, dependency_id):
        self._conn.execute("DELETE FROM task_dependencies WHERE id = ?", (dependency_id,))
        self._commit()

    # -- jobs ------------------------------------------------------------------

    def list_jobs(self):
        rows = self._conn.execute(
            "SELECT j.id, j.name, j.workflow_id, w.name AS workflow_name, "
            "j.default_milestone_id, m.name AS milestone_name, j.priority "
            "FROM jobs j "
            "JOIN workflows w ON w.id = j.workflow_id "
            "LEFT JOIN milestones m ON m.id = j.default_milestone_id "
            "ORDER BY j.name"
        ).fetchall()
        return [dict(r) for r in rows]

    def add_job(self, name, workflow_id, default_milestone_id, priority):
        try:
            cur = self._conn.execute(
                "INSERT INTO jobs(name, workflow_id, default_milestone_id, priority) "
                "VALUES (?, ?, ?, ?)",
                (name, workflow_id, default_milestone_id, priority),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"ジョブ名 '{name}' は既に使用されています") from e
        self._commit()
        return cur.lastrowid

    def update_job(self, job_id, name, workflow_id, default_milestone_id, priority):
        try:
            self._conn.execute(
                "UPDATE jobs SET name = ?, workflow_id = ?, default_milestone_id = ?, "
                "priority = ? WHERE id = ?",
                (name, workflow_id, default_milestone_id, priority, job_id),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"ジョブ名 '{name}' は既に使用されています") from e
        self._commit()

    def job_usage_count(self, job_id):
        """他のジョブがこのジョブに外部依存している数（削除時の警告用）"""
        row = self._conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM job_external_dependencies WHERE depends_on_job_id = ?) + "
            "(SELECT COUNT(*) FROM job_dependency_links WHERE depends_on_job_id = ? "
            " OR job_id = ?) AS n",
            (job_id, job_id, job_id),
        ).fetchone()
        return row["n"]

    def delete_job(self, job_id):
        self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        self._commit()

    # -- job_task_overrides -------------------------------------------------------

    def list_job_tasks_with_overrides(self, job_id):
        """選択ジョブのワークフローが持つ全タスクを、上書き情報（あれば）付きで返す。
        タスクの並びはワークフロー内のid順（登録順）。"""
        rows = self._conn.execute(
            "SELECT wt.id AS workflow_task_id, wt.name AS task_name, "
            "wt.team_id AS default_team_id, t.name AS default_team_name, "
            "wt.default_days, "
            "o.id AS override_id, o.is_active, o.override_days, "
            "o.milestone_id AS override_milestone_id, "
            "om.name AS override_milestone_name, "
            "o.team_id AS override_team_id, ot.name AS override_team_name "
            "FROM jobs j "
            "JOIN workflow_tasks wt ON wt.workflow_id = j.workflow_id "
            "JOIN teams t ON t.id = wt.team_id "
            "LEFT JOIN job_task_overrides o ON o.job_id = j.id AND o.workflow_task_id = wt.id "
            "LEFT JOIN milestones om ON om.id = o.milestone_id "
            "LEFT JOIN teams ot ON ot.id = o.team_id "
            "WHERE j.id = ? ORDER BY wt.id",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_job_task_override(self, job_id, workflow_task_id, is_active=True,
                                  override_days=None, milestone_id=None, team_id=None):
        existing = self._conn.execute(
            "SELECT id FROM job_task_overrides WHERE job_id = ? AND workflow_task_id = ?",
            (job_id, workflow_task_id),
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE job_task_overrides SET is_active = ?, override_days = ?, "
                "milestone_id = ?, team_id = ? WHERE id = ?",
                (int(is_active), override_days, milestone_id, team_id, existing["id"]),
            )
        else:
            self._conn.execute(
                "INSERT INTO job_task_overrides(job_id, workflow_task_id, is_active, "
                "override_days, milestone_id, team_id) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, workflow_task_id, int(is_active), override_days, milestone_id, team_id),
            )
        self._commit()

    def clear_job_task_override(self, job_id, workflow_task_id):
        """タスクが既定値に戻った場合、上書き行自体を削除する（差分のみ保持）。"""
        self._conn.execute(
            "DELETE FROM job_task_overrides WHERE job_id = ? AND workflow_task_id = ?",
            (job_id, workflow_task_id),
        )
        self._commit()

    # -- job_external_dependencies ----------------------------------------------

    def list_external_dependencies(self, job_id=None):
        """job_id を指定すると、そのジョブが依存する側の行だけに絞り込む
        （タブ3の「個別のタスク依存」セクション用）。省略時は全件。"""
        sql = (
            "SELECT d.id, "
            "d.job_id, j.name AS job_name, "
            "d.workflow_task_id, wt.name AS task_name, "
            "d.depends_on_job_id, dj.name AS depends_on_job_name, "
            "d.depends_on_workflow_task_id, dwt.name AS depends_on_task_name, "
            "d.source_link_id "
            "FROM job_external_dependencies d "
            "JOIN jobs j ON j.id = d.job_id "
            "JOIN workflow_tasks wt ON wt.id = d.workflow_task_id "
            "JOIN jobs dj ON dj.id = d.depends_on_job_id "
            "JOIN workflow_tasks dwt ON dwt.id = d.depends_on_workflow_task_id"
        )
        params = ()
        if job_id is not None:
            sql += " WHERE d.job_id = ?"
            params = (job_id,)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def add_external_dependency(self, job_id, workflow_task_id, depends_on_job_id,
                                 depends_on_workflow_task_id):
        if (job_id, workflow_task_id) == (depends_on_job_id, depends_on_workflow_task_id):
            raise ProjectDatabaseError("同じタスクへの自己依存は設定できません")
        try:
            cur = self._conn.execute(
                "INSERT INTO job_external_dependencies(job_id, workflow_task_id, "
                "depends_on_job_id, depends_on_workflow_task_id) VALUES (?, ?, ?, ?)",
                (job_id, workflow_task_id, depends_on_job_id, depends_on_workflow_task_id),
            )
        except sqlite3.IntegrityError as e:
            raise ProjectDatabaseError("この依存関係は既に登録されています") from e
        self._commit()
        return cur.lastrowid

    def delete_external_dependency(self, dependency_id):
        self._conn.execute(
            "DELETE FROM job_external_dependencies WHERE id = ?", (dependency_id,)
        )
        self._commit()

    # -- job_dependency_links（ジョブ単位の依存リンク。追加時に依存テンプレートを -----
    # -- 参照してタスク単位の job_external_dependencies を自動展開する） -----------

    def list_job_dependency_links(self, job_id):
        rows = self._conn.execute(
            "SELECT l.id, l.job_id, l.depends_on_job_id, dj.name AS depends_on_job_name "
            "FROM job_dependency_links l JOIN jobs dj ON dj.id = l.depends_on_job_id "
            "WHERE l.job_id = ? ORDER BY dj.name",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_job_dependency_link(self, job_id, depends_on_job_id):
        if job_id == depends_on_job_id:
            raise ProjectDatabaseError("同じジョブへの自己依存は設定できません")
        try:
            cur = self._conn.execute(
                "INSERT INTO job_dependency_links(job_id, depends_on_job_id) VALUES (?, ?)",
                (job_id, depends_on_job_id),
            )
        except sqlite3.IntegrityError as e:
            raise ProjectDatabaseError("このジョブへの依存は既に登録されています") from e
        link_id = cur.lastrowid

        job = self._conn.execute(
            "SELECT workflow_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        depends_on_job = self._conn.execute(
            "SELECT workflow_id FROM jobs WHERE id = ?", (depends_on_job_id,)
        ).fetchone()
        templates = self._conn.execute(
            "SELECT workflow_task_id, depends_on_workflow_task_id "
            "FROM workflow_dependency_templates "
            "WHERE workflow_id = ? AND depends_on_workflow_id = ?",
            (job["workflow_id"], depends_on_job["workflow_id"]),
        ).fetchall()
        for t in templates:
            self._conn.execute(
                "INSERT OR IGNORE INTO job_external_dependencies "
                "(job_id, workflow_task_id, depends_on_job_id, depends_on_workflow_task_id, "
                "source_link_id) VALUES (?, ?, ?, ?, ?)",
                (job_id, t["workflow_task_id"], depends_on_job_id,
                 t["depends_on_workflow_task_id"], link_id),
            )
        self._commit()
        return link_id

    def delete_job_dependency_link(self, link_id):
        """CASCADEにより、このリンクから自動生成された job_external_dependencies
        行（source_link_id が一致する行）も同時に削除される。手動追加行
        （source_link_id が NULL）は影響を受けない。"""
        self._conn.execute("DELETE FROM job_dependency_links WHERE id = ?", (link_id,))
        self._commit()

    # -- workflow_dependency_templates（ワークフローペア単位の既定タスク対応） -------

    def list_dependency_templates(self, workflow_id):
        rows = self._conn.execute(
            "SELECT tpl.id, tpl.workflow_id, tpl.workflow_task_id, wt.name AS task_name, "
            "tpl.depends_on_workflow_id, dw.name AS depends_on_workflow_name, "
            "tpl.depends_on_workflow_task_id, dwt.name AS depends_on_task_name "
            "FROM workflow_dependency_templates tpl "
            "JOIN workflow_tasks wt ON wt.id = tpl.workflow_task_id "
            "JOIN workflows dw ON dw.id = tpl.depends_on_workflow_id "
            "JOIN workflow_tasks dwt ON dwt.id = tpl.depends_on_workflow_task_id "
            "WHERE tpl.workflow_id = ? ORDER BY wt.name",
            (workflow_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _would_create_workflow_template_cycle(self, workflow_id, depends_on_workflow_id,
                                               exclude_template_id=None):
        """workflow_id が depends_on_workflow_id に依存するテンプレートを追加/変更
        した場合に、ワークフロー間の依存テンプレートのグラフ全体で閉路（循環依存）
        が生じるかどうかを判定する。exclude_template_id を指定すると、その
        テンプレート自身は既存のグラフから除外して判定する（編集時の自己比較を
        避けるため）。"""
        if workflow_id == depends_on_workflow_id:
            return True
        sql = "SELECT workflow_id, depends_on_workflow_id FROM workflow_dependency_templates"
        params = ()
        if exclude_template_id is not None:
            sql += " WHERE id != ?"
            params = (exclude_template_id,)
        graph = {}
        for r in self._conn.execute(sql, params).fetchall():
            graph.setdefault(r["workflow_id"], set()).add(r["depends_on_workflow_id"])
        # depends_on_workflow_id から既存の依存を辿って workflow_id まで戻ってこれる
        # ならば、新しいエッジ workflow_id -> depends_on_workflow_id と合わせて閉路になる。
        stack = [depends_on_workflow_id]
        seen = set()
        while stack:
            current = stack.pop()
            if current == workflow_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(graph.get(current, ()))
        return False

    def add_dependency_template(self, workflow_id, workflow_task_id,
                                 depends_on_workflow_id, depends_on_workflow_task_id):
        if self._would_create_workflow_template_cycle(workflow_id, depends_on_workflow_id):
            raise ProjectDatabaseError(
                "このテンプレートを追加すると、ワークフロー間で循環依存になるため追加できません"
            )
        try:
            cur = self._conn.execute(
                "INSERT INTO workflow_dependency_templates "
                "(workflow_id, workflow_task_id, depends_on_workflow_id, "
                "depends_on_workflow_task_id) VALUES (?, ?, ?, ?)",
                (workflow_id, workflow_task_id, depends_on_workflow_id, depends_on_workflow_task_id),
            )
        except sqlite3.IntegrityError as e:
            raise ProjectDatabaseError("このテンプレートは既に登録されています") from e
        self._commit()
        return cur.lastrowid

    def update_dependency_template(self, template_id, workflow_task_id,
                                    depends_on_workflow_id, depends_on_workflow_task_id):
        """テンプレートの依存先ワークフロー/タスクを変更する（ワークフロー自体は
        固定——このテンプレートが属する側のワークフローは変わらない）。"""
        row = self._conn.execute(
            "SELECT workflow_id FROM workflow_dependency_templates WHERE id = ?",
            (template_id,),
        ).fetchone()
        if row is None:
            raise ProjectDatabaseError("このテンプレートは存在しません")
        workflow_id = row["workflow_id"]
        if self._would_create_workflow_template_cycle(
            workflow_id, depends_on_workflow_id, exclude_template_id=template_id
        ):
            raise ProjectDatabaseError(
                "この変更を行うと、ワークフロー間で循環依存になるため変更できません"
            )
        try:
            self._conn.execute(
                "UPDATE workflow_dependency_templates SET workflow_task_id = ?, "
                "depends_on_workflow_id = ?, depends_on_workflow_task_id = ? WHERE id = ?",
                (workflow_task_id, depends_on_workflow_id, depends_on_workflow_task_id, template_id),
            )
        except sqlite3.IntegrityError as e:
            raise ProjectDatabaseError("このテンプレートは既に登録されています") from e
        self._commit()

    def delete_dependency_template(self, template_id):
        self._conn.execute(
            "DELETE FROM workflow_dependency_templates WHERE id = ?", (template_id,)
        )
        self._commit()
