"""
オプション設定ダイアログ（編集メニューの「オプション…」、docs/roadmap.md §10）。

gui/app_settings.py の AppSettings を編集する。載せるのは、その時点で実際に
効く項目だけにする（効かない項目を並べると、変えても何も起きない）。
表示言語・ガントのドラッグキー等は、対応する機能を入れるときにここへ足す。

変更は「OK」で保存し、「キャンセル」では何も保存しない。「既定値に戻す」は
画面上の値を既定値にするだけで、保存は「OK」を押したとき。
オプションはプロジェクトの内容ではないので、Undo/Redo の対象にしない。
"""

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from gui.app_settings import OPTIONS


class OptionsDialog(QDialog):
    def __init__(self, app_settings, parent=None):
        super().__init__(parent)
        self.app_settings = app_settings
        self.setWindowTitle("オプション")

        layout = QVBoxLayout(self)

        general = QGroupBox("全般")
        form = QFormLayout(general)
        undo_option = OPTIONS["undo_memory_limit_mb"]
        self.undo_memory_spin = QSpinBox()
        self.undo_memory_spin.setRange(undo_option.minimum, undo_option.maximum)
        self.undo_memory_spin.setSingleStep(16)
        self.undo_memory_spin.setSuffix(" MB")
        self.undo_memory_spin.setMinimumWidth(110)
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.undo_memory_spin.setValue(app_settings.get("undo_memory_limit_mb"))
        form.addRow("Undoに使うメモリの上限", self.undo_memory_spin)
        layout.addWidget(general)

        path_label = QLabel(f"保存先: {app_settings.path}")
        # 補足情報なので控えめな色にする（ダーク/ライトどちらのパレットにも追従）
        path_label.setForegroundRole(QPalette.PlaceholderText)
        path_label.setWordWrap(True)
        layout.addWidget(path_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("OK")
        buttons.button(QDialogButtonBox.Cancel).setText("キャンセル")
        self.reset_button = QPushButton("既定値に戻す")
        buttons.addButton(self.reset_button, QDialogButtonBox.ResetRole)
        self.reset_button.clicked.connect(self._reset_to_defaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _reset_to_defaults(self):
        self.undo_memory_spin.setValue(self.app_settings.default("undo_memory_limit_mb"))

    def accept(self):
        self.app_settings.set("undo_memory_limit_mb", self.undo_memory_spin.value())
        try:
            self.app_settings.sync()
        except OSError as e:
            # 値はこの起動中は有効なまま。次回起動時に元へ戻ることだけ知らせる。
            QMessageBox.warning(self, "オプション", str(e))
        super().accept()
