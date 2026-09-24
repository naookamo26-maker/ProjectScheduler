"""
オプション設定ダイアログ（編集メニューの「オプション…」、docs/roadmap.md §10）。

gui/app_settings.py の AppSettings を編集する。載せるのは、その時点で実際に
効く項目だけにする（効かない項目を並べると、変えても何も起きない）。
全面再計画のしきい値は、確定と変更案（docs/roadmap.md §8）を入れるときにここへ足す。
例外として表示言語は、翻訳（docs/roadmap.md §11）より先に選べるようにしてある
（利用者の要望）。翻訳が入るまでは、どれを選んでも日本語で表示される旨を注記する。

変更は「OK」で保存し、「キャンセル」では何も保存しない。「既定値に戻す」は
画面上の値を既定値にするだけで、保存は「OK」を押したとき。
オプションはプロジェクトの内容ではないので、Undo/Redo の対象にしない。
"""

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QComboBox,
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

from gui.app_settings import DRAG_MODIFIERS, OPTIONS, SUPPORTED_LANGUAGES

# 言語の選択肢は、それぞれの言語での呼び名で出す（別の言語に切り替えた人が
# 自分の言語を見つけられるように）。
LANGUAGE_LABELS = {
    "ja": "日本語",
    "en": "English",
    "vi": "Tiếng Việt",
    "zh_CN": "简体中文",
}

DRAG_MODIFIER_LABELS = {"shift": "Shift", "alt": "Alt"}


class OptionsDialog(QDialog):
    def __init__(self, app_settings, parent=None):
        super().__init__(parent)
        self.app_settings = app_settings
        self.setWindowTitle("オプション")

        layout = QVBoxLayout(self)

        general = QGroupBox("全般")
        form = QFormLayout(general)
        self.language_combo = QComboBox()
        for code in SUPPORTED_LANGUAGES:
            self.language_combo.addItem(LANGUAGE_LABELS[code], code)
        self._select_language(app_settings.get("language"))
        form.addRow("表示言語", self.language_combo)
        language_note = QLabel("再起動後に反映（翻訳は準備中のため、現在はどれを選んでも日本語で表示）")
        language_note.setForegroundRole(QPalette.PlaceholderText)
        language_note.setWordWrap(True)
        form.addRow("", language_note)

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

        gantt = QGroupBox("ガントチャート")
        gantt_form = QFormLayout(gantt)
        gantt_form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.drag_modifier_combo = QComboBox()
        for code in DRAG_MODIFIERS:
            self.drag_modifier_combo.addItem(DRAG_MODIFIER_LABELS[code], code)
        self._select_data(self.drag_modifier_combo, app_settings.get("gantt_drag_modifier"))
        gantt_form.addRow("バーをドラッグするときに押すキー", self.drag_modifier_combo)

        highlight_option = OPTIONS["moved_bar_highlight_seconds"]
        self.highlight_spin = QSpinBox()
        self.highlight_spin.setRange(highlight_option.minimum, highlight_option.maximum)
        self.highlight_spin.setSuffix(" 秒")
        # 0 は「次の操作まで」（次にチャートをクリックするか、次の編集で消える）
        self.highlight_spin.setSpecialValueText("次の操作まで")
        self.highlight_spin.setMinimumWidth(110)
        self.highlight_spin.setValue(app_settings.get("moved_bar_highlight_seconds"))
        gantt_form.addRow("編集で動いたバーを強調する時間", self.highlight_spin)
        layout.addWidget(gantt)

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

    def _select_language(self, code):
        self._select_data(self.language_combo, code)

    @staticmethod
    def _select_data(combo, value):
        combo.setCurrentIndex(combo.findData(value))

    def selected_language(self):
        return self.language_combo.currentData()

    def _reset_to_defaults(self):
        self._select_language(self.app_settings.default("language"))
        self.undo_memory_spin.setValue(self.app_settings.default("undo_memory_limit_mb"))
        self._select_data(self.drag_modifier_combo, self.app_settings.default("gantt_drag_modifier"))
        self.highlight_spin.setValue(self.app_settings.default("moved_bar_highlight_seconds"))

    def accept(self):
        self.app_settings.set("language", self.selected_language())
        self.app_settings.set("undo_memory_limit_mb", self.undo_memory_spin.value())
        self.app_settings.set("gantt_drag_modifier", self.drag_modifier_combo.currentData())
        self.app_settings.set("moved_bar_highlight_seconds", self.highlight_spin.value())
        try:
            self.app_settings.sync()
        except OSError as e:
            # 値はこの起動中は有効なまま。次回起動時に元へ戻ることだけ知らせる。
            QMessageBox.warning(self, "オプション", str(e))
        super().accept()
