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
import zlib
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from i18n import tr

# 再エクスポート: 呼び出し側・テストからは従来どおり gui.db から参照できるようにする。
from gui.db_schema import (  # noqa: F401
    SCHEMA_VERSION,
    SchemaError,
    _SCHEMA_SQL,
    check_openable,
    migrate as _migrate_schema,
)


class ProjectDatabaseError(Exception):
    """DB層で検出した業務エラー（一意性違反・不正な削除など）の基底クラス"""


class InvalidNameError(ProjectDatabaseError):
    """名前として使えない（空欄・空白だけ、または既に使われている）場合"""


class DuplicateNameError(InvalidNameError):
    """名前（または日付など、一意であるべき値）が既に使われている場合"""


class ReferencedEntityError(ProjectDatabaseError):
    """参照が残っている行を削除しようとした場合"""


#: ワークフロー内の依存関係の種別。値はDBに保存する文字列そのもの。
#: FS = Finish-to-Start（先行タスクの完了後に開始）
#: SS = Start-to-Start（先行タスクの開始に合わせて開始）
DEPENDENCY_KINDS = ("FS", "SS")

#: 入力を受け付ける日付の上限（締切日・休業日・開始固定日など）。スケジューラが
#: 日付を扱う pandas の Timestamp は 2262-04-11 までしか表せず、それより後の日付が
#: あると計算が想定外のエラー（OverflowError 等）で止まる。日本の祝日の計算式も
#: 遠い未来では成り立たない。実務には十分先で、その手前に余裕を持たせた値にする。
#: GUIの日付欄の上限（gui/widgets_common.py）と、生成前の検査
#: （gui/gantt_generator.py の validate_for_generation。この値より後の日付を持つ
#: 既存のファイル向け）で使う。
MAX_SUPPORTED_DATE = "2199-12-31"

#: ラグ（営業日）の許容範囲。上限は「1タスクの所要日数として現実的な桁」に
#: 合わせた安全弁で、業務上の意味があるわけではない（入力ミスで
#: 稼働日カレンダーが極端に伸びるのを防ぐためだけの値）。
MAX_LAG_DAYS = 3650


def normalize_dependency_kind(dep_type, lag_days):
    """依存関係の種別・ラグを検証し、(dep_type, lag_days) に正規化して返す。

    ALTER TABLE で後から足した列にはCHECK制約を付けられない（＝旧ファイルを
    移行した場合、DB側では不正な値を弾けない）ため、書き込み経路をここに
    集約して守る。
    """
    dep_type = str(dep_type or "FS").strip().upper()
    if dep_type not in DEPENDENCY_KINDS:
        raise ProjectDatabaseError(
            tr("依存関係の種別 '{dep_type}' は不正です（{value} のいずれか）", dep_type=dep_type, value=' / '.join(DEPENDENCY_KINDS))
        )
    try:
        lag_days = int(lag_days)
    except (TypeError, ValueError):
        raise ProjectDatabaseError(tr("ラグは整数（営業日）で指定してください")) from None
    if abs(lag_days) > MAX_LAG_DAYS:
        raise ProjectDatabaseError(
            tr("ラグ {lag_days} 日は範囲外です（±{MAX_LAG_DAYS}日まで）", lag_days=lag_days, MAX_LAG_DAYS=MAX_LAG_DAYS)
        )
    return dep_type, lag_days


def _assign_stable_key(conn, job_id):
    """ジョブの安定キー（jobs.stable_key）を作成時に1度だけ決める（以後変えない）。

    値は作成時の内部IDの文字列（"JOB_012"）。スケジューラがこれまで配置の
    ばらつきの種にしていた Job_ID と同じ値なので、配置は従来どおりで、同じ操作を
    すれば同じ結果になる。内部IDを直接使わずに列として固定しておくのは、将来
    ファイルの取り込み等で内部IDが振り直されても、配置が変わらないようにするため。
    （ランダムなUUIDにすると、同じ手順で作ったプロジェクトでも作るたびに配置が
    変わってしまう。）"""
    conn.execute(
        "UPDATE jobs SET stable_key = printf('JOB_%03d', id) WHERE id = ?", (job_id,)
    )


def normalize_name(name):
    """マイルストーン・チーム・ワークフロー・タスク・ジョブの名前を検証し、前後の
    空白を落として返す。空欄・空白だけなら InvalidNameError。

    画面の選択肢や絞り込みでは名前だけで見分けるため、空の名前は何も表示されない
    項目になり、前後の空白だけが違う名前（"A" と "A "）は見分けられない。GUIの
    入力欄ごとに検査すると漏れる（実際にマイルストーン・チーム・ジョブの名前は
    空で登録できていた）ので、書き込み経路のここに集約する。"""
    name = "" if name is None else str(name).strip()
    if not name:
        raise InvalidNameError(tr("名前を入力してください"))
    return name


def _validate_start_pin_date(value):
    """開始固定日の書式を検証し、'YYYY-MM-DD' または None に正規化する。

    ここで検証するのは、値がそのままスケジューラへ渡り、壊れた日付が
    `pd.to_datetime` で黙って NaT になって「固定が無かったこと」になる
    のを防ぐため（固定が静かに消えるのが最悪の壊れ方）。
    """
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except (TypeError, ValueError):
        raise ProjectDatabaseError(
            tr("開始固定日 '{value}' が不正です（YYYY-MM-DD 形式で指定してください）", value=value)
        ) from None


def _parse_iso_date(value, label):
    """'YYYY-MM-DD' を date にする。読めなければ項目名（label）入りの例外。"""
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ProjectDatabaseError(
            tr("{label} '{value}' が不正です（YYYY-MM-DD 形式で指定してください）", label=label, value=value)
        ) from None


def _validate_distribution_ratio(value):
    """配置コントロール（distribution_ratio）の値を検証する。
    ALTER TABLE で後から足した列にはCHECK制約を付けられないため、他の
    後付け列（normalize_dependency_kind 等）と同様に書き込み経路で守る。
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ProjectDatabaseError(tr("配置の値は数値で指定してください")) from None
    if not 0.0 <= value <= 1.0:
        raise ProjectDatabaseError(tr("配置の値は0.0〜1.0の範囲で指定してください"))
    return value


def normalize_tags(tags):
    """タグ入力（カンマ区切りの1文字列、またはリスト）を、正規化した
    カンマ区切り文字列に変換する。前後の空白を落とし、空要素は除外し、
    重複は最初の1つだけ残す（表示順は入力順を保つ）。ジョブ タグ・
    タスク タグのどちらにも使う共通の関数。

    ALTER TABLE で後から足した列にはCHECK制約を付けられないため、他の
    後付け列（normalize_dependency_kind 等）と同様に書き込み経路で正規化する。
    """
    if tags is None:
        return ""
    parts = tags.split(",") if isinstance(tags, str) else list(tags)
    seen = set()
    result = []
    for part in parts:
        tag = str(part).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        result.append(tag)
    return ", ".join(result)


def parse_tags(tags):
    """タグ文字列（カンマ区切り）を、タグ名のリストに分解する。
    normalize_tagsで正規化済みの文字列を主に想定するが、前後の空白除去・
    空要素の除外は独立して行うため、正規化前の生の文字列にも使える。
    ジョブ タグ・タスク タグのどちらにも使う共通の関数（ジョブタブ・
    ガントチャートタブのタグ絞り込みで共通して使う）。"""
    return [t.strip() for t in (tags or "").split(",") if t.strip()]


#: job_task_overrides.status に許される値（NULL＝未着手は別途Noneで表す）。
TASK_STATUSES = ("in_progress", "done")


def normalize_task_status(status):
    """タスクの進捗状態を検証し、None（未着手）/ "in_progress"（進行中）/
    "done"（完了）のいずれかに正規化する。

    ALTER TABLE で後から足した列にはCHECK制約を付けられないため、他の
    後付け列（normalize_dependency_kind 等）と同様に書き込み経路で守る。
    """
    if status is None or status == "":
        return None
    status = str(status).strip()
    if status not in TASK_STATUSES:
        raise ProjectDatabaseError(
            tr("タスクの状態 '{status}' は不正です（{value} のいずれか、または未着手はNone）", status=status, value=' / '.join(TASK_STATUSES))
        )
    return status


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
                try:
                    return fn(self, *args, **kwargs)
                except BaseException:
                    # 失敗した操作の書きかけ（コミット前の変更）を残さない。
                    # Undo単位を閉じる前に戻すので、Undoにも積まれない
                    # （_rollback_uncommitted のdocstring参照）。
                    self._rollback_uncommitted()
                    raise

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
        # 内容が変わるたびに1つ増える通し番号。「前回計算した時点から中身が
        # 変わったか」を安価に判定するために使う（ガントチャートタブが
        # スケジューリングをやり直すかどうかの判断など）。
        self._revision = 0
        # GUI側から差し込む変更通知フック（タイトルバーの未保存マーク更新等に使う）。
        # 引数なしで呼ばれる callable、または None。
        self.on_change = None
        # GUI側から差し込む gui.undo_manager.UndoManager（またはNone）。
        # None の間は undo_group が完全に無効化され、通常のCRUDとして動作する
        # （Qt非依存のテスト等、GUIを介さない利用を妨げないため）。
        self.undo_manager = None
        # GUI側から差し込む、表示中の日程を返す callable（またはNone）。
        # (job_id, workflow_task_id) の並びを受け取り、確定行と同じ形の辞書の並び
        # （gui/plan_actions.confirmed_rows_from_result）を返す。タスクを進行中・完了に
        # したとき、その日程を実績として記録するのに使う（_record_status_fact）。
        # None の間は記録しない。
        self.displayed_rows_provider = None
        # 1回の操作（Undoの1単位）の通し番号と、その中で取った表示中の日程
        # （_displayed_row。同じ操作の中では使い回す）。
        self._operation_serial = 0
        self._displayed_rows_memo = None
        # undo_group のネスト検知用（Trueの間は既に外側でスナップショットを
        # 取得済みなので、内側の呼び出しでは何もしない）。
        self._in_undoable_call = False
        # Undo/Redoの適用中（suspend_undo_recording）かどうか。
        self._undo_suppressed = False
        # begin_undo_group() で開いたまま保持している単位。
        # (ラベル, 操作前のDBスナップショット, 操作前のUI状態) または None。
        self._open_group = None

    @property
    def revision(self):
        """内容が変わるたびに増える通し番号（変更検知用。値の大小に意味はない）。"""
        return self._revision

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
        self._revision += 1
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
        self._operation_serial += 1
        before_db = self.serialize_state()
        before_ui = self.undo_manager.capture_ui_state()
        try:
            yield
        finally:
            self._in_undoable_call = False
            self._displayed_rows_memo = None
            after_db = self.serialize_state()
            if after_db != before_db:
                # ラベルは記録するときに表示言語へ訳す（docs/roadmap.md §11。表示言語は
                # 起動時に決まり途中で変わらない。名前を埋め込むラベルは呼び出し側が
                # tr() 済みのものを渡し、訳文はキーに無いのでそのまま残る）
                self.undo_manager.push(before_db, before_ui, after_db, tr(label))

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
        self._operation_serial += 1
        self._open_group = (tr(label), self.serialize_state(), self.undo_manager.capture_ui_state())

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
        self._displayed_rows_memo = None
        if self.undo_manager is None:
            # 開いた後にUndo管理が外された（別のプロジェクトへ切り替える途中に、
            # 旧タブの入力欄からフォーカスが外れた等）。積む先が無いので閉じるだけ。
            return False
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
        """既存の`.pschedule`ファイルを開く。

        次の場合は、素のsqlite3例外をそのまま伝播させず、原因が分かる
        `ProjectDatabaseError`にして送出する（GUI側は例外の`str()`を
        そのまま「プロジェクトを開けませんでした」ダイアログに出すため、
        素のsqlite3例外だと「file is not a database」のような技術的な
        文言しか伝わらない）。

        - SQLiteファイルとして開けない（中身が破損している、または
          そもそもSQLiteファイルではない）
        - `schema_meta`テーブルが無い（ProjectSchedulerのファイルではない
          可能性が高い）
        - `schema_version`が、このアプリが対応するバージョンより新しい
          （新しいバージョンのProjectSchedulerで作成されたファイルを、
          古いアプリで開こうとしている）。ここで拒否しないと、知らない
          カラム・テーブルはそのまま保持しつつGUIだけが理解できない
          スキーマに対して動き続けてしまい、保存すると新しいバージョンの
          意味を持つデータが（旧バージョンの認識のまま）書き換わる恐れが
          あるため（詳細は gui/db_schema.py の check_openable() 参照）。
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(tr("プロジェクトファイルが見つかりません: {path}", path=path))
        disk_conn = sqlite3.connect(str(p))
        try:
            db = cls(path)
            try:
                disk_conn.backup(db._conn)
            except sqlite3.DatabaseError as e:
                raise ProjectDatabaseError(
                    tr("ProjectSchedulerのプロジェクトファイル（.pschedule）として読み込めませんでした（{e}）。ファイルが破損しているか、SQLite形式ではない可能性があります。", e=e)
                ) from e
        finally:
            disk_conn.close()
        try:
            check_openable(db._conn)
        except SchemaError as e:
            db.close()
            raise ProjectDatabaseError(str(e)) from e
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
            raise ProjectDatabaseError(tr("保存先が未設定です（save_asでパスを指定してください）"))
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
        # 開いたままのトランザクションがあると backup() が終わらなくなる
        # （_rollback_uncommitted 参照）。変更系メソッドは失敗時に巻き戻し、成功時は
        # コミットするので通常は残らないが、残っていた場合の最後の砦として確定させる
        # ——メモリ上の内容（Undoのスナップショットにも写っている、画面に見えている
        # 内容）をそのまま保存するのが正しいため、捨てずにコミットする。
        if self._conn.in_transaction:
            self._conn.commit()
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
                tr("{cls}._commit() が @undoable / undo_group の外側から呼ばれました。変更系メソッドには @undoable(\"ラベル\") を付けてください。", cls=type(self).__name__)
            )
        self._conn.commit()
        self._dirty = True
        self._revision += 1
        self._notify_change()

    def _rollback_uncommitted(self):
        """コミットしていない変更を捨て、開いたままのトランザクションを閉じる。

        sqlite3 は INSERT/UPDATE/DELETE の前に暗黙にトランザクションを始めるため、
        その文が一意制約違反等で失敗すると、何も変わっていなくてもトランザクション
        だけが開いたまま残る。この状態で保存すると、_write_to() の backup() が
        「書き込み中」（SQLITE_LOCKED）を返し続け、Python がそれを無限に再試行して
        アプリが固まる（名前の重複エラーの直後に Ctrl+S を押すだけで起きていた）。"""
        try:
            if self._conn.in_transaction:
                self._conn.rollback()
        except sqlite3.ProgrammingError:
            pass  # 既に閉じたDB（戻すものは無い。元の例外をそのまま伝える）

    def _notify_change(self):
        if self.on_change is not None:
            self.on_change()

    # -- project（単一行） --------------------------------------------------

    def get_project(self):
        row = self._conn.execute(
            "SELECT project_name, start_date, distribution_ratio, replan_base_date, "
            "replanned_at, confirmed_at, confirmed_global_signature, "
            "pending_replan_base_date, pending_replanned_at FROM project WHERE id = 1"
        ).fetchone()
        return dict(row)

    @undoable("プロジェクト概要を変更")
    def set_project(self, project_name, start_date):
        self._conn.execute(
            "UPDATE project SET project_name = ?, start_date = ? WHERE id = 1",
            (project_name, start_date),
        )
        self._commit()

    @undoable("配置を変更")
    def set_distribution_ratio(self, distribution_ratio):
        """ガントチャートタブの「配置コントロール」で調整する distribution_ratio
        （project_scheduler.py 参照）をプロジェクト設定として保存する。"""
        distribution_ratio = _validate_distribution_ratio(distribution_ratio)
        self._conn.execute(
            "UPDATE project SET distribution_ratio = ? WHERE id = 1",
            (distribution_ratio,),
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

    @undoable(lambda self, name, end_date, note="": tr("マイルストーン「{name}」を追加", name=name))
    def add_milestone(self, name, end_date, note=""):
        name = normalize_name(name)
        try:
            cur = self._conn.execute(
                "INSERT INTO milestones(name, end_date, note) VALUES (?, ?, ?)",
                (name, end_date, note),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(tr("マイルストーン名 '{name}' は既に使用されています", name=name)) from e
        self._commit()
        return cur.lastrowid

    @undoable(lambda self, milestone_id, name, end_date, note="": tr("マイルストーン「{name}」を変更", name=name))
    def update_milestone(self, milestone_id, name, end_date, note=""):
        name = normalize_name(name)
        try:
            self._conn.execute(
                "UPDATE milestones SET name = ?, end_date = ?, note = ? WHERE id = ?",
                (name, end_date, note, milestone_id),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(tr("マイルストーン名 '{name}' は既に使用されています", name=name)) from e
        self._commit()

    def milestone_usage_count(self, milestone_id):
        row = self._conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM jobs WHERE default_milestone_id = ?) + "
            "(SELECT COUNT(*) FROM job_task_overrides WHERE milestone_id = ?) AS n",
            (milestone_id, milestone_id),
        ).fetchone()
        return row["n"]

    @undoable(lambda self, milestone_id: tr("マイルストーン「{name}」を削除", name=_entity_name(self._conn, 'milestones', milestone_id)))
    def delete_milestone(self, milestone_id):
        self._conn.execute("DELETE FROM milestones WHERE id = ?", (milestone_id,))
        self._commit()

    # -- teams ---------------------------------------------------------------

    def list_teams(self):
        rows = self._conn.execute(
            "SELECT id, name, max_lines FROM teams ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    @undoable(lambda self, name, max_lines: tr("チーム「{name}」を追加", name=name))
    def add_team(self, name, max_lines):
        name = normalize_name(name)
        try:
            cur = self._conn.execute(
                "INSERT INTO teams(name, max_lines) VALUES (?, ?)", (name, max_lines)
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(tr("チーム名 '{name}' は既に使用されています", name=name)) from e
        self._commit()
        return cur.lastrowid

    @undoable(lambda self, team_id, name, max_lines: tr("チーム「{name}」を変更", name=name))
    def update_team(self, team_id, name, max_lines):
        name = normalize_name(name)
        try:
            self._conn.execute(
                "UPDATE teams SET name = ?, max_lines = ? WHERE id = ?",
                (name, max_lines, team_id),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(tr("チーム名 '{name}' は既に使用されています", name=name)) from e
        self._commit()

    def team_usage_count(self, team_id):
        row = self._conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM workflow_tasks WHERE team_id = ?) + "
            "(SELECT COUNT(*) FROM job_task_overrides WHERE team_id = ?) AS n",
            (team_id, team_id),
        ).fetchone()
        return row["n"]

    @undoable(lambda self, team_id: tr("チーム「{name}」を削除", name=_entity_name(self._conn, 'teams', team_id)))
    def delete_team(self, team_id):
        if self.team_usage_count(team_id) > 0:
            raise ReferencedEntityError(tr("このチームはタスクに使用されているため削除できません"))
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

    @undoable(lambda self, team_id, start_date, lines: tr("同時ライン数の変更点（{start_date}）を追加", start_date=start_date))
    def add_team_capacity_change(self, team_id, start_date, lines):
        try:
            cur = self._conn.execute(
                "INSERT INTO team_capacity_changes(team_id, start_date, lines) "
                "VALUES (?, ?, ?)",
                (team_id, start_date, lines),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(
                tr("この日付（{start_date}）の変更点は既に登録されています", start_date=start_date)
            ) from e
        self._commit()
        return cur.lastrowid

    @undoable(lambda self, change_id, start_date, lines: tr("同時ライン数の変更点（{start_date}）を変更", start_date=start_date))
    def update_team_capacity_change(self, change_id, start_date, lines):
        try:
            self._conn.execute(
                "UPDATE team_capacity_changes SET start_date = ?, lines = ? WHERE id = ?",
                (start_date, lines, change_id),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(
                tr("この日付（{start_date}）の変更点は既に登録されています", start_date=start_date)
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

    @undoable(lambda self, date, team_id=None, note="": tr("休業日（{date}）を追加", date=date))
    def add_holiday(self, date, team_id=None, note=""):
        exists = self._conn.execute(
            "SELECT 1 FROM holidays WHERE date = ? AND team_id IS ?", (date, team_id)
        ).fetchone()
        if exists:
            raise DuplicateNameError(tr("同じ日付・チームの休業日が既に登録されています"))
        cur = self._conn.execute(
            "INSERT INTO holidays(date, team_id, note) VALUES (?, ?, ?)", (date, team_id, note)
        )
        self._commit()
        return cur.lastrowid

    @undoable(lambda self, holiday_id, date, team_id=None, note="": tr("休業日（{date}）を変更", date=date))
    def update_holiday(self, holiday_id, date, team_id=None, note=""):
        exists = self._conn.execute(
            "SELECT 1 FROM holidays WHERE date = ? AND team_id IS ? AND id != ?",
            (date, team_id, holiday_id),
        ).fetchone()
        if exists:
            raise DuplicateNameError(tr("同じ日付・チームの休業日が既に登録されています"))
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

    @undoable(lambda self, name: tr("ワークフロー「{name}」を追加", name=name))
    def add_workflow(self, name):
        name = normalize_name(name)
        next_order = self._conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM workflows"
        ).fetchone()["n"]
        try:
            cur = self._conn.execute(
                "INSERT INTO workflows(name, sort_order) VALUES (?, ?)", (name, next_order)
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(tr("ワークフロー名 '{name}' は既に使用されています", name=name)) from e
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

    @undoable(lambda self, workflow_id, name: tr("ワークフロー名を「{name}」に変更", name=name))
    def rename_workflow(self, workflow_id, name):
        name = normalize_name(name)
        try:
            self._conn.execute(
                "UPDATE workflows SET name = ? WHERE id = ?", (name, workflow_id)
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(tr("ワークフロー名 '{name}' は既に使用されています", name=name)) from e
        self._commit()

    @undoable(lambda self, workflow_id: tr("ワークフロー「{name}」を複製", name=_entity_name(self._conn, 'workflows', workflow_id)))
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
            raise ProjectDatabaseError(tr("複製元のワークフローが見つかりません"))

        # 「追加」ボタン連打時の衝突回避（gui/widgets_common.py の
        # unique_default_name）と同じ考え方だが、db.py はQt非依存を保つため
        # ここでは同じロジックをそのまま持つ（widgets_common.py はPySide6に
        # 依存しており、db.py からは import できない）。
        existing_names = {
            r["name"] for r in self._conn.execute("SELECT name FROM workflows").fetchall()
        }
        base_name = tr("{name}のコピー", name=row['name'])
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
            "SELECT id, name, team_id, default_days "
            "FROM workflow_tasks WHERE workflow_id = ? ORDER BY id",
            (workflow_id,),
        ).fetchall():
            cur = self._conn.execute(
                "INSERT INTO workflow_tasks(workflow_id, name, team_id, default_days) "
                "VALUES (?, ?, ?, ?)",
                (new_workflow_id, t["name"], t["team_id"], t["default_days"]),
            )
            task_id_map[t["id"]] = cur.lastrowid

        for d in self._conn.execute(
            "SELECT predecessor_task_id, successor_task_id, dep_type, lag_days "
            "FROM task_dependencies WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall():
            self._conn.execute(
                "INSERT INTO task_dependencies(workflow_id, predecessor_task_id, "
                "successor_task_id, dep_type, lag_days) VALUES (?, ?, ?, ?, ?)",
                (new_workflow_id, task_id_map[d["predecessor_task_id"]],
                 task_id_map[d["successor_task_id"]], d["dep_type"], d["lag_days"]),
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

    @undoable(lambda self, workflow_id: tr("ワークフロー「{name}」を削除", name=_entity_name(self._conn, 'workflows', workflow_id)))
    def delete_workflow(self, workflow_id):
        if self.workflow_usage_count(workflow_id) > 0:
            raise ReferencedEntityError(
                tr("このワークフローは既存のジョブに使用されているため削除できません")
            )
        self._conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        self._commit()

    # -- workflow_tasks -------------------------------------------------------

    def list_workflow_tasks(self, workflow_id):
        rows = self._conn.execute(
            "SELECT wt.id, wt.workflow_id, wt.name, wt.team_id, t.name AS team_name, "
            "wt.default_days "
            "FROM workflow_tasks wt JOIN teams t ON t.id = wt.team_id "
            "WHERE wt.workflow_id = ? ORDER BY wt.id",
            (workflow_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @undoable(lambda self, workflow_id, name, team_id, default_days: tr("タスク「{name}」を追加", name=name))
    def add_workflow_task(self, workflow_id, name, team_id, default_days):
        name = normalize_name(name)
        try:
            cur = self._conn.execute(
                "INSERT INTO workflow_tasks(workflow_id, name, team_id, default_days) "
                "VALUES (?, ?, ?, ?)",
                (workflow_id, name, team_id, default_days),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(
                tr("タスク名 '{name}' はこのワークフロー内で既に使用されています", name=name)
            ) from e
        self._commit()
        return cur.lastrowid

    @undoable(lambda self, task_id, name, team_id, default_days: tr("タスク「{name}」を変更", name=name))
    def update_workflow_task(self, task_id, name, team_id, default_days):
        name = normalize_name(name)
        try:
            self._conn.execute(
                "UPDATE workflow_tasks SET name = ?, team_id = ?, default_days = ? "
                "WHERE id = ?",
                (name, team_id, default_days, task_id),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(
                tr("タスク名 '{name}' はこのワークフロー内で既に使用されています", name=name)
            ) from e
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

    @undoable(lambda self, task_id: tr("タスク「{name}」を削除", name=_entity_name(self._conn, 'workflow_tasks', task_id)))
    def delete_workflow_task(self, task_id):
        self._conn.execute("DELETE FROM workflow_tasks WHERE id = ?", (task_id,))
        self._commit()

    # -- task_dependencies（ワークフロー内の依存＝Internal_Depends） -----------

    def list_task_dependencies(self, workflow_id):
        rows = self._conn.execute(
            "SELECT id, workflow_id, predecessor_task_id, successor_task_id, "
            "dep_type, lag_days "
            "FROM task_dependencies WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_task_dependency(self, dependency_id):
        """1件を取得する（見つからなければ None）。GUIの編集ダイアログ用。"""
        row = self._conn.execute(
            "SELECT id, workflow_id, predecessor_task_id, successor_task_id, "
            "dep_type, lag_days "
            "FROM task_dependencies WHERE id = ?",
            (dependency_id,),
        ).fetchone()
        return dict(row) if row else None

    @undoable("依存関係を追加")
    def add_task_dependency(self, workflow_id, predecessor_task_id, successor_task_id,
                            dep_type="FS", lag_days=0):
        """循環依存のチェックは呼び出し側（gui/node_canvas.py）が事前に行う想定。
        ここでは構造的制約（自己参照禁止・重複禁止）のみDB制約で守る。

        dep_type / lag_days の既定値（FS・0）は従来の唯一の挙動そのものなので、
        引数を渡さない既存の呼び出しは意味が変わらない。
        """
        dep_type, lag_days = normalize_dependency_kind(dep_type, lag_days)
        try:
            cur = self._conn.execute(
                "INSERT INTO task_dependencies(workflow_id, predecessor_task_id, "
                "successor_task_id, dep_type, lag_days) VALUES (?, ?, ?, ?, ?)",
                (workflow_id, predecessor_task_id, successor_task_id, dep_type, lag_days),
            )
        except sqlite3.IntegrityError as e:
            raise ProjectDatabaseError(tr("この依存関係は既に存在するか、不正です")) from e
        self._commit()
        return cur.lastrowid

    @undoable("依存関係の種別・ラグを変更")
    def update_task_dependency(self, dependency_id, dep_type, lag_days):
        """種別（FS/SS）とラグ（営業日）だけを差し替える。依存の向き
        （predecessor/successor）は変更しない——向きを変えることは
        「別の依存関係」であり、循環依存の再検査が要るため。"""
        dep_type, lag_days = normalize_dependency_kind(dep_type, lag_days)
        cur = self._conn.execute(
            "UPDATE task_dependencies SET dep_type = ?, lag_days = ? WHERE id = ?",
            (dep_type, lag_days, dependency_id),
        )
        if cur.rowcount == 0:
            raise ProjectDatabaseError(tr("対象の依存関係が見つかりません"))
        self._commit()

    @undoable("依存関係を削除")
    def delete_task_dependency(self, dependency_id):
        self._conn.execute("DELETE FROM task_dependencies WHERE id = ?", (dependency_id,))
        self._commit()

    # -- jobs ------------------------------------------------------------------

    def list_jobs(self):
        rows = self._conn.execute(
            "SELECT j.id, j.name, j.workflow_id, w.name AS workflow_name, "
            "j.default_milestone_id, m.name AS milestone_name, j.priority, j.tags, j.stable_key "
            "FROM jobs j "
            "JOIN workflows w ON w.id = j.workflow_id "
            "LEFT JOIN milestones m ON m.id = j.default_milestone_id "
            "ORDER BY j.name"
        ).fetchall()
        return [dict(r) for r in rows]

    @undoable(lambda self, name, workflow_id, default_milestone_id, priority=None, tags="": tr("ジョブ「{name}」を追加", name=name))
    def add_job(self, name, workflow_id, default_milestone_id, priority=None, tags=""):
        name = normalize_name(name)
        try:
            cur = self._conn.execute(
                "INSERT INTO jobs(name, workflow_id, default_milestone_id, priority, tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, workflow_id, default_milestone_id, priority, normalize_tags(tags)),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(tr("ジョブ名 '{name}' は既に使用されています", name=name)) from e
        _assign_stable_key(self._conn, cur.lastrowid)
        self._commit()
        return cur.lastrowid

    @undoable(lambda self, job_id, name, workflow_id, default_milestone_id, priority, tags: tr("ジョブ「{name}」を変更", name=name))
    def update_job(self, job_id, name, workflow_id, default_milestone_id, priority, tags):
        name = normalize_name(name)
        old = self._conn.execute(
            "SELECT workflow_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        workflow_changed = old is not None and old["workflow_id"] != workflow_id
        try:
            self._conn.execute(
                "UPDATE jobs SET name = ?, workflow_id = ?, default_milestone_id = ?, "
                "priority = ?, tags = ? WHERE id = ?",
                (name, workflow_id, default_milestone_id, priority, normalize_tags(tags), job_id),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateNameError(tr("ジョブ名 '{name}' は既に使用されています", name=name)) from e
        if workflow_changed:
            # 手動で追加した個別のタスク依存のうち、旧ワークフローのタスクを指す
            # もの（このジョブ側・このジョブに依存する他ジョブ側の両方）は、もう
            # このジョブに存在しないタスクを指すため意味を失う。スケジューラは
            # 存在しない依存先を黙って無視するので、残すと「画面には出ているのに
            # 効いていない依存」になる。テンプレート由来の分（source_link_id あり）は
            # 下の sync_dependency_templates が新しい組み合わせに合わせ直す。
            self._conn.execute(
                "DELETE FROM job_external_dependencies WHERE source_link_id IS NULL AND ("
                " (job_id = ? AND workflow_task_id NOT IN"
                "   (SELECT id FROM workflow_tasks WHERE workflow_id = ?))"
                " OR (depends_on_job_id = ? AND depends_on_workflow_task_id NOT IN"
                "   (SELECT id FROM workflow_tasks WHERE workflow_id = ?)))",
                (job_id, workflow_id, job_id, workflow_id),
            )
        self._commit()
        if workflow_changed:
            # ワークフローの組み合わせが変わると、依存先ジョブのタスク対応が
            # 参照すべきテンプレートも変わるため、最新の状態へ同期し直す。
            self.sync_dependency_templates()

    @undoable(lambda self, job_id: tr("ジョブ「{name}」を複製", name=_entity_name(self._conn, 'jobs', job_id)))
    def duplicate_job(self, job_id):
        """ジョブ1件を、タスク上書き（job_task_overrides）・依存先ジョブ
        （job_dependency_links）・そのタスク単位の対応（job_external_dependencies）
        ごと複製する。

        複製しないもの:
        - このジョブに依存している側（他ジョブの job_dependency_links /
          job_external_dependencies で depends_on_job_id がこのジョブを指す
          もの）。複製先を他ジョブの依存先へ勝手に加えると、意図しない
          副作用になるため（duplicate_workflow が「他ワークフローが複製元に
          依存する側のテンプレート」を複製しないのと同じ考え方）。

        新しいジョブは元と同じワークフロー・既定マイルストーン・優先度・
        タグを引き継ぐ。名前は「元の名前のコピー」を既定とし、衝突する
        場合は連番を付与する（duplicate_workflow と同じ考え方）。"""
        row = self._conn.execute(
            "SELECT name, workflow_id, default_milestone_id, priority, tags "
            "FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise ProjectDatabaseError(tr("複製元のジョブが見つかりません"))

        existing_names = {
            r["name"] for r in self._conn.execute("SELECT name FROM jobs").fetchall()
        }
        base_name = tr("{name}のコピー", name=row['name'])
        new_name = base_name
        n = 2
        while new_name in existing_names:
            new_name = f"{base_name} ({n})"
            n += 1

        cur = self._conn.execute(
            "INSERT INTO jobs(name, workflow_id, default_milestone_id, priority, tags) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_name, row["workflow_id"], row["default_milestone_id"], row["priority"], row["tags"]),
        )
        new_job_id = cur.lastrowid
        # 安定キーは複製元から引き継がない（別のジョブなので、配置のばらつきも別にする）
        _assign_stable_key(self._conn, new_job_id)

        for o in self._conn.execute(
            "SELECT workflow_task_id, is_active, override_days, milestone_id, team_id, "
            "start_pin_date, tags FROM job_task_overrides WHERE job_id = ?",
            (job_id,),
        ).fetchall():
            self._conn.execute(
                "INSERT INTO job_task_overrides(job_id, workflow_task_id, is_active, "
                "override_days, milestone_id, team_id, start_pin_date, tags) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new_job_id, o["workflow_task_id"], o["is_active"], o["override_days"],
                 o["milestone_id"], o["team_id"], o["start_pin_date"], o["tags"]),
            )

        # 依存先ジョブ（このジョブ→他ジョブ）は、リンクIDを付け替えつつ複製する。
        # workflow_task_id / depends_on_workflow_task_id は同じワークフロー・
        # 同じ依存先ジョブを指したまま変わらないため、付け替えが要るのは
        # source_link_id（複製前のリンクIDのまま残すと複製先ではなく元の
        # ジョブのリンクを指してしまう）だけ。
        link_id_map = {}
        for link in self._conn.execute(
            "SELECT id, depends_on_job_id FROM job_dependency_links WHERE job_id = ?",
            (job_id,),
        ).fetchall():
            cur = self._conn.execute(
                "INSERT INTO job_dependency_links(job_id, depends_on_job_id) VALUES (?, ?)",
                (new_job_id, link["depends_on_job_id"]),
            )
            link_id_map[link["id"]] = cur.lastrowid

        for d in self._conn.execute(
            "SELECT workflow_task_id, depends_on_job_id, depends_on_workflow_task_id, "
            "source_link_id, is_active FROM job_external_dependencies WHERE job_id = ?",
            (job_id,),
        ).fetchall():
            new_source_link_id = (
                link_id_map.get(d["source_link_id"]) if d["source_link_id"] is not None else None
            )
            self._conn.execute(
                "INSERT INTO job_external_dependencies(job_id, workflow_task_id, "
                "depends_on_job_id, depends_on_workflow_task_id, source_link_id, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (new_job_id, d["workflow_task_id"], d["depends_on_job_id"],
                 d["depends_on_workflow_task_id"], new_source_link_id, d["is_active"]),
            )

        self._commit()
        return new_job_id

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

    @undoable(lambda self, job_id: tr("ジョブ「{name}」を削除", name=_entity_name(self._conn, 'jobs', job_id)))
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
            "o.team_id AS override_team_id, ot.name AS override_team_name, "
            "o.start_pin_date, COALESCE(o.tags, '') AS tags, o.status "
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

    def list_all_job_task_overrides(self):
        """全ジョブ分のタスク上書き行を1回のクエリでまとめて返す。

        ガントチャート生成時に list_job_tasks_with_overrides() をジョブ数だけ
        呼ぶと、ジョブが増えるほどクエリ数とソートが積み上がる。生成時に必要な
        のは「既定から外れている行」だけなので、上書きテーブル側から引く。
        ジョブのワークフローに属さない古い上書き行（ジョブのワークフローを
        差し替えた場合などに残りうる）を拾わないよう、同じ結合条件で絞る。"""
        rows = self._conn.execute(
            "SELECT o.job_id, o.workflow_task_id, o.is_active, o.override_days, "
            "o.milestone_id AS override_milestone_id, o.team_id AS override_team_id, "
            "o.start_pin_date, o.tags, o.status "
            "FROM job_task_overrides o "
            "JOIN jobs j ON j.id = o.job_id "
            "JOIN workflow_tasks wt ON wt.id = o.workflow_task_id "
            "  AND wt.workflow_id = j.workflow_id"
        ).fetchall()
        return [dict(r) for r in rows]

    @undoable("タスク上書きを変更")
    def upsert_job_task_override(self, job_id, workflow_task_id, is_active=True,
                                  override_days=None, milestone_id=None, team_id=None,
                                  start_pin_date=None, tags="", status=None):
        start_pin_date = _validate_start_pin_date(start_pin_date)
        tags = normalize_tags(tags)
        status = normalize_task_status(status)
        existing = self._conn.execute(
            "SELECT id, status, start_pin_date FROM job_task_overrides "
            "WHERE job_id = ? AND workflow_task_id = ?",
            (job_id, workflow_task_id),
        ).fetchone()
        self._record_status_fact(job_id, workflow_task_id, existing["status"] if existing else None, status)
        if start_pin_date != (existing["start_pin_date"] if existing else None):
            self._drop_draft_move(job_id, workflow_task_id)
        if existing:
            self._conn.execute(
                "UPDATE job_task_overrides SET is_active = ?, override_days = ?, "
                "milestone_id = ?, team_id = ?, start_pin_date = ?, tags = ?, status = ? "
                "WHERE id = ?",
                (int(is_active), override_days, milestone_id, team_id, start_pin_date,
                 tags, status, existing["id"]),
            )
        else:
            self._conn.execute(
                "INSERT INTO job_task_overrides(job_id, workflow_task_id, is_active, "
                "override_days, milestone_id, team_id, start_pin_date, tags, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, workflow_task_id, int(is_active), override_days, milestone_id,
                 team_id, start_pin_date, tags, status),
            )
        self._commit()

    @undoable("タスク上書きを既定に戻す")
    def clear_job_task_override(self, job_id, workflow_task_id):
        """タスクが既定値に戻った場合、上書き行自体を削除する（差分のみ保持）。"""
        existing = self._conn.execute(
            "SELECT status, start_pin_date FROM job_task_overrides "
            "WHERE job_id = ? AND workflow_task_id = ?",
            (job_id, workflow_task_id),
        ).fetchone()
        if existing is not None:
            self._record_status_fact(job_id, workflow_task_id, existing["status"], None)
            if existing["start_pin_date"] is not None:
                self._drop_draft_move(job_id, workflow_task_id)
        self._conn.execute(
            "DELETE FROM job_task_overrides WHERE job_id = ? AND workflow_task_id = ?",
            (job_id, workflow_task_id),
        )
        self._commit()

    def _drop_draft_move(self, job_id, workflow_task_id):
        """開始固定日を変えたタスクの、変更案の移動（ドラッグ・ずらす）を消す。

        後からした操作を優先する。以前は移動の記録が固定日より優先され、ドラッグした
        タスクに固定日を設定しても（解除しても）動かず、固定日は保存されたまま隠れて
        いた（未確定に戻すとその日付へ飛んだ）。"""
        self._conn.execute(
            "DELETE FROM draft_moves WHERE job_id = ? AND workflow_task_id = ?",
            (job_id, workflow_task_id),
        )

    # 進行中・完了（実績として日程を固定する状態。gui/plan_confirmation._STARTED と同じ）
    _STARTED_STATUSES = ("in_progress", "done")

    def _record_status_fact(self, job_id, workflow_task_id, old_status, new_status):
        """タスクの状態が変わったとき、今の日程を実績（task_facts）として記録する
        （未着手に戻したら記録を消す）。

        進行中・完了のタスクは実績の日程に固定して計算する（gui/plan_confirmation.
        PlanState.facts）。状態を変えてもバーが動かないよう、状態を変える直前に表示
        していた日程（displayed_rows_provider が返す）を記録する。

        - 確定した計画（confirmed_schedule）とは別に持つ。確定していない計画・確定後に
          足したタスクでも記録する（以前は確定行に書いていたので、確定行の無いタスクは
          記録できず、後の編集で完了のタスクが動いていた）
        - 進行中→完了では開始日を残し、終了日を今の表示（開始日＋今の日数）にする
        - 表示中の日程が得られない（検証エラーで計算できない、GUIを介さない利用）ときは
          確定した日程を使い、それも無ければ記録しない"""
        was_started = old_status in self._STARTED_STATUSES
        now_started = new_status in self._STARTED_STATUSES
        if old_status == new_status or not (was_started or now_started):
            return
        key = (job_id, workflow_task_id)
        if not now_started:
            self._conn.execute(
                "DELETE FROM task_facts WHERE job_id = ? AND workflow_task_id = ?", key
            )
            return
        row = self._displayed_row(key)
        if row is None:
            row = self._conn.execute(
                "SELECT * FROM confirmed_schedule WHERE job_id = ? AND workflow_task_id = ?", key
            ).fetchone()
            if row is None:
                return
        existing = self._conn.execute(
            "SELECT start_date FROM task_facts WHERE job_id = ? AND workflow_task_id = ?", key
        ).fetchone()
        start = existing["start_date"] if existing is not None and was_started else row["start_date"]
        end = max(row["end_date"], (date.fromisoformat(start) + timedelta(days=1)).isoformat())
        self._conn.execute(
            "INSERT OR REPLACE INTO task_facts(job_id, workflow_task_id, start_date, end_date, days, team_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, workflow_task_id, start, end, int(row["days"]), row["team_id"]),
        )

    def _displayed_row(self, key):
        """key のタスクの、表示中の日程（確定行と同じ形の辞書。無ければ None）。

        1回の操作（Undoの1単位）の中では、最初に取った日程を使い回す。複数の
        タスクをまとめて進行中にするとき、1件目を書き込んだ時点で計算結果が古く
        なるが、残りも操作の前に表示していた日程で記録したいため（計算し直すと
        遅いうえ、書き込み途中の状態で計算することになる）。"""
        provider = self.displayed_rows_provider
        if provider is None:
            return None
        in_operation = self._in_undoable_call or self._open_group is not None
        memo = self._displayed_rows_memo
        if in_operation and memo is not None and memo[0] == self._operation_serial:
            rows = memo[1]
        else:
            rows = {(r["job_id"], r["workflow_task_id"]): r for r in (provider(None) or [])}
            if in_operation:
                self._displayed_rows_memo = (self._operation_serial, rows)
        return rows.get(key)

    def list_task_facts(self):
        """実績（進行中・完了のタスクの日程）。今のジョブのワークフローに属するタスク
        のものだけ（ワークフローを差し替えたジョブの古い行は拾わない）。"""
        rows = self._conn.execute(
            "SELECT f.job_id, f.workflow_task_id, f.start_date, f.end_date, f.days, f.team_id "
            "FROM task_facts f JOIN jobs j ON j.id = f.job_id "
            "JOIN workflow_tasks wt ON wt.id = f.workflow_task_id AND wt.workflow_id = j.workflow_id"
        ).fetchall()
        return [dict(r) for r in rows]

    @undoable("実績の日付を変更")
    def update_task_fact(self, job_id, workflow_task_id, start_date, end_date=None, days=None):
        """実績の日付を直す（タスクの編集ウィンドウの「実績の開始日」「実績の終了日」）。

        start_date / end_date は 'YYYY-MM-DD'。end_date は exclusive（画面の「終了日」
        の翌日）。進行中のタスクは開始日だけを使うので、end_date・days は省略してよい
        （記録済みの値を残し、開始日より前にならないようにだけ直す）。休業日でもその
        まま記録する（実際に作業した日なので）。"""
        status = self._conn.execute(
            "SELECT status FROM job_task_overrides WHERE job_id = ? AND workflow_task_id = ?",
            (job_id, workflow_task_id),
        ).fetchone()
        if status is None or status["status"] not in self._STARTED_STATUSES:
            raise ProjectDatabaseError(tr("実績の日付は、進行中・完了のタスクだけに記録できます"))
        start = _parse_iso_date(start_date, tr("実績の開始日"))
        existing = self._conn.execute(
            "SELECT end_date, days, team_id FROM task_facts WHERE job_id = ? AND workflow_task_id = ?",
            (job_id, workflow_task_id),
        ).fetchone()
        if end_date is not None:
            end = _parse_iso_date(end_date, tr("実績の終了日"))
            if end <= start:
                raise ProjectDatabaseError(tr("実績の終了日は、開始日以降にしてください"))
        else:
            end = date.fromisoformat(existing["end_date"]) if existing is not None else start
            end = max(end, start + timedelta(days=1))
        if days is None:
            days = existing["days"] if existing is not None else 1
        team_id = existing["team_id"] if existing is not None else None
        self._conn.execute(
            "INSERT OR REPLACE INTO task_facts(job_id, workflow_task_id, start_date, end_date, days, team_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, workflow_task_id, start.isoformat(), end.isoformat(), max(1, int(days)), team_id),
        )
        self._commit()

    # 上書き行の各列の既定値（＝「上書きなし」）。すべて既定なら行を持たない。
    _OVERRIDE_DEFAULTS = {
        "is_active": True, "override_days": None, "milestone_id": None, "team_id": None,
        "start_pin_date": None, "tags": "", "status": None,
    }

    @undoable("タスク上書きを変更")
    def update_job_task_override_fields(self, job_id, workflow_task_id, **fields):
        """タスク上書きのうち、指定した列だけを変える（他の列は現在の値のまま）。

        ガントチャートタブでの編集（開始日だけ・状態だけ、等）の書き込み先。
        upsert_job_task_override() は全列を受け取って行ごと置き換えるAPIのため、
        1列だけ変えたい呼び出し元が使うと、渡さなかった列（タグ等）が既定値で
        上書きされて消えてしまう。

        変更後の全列が既定値なら行自体を消す（差分のみ保持、タブ3と同じ）。
        続けてタブ3と同じくマイルストーンの前後整合（enforce_milestone_floor /
        cascade_milestone_to_successors）を取る。

        fields のキー: is_active / override_days / milestone_id / team_id /
        start_pin_date / tags / status。
        Returns: {"milestone_raised": bool, "milestone_cascaded": [workflow_task_id, ...]}
        """
        unknown = set(fields) - set(self._OVERRIDE_DEFAULTS)
        if unknown:
            raise ProjectDatabaseError(tr("タスク上書きに無い項目です: {value}", value=', '.join(sorted(unknown))))
        row = self._conn.execute(
            "SELECT is_active, override_days, milestone_id, team_id, start_pin_date, "
            "COALESCE(tags, '') AS tags, status "
            "FROM job_task_overrides WHERE job_id = ? AND workflow_task_id = ?",
            (job_id, workflow_task_id),
        ).fetchone()
        values = dict(self._OVERRIDE_DEFAULTS)
        if row is not None:
            values.update(dict(row))
            values["is_active"] = bool(values["is_active"])
        values.update(fields)
        values["is_active"] = bool(values["is_active"])
        if values["override_days"] is not None:
            values["override_days"] = int(values["override_days"])
            if values["override_days"] < 1:
                raise ProjectDatabaseError(tr("日数上書きは1以上で指定してください"))
        values["start_pin_date"] = _validate_start_pin_date(values["start_pin_date"])
        values["tags"] = normalize_tags(values["tags"])
        values["status"] = normalize_task_status(values["status"])

        if values == self._OVERRIDE_DEFAULTS:
            if row is not None:
                self.clear_job_task_override(job_id, workflow_task_id)
        else:
            self.upsert_job_task_override(job_id, workflow_task_id, **values)

        raised = self.enforce_milestone_floor(job_id, workflow_task_id)
        cascaded = self.cascade_milestone_to_successors(job_id, workflow_task_id)
        return {"milestone_raised": raised, "milestone_cascaded": cascaded}

    # -- 計画の確定と変更案（docs/roadmap.md §8） -----------------------------------
    #
    # 状態の判定・指紋・影響範囲の計算は gui/plan_confirmation.py が行う。ここは
    # 確定行（confirmed_schedule）・変更案の記録（draft_moves / draft_base）の
    # 読み書きだけを持つ。

    def list_confirmed_schedule(self):
        rows = self._conn.execute(
            "SELECT job_id, workflow_task_id, start_date, end_date, days, team_id, input_signature "
            "FROM confirmed_schedule"
        ).fetchall()
        return [dict(r) for r in rows]

    def has_confirmation(self):
        """計画を確定しているか（確定した日時 project.confirmed_at で判定する）。"""
        row = self._conn.execute("SELECT confirmed_at FROM project WHERE id = 1").fetchone()
        return row is not None and row["confirmed_at"] is not None

    @undoable("計画を確定")
    def replace_confirmed_schedule(self, rows, confirmed_at, global_signature):
        """確定行を丸ごと置き換える（「確定する」「変更を確定」）。rows は
        job_id / workflow_task_id / start_date / end_date / days / team_id /
        input_signature を持つ辞書の並び。変更案の記録（draft_moves）は消し、
        確定した時点の状態を破棄用に保存し直す（save_draft_base）。"""
        self._conn.execute("DELETE FROM confirmed_schedule")
        self._insert_confirmed_rows(rows)
        self._conn.execute("DELETE FROM draft_moves")
        self._conn.execute(
            "UPDATE project SET confirmed_at = ?, confirmed_global_signature = ? WHERE id = 1",
            (confirmed_at, global_signature),
        )
        # 全面再計画の変更案を確定したら、その基準日・実行日を正式に採用する
        self._conn.execute(
            "UPDATE project SET replan_base_date = pending_replan_base_date, "
            "replanned_at = pending_replanned_at, pending_replan_base_date = NULL, "
            "pending_replanned_at = NULL WHERE id = 1 AND pending_replan_base_date IS NOT NULL"
        )
        self._save_draft_base()
        self._commit()

    @undoable("選択した変更を確定")
    def update_confirmed_rows(self, rows, remove_keys, confirmed_at, global_signature=None):
        """確定行の一部だけを書き換える（「選択した変更を確定」）。rows を上書きし、
        remove_keys（(job_id, workflow_task_id) の並び。無効にしたタスク等）の行を
        消す。対象タスクの変更案の記録（draft_moves）も消す。global_signature を
        渡したとき（全体設定の変更も含めて確定する場合）だけ更新する。"""
        keys = list(remove_keys) + [(r["job_id"], r["workflow_task_id"]) for r in rows]
        for key in keys:
            self._conn.execute(
                "DELETE FROM confirmed_schedule WHERE job_id = ? AND workflow_task_id = ?", key
            )
            self._conn.execute(
                "DELETE FROM draft_moves WHERE job_id = ? AND workflow_task_id = ?", key
            )
        self._insert_confirmed_rows(rows)
        if global_signature is None:
            self._conn.execute("UPDATE project SET confirmed_at = ? WHERE id = 1", (confirmed_at,))
        else:
            self._conn.execute(
                "UPDATE project SET confirmed_at = ?, confirmed_global_signature = ? WHERE id = 1",
                (confirmed_at, global_signature),
            )
        self._merge_into_draft_base(keys)
        self._commit()

    def _merge_into_draft_base(self, keys):
        """「選択した変更を確定」した分（keys のタスクの確定行・入力・実績）を、破棄用の
        スナップショット（draft_base）にも書き込む（_DraftBaseMerger）。

        スナップショットは「最後に確定した時点の状態」なので、一部だけ確定したら
        その分も含めておく。以前は破棄の時点の上書き行を入れ直していたため、一部
        確定した後に同じタスクをさらに編集（チームの変更など）してから「変更を破棄」
        しても、その編集が戻らなかった。"""
        row = self._conn.execute("SELECT started_on, snapshot FROM draft_base WHERE id = 1").fetchone()
        if row is None:
            return
        base = _open_snapshot(row["snapshot"])
        try:
            before = _snapshot_signatures(base)
            merger = _DraftBaseMerger(self._conn, base)
            for key in sorted(set(keys)):
                merger.merge_task(key)
            self._follow_shared_inputs(base, before, set(keys))
            project = self._conn.execute(
                "SELECT confirmed_at, confirmed_global_signature FROM project WHERE id = 1"
            ).fetchone()
            base.execute(
                "UPDATE project SET confirmed_at = ?, confirmed_global_signature = ? WHERE id = 1",
                (project["confirmed_at"], project["confirmed_global_signature"]),
            )
            base.commit()
            snapshot = zlib.compress(base.serialize(), 6)
        finally:
            base.close()
        self._conn.execute(
            "UPDATE draft_base SET snapshot = ? WHERE id = 1", (snapshot,)
        )

    def _follow_shared_inputs(self, base, before, merged):
        """一部だけ確定したときに破棄用のスナップショットへ書き込んだ、ジョブ単位・
        ワークフロー単位の入力（ジョブの優先度・ワークフロー内の依存）に、一緒に確定
        しなかった同じジョブ・ワークフローのタスクの確定行の指紋を合わせる。

        これらの入力はジョブ・ワークフロー全体で1つなので、1タスクを確定した時点で
        確定した側の値になる。指紋を古い値のままにしておくと、そのタスクに別の変更も
        あって一緒に確定しなかった場合（gui/plan_confirmation.shared_change_companions
        の対象外）、「変更を破棄」でスナップショットに戻しても指紋が合わず、確定済みに
        戻らなかった。指紋がスナップショットの入力と合っていた確定行だけを書き換える
        （今のDB・スナップショットの両方）。"""
        after = _snapshot_signatures(base)
        for key, was in before.items():
            now = after.get(key)
            if key in merged or now is None or now == was:
                continue
            where = "job_id = ? AND workflow_task_id = ? AND input_signature = ?"
            for conn in (self._conn, base):
                conn.execute(
                    f"UPDATE confirmed_schedule SET input_signature = ? WHERE {where}", (now, *key, was)
                )

    def draft_base_shared_inputs(self):
        """最後に確定した時点（draft_base）の、ジョブ単位・ワークフロー単位の入力。
        「選択した変更を確定」で、優先度・ワークフロー内の依存のように1タスクだけでは
        確定できない変更を、同じ変更だけを受けたタスクへ広げるのに使う
        （gui/plan_confirmation.shared_change_companions）。スナップショットが無ければ None。

        Returns: {"priority": {job_id: 優先度}, "wf_preds": {後続の workflow_task_id:
        [("wf", 先行, 種別, ラグ), ...]}, "workflows": {workflow_id, ...}（依存を比べる対象）}"""
        row = self._conn.execute("SELECT snapshot FROM draft_base WHERE id = 1").fetchone()
        if row is None:
            return None
        base = _open_snapshot(row["snapshot"])
        try:
            priority = {r["id"]: r["priority"] for r in base.execute("SELECT id, priority FROM jobs")}
            wf_preds = {}
            for d in base.execute(
                "SELECT predecessor_task_id, successor_task_id, dep_type, lag_days FROM task_dependencies"
            ):
                wf_preds.setdefault(d["successor_task_id"], []).append(
                    ("wf", d["predecessor_task_id"], d["dep_type"], d["lag_days"])
                )
            workflows = {r["id"] for r in base.execute("SELECT id FROM workflows")}
        finally:
            base.close()
        return {"priority": priority, "wf_preds": wf_preds, "workflows": workflows}

    def _insert_confirmed_rows(self, rows):
        self._conn.executemany(
            "INSERT INTO confirmed_schedule(job_id, workflow_task_id, start_date, end_date, days, "
            "team_id, input_signature) VALUES (:job_id, :workflow_task_id, :start_date, :end_date, "
            ":days, :team_id, :input_signature)",
            rows,
        )

    @undoable("未確定に戻す")
    def clear_confirmation(self):
        """プロジェクト全体を未確定に戻す（手動ピン・進捗は残す）。変更案の記録と基準日も消す。

        進行中・完了のタスクの実績（task_facts）は確定とは別に持っているので残る。
        実施した事実なので、未確定に戻してもその日程で固定したまま計算する
        （gui/plan_confirmation.PlanState.facts）。"""
        self._conn.execute("DELETE FROM confirmed_schedule")
        self._conn.execute("DELETE FROM draft_moves")
        self._conn.execute("DELETE FROM draft_base")
        self._conn.execute(
            "UPDATE project SET replan_base_date = NULL, replanned_at = NULL, confirmed_at = NULL, "
            "pending_replan_base_date = NULL, pending_replanned_at = NULL, "
            "confirmed_global_signature = NULL WHERE id = 1"
        )
        self._commit()

    @undoable("全面再計画")
    def start_full_replan(self, base_date, executed_on):
        """全面再計画を始める（§8-6）。結果は変更案として見せるだけで、確定は
        「変更を確定」で初めて書き換わる。base_date: 新しい計画が始まる日（基準日 D）、
        executed_on: 実行した日（T）。いずれも 'YYYY-MM-DD'。"""
        if not self.has_confirmation():
            raise ProjectDatabaseError(tr("確定していない計画は全面再計画できません"))
        for value in (base_date, executed_on):
            date.fromisoformat(value)
        self._conn.execute(
            "UPDATE project SET pending_replan_base_date = ?, pending_replanned_at = ? WHERE id = 1",
            (base_date, executed_on),
        )
        self._commit()

    def list_draft_moves(self):
        rows = self._conn.execute(
            "SELECT job_id, workflow_task_id, start_date FROM draft_moves"
        ).fetchall()
        return [dict(r) for r in rows]

    @undoable("変更案でタスクを移動")
    def set_draft_move(self, job_id, workflow_task_id, start_date):
        """確定済みのファイルで、ガントからドラッグした開始日を変更案として記録する
        （手動ピンにはしない。§8-8）。"""
        start_date = _validate_start_pin_date(start_date)
        self._conn.execute(
            "INSERT INTO draft_moves(job_id, workflow_task_id, start_date) VALUES (?, ?, ?) "
            "ON CONFLICT(job_id, workflow_task_id) DO UPDATE SET start_date = excluded.start_date",
            (job_id, workflow_task_id, start_date),
        )
        self._commit()

    def _save_draft_base(self):
        """いまの状態（確定した直後の状態）を、変更案の破棄用に圧縮して保存する。
        draft_base 自身を含めないよう、行を消してから取り出す。

        取り出す前にコミットする。Undo・Redo・変更を破棄で DB を読み込み直した
        （deserialize）後は、コミットしていない変更が serialize() に入らず、確定の
        書き込みが抜けたスナップショットになっていた（その後の「変更を破棄」で確定
        する前へ戻り、未確定になることもあった）。"""
        self._conn.execute("DELETE FROM draft_base")
        self._conn.commit()
        snapshot = zlib.compress(self._conn.serialize(), 6)
        self._conn.execute(
            "INSERT INTO draft_base(id, started_on, snapshot) VALUES (1, ?, ?)",
            (date.today().isoformat(), snapshot),
        )

    def has_draft_base(self):
        return self._conn.execute("SELECT 1 FROM draft_base WHERE id = 1").fetchone() is not None

    @undoable("変更を破棄")
    def discard_draft(self, restore_statuses=False):
        """変更案を破棄し、最後に確定した時点の状態に戻す（§8-8）。

        restore_statuses: タスクの状態（未着手／進行中／完了）と実績も確定した時点に
        戻すか。既定（False）では今のまま残す——実際に起きたことの記録であって、計画の
        変更ではないため（破棄の確認画面のチェックで選ぶ）。
        「選択した変更を確定」で確定した分は、確定した時点でスナップショットにも
        書き込んである（_merge_into_draft_base）ので、確定の一部として残る。"""
        row = self._conn.execute("SELECT snapshot FROM draft_base WHERE id = 1").fetchone()
        if row is None:
            raise ProjectDatabaseError(tr("破棄して戻す先（最後に確定した時点の状態）がありません"))
        snapshot = row["snapshot"]
        kept = None if restore_statuses else self._statuses_and_facts(self._conn)
        before = self._conn.serialize()
        self._conn.deserialize(zlib.decompress(snapshot))
        self._conn.execute("PRAGMA foreign_keys = ON")
        try:
            _upgrade_snapshot_schema(self._conn)
            self._restore_kept_changes_after_discard(snapshot)
            if kept is not None:
                self._reapply_statuses_and_facts(*kept)
        except BaseException:
            # 途中で失敗したら、破棄する前の状態へ丸ごと戻す。deserialize() は
            # トランザクションの巻き戻しでは戻らないため、そのままだと半端な状態が残る。
            self._rollback_uncommitted()
            self._conn.deserialize(before)
            self._conn.execute("PRAGMA foreign_keys = ON")
            raise
        self._commit()

    def _restore_kept_changes_after_discard(self, snapshot):
        """discard_draft() の後半: 最後に確定した時点へ戻したDBに、破棄用のスナップ
        ショット自身を入れ直す（破棄後も同じ内容＝最後に確定した時点を持ち続ける。
        スナップショットは draft_base 自身を含まないため）。"""
        self._conn.execute("DELETE FROM draft_base")
        self._conn.execute(
            "INSERT INTO draft_base(id, started_on, snapshot) VALUES (1, ?, ?)",
            (date.today().isoformat(), snapshot),
        )

    @staticmethod
    def _statuses_and_facts(conn):
        """({(job_id, workflow_task_id): 状態}（進行中・完了のもの）, {同: 実績の行})。"""
        statuses = {
            (r["job_id"], r["workflow_task_id"]): r["status"]
            for r in conn.execute(
                "SELECT job_id, workflow_task_id, status FROM job_task_overrides "
                "WHERE status IN ('in_progress', 'done')"
            ).fetchall()
        }
        facts = {
            (r["job_id"], r["workflow_task_id"]): dict(r)
            for r in conn.execute("SELECT * FROM task_facts").fetchall()
        }
        return statuses, facts

    def _reapply_statuses_and_facts(self, statuses, facts):
        """discard_draft() で、破棄する前の状態と実績を戻したDBへ入れ直す（破棄で
        消えたジョブ・タスクの分は捨てる）。"""
        tasks = {
            (r["job_id"], r["workflow_task_id"])
            for r in self._conn.execute(
                "SELECT j.id AS job_id, wt.id AS workflow_task_id FROM jobs j "
                "JOIN workflow_tasks wt ON wt.workflow_id = j.workflow_id"
            ).fetchall()
        }
        teams = {r["id"] for r in self._conn.execute("SELECT id FROM teams").fetchall()}
        self._conn.execute("UPDATE job_task_overrides SET status = NULL WHERE status IS NOT NULL")
        for key, status in statuses.items():
            if key not in tasks:
                continue
            updated = self._conn.execute(
                "UPDATE job_task_overrides SET status = ? WHERE job_id = ? AND workflow_task_id = ?",
                (status, *key),
            ).rowcount
            if not updated:
                self._conn.execute(
                    "INSERT INTO job_task_overrides(job_id, workflow_task_id, is_active, status) "
                    "VALUES (?, ?, 1, ?)",
                    (*key, status),
                )
        # 状態を消しただけで既定値に戻った上書き行は持たない（差分のみ保持）
        self._conn.execute(
            "DELETE FROM job_task_overrides WHERE is_active = 1 AND override_days IS NULL "
            "AND milestone_id IS NULL AND team_id IS NULL AND start_pin_date IS NULL "
            "AND COALESCE(tags, '') = '' AND status IS NULL"
        )
        self._conn.execute("DELETE FROM task_facts")
        for key, fact in facts.items():
            if key not in tasks or key not in statuses:
                continue
            fact = dict(fact)
            if fact["team_id"] not in teams:
                fact["team_id"] = None
            _insert_row(self._conn, "task_facts", fact)

    def status_changes_since_base(self):
        """最後に確定した時点から、状態・実績が変わったタスクの数（破棄の確認画面で
        「状態も元に戻す」を出すかどうか）。スナップショットが無ければ0。"""
        row = self._conn.execute("SELECT snapshot FROM draft_base WHERE id = 1").fetchone()
        if row is None:
            return 0
        base = _open_snapshot(row["snapshot"])
        try:
            was_statuses, was_facts = self._statuses_and_facts(base)
        finally:
            base.close()
        now_statuses, now_facts = self._statuses_and_facts(self._conn)
        keys = set(was_statuses) | set(now_statuses) | set(was_facts) | set(now_facts)
        return sum(
            1 for k in keys
            if was_statuses.get(k) != now_statuses.get(k) or was_facts.get(k) != now_facts.get(k)
        )

    def _set_override_milestone(self, job_id, workflow_task_id, milestone_id):
        """上書き行のマイルストーンだけを差し替える（他の列には触れない）。

        upsert_job_task_override() は全列を受け取って行ごと置き換えるAPIであり、
        呼び出し元が渡さなかった列は既定値（start_pin_date=None, tags=""）で
        上書きされる。マイルストーンの自動調整（enforce_milestone_floor /
        cascade_milestone_to_successors / apply_milestone_consistency_repair）は
        マイルストーン以外を変えるつもりが無いため、全列版を使うと
        「開始固定日とタスク タグが黙って消える」ことになる。列を1つ足すたびに
        同じ事故が起きる形を残さないよう、これらの経路は最初から
        「マイルストーン列だけのUPDATE」を使う。

        まだ上書き行が無い場合は、マイルストーンだけを持つ行を新規に作る
        （他の列は既定のまま＝「上書きなし」）。
        """
        existing = self._conn.execute(
            "SELECT id FROM job_task_overrides WHERE job_id = ? AND workflow_task_id = ?",
            (job_id, workflow_task_id),
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE job_task_overrides SET milestone_id = ? WHERE id = ?",
                (milestone_id, existing["id"]),
            )
        else:
            self._conn.execute(
                "INSERT INTO job_task_overrides(job_id, workflow_task_id, is_active, "
                "milestone_id) VALUES (?, ?, 1, ?)",
                (job_id, workflow_task_id, milestone_id),
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
                self._set_override_milestone(job_id, succ_id, this_ms["milestone_id"])
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

        self._set_override_milestone(job_id, workflow_task_id, floor["milestone_id"])
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

    def _milestone_repair_plan_for_job(self, job, milestone_names):
        """1ジョブ分の再調整計画。enforce_milestone_floor と同じ判定
        （実効マイルストーンが未設定、または先行タスクの最も遅い実効
        マイルストーンより早ければ、そこまで引き上げる）を、依存の深さ順に
        伝播させて計算する。書き込みは一切行わない。

        milestone_names: {milestone_id: 名前}。確認ダイアログで「どのマイル
        ストーンへ変わるか」まで見せるため、日付だけでなく名前も計画に含める
        （引き上げでは締切日だけでなくマイルストーン自体が先行タスクのものに
        差し替わるので、名前が見えないと変更内容が伝わらない）。"""
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
                "from_milestone_id": current[1] if current else None,
                "from_milestone_name": milestone_names.get(current[1]) if current else None,
                "to_end_date": floor[0], "to_milestone_id": floor[1],
                "to_milestone_name": milestone_names.get(floor[1]),
            })
            # 引き上げた結果をさらに後続へ伝播させる（cascade相当）
            effective[tid] = floor
        return plan

    def plan_milestone_consistency_repair(self):
        """全ジョブを検査し、先行タスクより早い締切のマイルストーンが設定されて
        いるタスクの再調整計画を返す（書き込みは行わない）。空リストなら
        整合しており、何もする必要がない。適用は
        apply_milestone_consistency_repair(plan)。"""
        milestone_names = {m["id"]: m["name"] for m in self.list_milestones()}
        plan = []
        for job in self.list_jobs():
            plan.extend(self._milestone_repair_plan_for_job(job, milestone_names))
        return plan

    @undoable("マイルストーンの整合性を再調整")
    def apply_milestone_consistency_repair(self, plan):
        """plan_milestone_consistency_repair() が返した計画をそのまま適用する。
        計画と適用を分けているため、確認ダイアログに出した内容と実際に適用される
        内容が食い違うことはない。"""
        for item in plan:
            self._set_override_milestone(
                item["job_id"], item["workflow_task_id"], item["to_milestone_id"]
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
            raise ProjectDatabaseError(tr("同じタスクへの自己依存は設定できません"))
        try:
            cur = self._conn.execute(
                "INSERT INTO job_external_dependencies(job_id, workflow_task_id, "
                "depends_on_job_id, depends_on_workflow_task_id) VALUES (?, ?, ?, ?)",
                (job_id, workflow_task_id, depends_on_job_id, depends_on_workflow_task_id),
            )
        except sqlite3.IntegrityError as e:
            raise ProjectDatabaseError(tr("この依存関係は既に登録されています")) from e
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
            raise ProjectDatabaseError(tr("この依存関係は存在しません"))
        job_id = row["job_id"]
        if (job_id, workflow_task_id) == (depends_on_job_id, depends_on_workflow_task_id):
            raise ProjectDatabaseError(tr("同じタスクへの自己依存は設定できません"))
        try:
            self._conn.execute(
                "UPDATE job_external_dependencies SET workflow_task_id = ?, "
                "depends_on_job_id = ?, depends_on_workflow_task_id = ? WHERE id = ?",
                (workflow_task_id, depends_on_job_id, depends_on_workflow_task_id, dependency_id),
            )
        except sqlite3.IntegrityError as e:
            raise ProjectDatabaseError(tr("この依存関係は既に登録されています")) from e
        self._commit()

    @undoable(lambda self, dependency_id, is_active: tr("個別のタスク依存を有効化") if is_active else tr("個別のタスク依存を無効化"))
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
        NULL）には一切触れない。

        実際に何も変わらなかった場合はcommitしない（revisionを進めない）。
        この関数は各タブのrefresh_choices()（gui/tab_jobs.py）からタブを
        切り替えるたびに呼ばれる「保険」の同期であり、ほとんどの呼び出しは
        何も変えずに終わる。無条件にcommitするとrevisionが毎回進み、
        gui/tab_gantt.pyがrevisionの一致でスケジューリングの再実行を省く
        判定をしているため、タブを切り替えるだけで毎回フルの再スケジュー
        リングが走ってしまう（実際には何も変わっていないのに、である）。"""
        changes_before = self._conn.total_changes
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
        if self._conn.total_changes == changes_before:
            return  # 実際には何も変わらなかった（revisionを進めない）
        self._commit()

    @undoable("依存先ジョブを追加")
    def add_job_dependency_link(self, job_id, depends_on_job_id):
        if job_id == depends_on_job_id:
            raise ProjectDatabaseError(tr("同じジョブへの自己依存は設定できません"))
        try:
            cur = self._conn.execute(
                "INSERT INTO job_dependency_links(job_id, depends_on_job_id) VALUES (?, ?)",
                (job_id, depends_on_job_id),
            )
        except sqlite3.IntegrityError as e:
            raise ProjectDatabaseError(tr("このジョブへの依存は既に登録されています")) from e
        link_id = cur.lastrowid
        self.sync_dependency_templates()
        # sync_dependency_templates は自分が何も変えなければコミットしない
        # （このリンクに当てはまるテンプレートが無い場合）。リンクの追加自体は
        # ここで確定させる——しないとトランザクションが開いたまま残り、直後の
        # 保存が固まる（_rollback_uncommitted 参照）うえ、revision も進まない。
        self._commit()
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
                tr("このテンプレートを追加すると、ワークフロー間で循環依存になるため追加できません")
            )
        try:
            cur = self._conn.execute(
                "INSERT INTO workflow_dependency_templates "
                "(workflow_id, workflow_task_id, depends_on_workflow_id, "
                "depends_on_workflow_task_id) VALUES (?, ?, ?, ?)",
                (workflow_id, workflow_task_id, depends_on_workflow_id, depends_on_workflow_task_id),
            )
        except sqlite3.IntegrityError as e:
            raise ProjectDatabaseError(tr("このテンプレートは既に登録されています")) from e
        template_id = cur.lastrowid
        # 既にこのワークフローペアで「依存先ジョブ」のリンクが張られている
        # ジョブがあれば、新しいテンプレートのタスク対応をそのリンクにも展開
        # する（依存先ジョブを先に追加し、後からテンプレートを設定した場合の
        # 救済）。
        self.sync_dependency_templates()
        self._commit()  # 展開先が無くてもテンプレートの追加自体は確定させる（add_job_dependency_link 参照）
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
            raise ProjectDatabaseError(tr("このテンプレートは存在しません"))
        workflow_id = row["workflow_id"]
        if self._would_create_workflow_template_cycle(
            workflow_id, depends_on_workflow_id, exclude_template_id=template_id
        ):
            raise ProjectDatabaseError(
                tr("この変更を行うと、ワークフロー間で循環依存になるため変更できません")
            )
        try:
            self._conn.execute(
                "UPDATE workflow_dependency_templates SET workflow_task_id = ?, "
                "depends_on_workflow_id = ?, depends_on_workflow_task_id = ? WHERE id = ?",
                (workflow_task_id, depends_on_workflow_id, depends_on_workflow_task_id, template_id),
            )
        except sqlite3.IntegrityError as e:
            raise ProjectDatabaseError(tr("このテンプレートは既に登録されています")) from e
        # 変更前のタスク対応で自動生成されていた行は、もう現在のテンプレートに
        # 合致しなくなるため sync_dependency_templates が削除し、変更後の
        # タスク対応が新たに展開される。
        self.sync_dependency_templates()
        self._commit()  # 展開先が無くても変更自体は確定させる（add_job_dependency_link 参照）

    @undoable("依存テンプレートを削除")
    def delete_dependency_template(self, template_id):
        self._conn.execute(
            "DELETE FROM workflow_dependency_templates WHERE id = ?", (template_id,)
        )
        # このテンプレートから自動生成されていたタスク対応は、もうどの
        # テンプレートにも合致しなくなるため sync_dependency_templates が削除する。
        self.sync_dependency_templates()
        self._commit()  # 展開先が無くても削除自体は確定させる（add_job_dependency_link 参照）


# -- 破棄用のスナップショット（draft_base）の読み書き --------------------------------


def _upgrade_snapshot_schema(conn):
    """読み込んだスナップショット（conn）が古いスキーマなら今の形に移す。

    draft_base には確定した時点のDB全体が入っているので、古いバージョンで確定した
    ファイルでは、その時点のスキーマのまま残っている。そのまま戻すと、今のスキーマに
    ある表（例: task_facts）が無くなってしまう。"""
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None or row["value"] != SCHEMA_VERSION:
        _migrate_schema(conn)


def _snapshot_signatures(conn):
    """接続 conn（破棄用のスナップショット等）の、全タスクの入力の指紋
    （gui/plan_confirmation.task_signatures と同じもの）。"""
    from types import SimpleNamespace

    from gui.plan_confirmation import task_signatures

    return task_signatures(SimpleNamespace(_conn=conn))


def _open_snapshot(blob):
    """draft_base のスナップショット（zlib 圧縮）を、別のメモリ上の接続として開く。"""
    base = sqlite3.connect(":memory:")
    base.row_factory = sqlite3.Row
    base.deserialize(zlib.decompress(blob))
    _upgrade_snapshot_schema(base)
    return base


def _row_values(row, drop=()):
    return {k: row[k] for k in row.keys() if k not in drop}


def _insert_row(conn, table, values, unique_name=False):
    """values（{列: 値}）を1行挿入する。unique_name なら、名前の重複（確定した後に
    名前を付け替えた別のチーム等）を避けるため、ぶつかったら「名前 (2)」の形にする。
    （INSERT OR REPLACE で上書きすると、同じ名前の別の行が消えてしまう）"""
    values = dict(values)
    columns = list(values)
    sql = (f"INSERT INTO {table}({', '.join(columns)}) "
           f"VALUES ({', '.join('?' for _ in columns)})")
    name = values.get("name")
    for n in range(2, 1000):
        try:
            conn.execute(sql, [values[c] for c in columns])
            return
        except sqlite3.IntegrityError:
            if not unique_name or name is None:
                raise
            values["name"] = f"{name} ({n})"
    raise ProjectDatabaseError(tr("名前が重複しています: {name}", name=name))


class _DraftBaseMerger:
    """「選択した変更を確定」したタスクを、破棄用のスナップショット（最後に確定した
    時点のDB。base）へ書き込む（ProjectDatabase._merge_into_draft_base）。

    「変更を破棄」の後も、確定したタスクが確定したときと同じ入力・日程で残るように
    する。以前は確定行と上書き行だけを書き込んでいたため、
    - 確定した後に足したジョブのタスクは書き込めず、破棄でジョブごと消えていた
    - ワークフローの既定の日数・チームを変えて確定したタスクは、破棄で既定が戻り、
      破棄した直後から「変更あり」になっていた

    確定した後に足したジョブ・ワークフロー・チーム・マイルストーンは、必要なものだけ
    スナップショットへ写す。ワークフローの既定（全ジョブで共有）はスナップショット
    側では変えず、確定したタスクの上書き（日数・チーム）として写す。"""

    def __init__(self, conn, base):
        self.conn = conn
        self.base = base

    def _current(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def _base_has(self, table, entity_id):
        return self.base.execute(f"SELECT 1 FROM {table} WHERE id = ?", (entity_id,)).fetchone() is not None

    def _copy_entity(self, table, entity_id):
        """今のDBの table の id の行を、スナップショットに無ければ写す。写せたら（既に
        あったら）真。"""
        if entity_id is None:
            return False
        if self._base_has(table, entity_id):
            return True
        row = self.conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
        if row is None:
            return False
        _insert_row(self.base, table, _row_values(row), unique_name="name" in row.keys())
        return True

    def ensure_team(self, team_id):
        if team_id is None or self._base_has("teams", team_id):
            return
        if self._copy_entity("teams", team_id):
            for c in self._current("SELECT * FROM team_capacity_changes WHERE team_id = ?", (team_id,)):
                _insert_row(self.base, "team_capacity_changes", _row_values(c, drop=("id",)))

    def ensure_milestone(self, milestone_id):
        if milestone_id is not None:
            self._copy_entity("milestones", milestone_id)

    def ensure_workflow(self, workflow_id):
        if self._base_has("workflows", workflow_id):
            return
        if not self._copy_entity("workflows", workflow_id):
            return
        for t in self._current("SELECT * FROM workflow_tasks WHERE workflow_id = ?", (workflow_id,)):
            self.ensure_team(t["team_id"])
            _insert_row(self.base, "workflow_tasks", _row_values(t), unique_name=True)
        for d in self._current("SELECT * FROM task_dependencies WHERE workflow_id = ?", (workflow_id,)):
            _insert_row(self.base, "task_dependencies", _row_values(d, drop=("id",)))

    def ensure_workflow_task(self, workflow_task_id):
        """確定した後にワークフローへ足したタスク。ワークフローごと無ければ丸ごと写す。"""
        if self._base_has("workflow_tasks", workflow_task_id):
            return
        t = self.conn.execute("SELECT * FROM workflow_tasks WHERE id = ?", (workflow_task_id,)).fetchone()
        if t is None:
            return
        self.ensure_workflow(t["workflow_id"])
        if self._base_has("workflow_tasks", workflow_task_id):
            return
        self.ensure_team(t["team_id"])
        _insert_row(self.base, "workflow_tasks", _row_values(t), unique_name=True)
        for d in self._current(
            "SELECT * FROM task_dependencies WHERE predecessor_task_id = ? OR successor_task_id = ?",
            (workflow_task_id, workflow_task_id),
        ):
            if (self._base_has("workflow_tasks", d["predecessor_task_id"])
                    and self._base_has("workflow_tasks", d["successor_task_id"])):
                self.base.execute(
                    "DELETE FROM task_dependencies WHERE predecessor_task_id = ? AND successor_task_id = ?",
                    (d["predecessor_task_id"], d["successor_task_id"]),
                )
                _insert_row(self.base, "task_dependencies", _row_values(d, drop=("id",)))

    def ensure_job(self, job_id):
        if self._base_has("jobs", job_id):
            return True
        job = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return False
        self.ensure_workflow(job["workflow_id"])
        values = _row_values(job)
        if not self._copy_entity("milestones", job["default_milestone_id"]):
            values["default_milestone_id"] = None
        _insert_row(self.base, "jobs", values, unique_name=True)
        return True

    def _effective(self, conn, key):
        row = conn.execute(
            "SELECT wt.default_days, wt.team_id AS default_team_id, o.override_days, "
            "o.team_id AS override_team_id FROM jobs j "
            "JOIN workflow_tasks wt ON wt.workflow_id = j.workflow_id AND wt.id = ? "
            "LEFT JOIN job_task_overrides o ON o.job_id = j.id AND o.workflow_task_id = wt.id "
            "WHERE j.id = ?",
            (key[1], key[0]),
        ).fetchone()
        if row is None:
            return None
        return (row["override_days"] or row["default_days"],
                row["override_team_id"] or row["default_team_id"])

    def merge_task(self, key):
        job_id, task_id = key
        if not self.ensure_job(job_id):
            return  # 削除したジョブ（選んで確定することは無い）
        self.ensure_workflow_task(task_id)
        in_job = self.base.execute(
            "SELECT 1 FROM jobs j JOIN workflow_tasks wt ON wt.workflow_id = j.workflow_id "
            "WHERE j.id = ? AND wt.id = ?", key,
        ).fetchone()
        if in_job is None:
            return  # 確定した後にジョブのワークフローを差し替えた。入力を写しようがない
        where = "job_id = ? AND workflow_task_id = ?"

        # 確定行
        self.base.execute(f"DELETE FROM confirmed_schedule WHERE {where}", key)
        for r in self._current(f"SELECT * FROM confirmed_schedule WHERE {where}", key):
            self.ensure_team(r["team_id"])
            _insert_row(self.base, "confirmed_schedule", _row_values(r))

        # 入力（上書き行）。ワークフローの既定を変えて確定した分は、上書きとして写す
        self.base.execute(f"DELETE FROM job_task_overrides WHERE {where}", key)
        override = self.conn.execute(f"SELECT * FROM job_task_overrides WHERE {where}", key).fetchone()
        values = _row_values(override, drop=("id",)) if override is not None else {
            "job_id": job_id, "workflow_task_id": task_id, "is_active": 1,
        }
        self.ensure_milestone(values.get("milestone_id"))
        self.ensure_team(values.get("team_id"))
        _insert_row(self.base, "job_task_overrides", values)
        now = self._effective(self.conn, key)
        was = self._effective(self.base, key)
        if now is not None and was is not None:
            if now[0] != was[0]:
                self.base.execute(
                    f"UPDATE job_task_overrides SET override_days = ? WHERE {where}", (now[0], *key)
                )
            if now[1] != was[1]:
                self.ensure_team(now[1])
                self.base.execute(
                    f"UPDATE job_task_overrides SET team_id = ? WHERE {where}", (now[1], *key)
                )
        if override is None and now == was:
            # 既定のままのタスク（上書き行が要らない）
            self.base.execute(f"DELETE FROM job_task_overrides WHERE {where}", key)

        # ジョブ間の依存（このタスクが後続の側）
        self.base.execute(f"DELETE FROM job_external_dependencies WHERE {where}", key)
        for e in self._current(f"SELECT * FROM job_external_dependencies WHERE {where}", key):
            if not (self._base_has("jobs", e["depends_on_job_id"])
                    and self._base_has("workflow_tasks", e["depends_on_workflow_task_id"])):
                continue
            values = _row_values(e, drop=("id",))
            link = e["source_link_id"]
            if link is not None and not self._base_has("job_dependency_links", link):
                row = self.conn.execute("SELECT * FROM job_dependency_links WHERE id = ?", (link,)).fetchone()
                if row is not None and self._base_has("jobs", row["depends_on_job_id"]):
                    self.base.execute(
                        "DELETE FROM job_dependency_links WHERE job_id = ? AND depends_on_job_id = ?",
                        (row["job_id"], row["depends_on_job_id"]),
                    )
                    _insert_row(self.base, "job_dependency_links", _row_values(row))
                else:
                    values["source_link_id"] = None
            _insert_row(self.base, "job_external_dependencies", values)

        # ジョブの優先度・ワークフロー内の依存（ジョブ単位・ワークフロー単位の入力）
        priority = self.conn.execute("SELECT priority FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.base.execute("UPDATE jobs SET priority = ? WHERE id = ?", (priority["priority"], job_id))
        workflow_id = self.conn.execute(
            "SELECT workflow_id FROM workflow_tasks WHERE id = ?", (task_id,)
        ).fetchone()["workflow_id"]
        deps_sql = ("SELECT predecessor_task_id, successor_task_id, dep_type, lag_days "
                    "FROM task_dependencies WHERE workflow_id = ? ORDER BY predecessor_task_id, successor_task_id")
        now_deps = [tuple(r) for r in self._current(deps_sql, (workflow_id,))]
        if now_deps != [tuple(r) for r in self.base.execute(deps_sql, (workflow_id,)).fetchall()]:
            self.base.execute("DELETE FROM task_dependencies WHERE workflow_id = ?", (workflow_id,))
            for d in self._current("SELECT * FROM task_dependencies WHERE workflow_id = ?", (workflow_id,)):
                if (self._base_has("workflow_tasks", d["predecessor_task_id"])
                        and self._base_has("workflow_tasks", d["successor_task_id"])):
                    _insert_row(self.base, "task_dependencies", _row_values(d, drop=("id",)))

        # 実績
        self.base.execute(f"DELETE FROM task_facts WHERE {where}", key)
        for f in self._current(f"SELECT * FROM task_facts WHERE {where}", key):
            self.ensure_team(f["team_id"])
            _insert_row(self.base, "task_facts", _row_values(f))
