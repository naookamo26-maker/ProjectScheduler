"""
DBスナップショット方式によるUndo/Redoの管理（Qt非依存）。

gui/db.py の ProjectDatabase は、変更系メソッドを @undoable / db.undo_group() で
包んでおり、1つの論理操作（ネストした呼び出しも含む）が完了するたびに
「操作前のDB全体のバイト列スナップショット」を1エントリとしてこのクラスの
Undoスタックへ積む。Undo/Redoは、内容だけでなく操作直前の選択・フォーカスも
まとめて元に戻すため、DBスナップショットと対になる「UI状態」も同じエントリに
保持する。

UI状態の取得・復元・DBスナップショットの適用はQt依存のため、この層は
capture_ui_state / restore_ui_state / apply_db_state という3つのcallableとして
呼び出し側（gui/main.py）から差し込んでもらう（このモジュール自体はQtを
一切importしない）。
"""

_MAX_STACK_SIZE = 200


class _UndoEntry:
    __slots__ = ("db_state", "ui_state", "label")

    def __init__(self, db_state, ui_state, label):
        self.db_state = db_state
        self.ui_state = ui_state
        self.label = label


class UndoManager:
    def __init__(self, db, capture_ui_state, restore_ui_state, apply_db_state, on_stack_changed=None):
        """
        db: gui.db.ProjectDatabase（このマネージャを db.undo_manager に設定するのは
            呼び出し側の責務。db側は capture_ui_state() を呼ぶだけの薄い依存で済む）。
        capture_ui_state: () -> 任意のシリアライズ可能なオブジェクト。
        restore_ui_state: (state) -> None。DBスナップショットの適用後に呼ばれる。
        apply_db_state: (blob) -> None。db.restore_state(blob) に加えて、GUI側の
            各タブの表示をDBの最新内容へ作り直す責務を呼び出し側に持たせるため、
            db.restore_state そのものではなくラップしたcallableを受け取る。
        on_stack_changed: () -> None。Undo/Redoメニューの有効化・ラベル更新用。
        """
        self.db = db
        self.capture_ui_state = capture_ui_state
        self.restore_ui_state = restore_ui_state
        self.apply_db_state = apply_db_state
        self.on_stack_changed = on_stack_changed
        self._undo_stack = []
        self._redo_stack = []

    def push(self, before_db_state, before_ui_state, label):
        self._undo_stack.append(_UndoEntry(before_db_state, before_ui_state, label))
        if len(self._undo_stack) > _MAX_STACK_SIZE:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._notify()

    def reset(self):
        """新規プロジェクトを開いた際に呼び、履歴をすべて破棄する。"""
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._notify()

    def can_undo(self):
        return bool(self._undo_stack)

    def can_redo(self):
        return bool(self._redo_stack)

    def undo_label(self):
        return self._undo_stack[-1].label if self._undo_stack else None

    def redo_label(self):
        return self._redo_stack[-1].label if self._redo_stack else None

    def undo(self):
        if not self._undo_stack:
            return
        entry = self._undo_stack.pop()
        current = _UndoEntry(self.db.serialize_state(), self.capture_ui_state(), entry.label)
        self._redo_stack.append(current)
        self.apply_db_state(entry.db_state)
        self.restore_ui_state(entry.ui_state)
        self._notify()

    def redo(self):
        if not self._redo_stack:
            return
        entry = self._redo_stack.pop()
        current = _UndoEntry(self.db.serialize_state(), self.capture_ui_state(), entry.label)
        self._undo_stack.append(current)
        self.apply_db_state(entry.db_state)
        self.restore_ui_state(entry.ui_state)
        self._notify()

    def _notify(self):
        if self.on_stack_changed is not None:
            self.on_stack_changed()
