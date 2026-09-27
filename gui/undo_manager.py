"""
DBスナップショット方式によるUndo/Redoの管理（Qt非依存）。

gui/db.py の ProjectDatabase は、変更系メソッドを @undoable / db.undo_group() で
包んでおり、1つの論理操作（ネストした呼び出しも含む）が完了するたびに1件の
エントリをこのクラスへ積む。1エントリは、その操作の「前」と「後」の両方の
状態——DB全体のバイト列スナップショットと、対になるUI状態（選択・フォーカス）
——を持つ。Undoは前の状態を、Redoは後の状態を復元する、という対称な作りに
することで、「操作を行ったタブへ戻して、その時の選択を復元する」という
振る舞いがUndo/Redoのどちらでも同じように成り立つ。

UI状態の取得・復元・DBスナップショットの適用はQt依存のため、この層は
callableとして呼び出し側（gui/main.py）から差し込んでもらう（このモジュール
自体はQtを一切importしない）。

「後」のUI状態だけは、操作の直後ではなく少し遅らせて取得する。GUI側の
1つの操作は「DBを更新する→表やキャンバスを作り直す→新しい行を選択する」と
いう順で進むため、DB呼び出しが終わった時点ではまだ選択が確定していない
（例: gui/tab_basic_info.py の _add_team は add_team() の後に refresh_teams()
と select_row_by_id() を呼ぶ）。そこで schedule_after_capture 経由で
「現在のイベント処理が終わった後」に取得させ、実際に画面が落ち着いた状態を
記録する。イベントループが無い場面（Qt非依存のテスト等）では取得されない
ままになるが、その場合もDBの復元は通常どおり行われる。
"""

# 1エントリが操作前後2つのDBスナップショット（移行済みサンプルプロジェクトで
# 1つ約124KB）を保持するため、上限がそのままメモリ使用量に効く。100段あれば
# 実用上の「戻したい範囲」は十分に賄えるため、メモリとのバランスでこの値にしている。
#
# 加えて、プロジェクトが大きくなるとスナップショット1つが大きくなるため、
# 段数だけでは使用量が読めない。合計バイト数でも上限を設け、大きい
# プロジェクトでは段数が減る（＝メモリ使用量は頭打ちになる）ようにしている。
#
# 合計バイト数の上限はオプション設定（gui/app_settings.py の
# undo_memory_limit_mb）で変えられる。ここの値はその既定値と揃えてある。
_MAX_STACK_SIZE = 100
_MAX_TOTAL_BYTES = 128 * 1024 * 1024

# 「一度も保存していない」ことを表す番兵。None は「保存した時点で操作履歴が
# 空だった」という正当な状態を表すため、区別する必要がある。
_NEVER_SAVED = object()


class _UndoEntry:
    __slots__ = ("before_db", "before_ui", "after_db", "after_ui", "label")

    def __init__(self, before_db, before_ui, after_db, label):
        self.before_db = before_db
        self.before_ui = before_ui
        self.after_db = after_db
        # 操作直後のUI状態。schedule_after_capture 経由で後から埋まる
        # （埋まらないままでも、DBの復元自体は after_db で正しく行える）。
        self.after_ui = None
        self.label = label


class UndoManager:
    def __init__(self, db, capture_ui_state, restore_ui_state, apply_db_state,
                 on_stack_changed=None, schedule_after_capture=None,
                 max_total_bytes=_MAX_TOTAL_BYTES):
        """
        db: gui.db.ProjectDatabase（このマネージャを db.undo_manager に設定するのは
            呼び出し側の責務）。
        capture_ui_state: () -> 任意のオブジェクト（選択・フォーカス等の記録）。
        restore_ui_state: (state) -> None。DBスナップショットの適用後に呼ばれる。
            state が None（＝操作直後のUI状態を取得できていない場合）でも、
            表示の最新化だけは行えるように実装しておくこと。
        apply_db_state: (blob) -> None。db.restore_state(blob) を呼ぶcallable。
        on_stack_changed: () -> None。Undo/Redoメニューの有効化・ラベル更新用。
        schedule_after_capture: (callable) -> None。渡されたcallableを「現在の
            イベント処理が終わった後」に呼ぶよう予約する（Qt側では
            QTimer.singleShot(0, ...)）。省略時は操作直後のUI状態を記録しない。
        max_total_bytes: 履歴が保持するスナップショットの合計バイト数の上限。
        """
        self.db = db
        self.capture_ui_state = capture_ui_state
        self.restore_ui_state = restore_ui_state
        self.apply_db_state = apply_db_state
        self.on_stack_changed = on_stack_changed
        self.schedule_after_capture = schedule_after_capture
        self._max_total_bytes = max_total_bytes
        self._undo_stack = []
        self._redo_stack = []
        # 「操作直後のUI状態」がまだ埋まっていないエントリ（高々1件）。
        self._pending_after = None
        # 保存済みの状態を指すエントリ（mark_clean で設定）。詳細は is_clean 参照。
        self._clean_marker = _NEVER_SAVED

    def push(self, before_db, before_ui, after_db, label):
        # 直前の操作の「後」の状態がまだ埋まっていなければ、ここで確定させる
        # （1回のイベント処理の中で複数の操作が続けて起きた場合、予約した
        # 取得処理が走る前に次の操作が来るため）。
        self.flush_pending_after_state()

        # 直前のエントリの「後」と今回の「前」は、間に別の変更が無ければ同じ
        # 内容になる。同じbytesオブジェクトを共有させ、保持するスナップショットの
        # 実数をエントリ数+1に抑える（比較はメモリ上の単純な突き合わせで、
        # 既に2回行っているserialize()に比べれば十分安い）。
        if self._undo_stack and self._undo_stack[-1].after_db == before_db:
            before_db = self._undo_stack[-1].after_db

        entry = _UndoEntry(before_db, before_ui, after_db, label)
        self._undo_stack.append(entry)
        self._redo_stack.clear()
        self._trim()

        self._pending_after = entry
        if self.schedule_after_capture is not None:
            self.schedule_after_capture(self.flush_pending_after_state)
        self._notify()

    def _trim(self):
        """段数・合計バイト数の上限を超えた分を、古い方から捨てる。"""
        while len(self._undo_stack) > _MAX_STACK_SIZE:
            self._discard_oldest()
        while len(self._undo_stack) > 1 and self._total_bytes() > self._max_total_bytes:
            self._discard_oldest()

    def set_max_total_bytes(self, max_total_bytes):
        """合計バイト数の上限を変える（オプション設定の変更時）。小さくした場合は、
        超えた分をその場で古い方から捨てる。"""
        self._max_total_bytes = max_total_bytes
        before = len(self._undo_stack)
        self._trim()
        if len(self._undo_stack) != before:
            self._notify()

    def _total_bytes(self):
        # push() で隣接エントリ間のスナップショットを共有しているため、
        # 実際に保持しているのは「先頭の before + 各エントリの after」に相当する。
        if not self._undo_stack:
            return 0
        return len(self._undo_stack[0].before_db) + sum(
            len(e.after_db) for e in self._undo_stack
        )

    def _discard_oldest(self):
        discarded = self._undo_stack.pop(0)
        if discarded is self._clean_marker or self._clean_marker is None:
            # 保存済みの状態を指していたエントリを捨てたので、以降は
            # 「保存時と同じ内容かどうか」を判定できない（＝常に未保存扱い）。
            # None（履歴が空の時点＝最古のエントリの「前」で保存した）も同じ:
            # 最古のエントリを捨てた以上、その状態へはもうUndoで戻れない。
            # 見逃すと、すべてUndoしてスタックが空になった時点で、内容は
            # 保存時と違う（捨てたエントリの変更が残っている）のに「保存済み」と
            # 判定され、閉じても確認が出ずに変更が失われる。
            self._clean_marker = _NEVER_SAVED

    # -- 保存済み状態の追跡 ---------------------------------------------------

    def mark_clean(self):
        """現在の状態を「保存済み」として覚える（保存の直後に呼ぶ）。"""
        self._clean_marker = self._undo_stack[-1] if self._undo_stack else None

    def is_clean(self):
        """現在の内容が、最後に保存した時点と同じなら True。

        Undoで保存時点まで戻した場合も True になる（変更→保存→変更→Undo で
        未保存マークが消える）。判定にはUndoスタックの位置——正確には
        「最後に適用されたエントリ」の同一性——を使う。スナップショット同士を
        比較する方法もあるが、変更のたびにDB全体を比較することになるため、
        位置で見る方が安い。"""
        if self._clean_marker is _NEVER_SAVED:
            return False
        top = self._undo_stack[-1] if self._undo_stack else None
        return top is self._clean_marker

    def flush_pending_after_state(self):
        """「操作直後のUI状態」がまだ記録されていないエントリがあれば、現在の
        画面状態をそれとして記録する。予約された取得処理の本体であり、次の
        操作時やUndo実行時にも取りこぼし防止のため呼ばれる（何も保留が
        無ければ何もしない）。"""
        if self._pending_after is None:
            return
        entry, self._pending_after = self._pending_after, None
        entry.after_ui = self.capture_ui_state()

    def can_undo(self):
        return bool(self._undo_stack)

    def can_redo(self):
        return bool(self._redo_stack)

    def undo_label(self):
        return self._undo_stack[-1].label if self._undo_stack else None

    def redo_label(self):
        return self._redo_stack[-1].label if self._redo_stack else None

    def undo(self):
        # 編集途中（スピンボックスにフォーカスが残ったまま等）なら、その編集を
        # 先に1つのUndo単位として確定させる。そうしないと、これから戻す対象が
        # 「編集前の状態」ではなく1つ前の操作になってしまう。
        self.db.end_undo_group()
        # Undoする操作自体の「後」の状態を確定させてから戻す
        # （この後Redoされたときに、操作直後の画面へ復元できるようにするため）。
        self.flush_pending_after_state()
        if not self._undo_stack:
            return
        entry = self._undo_stack.pop()
        self._redo_stack.append(entry)
        self._apply(entry.before_db, entry.before_ui)
        self._notify()

    def redo(self):
        self.db.end_undo_group()
        if not self._redo_stack:
            return
        entry = self._redo_stack.pop()
        self._undo_stack.append(entry)
        self._apply(entry.after_db, entry.after_ui)
        self._notify()

    def _apply(self, db_state, ui_state):
        """スナップショットの復元とUI状態の復元。この区間でのDBへの変更は
        新しいUndoエントリとして記録しない——GUI側の再読込（各タブの
        refresh_choices()）がDBに書き込むことがあり、抑止しないとそれが
        ユーザーの新しい操作と誤認されてRedoスタックが破棄されてしまうため
        （gui/db.py の suspend_undo_recording 参照）。"""
        with self.db.suspend_undo_recording():
            self.apply_db_state(db_state)
            self.restore_ui_state(ui_state)

    def _notify(self):
        if self.on_stack_changed is not None:
            self.on_stack_changed()
