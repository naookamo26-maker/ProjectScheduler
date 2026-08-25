"""
プロジェクトファイル（`.pschedule`）のスキーマ定義と、旧バージョンからの
マイグレーション。

CRUD本体（gui/db.py）から分離しているのは、この2つが「増え続ける」性質を
持ち、かつ互いに強く結びついているため。テーブルを1つ足すたびにDDLと
マイグレーション手順の両方が伸びるが、それはCRUDの実装とは別の関心事であり、
同じファイルに置くと db.py が読みづらくなる。

新しいスキーマ変更を加える手順:

1. `_SCHEMA_SQL` を新しい形（＝新規作成時の正しい形）に直す。
2. `SCHEMA_VERSION` を1つ進める。
3. `migrate(conn)` に「1つ前のバージョンから今の形へ移す」ブロックを追加する。
   既存データは保持し、不足しているカラム/テーブルだけを足すこと。
4. `docs/db_design.md` のテーブル一覧を追随させる。
"""

SCHEMA_VERSION = "5"

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

CREATE TABLE team_capacity_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    start_date TEXT NOT NULL,
    lines INTEGER NOT NULL CHECK (lines >= 1),
    UNIQUE(team_id, start_date)
);

CREATE TABLE holidays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    team_id INTEGER REFERENCES teams(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX ux_holidays_team ON holidays(date, team_id) WHERE team_id IS NOT NULL;

CREATE TABLE workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0
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
    is_active INTEGER NOT NULL DEFAULT 1,
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


def migrate(conn):
    """旧バージョンの.pscheduleファイルを開いた際、不足しているカラム等を
    後から追加する（既存データはそのまま維持し、schema_versionだけ進める）。"""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    version = row["value"] if row else "1"

    if version == "1":
        # v2: workflows.sort_order を追加し、既存の並び順（従来の表示順である
        # 名前順）に基づいて連番を振る。
        conn.execute(
            "ALTER TABLE workflows ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
        )
        rows = conn.execute("SELECT id FROM workflows ORDER BY name").fetchall()
        for i, r in enumerate(rows):
            conn.execute(
                "UPDATE workflows SET sort_order = ? WHERE id = ?", (i, r["id"])
            )
        version = "2"

    if version == "2":
        # v3: job_external_dependencies.is_active を追加（個別のタスク依存を
        # 削除せず一時的に無効化できるようにするため）。既存行はすべて有効。
        # テーブル自体が無い（テスト用の簡略化した旧スキーマ等）場合は何もしない。
        cols = [
            r["name"] for r in
            conn.execute("PRAGMA table_info(job_external_dependencies)").fetchall()
        ]
        if cols and "is_active" not in cols:
            conn.execute(
                "ALTER TABLE job_external_dependencies ADD COLUMN is_active "
                "INTEGER NOT NULL DEFAULT 1"
            )
        version = "3"

    if version == "3":
        # v4: team_capacity_changes を追加（チームの同時ライン数を、
        # 開発開始日からの既定値（teams.max_lines）に加えて、途中の日付から
        # 変動させられるようにするため）。旧ファイルには変更点が無い
        # （＝全期間 teams.max_lines のまま）ものとして扱う。
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'team_capacity_changes'"
        ).fetchone()
        if not exists:
            conn.execute(
                "CREATE TABLE team_capacity_changes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE, "
                "start_date TEXT NOT NULL, "
                "lines INTEGER NOT NULL CHECK (lines >= 1), "
                "UNIQUE(team_id, start_date))"
            )
        version = "4"

    if version == "4":
        # v5: 「個別のタスク依存」を「依存先ジョブ」のリンクに従属させる設計に
        # 統一した（GUI側、gui/tab_jobs.py）。旧バージョンでは依存先ジョブの
        # リンクを作らずに個別のタスク依存だけを追加できたため、対応する
        # job_dependency_links 行が無い (job_id, depends_on_job_id) の組が
        # あれば補完する（新UIでタスク対応が見えなくなることを防ぐため）。
        tables = {
            r["name"] for r in
            conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "job_external_dependencies" in tables and "job_dependency_links" in tables:
            orphans = conn.execute(
                "SELECT DISTINCT d.job_id, d.depends_on_job_id "
                "FROM job_external_dependencies d "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM job_dependency_links l "
                "  WHERE l.job_id = d.job_id AND l.depends_on_job_id = d.depends_on_job_id"
                ")"
            ).fetchall()
            for o in orphans:
                conn.execute(
                    "INSERT INTO job_dependency_links(job_id, depends_on_job_id) VALUES (?, ?)",
                    (o["job_id"], o["depends_on_job_id"]),
                )
        version = "5"

    conn.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'", (version,)
    )
    conn.commit()
