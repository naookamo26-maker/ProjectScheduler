# CLAUDE.md

## 作業ルール

- 作業の途中経過は日本語で報告すること。
- 画面（GUI）に関係する実装を行った場合は、その画面のスクリーンショットを共有すること。

## Undo/Redo

- GUIで編集可能な機能はすべてUndo/Redoで元に戻せること（内容・選択・アクティブタブを復元。フォーカスは復元しない）。
- `ProjectDatabase`(`gui/db.py`)の変更系メソッドには`@undoable("ラベル")`を付け、`_commit()`経由で書き込む。
- 複数DB呼び出し・複数選択への一括操作は`with db.undo_group("ラベル"):`で1つのUndo単位にまとめる。
- 連続して値が変わる入力（スピンボックス等）は`bind_undo_session()`でフォーカス単位の1Undoにまとめる。
- 新しいタブ・編集機能には`capture_ui_state()`/`restore_ui_state(state)`を実装する。
- 詳細は`docs/architecture.md`の「Undo/Redo」を参照。
