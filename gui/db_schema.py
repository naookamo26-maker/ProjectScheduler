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

SCHEMA_VERSION = "18"


class SchemaError(Exception):
    """開いたファイルが、このバージョンのProjectSchedulerでは扱えないと
    判断した場合に送出する（ProjectSchedulerのファイルではない、または
    より新しいバージョンで作成されている）。gui/db.py の open_existing() が
    ProjectDatabaseError に読み替えて送出する（db_schema.py はgui/db.pyの
    例外階層に依存させたくないため、独立した例外として定義する）。"""


def check_openable(conn):
    """ファイルを開いてよいかどうかを、migrate() を呼ぶ前に検証する。

    1. `schema_meta` テーブルが無ければ拒否する。`_SCHEMA_SQL` は
       スキーマv1の時点から常にこのテーブルを含んでいるため、テーブル
       自体が無いことは「ProjectSchedulerのプロジェクトファイルではない」
       ことの強い手がかりになる（別アプリのSQLiteファイル、空ファイル等）。
       検証しないと `no such table: schema_meta` という素のsqlite3例外が
       そのままGUIの「プロジェクトを開けませんでした」ダイアログに出て、
       原因がファイルの中身の問題だと伝わらない。
    2. `schema_version` の行が無ければ拒否する。`_SCHEMA_SQL` は
       スキーマv1の時点から常に`create_new()`でこの行を挿入しているため
       （同梱の実サンプルも例外なく持つ）、テーブルはあるのに行が無い状態は
       実在する正規のファイルには起こらない。かつては「schema_meta導入前の
       最初期のファイル」を想定してバージョン"1"として扱い読み進める
       フォールバックがあったが、これは未検証のまま壊れていた——例えば
       v14で作られたファイルからこの行だけが失われると、v1からの全
       migrationが素通りせず適用され、`workflows.sort_order`のような
       既に存在する列を再度ALTER TABLEしようとして
       `duplicate column name`で例外になる。
    3. `schema_version` が、このアプリが対応する `SCHEMA_VERSION` より
       新しい場合は拒否する。ここで弾かずに読み進めると、未知のカラム・
       テーブルはそのまま素通りしつつGUI側だけが理解できないスキーマに
       対して動き続けてしまい、保存すると新しいバージョンの意味を持つ
       データが（旧バージョンの認識のまま）書き換わる恐れがある。

    戻り値: 検証を通過した場合は何も返さない（None）。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
    ).fetchone()
    if not exists:
        raise SchemaError(
            "ProjectSchedulerのプロジェクトファイル（.pschedule）ではないようです"
            "（schema_metaテーブルが見つかりません）。"
        )
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        raise SchemaError(
            "ProjectSchedulerのプロジェクトファイル（.pschedule）として"
            "壊れています（schema_versionの記録が見つかりません）。"
        )
    version = row["value"]
    try:
        version_num = int(version)
        current_num = int(SCHEMA_VERSION)
    except (TypeError, ValueError):
        raise SchemaError(
            f"schema_versionの値 '{version}' を解釈できません。"
            "ファイルが壊れている可能性があります。"
        ) from None
    if version_num > current_num:
        raise SchemaError(
            "このファイルはより新しいバージョンのProjectSchedulerで作成されています"
            f"（ファイルのバージョン: {version}、このアプリが対応するバージョン: "
            f"{SCHEMA_VERSION}）。アプリを最新版に更新してから開いてください。"
        )


_SCHEMA_SQL = """
CREATE TABLE schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE project (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    project_name TEXT NOT NULL DEFAULT '',
    start_date   TEXT NOT NULL DEFAULT '',
    -- ガントチャートタブの「配置コントロール」で調整する
    -- project_scheduler.py の distribution_ratio（0.0=最速 ASAP 〜
    -- 1.0=ギリギリ ALAP）。既定はスケジューラ本体の既定値と同じ0.7。
    distribution_ratio REAL NOT NULL DEFAULT 0.7,
    -- 計画の確定と再計画（docs/roadmap.md §8）。
    -- replan_base_date: 確定行を持たないタスクをこの日より前に置かない下限
    --   （全面再計画を確定したときに書く。未設定なら開発開始日と同じ扱い）
    -- replanned_at: 全面再計画を実行した日（過去の違反を報告しない境界）
    -- pending_replan_base_date / pending_replanned_at: 全面再計画を実行して、まだ
    --   「変更を確定」していない間の基準日と実行日。確定すると上の2列へ移す
    --   （変更案として保存・破棄できるよう、確定とは別に持つ）
    -- confirmed_at: 最後に確定した日時（状態帯の表示用）
    -- confirmed_global_signature: 確定時の全体設定（休業日・ライン数の推移・
    --   配置コントロール・ジョブの優先度）の指紋。変わったら「変更あり」
    replan_base_date TEXT,
    replanned_at TEXT,
    confirmed_at TEXT,
    confirmed_global_signature TEXT,
    pending_replan_base_date TEXT,
    pending_replanned_at TEXT
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
    -- NULL＝指定なし（上限を設けない。新規チームの既定）、0＝その期間は
    -- 稼働なし、N＝N本。
    max_lines INTEGER CHECK (max_lines IS NULL OR max_lines >= 0)
);

CREATE TABLE team_capacity_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    start_date TEXT NOT NULL,
    lines INTEGER CHECK (lines IS NULL OR lines >= 0),
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
    priority INTEGER CHECK (priority IS NULL OR priority >= 1),
    tags TEXT NOT NULL DEFAULT '',
    -- 作成時に決めて変えない安定キー。スケジューラの配置のばらつきの種にする
    -- （内部IDを種にすると、ジョブを作り直すだけで無関係なジョブまで動くため）。
    stable_key TEXT NOT NULL DEFAULT ''
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
    -- タスク タグ（ジョブ タグ=jobs.tagsと同じ仕様、カンマ区切りの1文字列）。
    tags TEXT NOT NULL DEFAULT '',
    -- 実際の進捗（ユーザーが手動で記録する）。NULL＝未着手、'in_progress'＝
    -- 進行中、'done'＝完了。日付からの推測ではなく、記録された実データ。
    status TEXT,
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

-- 合意した日程（docs/roadmap.md §8-2）。確定したときの計算結果をタスクごとに持つ。
-- end_date は exclusive（計算結果の End_Date と同じ）。days・team_id を持つのは、
-- あとでワークフローを直しても終わった仕事の実績が遡って変わらないようにするため。
-- input_signature は確定時に実際に使われた入力の指紋（§8-7。変わったら「変更あり」）。
CREATE TABLE confirmed_schedule (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    workflow_task_id INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    days INTEGER NOT NULL,
    team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    input_signature TEXT NOT NULL,
    PRIMARY KEY (job_id, workflow_task_id)
);

-- 変更案に入った時点の状態（§8-8。破棄のため）。1行だけ持つ。
CREATE TABLE draft_base (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    started_on TEXT NOT NULL,
    snapshot BLOB NOT NULL
);

-- 変更案の中でガントからドラッグした開始日（§8-8）。確定したら確定行へ書き込んで消す。
CREATE TABLE draft_moves (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    workflow_task_id INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    start_date TEXT NOT NULL,
    PRIMARY KEY (job_id, workflow_task_id)
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

    if version == "11":
        # v12: jobs.priority を「未指定」を許容するよう緩和する。従来は
        # NOT NULL DEFAULT 100 のため新規ジョブは常に「優先度100」で作られて
        # いたが、以後はNULL（未指定）を許し、スケジューリング時に自動的に
        # 最低優先度として扱う（project_scheduler.DEFAULT_LOW_PRIORITY。
        # 元々「未指定なら最低優先」というロジック自体はスケジューラ側に
        # 用意されていたが、DB側が常に非NULLの100を書き込んでいたため
        # 実質使われていなかった）。既存ジョブの値（100を含む）はユーザーが
        # 明示的に設定した値と区別できないため、勝手にNULLへ書き換えない。
        # SQLiteはALTER TABLEでNOT NULL/CHECKを直接変更できないため、
        # テーブルを作り直して既存データを移し替える。
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")

        job_cols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        if job_cols and "priority" in job_cols:
            conn.execute(
                "CREATE TABLE jobs_new ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT NOT NULL UNIQUE, "
                "workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT, "
                "default_milestone_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL, "
                "priority INTEGER CHECK (priority IS NULL OR priority >= 1), "
                "tags TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(
                "INSERT INTO jobs_new(id, name, workflow_id, default_milestone_id, priority, tags) "
                "SELECT id, name, workflow_id, default_milestone_id, priority, tags FROM jobs"
            )
            conn.execute("DROP TABLE jobs")
            conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        version = "12"

    if version == "12":
        # v13: project.distribution_ratio を追加。ガントチャートタブの
        # 「配置コントロール」（スライダー）で調整する project_scheduler.py の
        # distribution_ratio を、これまでタブ内だけの一時パラメータだった
        # ものからプロジェクトの永続設定へ格上げする。既存ファイルは
        # スケジューラ本体の既定値と同じ0.7で補う。
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(project)").fetchall()]
        if cols and "distribution_ratio" not in cols:
            conn.execute(
                "ALTER TABLE project ADD COLUMN distribution_ratio REAL NOT NULL DEFAULT 0.7"
            )
        version = "13"

    if version == "13":
        # v14: job_task_overrides.tags を追加（タスク タグ。jobs.tags＝
        # ジョブ タグと同じ仕様で、こちらはジョブ内の個々のタスクに付ける）。
        # 既存の上書き行はタグ無し（空文字）として扱う。
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(job_task_overrides)").fetchall()]
        if cols and "tags" not in cols:
            conn.execute("ALTER TABLE job_task_overrides ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
        version = "14"

    if version == "14":
        # v15: teams.max_lines / team_capacity_changes.lines の NOT NULL を
        # 外し、NULL＝「指定なし」（上限を設けない）を表現できるようにする
        # （docs/project_analysis_tab_design.md参照）。0＝その期間は稼働なし、
        # N＝N本という既存の意味はそのまま変えない。
        #
        # 既存の値をNULLに書き換えない——「意図して設定した本数」と「既定値
        # のまま放置された本数」を区別する情報が無いため。既存ファイルは
        # 全チーム上限ありのまま開く。
        #
        # SQLiteはALTER TABLEでCHECK制約を直接変更できないため、v11のとき
        # と同じ手順（テーブルを作り直して既存データを移し替える）を使う。
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")

        team_cols = [r["name"] for r in conn.execute("PRAGMA table_info(teams)").fetchall()]
        if team_cols and "max_lines" in team_cols:
            conn.execute(
                "CREATE TABLE teams_new ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT NOT NULL UNIQUE, "
                "max_lines INTEGER CHECK (max_lines IS NULL OR max_lines >= 0))"
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
                "lines INTEGER CHECK (lines IS NULL OR lines >= 0), "
                "UNIQUE(team_id, start_date))"
            )
            conn.execute(
                "INSERT INTO team_capacity_changes_new(id, team_id, start_date, lines) "
                "SELECT id, team_id, start_date, lines FROM team_capacity_changes"
            )
            conn.execute("DROP TABLE team_capacity_changes")
            conn.execute(
                "ALTER TABLE team_capacity_changes_new RENAME TO team_capacity_changes"
            )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        version = "15"

    if version == "15":
        # v16: job_task_overrides.status を追加（実際の進捗をユーザーが手動で
        # 記録する。NULL＝未着手、'in_progress'＝進行中、'done'＝完了）。
        # プロジェクト分析タブの「タスクの状態」KPIは、日付からの推測ではなく
        # この実データを集計する（docs/project_analysis_tab_design.md参照）。
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(job_task_overrides)").fetchall()]
        if cols and "status" not in cols:
            conn.execute("ALTER TABLE job_task_overrides ADD COLUMN status TEXT")
        version = "16"

    if version == "16":
        # v17: 計画の確定と再計画（docs/roadmap.md §8）。
        # - jobs.stable_key: 既存ジョブには今の内部IDの文字列（"JOB_012"）を入れる。
        #   スケジューラはこれまで Job_ID をばらつきの種にしていたので、同じ値に
        #   しておけば移行しただけで日程が動くことは無い。
        # - project の基準日・確定日時等、confirmed_schedule / draft_base /
        #   draft_moves を追加（いずれも空で始まる＝未確定のファイル）。
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        if cols and "stable_key" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN stable_key TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE jobs SET stable_key = printf('JOB_%03d', id)")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(project)").fetchall()]
        for column in ("replan_base_date", "replanned_at", "confirmed_at", "confirmed_global_signature"):
            if cols and column not in cols:
                conn.execute(f"ALTER TABLE project ADD COLUMN {column} TEXT")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS confirmed_schedule ("
            "job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, "
            "workflow_task_id INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE, "
            "start_date TEXT NOT NULL, end_date TEXT NOT NULL, days INTEGER NOT NULL, "
            "team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL, "
            "input_signature TEXT NOT NULL, PRIMARY KEY (job_id, workflow_task_id))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS draft_base ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), started_on TEXT NOT NULL, snapshot BLOB NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS draft_moves ("
            "job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, "
            "workflow_task_id INTEGER NOT NULL REFERENCES workflow_tasks(id) ON DELETE CASCADE, "
            "start_date TEXT NOT NULL, PRIMARY KEY (job_id, workflow_task_id))"
        )
        version = "17"

    if version == "17":
        # v18: 全面再計画を実行中（まだ確定していない）の基準日と実行日
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(project)").fetchall()]
        for column in ("pending_replan_base_date", "pending_replanned_at"):
            if cols and column not in cols:
                conn.execute(f"ALTER TABLE project ADD COLUMN {column} TEXT")
        version = "18"

    conn.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'", (version,)
    )
    conn.commit()
