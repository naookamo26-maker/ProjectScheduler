"""
タブ横断で使う共通UI部品。

- CrudSection: 「タイトル + 追加/削除ボタン + 表」の1セクション。
  基本情報設定・ジョブ・依存関係タブで繰り返し使う型。
- make_fk_combo: 外部キー選択用のQComboBox（表示は名前、内部値はDBの整数ID）。
- confirm_or_block_delete: 削除前の参照整合性チェック用ダイアログ。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

ROW_ID_ROLE = Qt.UserRole


def row_id(table, row):
    """table の row 行に紐づくDB上のID（先頭列のQt.UserRoleに格納）を返す。"""
    item = table.item(row, 0)
    return item.data(ROW_ID_ROLE) if item else None


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
    combo = QComboBox()
    if allow_blank:
        combo.addItem(blank_label, None)
    for entity_id, name in options:
        combo.addItem(name, entity_id)
    if current_id is not None:
        idx = combo.findData(current_id)
        if idx >= 0:
            combo.setCurrentIndex(idx)
    return combo


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

    def __init__(self, title, column_labels, on_add, on_delete, parent=None):
        super().__init__(title, parent)
        self.on_add = on_add
        self.on_delete = on_delete

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        add_btn = QPushButton("＋ 追加")
        add_btn.clicked.connect(self._handle_add)
        del_btn = QPushButton("－ 削除")
        del_btn.clicked.connect(self._handle_delete)
        toolbar.addWidget(add_btn)
        toolbar.addWidget(del_btn)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, len(column_labels))
        self.table.setHorizontalHeaderLabels(column_labels)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        layout.addWidget(self.table)

    def _handle_add(self):
        self.on_add()

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
