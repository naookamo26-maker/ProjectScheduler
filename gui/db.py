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

スキーマ定義（DDL）と旧バージョンからのマイグレーションは gui/db_schema.py に
分離している（テーブルを足すたびに伸びる部分を、CRUDの実装から切り離すため）。
"""

import functools
import heapq
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

# 再エクスポート: 呼び出し側・テストからは従来どおり gui.db から参照できるようにする。
from gui.db_schema import SCHEMA_VERSION, _SCHEMA_SQL, migrate as _migrate_schema  # noqa: F401


class ProjectDatabaseError(Exception):
    """DB層で検出した業務エラー（一意性違反・不正な削除など）の基底クラス"""


class DuplicateNameError(ProjectDatabaseError):
    pass


class ReferencedEntityError(ProjectDatabaseError):
    """参照が残っている行を削除しようとした場合"""


def undoable(label):
    """ProjectDatabaseの変更系メソッドに付け、Undo/Redoの記録対象にするデコレータ。

    label は固定文字列、または (self, *args, **kwargs) -> str の callable
    （呼び出し内容に応じたラベルにしたい場合、例:「マイルストーン「本番リリース」を追加」）。

    実体は self.undo_group(label) にメソッド本体を丸ごと委譲するだけ。ネストした
    呼び出し（例: add_job_dependency_link 内から sync_dependency_templates を呼ぶ）は
    undo_group 側のガードにより自動的に1つのUndo単位へまとめられる。

    新しく変更系メソッドを追加する際は必ずこのデコレータを付けること。付け忘れは
    2つの経路で検知される:

    1. 実行時ガード — 付け忘れたメソッドが self._commit() を呼ぶと、Undo管理が
       有効な場面（GUI実行時、およびそれを模したテスト）で例外が送出される。
       ただし self._conn.commit() を直接呼ぶ実装にはこのガードが効かないため、
       変更系メソッドは必ず _commit() を経由すること。
    2. 反射テスト — tests/test_undo_redo.py が、変更系の命名（add_/update_/
       delete_ 等）を持つ公開メソッドすべてに下記の _is_undoable マーカーが
       付いていることを検査する。_commit() を経由しない実装でも検知できる。"""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            resolved_label = label(self, *args, **kwargs) if callable(label) else label
            with self.undo_group(resolved_label):
                return fn(self, *args, **kwargs)

        # 反射テストからデコレータの適用有無を判定するための目印。
        wrapper._is_undoable = True
        return wrapper

    return decorator


# Undoメニューのラベルに使う「名前を引くSQL」。テーブル名をSQL文字列へ
# 組み立てず、あらかじめ用意した文の中からキーで選ぶ形にしている
# （呼び出し元は常に固定の文字列を渡すが、SQLを動的に組み立てる書き方自体を
# 残さないため）。
_ENTITY_NAME_SQL = {
    "milestones": "SELECT name FROM milestones WHERE id = ?",
    "teams": "SELECT name FROM teams WHERE id = ?",
    "workflows": "SELECT name FROM workflows WHERE id = ?",
    "workflow_tasks": "SELECT name FROM workflow_tasks WHERE id = ?",
    "jobs": "SELECT name FROM jobs WHERE id = ?",
}


def _entity_name(conn, table, entity_id):
    """Undoメニューのラベル用に、削除対象の名前を引く小さなヘルパー。
    対象が既に存在しない場合は "?" を返す（ラベルのためだけの処理なので、
    ここで失敗させない）。"""
    row = conn.execute(_ENTITY_NAME_SQL[table], (entity_id,)).fetchone()
    return row["name"] if row else "?"


class ProjectDatabase:
    def __init__(self, path):
        # pathがNoneの場合は「まだ保存先未定の新規プロジェクト」を表す
        # （初回保存時にsave_as相当でパスが確定する）。
        self.path = str(path) if path is not None else None
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._dirty = False
        # GUI側から差し込む変更通知フック（タイトルバーの未保存マーク更新等に使う）。
        # 引数なしで呼ばれる callable、または None。
        self.on_change = None
        # GUI側から差し込む gui.undo_manager.UndoManager（またはNone）。
        # None の間は undo_group が完全に無効化され、通常のCRUDとして動作する
        # （Qt非依存のテスト等、GUIを介さない利用を妨げないため）。
        self.undo_manager = None
        # undo_group のネスト検知用（Trueの間は既に外側でスナップショットを
        # 取得済みなので、内側の呼び出しでは何もしない）。
        self._in_undoable_call = False
        # Undo/Redoの適用中（suspend_undo_recording）かどうか。
        self._undo_suppressed = False
        # begin_undo_group() で開いたまま保持している単位。
        # (ラベル, 操作前のDBスナップショット, 操作前のUI状態) または None。
        self._open_group = None

    def close(self):
        self._conn.close()

    def serialize_state(self):
        """メモリ上のDB全体をバイト列として取り出す（Undo用スナップショット）。"""
        return self._conn.serialize()

    def restore_state(self, blob):
        """serialize_state() が返したバイト列で、メモリ上のDB内容を丸ごと置き換える
        （Undo/Redoの実行時にのみ呼ぶ）。通常の変更系メソッドと同様、未保存フラグを
        立て、変更通知フックを呼ぶ。"""
        self._conn.deserialize(blob)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._dirty = True
        self._notify_change()

    @contextmanager
    def undo_group(self, label):
        """このwithブロック内でのDBへの変更をまとめて1つのUndo単位として記録する。

        GUI側で複数のDB呼び出しにまたがる1つのユーザー操作（例: タスク追加後の
        自動レイアウト）をまとめたい場合に明示的に使う。@undoable デコレータも
        内部的にこれを使っており、既に外側でこのブロックに入っている場合
        （ネストした呼び出し）は何もしない——外側の呼び出しが取得したスナップショットに
        自動的に合流する。

        undo_manager が未設定（GUIを介さない利用）の場合、
        suspend_undo_recording() の内側（Undo/Redoの適用中）の場合、および
        begin_undo_group() で開いた単位の内側の場合は素通しする。

        ブロックの内容が実際にDBを変えなかった場合はUndoエントリを積まない
        （重複名エラー等で何も変わらずに終わった操作で、空のUndoが増えるのを
        防ぐ）。逆に、例外で中断した場合でも、その時点までに実際にコミット
        された変更が残っていればUndoエントリを積む——取り消せない変更が
        残ってしまう方が、余分なUndoエントリより有害なため。"""
        if (self.undo_manager is None or self._in_undoable_call
                or self._undo_suppressed or self._open_group is not None):
            yield
            return
        self._in_undoable_call = True
        before_db = self.serialize_state()
        before_ui = self.undo_manager.capture_ui_state()
        try:
            yield
        finally:
            self._in_undoable_call = False
            after_db = self.serialize_state()
            if after_db != before_db:
                self.undo_manager.push(before_db, before_ui, after_db, label)

    def begin_undo_group(self, label):
        """複数のイベントにまたがる編集を1つのUndo単位にまとめ始める。
        end_undo_group() を呼ぶまで、その間のすべての変更が1エントリに合流する。

        スピンボックスや日付欄のように、1回の編集で値が何度も変わる入力のために
        用意している。これらは変更のたびにDBへ書き込むが（＝DBは常に最新なので、
        値を変えた直後に保存しても取りこぼさない）、Undoの単位まで変更ごとに
        分かれると「▲を5回押したのに5回Undoしないと戻らない」ことになる。
        フォーカスを得た時に開き、外れた時に閉じることで、1回の編集＝1回のUndoに
        なる（gui/widgets_common.py の bind_undo_session）。

        既に開いている単位があれば先に確定する。値が結局変わらなければ、
        閉じる時に何も記録しない。"""
        if self.undo_manager is None or self._undo_suppressed or self._in_undoable_call:
            return
        self.end_undo_group()
        self._open_group = (label, self.serialize_state(), self.undo_manager.capture_ui_state())

    def end_undo_group(self):
        """begin_undo_group() で開いた単位を確定する（開いていなければ何もしない）。
        実際に内容が変わってUndoエントリを積んだ場合は True、変わらなかった
        （開いてすらいなかった場合を含む）場合は False を返す——
        gui/widgets_common.py の bind_undo_session が、フォーカスが素通り
        しただけ（値は変わっていない）で on_session_end を呼ばないようにする
        ために使う。"""
        if self._open_group is None:
            return False
        label, before_db, before_ui = self._open_group
        self._open_group = None
        after_db = self.serialize_state()
        if after_db != before_db:
            self.undo_manager.push(before_db, before_ui, after_db, label)
            return True
        return False

    @contextmanager
    def suspend_undo_recording(self):
        """このブロック内でのDBへの変更を、新しいUndoエントリとして記録しない。

        Undo/Redoの適用中（gui/undo_manager.py）に使う。適用処理は、スナップ
        ショットの復元に続けてGUI側の再読込（各タブの refresh_choices()）まで
        行うが、この再読込がDBに書き込むことがある——現に
        gui/tab_jobs.py の refresh_choices() は sync_dependency_templates() を
        呼ぶ。抑止しないと、その書き込みが「ユーザーの新しい操作」として
        Undoスタックに積まれ、同時にRedoスタックが破棄されてしまう
        （＝Undoした直後にRedoできなくなる）。

        今後追加するタブの refresh_choices() が同様にDBへ書き込む場合も、
        この抑止によって自動的に守られる。"""
        previous = self._undo_suppressed
        self._undo_suppressed = True
        try:
            yield
        finally:
            self._undo_suppressed = previous

    # -- 生成/オープン -----------------------------------------------------

    @classmethod
    def create_new(cls, path=None):
        """新規プロジェクトを（メモリ上に）作成する。この時点ではまだファイルへの
        書き込みは行わない——ユーザーが保存（Ctrl+S）するまでディスク上には
        何も生成されない。path省略時（GUIの「新規プロジェクト」）は保存先未定
        のまま開き、初回保存時にパスを選ばせる。"""
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
        _migrate_schema(db._conn)
        db._dirty = False
        return db

    # -- 保存（明示的） -----------------------------------------------------

    def is_dirty(self):
        """前回の保存（またはオープン/新規作成）以降に変更があれば True。

        Undo管理が有効な場合は、単純な「変更したか」のフラグではなく、Undo履歴
        上の位置で判定する（gui/undo_manager.py の is_clean）。変更→保存→変更→
        Undo と操作して保存時点の内容に戻った場合、未保存マークが消える。"""
        if self.undo_manager is not None:
            return not self.undo_manager.is_clean()
        return self._dirty

    def _mark_saved(self):
        self._dirty = False
        if self.undo_manager is not None:
            self.undo_manager.mark_clean()
        self._notify_change()

    def save(self):
        """メモリ上の内容を self.path のファイルへ書き出す。保存先未定
        （path未確定の新規プロジェクト）の場合は呼び出し側の責務漏れなので
        明示的に失敗させる（GUI側はこの場合save_as相当のパス選択に迂回する）。"""
        if self.path is None:
            raise ProjectDatabaseError("保存先が未設定です（save_asでパスを指定してください）")
        # 編集途中（スピンボックスにフォーカスが残ったまま等）で保存された場合、
        # その編集を先に1つのUndo単位として確定させる。確定させないまま
        # 「保存済み」を記録すると、後でフォーカスが外れてエントリが積まれた
        # 瞬間に、内容は保存済みなのに未保存マークが付いてしまう。
        self.end_undo_group()
        self._write_to(self.path)
        self._mark_saved()

    def save_as(self, new_path):
        """メモリ上の内容を new_path へ書き出し、以降そのパスを対象とする。"""
        self.end_undo_group()
        self._write_to(new_path)
        self.path = str(new_path)
        self._mark_saved()

    def _write_to(self, path):
        """同じフォルダの一時ファイルへ書き出し、成功してから os.replace() で
        本来の名前に置き換える。

        書き込み先を先に削除してから書くと、途中で失敗した場合（ディスク満杯・
        権限エラー・書き込み中のクラッシュ等）に、保存済みの内容ごと失われて
        しまう。Undo履歴はメモリ上にしか無いため、こうなると復旧手段が無い。
        一時ファイル経由なら、失敗しても既存の保存済みファイルは無傷のまま残る。

        一時ファイルを同じフォルダに作るのは、os.replace() が同一ファイル
        システム上でしか原子的に置き換えられないため（テンポラリ領域が別の
        ドライブにあると保証が崩れる）。"""
        p = Path(path)
        fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
        os.close(fd)  # sqlite3が自分で開き直すため、ここではファイル名の確保だけが目的
        try:
            dest_conn = sqlite3.connect(tmp_name)
            try:
                self._conn.backup(dest_conn)
            finally:
                dest_conn.close()
            os.replace(tmp_name, str(p))
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _commit(self):
        """変更系メソッドの末尾から呼ぶ内部コミット。メモリ上のトランザクションを
        確定し、未保存フラグを立てて変更通知フックを呼ぶ（ファイルへの書き込みは
        行わない——それは save()/save_as() の役目）。

        undo_manager が設定されている場合、@undoable / undo_group の外側から
        呼ばれると例外を送出する。変更系メソッドに @undoable を付け忘れると
        ここで即座に検知できるようにするためのガード（Undo/Redoの記録漏れ防止）。
        Undo/Redoの適用中（suspend_undo_recording）はそもそも記録しない区間
        なので、このガードも見送る。begin_undo_group() で開いた単位の内側も
        同様に記録済みとして扱う。"""
        if (self.undo_manager is not None and not self._in_undoable_call
                and not self._undo_suppressed and self._open_group is None):
            raise AssertionError(
                f"{type(self).__name__}._commit() が @undoable / undo_group の外側から呼ばれました。"
                "変更系メソッドには @undoable(\"ラベル\") を付けてください。"
            )
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

    @undoable("プロジェクト概要を変更")
    def set_project(self, project_name, start_date):
        self._conn.execute(
            "UPDATE project SET project_name = ?, start_date = ? WHERE id = 1",
            (project_name, start_date),
        )
        self._commit()

    # -- milestones ---------------------------------------------------------

    def list_milestones(self):
        rows = self._conn.execute(
            "SELECT id, name, end_date, note FROM milestones ORDER BY end_date, name"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_milestone(self, milestone_id):
        row = self._conn.execute(
            "SELECT id, name, end_date, note FROM milestones WHERE id = ?", (milestone_id,)
        ).fetchone()
        return dict(row) if row else None

    @undoable(lambda self, name, end_date, note="": f"マイルストーン「{name}」を追加")
    def add_milestone(self, name, end_date, note=""):
        try:
            cur = self._conn.execute(
                "INSERT INTO milestones(name, end_date, note) VALUES (?, ?, ?)",
                (name, end_date, note),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"マイルストーン名 '{name}' は既に使用されています") from e
        self._commit()
        return cur.lastrowid

    @undoable(lambda self, milestone_id, name, end_date, note="": f"マイルストーン「{name}」を変更")
    def update_milestone(self, milestone_id, name, end_date, note=""):
        try:
            self._conn.execute(
                "UPDATE milestones SET name = ?, end_date = ?, note = ? WHERE id = ?",
                (name, end_date, note, milestone_id),
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

    @undoable(lambda self, milestone_id: f"マイルストーン「{_entity_name(self._conn, 'milestones', milestone_id)}」を削除")
    def delete_milestone(self, milestone_id):
        self._conn.execute("DELETE FROM milestones WHERE id = ?", (milestone_id,))
        self._commit()

    # -- teams ---------------------------------------------------------------

    def list_teams(self):
        rows = self._conn.execute(
            "SELECT id, name, max_lines FROM teams ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    @undoable(lambda self, name, max_lines: f"チーム「{name}」を追加")
    def add_team(self, name, max_lines):
        try:
            cur = self._conn.execute(
                "INSERT INTO teams(name, max_lines) VALUES (?, ?)", (name, max_lines)
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"チーム名 '{name}' は既に使用されています") from e
        self._commit()
        return cur.lastrowid

    @undoable(lambda self, team_id, name, max_lines: f"チーム「{name}」を変更")
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

    @undoable(lambda self, team_id: f"チーム「{_entity_name(self._conn, 'teams', team_id)}」を削除")
    def delete_team(self, team_id):
        if self.team_usage_count(team_id) > 0:
            raise ReferencedEntityError("このチームはタスクに使用されているため削除できません")
        self._conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        self._commit()

    # -- team_capacity_changes（チームの同時ライン数の期間変動） ------------------------
    #
    # teams.max_lines は「開発開始日からの既定値」。それ以降、ライン数が変わる
    # 日付があれば、この表に (start_date, lines) の変更点として追加する
    # （teams.max_lines 自体はいつまでも「最初の期間」の値として残る）。

    def list_team_capacity_changes(self, team_id):
        rows = self._conn.execute(
            "SELECT id, team_id, start_date, lines FROM team_capacity_changes "
            "WHERE team_id = ? ORDER BY start_date",
            (team_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @undoable(lambda self, team_id, start_date, lines: f"同時ライン数の変更点（{start_date}）を追加")
    def add_team_capacity_change(self, team_id, start_date, lines):
        try:
            cur = self._conn.execute(
                "INSERT INTO team_capacity_changes(team_id, start_date, lines) "
                "VALUES (?, ?, ?)",
                (team_id, start_date, lines),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(
                f"この日付（{start_date}）の変更点は既に登録されています"
            ) from e
        self._commit()
        return cur.lastrowid

    @undoable(lambda self, change_id, start_date, lines: f"同時ライン数の変更点（{start_date}）を変更")
    def update_team_capacity_change(self, change_id, start_date, lines):
        try:
            self._conn.execute(
                "UPDATE team_capacity_changes SET start_date = ?, lines = ? WHERE id = ?",
                (start_date, lines, change_id),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(
                f"この日付（{start_date}）の変更点は既に登録されています"
            ) from e
        self._commit()

    @undoable("同時ライン数の変更点を削除")
    def delete_team_capacity_change(self, change_id):
        self._conn.execute("DELETE FROM team_capacity_changes WHERE id = ?", (change_id,))
        self._commit()

    # -- holidays --------------------------------------------------------------

    def list_holidays(self):
        rows = self._conn.execute(
            "SELECT h.id, h.date, h.team_id, h.note, t.name AS team_name "
            "FROM holidays h LEFT JOIN teams t ON t.id = h.team_id "
            "ORDER BY h.date"
        ).fetchall()
        return [dict(r) for r in rows]

    @undoable(lambda self, date, team_id=None, note="": f"休業日（{date}）を追加")
    def add_holiday(self, date, team_id=None, note=""):
        exists = self._conn.execute(
            "SELECT 1 FROM holidays WHERE date = ? AND team_id IS ?", (date, team_id)
        ).fetchone()
        if exists:
            raise DuplicateNameError("同じ日付・チームの休業日が既に登録されています")
        cur = self._conn.execute(
            "INSERT INTO holidays(date, team_id, note) VALUES (?, ?, ?)", (date, team_id, note)
        )
        self._commit()
        return cur.lastrowid

    @undoable(lambda self, holiday_id, date, team_id=None, note="": f"休業日（{date}）を変更")
    def update_holiday(self, holiday_id, date, team_id=None, note=""):
        exists = self._conn.execute(
            "SELECT 1 FROM holidays WHERE date = ? AND team_id IS ? AND id != ?",
            (date, team_id, holiday_id),
        ).fetchone()
        if exists:
            raise DuplicateNameError("同じ日付・チームの休業日が既に登録されています")
        self._conn.execute(
            "UPDATE holidays SET date = ?, team_id = ?, note = ? WHERE id = ?",
            (date, team_id, note, holiday_id),
        )
        self._commit()

    @undoable("休業日を削除")
    def delete_holiday(self, holiday_id):
        self._conn.execute("DELETE FROM holidays WHERE id = ?", (holiday_id,))
        self._commit()

    # -- workflows ----------------------------------------------------------

    def list_workflows(self):
        rows = self._conn.execute(
            "SELECT id, name, sort_order FROM workflows ORDER BY sort_order, id"
        ).fetchall()
        return [dict(r) for r in rows]

    @undoable(lambda self, name: f"ワークフロー「{name}」を追加")
    def add_workflow(self, name):
        next_order = self._conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM workflows"
        ).fetchone()["n"]
        try:
            cur = self._conn.execute(
                "INSERT INTO workflows(name, sort_order) VALUES (?, ?)", (name, next_order)
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"ワークフロー名 '{name}' は既に使用されています") from e
        self._commit()
        return cur.lastrowid

    @undoable("ワークフローの並び順を変更")
    def reorder_workflows(self, ordered_workflow_ids):
        """左のワークフロー一覧をドラッグで並び替えた結果を反映する。
        ordered_workflow_ids は表示させたい順のワークフローIDの並び。"""
        for i, workflow_id in enumerate(ordered_workflow_ids):
            self._conn.execute(
                "UPDATE workflows SET sort_order = ? WHERE id = ?", (i, workflow_id)
            )
        self._commit()

    @undoable(lambda self, workflow_id, name: f"ワークフロー名を「{name}」に変更")
    def rename_workflow(self, workflow_id, name):
        try:
            self._conn.execute(
                "UPDATE workflows SET name = ? WHERE id = ?", (name, workflow_id)
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"ワークフロー名 '{name}' は既に使用されています") from e
        self._commit()

    @undoable(lambda self, workflow_id: f"ワークフロー「{_entity_name(self._conn, 'workflows', workflow_id)}」を複製")
    def duplicate_workflow(self, workflow_id):
        """ワークフロー1件を、配下のタスク・タスク間依存・このワークフロー自身が
        持つ依存テンプレート（他ワークフローへの依存）ごと複製する。

        複製しないもの:
        - ジョブ・タスク上書き・ジョブ間依存（ジョブは「ジョブ作成」タブで
          ワークフローを実体化した別個のデータであり、ワークフロー定義の
          複製に含めるべきではないため）。
        - 他のワークフローが「このワークフローに依存する」側として持つ
          依存テンプレート（複製先を勝手に他ワークフローの依存先に加えると、
          意図しない副作用になるため）。

        新しいワークフローは元の直後（表示順で隣）に挿入する。名前は
        「元の名前のコピー」を既定とし、衝突する場合は連番を付与する。"""
        row = self._conn.execute(
            "SELECT name, sort_order FROM workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise ProjectDatabaseError("複製元のワークフローが見つかりません")

        # 「追加」ボタン連打時の衝突回避（gui/widgets_common.py の
        # unique_default_name）と同じ考え方だが、db.py はQt非依存を保つため
        # ここでは同じロジックをそのまま持つ（widgets_common.py はPySide6に
        # 依存しており、db.py からは import できない）。
        existing_names = {
            r["name"] for r in self._conn.execute("SELECT name FROM workflows").fetchall()
        }
        base_name = f"{row['name']}のコピー"
        new_name = base_name
        n = 2
        while new_name in existing_names:
            new_name = f"{base_name} ({n})"
            n += 1

        self._conn.execute(
            "UPDATE workflows SET sort_order = sort_order + 1 WHERE sort_order > ?",
            (row["sort_order"],),
        )
        cur = self._conn.execute(
            "INSERT INTO workflows(name, sort_order) VALUES (?, ?)",
            (new_name, row["sort_order"] + 1),
        )
        new_workflow_id = cur.lastrowid

        task_id_map = {}
        for t in self._conn.execute(
            "SELECT id, name, team_id, default_days, canvas_x, canvas_y "
            "FROM workflow_tasks WHERE workflow_id = ? ORDER BY id",
            (workflow_id,),
        ).fetchall():
            cur = self._conn.execute(
                "INSERT INTO workflow_tasks(workflow_id, name, team_id, default_days, "
                "canvas_x, canvas_y) VALUES (?, ?, ?, ?, ?, ?)",
                (new_workflow_id, t["name"], t["team_id"], t["default_days"],
                 t["canvas_x"], t["canvas_y"]),
            )
            task_id_map[t["id"]] = cur.lastrowid

        for d in self._conn.execute(
            "SELECT predecessor_task_id, successor_task_id FROM task_dependencies "
            "WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall():
            self._conn.execute(
                "INSERT INTO task_dependencies(workflow_id, predecessor_task_id, "
                "successor_task_id) VALUES (?, ?, ?)",
                (new_workflow_id, task_id_map[d["predecessor_task_id"]],
                 task_id_map[d["successor_task_id"]]),
            )

        # 依存先（depends_on_workflow_id/depends_on_workflow_task_id）は他
        # ワークフローのタスクをそのまま指すため、新しいワークフローのタスクへの
        # 付け替えは不要（複製元と同じ相手に依存する形で複製する）。
        for tpl in self._conn.execute(
            "SELECT workflow_task_id, depends_on_workflow_id, depends_on_workflow_task_id "
            "FROM workflow_dependency_templates WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall():
            self._conn.execute(
                "INSERT INTO workflow_dependency_templates(workflow_id, workflow_task_id, "
                "depends_on_workflow_id, depends_on_workflow_task_id) VALUES (?, ?, ?, ?)",
                (new_workflow_id, task_id_map[tpl["workflow_task_id"]],
                 tpl["depends_on_workflow_id"], tpl["depends_on_workflow_task_id"]),
            )

        self._commit()
        return new_workflow_id

    def workflow_usage_count(self, workflow_id):
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        return row["n"]

    @undoable(lambda self, workflow_id: f"ワークフロー「{_entity_name(self._conn, 'workflows', workflow_id)}」を削除")
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

    @undoable(lambda self, workflow_id, name, team_id, default_days, x=0.0, y=0.0: f"タスク「{name}」を追加")
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

    @undoable(lambda self, task_id, name, team_id, default_days: f"タスク「{name}」を変更")
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

    @undoable("タスクの位置を変更")
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

    @undoable(lambda self, task_id: f"タスク「{_entity_name(self._conn, 'workflow_tasks', task_id)}」を削除")
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

    @undoable("依存関係を追加")
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

    @undoable("依存関係を削除")
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

    @undoable(lambda self, name, workflow_id, default_milestone_id, priority: f"ジョブ「{name}」を追加")
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

    @undoable(lambda self, job_id, name, workflow_id, default_milestone_id, priority: f"ジョブ「{name}」を変更")
    def update_job(self, job_id, name, workflow_id, default_milestone_id, priority):
        old = self._conn.execute(
            "SELECT workflow_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        workflow_changed = old is not None and old["workflow_id"] != workflow_id
        try:
            self._conn.execute(
                "UPDATE jobs SET name = ?, workflow_id = ?, default_milestone_id = ?, "
                "priority = ? WHERE id = ?",
                (name, workflow_id, default_milestone_id, priority, job_id),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(f"ジョブ名 '{name}' は既に使用されています") from e
        self._commit()
        if workflow_changed:
            # ワークフローの組み合わせが変わると、依存先ジョブのタスク対応が
            # 参照すべきテンプレートも変わるため、最新の状態へ同期し直す。
            self.sync_dependency_templates()

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

    @undoable(lambda self, job_id: f"ジョブ「{_entity_name(self._conn, 'jobs', job_id)}」を削除")
    def delete_job(self, job_id):
        self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        self._commit()

    # -- job_task_overrides -------------------------------------------------------

    def _topological_task_order(self, workflow_id):
        """ワークフロー内のタスクIDを、依存関係上で先行タスクが必ず前に来る順
        （トポロジカル順）に並べたリストで返す。依存関係の無いタスク同士は
        タスクID順（＝登録順）を保つ（優先度付きKahn法、ヒープのタイブレークに
        タスクIDそのものを使う）。循環依存は他所（node_canvas.py側）で事前に
        防止されている前提だが、万一取りこぼした場合も元のID順で末尾に補う。"""
        task_ids = [
            r["id"] for r in self._conn.execute(
                "SELECT id FROM workflow_tasks WHERE workflow_id = ? ORDER BY id",
                (workflow_id,),
            ).fetchall()
        ]
        preds_map = self._task_predecessors_map(workflow_id)
        succ_map = self._task_successors_map(workflow_id)
        indegree = {tid: len(preds_map.get(tid, [])) for tid in task_ids}

        heap = [tid for tid in task_ids if indegree[tid] == 0]
        heapq.heapify(heap)
        order = []
        while heap:
            tid = heapq.heappop(heap)
            order.append(tid)
            for succ in succ_map.get(tid, []):
                indegree[succ] -= 1
                if indegree[succ] == 0:
                    heapq.heappush(heap, succ)

        ordered_set = set(order)
        order.extend(tid for tid in task_ids if tid not in ordered_set)
        return order

    def list_job_tasks_with_overrides(self, job_id):
        """選択ジョブのワークフローが持つ全タスクを、上書き情報（あれば）付きで返す。
        タスクの並びは依存関係上のトポロジカル順（先行タスクが上に来る）。"""
        job = self._conn.execute("SELECT workflow_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return []
        order = self._topological_task_order(job["workflow_id"])
        order_index = {tid: i for i, tid in enumerate(order)}

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
            "WHERE j.id = ?",
            (job_id,),
        ).fetchall()
        result = [dict(r) for r in rows]
        result.sort(key=lambda r: order_index.get(r["workflow_task_id"], len(order)))
        return result

    @undoable("タスク上書きを変更")
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

    @undoable("タスク上書きを既定に戻す")
    def clear_job_task_override(self, job_id, workflow_task_id):
        """タスクが既定値に戻った場合、上書き行自体を削除する（差分のみ保持）。"""
        self._conn.execute(
            "DELETE FROM job_task_overrides WHERE job_id = ? AND workflow_task_id = ?",
            (job_id, workflow_task_id),
        )
        self._commit()

    # -- マイルストーンの整合性（先行/後続タスク間） -----------------------------------
    # 「後継タスクが先行タスクのマイルストーンより早まらないようにする」ための
    # ヘルパー群。先行/後続の対象は同一ワークフロー内のtask_dependenciesのみ
    # （ジョブをまたぐ依存 job_external_dependencies は対象外）。

    def effective_milestone(self, job_id, workflow_task_id):
        """あるジョブの特定タスクが実際に使うマイルストーンを返す
        （{"milestone_id":.., "end_date":..}、上書きがあればそれ、無ければ
        ジョブの既定マイルストーン。どちらも未設定ならNone）。"""
        row = self._conn.execute(
            "SELECT COALESCE(o.milestone_id, j.default_milestone_id) AS milestone_id "
            "FROM jobs j LEFT JOIN job_task_overrides o "
            "ON o.job_id = j.id AND o.workflow_task_id = ? "
            "WHERE j.id = ?",
            (workflow_task_id, job_id),
        ).fetchone()
        if row is None or row["milestone_id"] is None:
            return None
        ms = self._conn.execute(
            "SELECT id, end_date FROM milestones WHERE id = ?", (row["milestone_id"],)
        ).fetchone()
        if ms is None:
            return None
        return {"milestone_id": ms["id"], "end_date": ms["end_date"]}

    def _task_predecessors_map(self, workflow_id):
        """{successor_task_id: [predecessor_task_id, ...]}"""
        m = {}
        for d in self.list_task_dependencies(workflow_id):
            m.setdefault(d["successor_task_id"], []).append(d["predecessor_task_id"])
        return m

    def _task_successors_map(self, workflow_id):
        """{predecessor_task_id: [successor_task_id, ...]}"""
        m = {}
        for d in self.list_task_dependencies(workflow_id):
            m.setdefault(d["predecessor_task_id"], []).append(d["successor_task_id"])
        return m

    def minimum_milestone_end_date(self, job_id, workflow_task_id):
        """このタスクに設定してよいマイルストーンのend_dateの下限
        （＝直接の先行タスクの実効マイルストーンend_dateのうち最も遅いもの）を
        返す。先行タスクが無い、またはどの先行タスクにもマイルストーンが
        未設定なら None（下限なし）。"""
        job = self._conn.execute("SELECT workflow_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        preds = self._task_predecessors_map(job["workflow_id"]).get(workflow_task_id, [])
        end_dates = []
        for pred_id in preds:
            ms = self.effective_milestone(job_id, pred_id)
            if ms is not None:
                end_dates.append(ms["end_date"])
        return max(end_dates) if end_dates else None

    @undoable("マイルストーンを後続タスクへ反映")
    def cascade_milestone_to_successors(self, job_id, workflow_task_id):
        """workflow_task_idのマイルストーンを変更した直後に呼ぶ。後続タスク
        （transitively）の実効マイルストーンが、このタスクの実効マイルストーン
        より早い場合、後続タスクのマイルストーン上書きをこのタスクと同じ
        マイルストーンに自動的に合わせる。変更した後続タスクのworkflow_task_id
        一覧を返す（GUI側の通知用。空リストなら調整不要だった）。"""
        job = self._conn.execute("SELECT workflow_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        this_ms = self.effective_milestone(job_id, workflow_task_id)
        if this_ms is None:
            return []

        succ_map = self._task_successors_map(job["workflow_id"])
        changed = []
        queue = list(succ_map.get(workflow_task_id, []))
        seen = set()
        while queue:
            succ_id = queue.pop(0)
            if succ_id in seen:
                continue
            seen.add(succ_id)
            succ_ms = self.effective_milestone(job_id, succ_id)
            if succ_ms is None or succ_ms["end_date"] < this_ms["end_date"]:
                existing = self._conn.execute(
                    "SELECT is_active, override_days, team_id FROM job_task_overrides "
                    "WHERE job_id = ? AND workflow_task_id = ?",
                    (job_id, succ_id),
                ).fetchone()
                self.upsert_job_task_override(
                    job_id, succ_id,
                    is_active=bool(existing["is_active"]) if existing else True,
                    override_days=existing["override_days"] if existing else None,
                    milestone_id=this_ms["milestone_id"],
                    team_id=existing["team_id"] if existing else None,
                )
                changed.append(succ_id)
                queue.extend(succ_map.get(succ_id, []))
        return changed

    @undoable("マイルストーンを先行タスクに合わせて調整")
    def enforce_milestone_floor(self, job_id, workflow_task_id):
        """workflow_task_idの上書きを変更・解除した直後に呼ぶ。先行タスク
        （同一ワークフロー内、直接のみ）の実効マイルストーンより、このタスク
        自身の実効マイルストーンが早くなってしまった場合（既定に戻した結果
        早いマイルストーンに戻った場合を含む）、先行タスクのうち最も遅い
        実効マイルストーンに自動的に引き上げる。実際に引き上げた場合は
        Trueを返す（GUI側の通知用）。先行タスクが無い、またはどの先行タスクにも
        マイルストーンが未設定なら何もせず False。"""
        job = self._conn.execute("SELECT workflow_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        preds = self._task_predecessors_map(job["workflow_id"]).get(workflow_task_id, [])
        floor = None
        for pred_id in preds:
            pred_ms = self.effective_milestone(job_id, pred_id)
            if pred_ms is not None and (floor is None or pred_ms["end_date"] > floor["end_date"]):
                floor = pred_ms
        if floor is None:
            return False

        this_ms = self.effective_milestone(job_id, workflow_task_id)
        if this_ms is not None and this_ms["end_date"] >= floor["end_date"]:
            return False

        existing = self._conn.execute(
            "SELECT is_active, override_days, team_id FROM job_task_overrides "
            "WHERE job_id = ? AND workflow_task_id = ?",
            (job_id, workflow_task_id),
        ).fetchone()
        self.upsert_job_task_override(
            job_id, workflow_task_id,
            is_active=bool(existing["is_active"]) if existing else True,
            override_days=existing["override_days"] if existing else None,
            milestone_id=floor["milestone_id"],
            team_id=existing["team_id"] if existing else None,
        )
        return True

    # -- マイルストーン整合性の一括検査・再調整 -------------------------------------
    #
    # enforce_milestone_floor / cascade_milestone_to_successors は「タブ3で
    # 上書きを編集した瞬間」にしか走らないため、次の2つの経路では不変条件
    # （先行タスクの実効マイルストーン <= 後続タスクの実効マイルストーン）が
    # 後から破れてしまう。
    #
    # 1. タブ1でマイルストーンの締切日を変え、既存の前後関係が入れ替わる
    # 2. タブ2で、既にジョブ側でマイルストーンを設定済みのタスク間に
    #    新しい依存関係を追加する（マイルストーンの日付は変えていない）
    #
    # どちらも「不変条件の判定材料（日付・依存グラフ）だけが後から変わる」
    # ケースで、上書き自体は触られないため既存の再調整が発火しない。
    # plan_/apply_ の2段構えにしているのは、実行前に「何がどう調整されるか」を
    # ユーザーへ提示して確認を取るため（GUI側で確認ダイアログを出す）。

    def _milestone_repair_plan_for_job(self, job):
        """1ジョブ分の再調整計画。enforce_milestone_floor と同じ判定
        （実効マイルストーンが未設定、または先行タスクの最も遅い実効
        マイルストーンより早ければ、そこまで引き上げる）を、依存の深さ順に
        伝播させて計算する。書き込みは一切行わない。"""
        wf_id = job["workflow_id"]
        tasks = self.list_workflow_tasks(wf_id)
        preds = self._task_predecessors_map(wf_id)

        # 先行タスクが必ず先に確定するよう、依存の深さ順に処理する
        # （gui/node_canvas.py の compute_task_depths と同じ考え方。db.pyは
        # Qt非依存に保つため、共有せずここに小さく持つ）。
        depth = {}

        def calc_depth(tid, path):
            if tid in depth:
                return depth[tid]
            if tid in path:  # 循環がある場合の保険（通常はキャンバス側で防止済み）
                return 0
            ps = preds.get(tid, [])
            depth[tid] = max((calc_depth(p, path | {tid}) for p in ps), default=-1) + 1
            return depth[tid]

        for t in tasks:
            calc_depth(t["id"], set())

        effective = {}
        for t in tasks:
            ms = self.effective_milestone(job["id"], t["id"])
            effective[t["id"]] = (ms["end_date"], ms["milestone_id"]) if ms else None

        plan = []
        for t in sorted(tasks, key=lambda t: depth[t["id"]]):
            tid = t["id"]
            floor = None
            for p in preds.get(tid, []):
                pv = effective.get(p)
                if pv is not None and (floor is None or pv[0] > floor[0]):
                    floor = pv
            if floor is None:
                continue
            current = effective.get(tid)
            if current is not None and current[0] >= floor[0]:
                continue
            plan.append({
                "job_id": job["id"], "job_name": job["name"],
                "workflow_task_id": tid, "task_name": t["name"],
                "from_end_date": current[0] if current else None,
                "to_end_date": floor[0], "to_milestone_id": floor[1],
            })
            # 引き上げた結果をさらに後続へ伝播させる（cascade相当）
            effective[tid] = floor
        return plan

    def plan_milestone_consistency_repair(self):
        """全ジョブを検査し、先行タスクより早い締切のマイルストーンが設定されて
        いるタスクの再調整計画を返す（書き込みは行わない）。空リストなら
        整合しており、何もする必要がない。適用は
        apply_milestone_consistency_repair(plan)。"""
        plan = []
        for job in self.list_jobs():
            plan.extend(self._milestone_repair_plan_for_job(job))
        return plan

    @undoable("マイルストーンの整合性を再調整")
    def apply_milestone_consistency_repair(self, plan):
        """plan_milestone_consistency_repair() が返した計画をそのまま適用する。
        計画と適用を分けているため、確認ダイアログに出した内容と実際に適用される
        内容が食い違うことはない。"""
        for item in plan:
            existing = self._conn.execute(
                "SELECT is_active, override_days, team_id FROM job_task_overrides "
                "WHERE job_id = ? AND workflow_task_id = ?",
                (item["job_id"], item["workflow_task_id"]),
            ).fetchone()
            self.upsert_job_task_override(
                item["job_id"], item["workflow_task_id"],
                is_active=bool(existing["is_active"]) if existing else True,
                override_days=existing["override_days"] if existing else None,
                milestone_id=item["to_milestone_id"],
                team_id=existing["team_id"] if existing else None,
            )

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
            "d.source_link_id, d.is_active "
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

    @undoable("個別のタスク依存を追加")
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

    @undoable("個別のタスク依存を変更")
    def update_external_dependency(self, dependency_id, workflow_task_id, depends_on_job_id,
                                    depends_on_workflow_task_id):
        """手動追加した個別のタスク依存の内容を選び直す（自動生成分＝
        source_link_idがNULLでない行は呼び出し側で編集不可にガードする想定）。"""
        row = self._conn.execute(
            "SELECT job_id FROM job_external_dependencies WHERE id = ?", (dependency_id,)
        ).fetchone()
        if row is None:
            raise ProjectDatabaseError("この依存関係は存在しません")
        job_id = row["job_id"]
        if (job_id, workflow_task_id) == (depends_on_job_id, depends_on_workflow_task_id):
            raise ProjectDatabaseError("同じタスクへの自己依存は設定できません")
        try:
            self._conn.execute(
                "UPDATE job_external_dependencies SET workflow_task_id = ?, "
                "depends_on_job_id = ?, depends_on_workflow_task_id = ? WHERE id = ?",
                (workflow_task_id, depends_on_job_id, depends_on_workflow_task_id, dependency_id),
            )
        except sqlite3.IntegrityError as e:
            raise ProjectDatabaseError("この依存関係は既に登録されています") from e
        self._commit()

    @undoable(lambda self, dependency_id, is_active: "個別のタスク依存を有効化" if is_active else "個別のタスク依存を無効化")
    def set_external_dependency_active(self, dependency_id, is_active):
        """個別のタスク依存（自動生成分・手動追加分いずれも）を、削除せずに
        有効/無効だけ切り替える。無効化した依存はスケジューリング時に無視される
        （gui/gantt_generator.py参照）が、行自体はDBに残るため、自動生成分でも
        後から元に戻せる。"""
        self._conn.execute(
            "UPDATE job_external_dependencies SET is_active = ? WHERE id = ?",
            (int(is_active), dependency_id),
        )
        self._commit()

    @undoable("個別のタスク依存を削除")
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

    def _upsert_dependency_pair(self, job_id, workflow_task_id, depends_on_job_id,
                                 depends_on_workflow_task_id, source_link_id):
        """依存テンプレートから展開されるタスク対応を追加する。同じ組み合わせ
        （job_id, workflow_task_id, depends_on_job_id, depends_on_workflow_task_id
        ——job_external_dependenciesのUNIQUEキーそのもの）の行が既に存在する
        場合は重複させず、source_link_idが未設定（＝手動追加分）であれば
        テンプレート由来として更新する（手動→自動への昇格）。どのタスク同士を
        対応させるかという内容自体は複合キーなので、この昇格で変わらない。"""
        existing = self._conn.execute(
            "SELECT id, source_link_id FROM job_external_dependencies WHERE "
            "job_id = ? AND workflow_task_id = ? AND depends_on_job_id = ? "
            "AND depends_on_workflow_task_id = ?",
            (job_id, workflow_task_id, depends_on_job_id, depends_on_workflow_task_id),
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO job_external_dependencies (job_id, workflow_task_id, "
                "depends_on_job_id, depends_on_workflow_task_id, source_link_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, workflow_task_id, depends_on_job_id, depends_on_workflow_task_id, source_link_id),
            )
        elif existing["source_link_id"] is None:
            self._conn.execute(
                "UPDATE job_external_dependencies SET source_link_id = ? WHERE id = ?",
                (source_link_id, existing["id"]),
            )

    @undoable("依存テンプレートの同期")
    def sync_dependency_templates(self):
        """既存の「依存先ジョブ」リンクすべてに対し、現在の依存テンプレートの
        内容を改めて適用し直す（テンプレート由来のタスク対応を最新状態へ
        揃える）。

        テンプレートの新規追加時（add_dependency_template）・依存先ジョブの
        新規追加時（add_job_dependency_link）は、追加されたその場でタスク
        対応を展開しているが、それ以外の変更経路——テンプレートの編集
        （update_dependency_template）・削除（delete_dependency_template）・
        ジョブのワークフロー再割当て（update_job）——は、どのリンクに影響する
        かをその場で正確に絞り込むより、影響しうる全リンクをこの関数で
        一括して現在のテンプレート状態に合わせ直す方が単純で漏れがない。
        そのため、これらの変更経路すべてからこの関数を呼び出す。

        テンプレートに合致する対応は追加/自動生成扱いへ昇格（既存の
        _upsert_dependency_pairと同じ「重複させない・手動を尊重」ルール）し、
        このリンクから自動生成された対応（source_link_id一致）のうち、
        現在どのテンプレートにも合致しなくなったもの（テンプレートの編集・
        削除で古くなったもの）は削除する。手動追加分（source_link_idが
        NULL）には一切触れない。"""
        links = self._conn.execute(
            "SELECT l.id AS link_id, l.job_id, l.depends_on_job_id, "
            "j.workflow_id AS job_workflow_id, dj.workflow_id AS depends_on_workflow_id "
            "FROM job_dependency_links l "
            "JOIN jobs j ON j.id = l.job_id "
            "JOIN jobs dj ON dj.id = l.depends_on_job_id"
        ).fetchall()
        for link in links:
            templates = self._conn.execute(
                "SELECT workflow_task_id, depends_on_workflow_task_id "
                "FROM workflow_dependency_templates "
                "WHERE workflow_id = ? AND depends_on_workflow_id = ?",
                (link["job_workflow_id"], link["depends_on_workflow_id"]),
            ).fetchall()
            expected = {
                (t["workflow_task_id"], t["depends_on_workflow_task_id"]) for t in templates
            }
            for workflow_task_id, depends_on_workflow_task_id in expected:
                self._upsert_dependency_pair(
                    link["job_id"], workflow_task_id, link["depends_on_job_id"],
                    depends_on_workflow_task_id, link["link_id"],
                )
            auto_rows = self._conn.execute(
                "SELECT id, workflow_task_id, depends_on_workflow_task_id "
                "FROM job_external_dependencies WHERE source_link_id = ?",
                (link["link_id"],),
            ).fetchall()
            for row in auto_rows:
                if (row["workflow_task_id"], row["depends_on_workflow_task_id"]) not in expected:
                    self._conn.execute(
                        "DELETE FROM job_external_dependencies WHERE id = ?", (row["id"],)
                    )
        self._commit()

    @undoable("依存先ジョブを追加")
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
        self.sync_dependency_templates()
        return link_id

    @undoable("依存先ジョブを削除")
    def delete_job_dependency_link(self, link_id):
        """CASCADEにより、このリンクから自動生成された job_external_dependencies
        行（source_link_id が一致する行）も同時に削除される。個別のタスク対応は
        「依存先ジョブ」のリンクに従属する設計（GUI上、依存先ジョブを選んだ後に
        タスク対応を選ぶ導線しか持たない）のため、同じ (job_id, depends_on_job_id)
        を指す手動追加行（source_link_id が NULL）も合わせて削除する。"""
        row = self._conn.execute(
            "SELECT job_id, depends_on_job_id FROM job_dependency_links WHERE id = ?",
            (link_id,),
        ).fetchone()
        if row is not None:
            self._conn.execute(
                "DELETE FROM job_external_dependencies WHERE job_id = ? "
                "AND depends_on_job_id = ? AND source_link_id IS NULL",
                (row["job_id"], row["depends_on_job_id"]),
            )
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

    @undoable("依存テンプレートを追加")
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
        template_id = cur.lastrowid
        # 既にこのワークフローペアで「依存先ジョブ」のリンクが張られている
        # ジョブがあれば、新しいテンプレートのタスク対応をそのリンクにも展開
        # する（依存先ジョブを先に追加し、後からテンプレートを設定した場合の
        # 救済）。
        self.sync_dependency_templates()
        return template_id

    @undoable("依存テンプレートを変更")
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
        # 変更前のタスク対応で自動生成されていた行は、もう現在のテンプレートに
        # 合致しなくなるため sync_dependency_templates が削除し、変更後の
        # タスク対応が新たに展開される。
        self.sync_dependency_templates()

    @undoable("依存テンプレートを削除")
    def delete_dependency_template(self, template_id):
        self._conn.execute(
            "DELETE FROM workflow_dependency_templates WHERE id = ?", (template_id,)
        )
        # このテンプレートから自動生成されていたタスク対応は、もうどの
        # テンプレートにも合致しなくなるため sync_dependency_templates が削除する。
        self.sync_dependency_templates()
