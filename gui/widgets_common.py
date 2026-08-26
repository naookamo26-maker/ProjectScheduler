"""
タブ横断で使う共通UI部品。

- CrudSection: 「タイトル + 追加/削除ボタン + 表」の1セクション。
  基本情報設定・ワークフロー設計・ジョブタブで繰り返し使う型。
- make_fk_combo: 外部キー選択用のQComboBox（表示は名前、内部値はDBの整数ID）。
- confirm_or_block_delete: 削除前の参照整合性チェック用ダイアログ。
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDateEdit,
    QGroupBox,
    QHBoxLayout,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

ROW_ID_ROLE = Qt.UserRole


# -- ホイールスクロールの誤操作防止 ---------------------------------------------------
#
# QComboBox/QSpinBox/QDateEdit/QListWidgetは、マウスカーソルが乗っているだけで
# （クリックしてフォーカスしていなくても）ホイール操作を自分の値変更/スクロールと
# して奪ってしまう。テーブルや画面全体をホイールでスクロールしようとした際、
# たまたまカーソルがこれらのウィジェット上を通過しただけで値が変わってしまう
# 事故を防ぐため、フォーカスを持っている（＝クリックやTabで明示的に選択した）
# 間だけホイール操作を受け付け、それ以外は無視して親ビュー（テーブルの
# スクロールバーやQScrollArea）側にホイールイベントを渡す。


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


# -- 連続した値編集を1つのUndo単位にまとめる ------------------------------------------
#
# スピンボックスや日付欄は、1回の編集で値が何度も変わる（▲を押すたび、
# 矢印キーを押すたび）。変更のたびにDBへ書き込む方針自体はそのままにしつつ
# （DBが常に最新なら、値を変えた直後に保存しても取りこぼさない）、Undoの単位は
# 「フォーカスを得てから外れるまで」でまとめる。そうしないと「▲を5回押したのに
# 5回Undoしないと戻らない」ことになる。


class _UndoSessionMixin:
    """フォーカスの出入りを ProjectDatabase の begin/end_undo_group に繋ぐ。
    bind_undo_session() を呼んだウィジェットでのみ有効になる。"""

    _undo_session = None  # (db, ラベル, セッション終了時のコールバック) または None

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self._undo_session is not None:
            db, label, _on_end = self._undo_session
            db.begin_undo_group(label)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if self._undo_session is not None:
            db, _label, on_end = self._undo_session
            db.end_undo_group()
            if on_end is not None:
                on_end()


def bind_undo_session(widget, db, label, on_session_end=None):
    """widget にフォーカスがある間の連続した変更を、1つのUndo単位にまとめる。

    on_session_end を渡すと、編集が終わった（フォーカスが外れた）時に呼ばれる。
    並べ替えを伴う表など、「編集中に作り直すとフォーカスが飛んでしまうので、
    編集が終わってから作り直したい」処理をここに載せる。"""
    widget._undo_session = (db, label, on_session_end)
    return widget


class NoWheelSpinBox(_UndoSessionMixin, QSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 既定のWheelFocus（ホイールを回しただけでフォーカスを奪う）は「選択した
        # 状態でのみ」という意図に反するため、クリック/Tabでのみフォーカスさせる。
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class DefaultAwareSpinBox(NoWheelSpinBox):
    """最小値（0）を「既定値を使用」という特殊値として扱うQSpinBox。
    setSpecialValueTextで見た目は「既定（n日）」のようにできるが、素の0から
    ▲▼で増減すると1やマイナスからしか始まらず使い勝手が悪い。この特殊値の
    状態から増減した場合は、既定値を起点に増減させる（0の状態で▲を押すと
    既定値+1、▼を押すと既定値-1になる）。DB保存時の「0＝既定を使用」という
    意味はそのまま変えない（0に戻ればまた「既定（n日）」の表示に戻る）。

    また、増減の結果として値が既定値とちょうど同じ数値になった場合は、
    自動的に特殊値（0）へ畳み込む（既定+1してから-1すると既定と同じ数値の
    「上書き」が残ってしまい既定表示に戻らない、という問題への対応）。
    逆に、通常の減算で0（既定を使用）へ意図せず落ち込まないよう、増減で
    到達できる実際の日数としての下限は1にする（0日という設定自体に意味が
    無いため）。0に戻すのは既定値と一致した場合の自動畳み込み、または
    直接入力のみとする。"""

    def __init__(self, default_value, parent=None):
        super().__init__(parent)
        self.default_value = default_value
        self.valueChanged.connect(self._collapse_to_default_if_matched)

    def _collapse_to_default_if_matched(self, value):
        if value == self.default_value and value != self.minimum():
            self.setValue(self.minimum())

    def stepBy(self, steps):
        if self.value() == self.minimum() and steps != 0:
            self.setValue(max(self.minimum() + 1, self.default_value + steps))
        elif steps < 0 and self.value() + steps <= self.minimum():
            self.setValue(self.minimum() + 1)
        else:
            super().stepBy(steps)

    def stepEnabled(self):
        # QAbstractSpinBoxは既定で「value == minimum() なら▼を無効化」するため、
        # 特殊値（0＝既定を使用）の状態から▼を押しても何も起きなくなってしまう。
        # 上のstepByで0からの▼を「既定値-1」として扱えるよう、常に両方向を
        # 有効にする（数値としての下限自体は0のままなので範囲外にはならない）。
        if not self.isEnabled():
            return QAbstractSpinBox.StepNone
        return QAbstractSpinBox.StepUpEnabled | QAbstractSpinBox.StepDownEnabled


class NoWheelDateEdit(_UndoSessionMixin, QDateEdit):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoWheelListWidget(QListWidget):
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


def keep_selection_visible(view):
    """フォーカスが他のウィジェット（別テーブルのセルウィジェット等）に移っても、
    選択中の行がグレーアウトして見えなくならないようにする。

    Qtの既定パレットは、ウィジェットが非アクティブ（フォーカスを持たない）な
    間、選択色（Highlight）を薄いグレーに変える。タスク上書き欄のコンボ等を
    操作するとジョブ一覧からフォーカスが離れるため、この既定動作のせいで
    「選択していたジョブが分からなくなった（選択が解除された）」ように見える
    ことがある。アクティブ時と同じ色を非アクティブ時にも使うことで、選択状態を
    常に視認できるようにする。"""
    palette = view.palette()
    palette.setColor(QPalette.Inactive, QPalette.Highlight, palette.color(QPalette.Active, QPalette.Highlight))
    palette.setColor(
        QPalette.Inactive, QPalette.HighlightedText, palette.color(QPalette.Active, QPalette.HighlightedText)
    )
    view.setPalette(palette)


def row_id(table, row):
    """table の row 行に紐づくDB上のID（先頭列のQt.UserRoleに格納）を返す。"""
    item = table.item(row, 0)
    return item.data(ROW_ID_ROLE) if item else None


def capture_table_state(table):
    """QTableWidgetの選択位置を、Undo/Redo後に復元できる形で取り出す
    （選択行はDB上の実体IDで、列は番号で覚える）。復元は restore_table_state。

    フォーカスは意図的に記録しない。スピンボックス・日付欄・テキスト欄は
    いずれもCtrl+Zを自分のものとして横取りする（QAbstractSpinBoxやQLineEditが
    ShortcutOverrideを受け取る）ため、Undoのたびにフォーカスをそれらへ戻すと、
    次のCtrl+Zがメニューまで届かず「Undoが効かなくなった」ように見える。"""
    row = table.currentRow()
    if row < 0:
        return {"entity_id": None, "column": None}
    return {"entity_id": row_id(table, row), "column": table.currentColumn()}


def restore_table_state(table, state):
    """capture_table_state() の戻り値から選択位置を復元する。対象の entity_id が
    （Undo/Redoの結果）もう存在しない場合は何もしない。

    現在セルの移動は、その列にセルウィジェット（スピンボックスや日付欄）が
    置かれていると、そのウィジェットへフォーカスを移してしまう。これらは
    Ctrl+Zを自分のものとして横取りするため、Undoのたびにフォーカスが入ると
    次のCtrl+Zがメニューまで届かなくなる。選択だけを復元してフォーカスは
    元の位置に留めるため、移ってしまった場合は戻す。"""
    if not state or state.get("entity_id") is None:
        return
    previous_focus = QApplication.focusWidget()
    select_row_by_id(table, state["entity_id"])
    row = table.currentRow()
    column = state.get("column")
    if row >= 0 and column is not None and 0 <= column < table.columnCount():
        table.setCurrentCell(row, column)

    moved_focus = QApplication.focusWidget()
    if moved_focus is not previous_focus:
        if previous_focus is not None:
            previous_focus.setFocus()
        elif moved_focus is not None:
            moved_focus.clearFocus()


def set_current_tree_item_keeping_focus(tree, item):
    """QTreeWidget.setCurrentItem() は、その項目の列にセルウィジェット
    （スピンボックスや日付欄）が置かれていると、そのウィジェットへフォーカスを
    移してしまうことがある（restore_table_state と同じ理由）。選択（現在項目）
    だけを変更し、フォーカスは元の位置に留める（プログラムからの選択変更で
    ユーザーの入力中の欄からフォーカスを奪わないようにするため）。"""
    previous_focus = QApplication.focusWidget()
    tree.setCurrentItem(item)
    moved_focus = QApplication.focusWidget()
    if moved_focus is not previous_focus:
        if previous_focus is not None:
            previous_focus.setFocus()
        elif moved_focus is not None:
            moved_focus.clearFocus()


def select_row_by_id(table, entity_id):
    """entity_id に対応する行を選択状態にする（新規追加直後の行を選ぶ用途）。
    見つからない場合は何もしない。"""
    for row in range(table.rowCount()):
        if row_id(table, row) == entity_id:
            table.setCurrentCell(row, 0)
            return


def set_row_id(table, row, entity_id):
    item = table.item(row, 0)
    if item is None:
        item = QTableWidgetItem()
        table.setItem(row, 0, item)
    item.setData(ROW_ID_ROLE, entity_id)


def unique_default_name(existing_names, base):
    """「追加」ボタン連打で名前が衝突しエラーダイアログが出る事態を避けるため、
    既存名と衝突しない既定名を自動生成する（例: 新しいジョブ, 新しいジョブ (2), ...）。"""
    if base not in existing_names:
        return base
    n = 2
    while f"{base} ({n})" in existing_names:
        n += 1
    return f"{base} ({n})"


def make_fk_combo(options, current_id=None, allow_blank=False, blank_label="（未設定）"):
    """options: [(id, display_name), ...]。選択中の値はコンボの currentData() で
    取得できる（未設定/空欄の場合は None）。"""
    combo = NoWheelComboBox()
    if allow_blank:
        combo.addItem(blank_label, None)
    for entity_id, name in options:
        combo.addItem(name, entity_id)
    if current_id is not None:
        idx = combo.findData(current_id)
        if idx >= 0:
            combo.setCurrentIndex(idx)
    return combo


def auto_size_columns(table, min_width=90):
    """データ読み込み後に呼び出し、列幅を内容に合わせて自動調整する。
    列が狭くなりすぎないよう min_width で下限を設ける。"""
    table.resizeColumnsToContents()
    for col in range(table.columnCount()):
        if table.columnWidth(col) < min_width:
            table.setColumnWidth(col, min_width)


def confirm_or_block_delete(parent, usage_count, entity_label, hard_block):
    """削除前の確認/ブロックダイアログ。

    usage_count が0なら何も表示せず True（そのまま削除してよい）。
    hard_block=True の場合、usage_count>0 なら削除不可を通知して False。
    hard_block=False の場合、usage_count>0 なら確認ダイアログを出し、
    ユーザーが続行を選べば True。
    """
    if usage_count <= 0:
        return True
    if hard_block:
        QMessageBox.warning(
            parent, "削除できません",
            f"{entity_label}は {usage_count} 件から参照されているため削除できません。",
        )
        return False
    reply = QMessageBox.question(
        parent, "削除の確認",
        f"{entity_label}は {usage_count} 件から参照されています。"
        "削除すると、それらの参照が解除されます。続行しますか？",
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
    )
    return reply == QMessageBox.Yes


class CrudSection(QGroupBox):
    """タイトル付きグループボックスの中に「追加/削除」ボタンと表を持つ、
    タブ内の1セクション。表本体（QTableWidget）は呼び出し側が組み立て、
    列やセルウィジェットの構成は各タブ側の責務とする（追加/削除の導線と
    見た目の一貫性だけをここで共通化する）。"""

    def __init__(
        self, title, column_labels, on_add, on_delete, on_edit=None,
        edit_dblclick_columns=None, parent=None,
    ):
        super().__init__(title, parent)
        self.on_add = on_add
        self.on_delete = on_delete
        self.on_edit = on_edit
        # on_edit をダブルクリックで開く対象列を限定したい場合に指定する
        # （例: 列0がインライン編集可能なテキストで、別の列だけダイアログを
        # 開かせたいケース）。Noneなら全列で発火（従来互換の挙動）。
        self.edit_dblclick_columns = edit_dblclick_columns

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        add_btn = QPushButton("＋ 追加")
        add_btn.clicked.connect(self._handle_add)
        toolbar.addWidget(add_btn)
        if on_edit is not None:
            edit_btn = QPushButton("編集...")
            edit_btn.clicked.connect(self._handle_edit)
            toolbar.addWidget(edit_btn)
        del_btn = QPushButton("－ 削除")
        del_btn.clicked.connect(self._handle_delete)
        toolbar.addWidget(del_btn)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, len(column_labels))
        self.table.setHorizontalHeaderLabels(column_labels)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        keep_selection_visible(self.table)
        if on_edit is not None:
            self.table.itemDoubleClicked.connect(self._handle_item_double_clicked)
        layout.addWidget(self.table)

    def _handle_item_double_clicked(self, item):
        if self.edit_dblclick_columns is not None and item.column() not in self.edit_dblclick_columns:
            return
        self._handle_edit()

    def _handle_add(self):
        self.on_add()

    def _handle_edit(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "編集", "編集する行を選択してください。")
            return
        self.on_edit(row)

    def _handle_delete(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "削除", "削除する行を選択してください。")
            return
        self.on_delete(row)

    def current_row_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        return row_id(self.table, row)
