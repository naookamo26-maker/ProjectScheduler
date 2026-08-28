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

SCHEMA_VERSION = "11"

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
    end_date TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    max_lines INTEGER NOT NULL CHECK (max_lines >= 0)
);

CREATE TABLE team_capacity_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    start_date TEXT NOT NULL,
    lines INTEGER NOT NULL CHECK (lines >= 0),
    UNIQUE(team_id, start_date)
);

CREATE TABLE holidays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    team_id INTEGER REFERENCES teams(id) ON DELETE CASCADE,
    note TEXT NOT NULL DEFAULT ''
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
    UNIQUE(workflow_id, name)
);

CREATE TABLE task_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    predecessor_task_id INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    successor_task_id   INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    dep_type TEXT NOT NULL DEFAULT 'FS' CHECK (dep_type IN ('FS', 'SS')),
    lag_days INTEGER NOT NULL DEFAULT 0,
    UNIQUE(predecessor_task_id, successor_task_id),
    CHECK (predecessor_task_id != successor_task_id)
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
    default_milestone_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL,
    priority INTEGER NOT NULL DEFAULT 100 CHECK (priority >= 1),
    tags TEXT NOT NULL DEFAULT ''
);

CREATE TABLE job_task_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    workflow_task_id INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    is_active INTEGER NOT NULL DEFAULT 1,
    override_days INTEGER,
    milestone_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL,
    team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    start_pin_date TEXT,
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

    if version == "5":
        # v6: milestones/holidays に note（備考）を追加。既存行は空文字のまま扱う。
        # テーブル自体が無い（テスト用の簡略化した旧スキーマ等）場合は何もしない。
        for table in ("milestones", "holidays"):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if cols and "note" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        version = "6"

    if version == "6":
        # v7: task_dependencies に dep_type（FS/SS）と lag_days（営業日）を追加。
        # 旧ファイルの依存関係はすべて「FS・ラグ0」＝ 既定値のままで従来と同じ
        # 意味になるため、列を足すだけで移行は完了する。
        cols = [
            r["name"] for r in
            conn.execute("PRAGMA table_info(task_dependencies)").fetchall()
        ]
        if cols and "dep_type" not in cols:
            # ALTER TABLE ADD COLUMN では CHECK 制約を後付けできないため、
            # 値の妥当性は gui/db.py 側（_normalize_dependency_kind）で守る。
            conn.execute(
                "ALTER TABLE task_dependencies ADD COLUMN dep_type TEXT NOT NULL DEFAULT 'FS'"
            )
        if cols and "lag_days" not in cols:
            conn.execute(
                "ALTER TABLE task_dependencies ADD COLUMN lag_days INTEGER NOT NULL DEFAULT 0"
            )
        version = "7"

    if version == "7":
        # v8: task_constraints を追加（日付を「入力」として持つための唯一の場所。
        # タスクに start_date を持たせると計算結果と入力が同じ列に混ざるため、
        # 制約として別テーブルに分離する。docs/architecture.md 参照）。
        # 旧ファイルには制約が1件も無い状態として扱えばよいので、テーブルを
        # 作るだけで移行は完了する。
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_constraints'"
        ).fetchone()
        if not exists:
            conn.execute(
                "CREATE TABLE task_constraints ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, "
                "workflow_task_id INTEGER REFERENCES workflow_tasks(id) ON DELETE CASCADE, "
                "kind TEXT NOT NULL CHECK (kind IN ('SNET', 'SNLT', 'FNLT', 'START_ON')), "
                "date TEXT NOT NULL, "
                "note TEXT NOT NULL DEFAULT '', "
                "UNIQUE(job_id, workflow_task_id, kind))"
            )
            conn.execute(
                "CREATE UNIQUE INDEX ux_task_constraints_job ON task_constraints(job_id, kind) "
                "WHERE workflow_task_id IS NULL"
            )
        version = "8"

    if version == "8":
        # v9: 日付制約(task_constraints)を SNET/SNLT/FNLT ごと廃止し、
        # START_ON だけを job_task_overrides.start_pin_date として残す。
        #
        # 経緯: マイルストーン単位で SNET/SNLT/FNLT を一括設定するテンプレート
        # 機構を検討したが、概念（テンプレート×実効マイルストーンの解決×
        # 優先順位）が実装コストに見合わないと判断して撤回した。個別ジョブ単位の
        # SNET/SNLT/FNLT はジョブ数が増えると設定しきれず使われない機能になる
        # だけなので、START_ON（実績確定・外部都合のピン留め。1ジョブ1タスクに
        # 閉じた意味を持つ）だけを残す。
        #
        # START_ON は「このジョブのこのタスクだけ既定と違う」という他の上書き
        # （override_days等）と性質が同じなので、専用テーブルを持たず
        # job_task_overrides に列を足すだけで「差分のみ保持」パターンにそのまま
        # 乗る。docs/architecture.md「開始固定日（start_pin_date）」参照。
        cols = [
            r["name"] for r in
            conn.execute("PRAGMA table_info(job_task_overrides)").fetchall()
        ]
        if cols and "start_pin_date" not in cols:
            conn.execute(
                "ALTER TABLE job_task_overrides ADD COLUMN start_pin_date TEXT"
            )
        tables = {
            r["name"] for r in
            conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "task_constraints" in tables:
            pins = conn.execute(
                "SELECT job_id, workflow_task_id, date FROM task_constraints "
                "WHERE kind = 'START_ON' AND workflow_task_id IS NOT NULL"
            ).fetchall()
            for p in pins:
                existing = conn.execute(
                    "SELECT id FROM job_task_overrides WHERE job_id = ? AND workflow_task_id = ?",
                    (p["job_id"], p["workflow_task_id"]),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE job_task_overrides SET start_pin_date = ? WHERE id = ?",
                        (p["date"], existing["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO job_task_overrides(job_id, workflow_task_id, "
                        "is_active, start_pin_date) VALUES (?, ?, 1, ?)",
                        (p["job_id"], p["workflow_task_id"], p["date"]),
                    )
            # ジョブ全体（workflow_task_id IS NULL）のSNET/FNLTと、タスク個別の
            # SNET/SNLT/FNLTは、この移行では引き継がない対象（撤回した機能の
            # データ）なので、テーブルごと破棄する。
            conn.execute("DROP TABLE task_constraints")
        version = "9"

    if version == "9":
        # v10: workflow_tasks.canvas_x/canvas_y を削除。ノードグラフの座標は
        # 「保持しておく意味がないデータ」と判断し、表示のたびに依存の深さから
        # 計算し直す方式に統一した（依存テンプレートの疑似ノードが元々この
        # 方式で、以前から座標カラムを持たない。docs/architecture.md参照）。
        # ドラッグでの並べ替えは引き続きできるが、保存されない一時的な
        # ものになる。
        cols = [
            r["name"] for r in
            conn.execute("PRAGMA table_info(workflow_tasks)").fetchall()
        ]
        if "canvas_x" in cols:
            conn.execute("ALTER TABLE workflow_tasks DROP COLUMN canvas_x")
        if "canvas_y" in cols:
            conn.execute("ALTER TABLE workflow_tasks DROP COLUMN canvas_y")
        version = "10"

    if version == "10":
        # v11: jobs.tags を追加（複数タグ、カンマ区切りの1文字列として保持）。
        # 既存ジョブは「タグ無し」（空文字）として扱う。
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        if cols and "tags" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN tags TEXT NOT NULL DEFAULT ''")

        # teams.max_lines / team_capacity_changes.lines の CHECK を
        # 「1以上」から「0以上」に緩和する（早めに引き上げる・遅く合流する
        # チームを、途中区間のライン数0で表現できるようにするため）。
        # SQLiteはALTER TABLEでCHECK制約を直接変更できないため、テーブルを
        # 作り直して既存データを移し替える。テスト用の簡略化した旧スキーマ
        # （該当テーブル/列自体が無い）場合は何もしない。
        #
        # PRAGMA foreign_keys の変更はトランザクションの外でしか効かないため、
        # ここまでの変更（jobs.tags の追加等）を一旦コミットしてから切り替える。
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")

        team_cols = [r["name"] for r in conn.execute("PRAGMA table_info(teams)").fetchall()]
        if team_cols and "max_lines" in team_cols:
            conn.execute(
                "CREATE TABLE teams_new ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT NOT NULL UNIQUE, "
                "max_lines INTEGER NOT NULL CHECK (max_lines >= 0))"
            )
            conn.execute(
                "INSERT INTO teams_new(id, name, max_lines) SELECT id, name, max_lines FROM teams"
            )
            conn.execute("DROP TABLE teams")
            conn.execute("ALTER TABLE teams_new RENAME TO teams")

        capacity_cols = [
            r["name"] for r in
            conn.execute("PRAGMA table_info(team_capacity_changes)").fetchall()
        ]
        if capacity_cols and "lines" in capacity_cols:
            conn.execute(
                "CREATE TABLE team_capacity_changes_new ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE, "
                "start_date TEXT NOT NULL, "
                "lines INTEGER NOT NULL CHECK (lines >= 0), "
                "UNIQUE(team_id, start_date))"
            )
            conn.execute(
                "INSERT INTO team_capacity_changes_new(id, team_id, start_date, lines) "
                "SELECT id, team_id, start_date, lines FROM team_capacity_changes"
            )
            conn.execute("DROP TABLE team_capacity_changes")
            conn.execute("ALTER TABLE team_capacity_changes_new RENAME TO team_capacity_changes")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        version = "11"

    conn.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'", (version,)
    )
    conn.commit()
