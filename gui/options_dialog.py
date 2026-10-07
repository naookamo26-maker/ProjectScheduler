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
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from gui.app_settings import BAR_DAY_COUNTS, DRAG_MODIFIERS, OPTIONS, SUPPORTED_LANGUAGES
from i18n import LANGUAGES, N_, tr

# 言語の選択肢は、それぞれの言語での呼び名で出す（別の言語に切り替えた人が
# 自分の言語を見つけられるように）。
LANGUAGE_LABELS = dict(LANGUAGES)

DRAG_MODIFIER_LABELS = {"shift": "Shift", "alt": "Alt"}

# ガントのバーに出す項目（オプション名, 表示名）。並びはバーに添える順
# （gui/gantt_view.py の BAR_LABEL_EXTRAS）と同じ。
BAR_LABEL_CHECKS = (
    ("gantt_bar_show_name", N_("タスク名")),
    ("gantt_bar_show_days", N_("日数")),
    ("gantt_bar_show_slack", N_("締切までの余裕")),
    ("gantt_bar_show_period", N_("開始日〜終了日")),
    ("gantt_bar_show_team", N_("チーム名")),
    ("gantt_bar_show_milestone", N_("マイルストーン名")),
)
BAR_DAY_COUNT_LABELS = {"work": N_("営業日（休業日を除く）"), "calendar": N_("暦日（休業日を含む）")}


class OptionsDialog(QDialog):
    def __init__(self, app_settings, parent=None):
        super().__init__(parent)
        self.app_settings = app_settings
        self.setWindowTitle(tr("オプション"))

        layout = QVBoxLayout(self)

        general = QGroupBox(tr("全般"))
        form = QFormLayout(general)
        self.language_combo = QComboBox()
        for code in SUPPORTED_LANGUAGES:
            self.language_combo.addItem(LANGUAGE_LABELS[code], code)
        self._select_language(app_settings.get("language"))
        form.addRow(tr("表示言語"), self.language_combo)
        language_note = QLabel(tr("再起動後に反映"))
        language_note.setForegroundRole(QPalette.PlaceholderText)
        language_note.setWordWrap(True)
        # 説明文は行の全幅に置く（2列目に置くと、訳文が長い言語で折り返した行の
        # 高さが足りず上下が切れる）
        form.addRow(language_note)

        undo_option = OPTIONS["undo_memory_limit_mb"]
        self.undo_memory_spin = QSpinBox()
        self.undo_memory_spin.setRange(undo_option.minimum, undo_option.maximum)
        self.undo_memory_spin.setSingleStep(16)
        self.undo_memory_spin.setSuffix(" MB")
        self.undo_memory_spin.setMinimumWidth(110)
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.undo_memory_spin.setValue(app_settings.get("undo_memory_limit_mb"))
        form.addRow(tr("Undoに使うメモリの上限"), self.undo_memory_spin)
        layout.addWidget(general)

        gantt = QGroupBox(tr("ガントチャート"))
        gantt_form = QFormLayout(gantt)
        gantt_form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.drag_modifier_combo = QComboBox()
        for code in DRAG_MODIFIERS:
            self.drag_modifier_combo.addItem(DRAG_MODIFIER_LABELS[code], code)
        self._select_data(self.drag_modifier_combo, app_settings.get("gantt_drag_modifier"))
        gantt_form.addRow(tr("バーをドラッグするときに押すキー"), self.drag_modifier_combo)

        highlight_option = OPTIONS["moved_bar_highlight_seconds"]
        self.highlight_spin = QSpinBox()
        self.highlight_spin.setRange(highlight_option.minimum, highlight_option.maximum)
        self.highlight_spin.setSuffix(tr(" 秒"))
        # 0 は「次の操作まで」（次にチャートをクリックするか、次の編集で消える）
        self.highlight_spin.setSpecialValueText(tr("次の操作まで"))
        # 特殊値の文言（「次の操作まで」）は言語によって長いので、その幅を確保する
        self.highlight_spin.setMinimumWidth(
            max(110, self.highlight_spin.fontMetrics().horizontalAdvance(tr("次の操作まで")) + 48)
        )
        self.highlight_spin.setValue(app_settings.get("moved_bar_highlight_seconds"))
        gantt_form.addRow(tr("編集で動いたバーを強調する時間"), self.highlight_spin)

        # バーに出す項目。2列に並べる（縦に6行並べるとダイアログが縦に長くなる）
        bar_items = QWidget()
        bar_grid = QGridLayout(bar_items)
        bar_grid.setContentsMargins(0, 0, 0, 0)
        self.bar_label_checks = {}
        for index, (name, label) in enumerate(BAR_LABEL_CHECKS):
            check = QCheckBox(tr(label))
            check.setChecked(app_settings.get(name))
            bar_grid.addWidget(check, index // 2, index % 2)
            self.bar_label_checks[name] = check
        gantt_form.addRow(tr("バーに表示する項目"), bar_items)
        self.bar_day_count_combo = QComboBox()
        for code in BAR_DAY_COUNTS:
            self.bar_day_count_combo.addItem(tr(BAR_DAY_COUNT_LABELS[code]), code)
        self._select_data(self.bar_day_count_combo, app_settings.get("gantt_bar_day_count"))
        gantt_form.addRow(tr("日数・余裕の数え方"), self.bar_day_count_combo)
        bar_note = QLabel(tr("タスク名以外の項目は、バーに余裕があるときだけ表示します。"))
        bar_note.setForegroundRole(QPalette.PlaceholderText)
        bar_note.setWordWrap(True)
        gantt_form.addRow(bar_note)
        layout.addWidget(gantt)

        plan = QGroupBox(tr("計画の確定"))
        plan_form = QFormLayout(plan)
        plan_form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        threshold_option = OPTIONS["full_replan_threshold_percent"]
        self.replan_threshold_spin = QSpinBox()
        self.replan_threshold_spin.setRange(threshold_option.minimum, threshold_option.maximum)
        self.replan_threshold_spin.setSuffix(" %")
        self.replan_threshold_spin.setMinimumWidth(110)
        self.replan_threshold_spin.setValue(app_settings.get("full_replan_threshold_percent"))
        plan_form.addRow(tr("全面再計画を案内する影響範囲"), self.replan_threshold_spin)
        threshold_note = QLabel(tr("変更案で動くタスクが全タスクのこの割合を超えたら、状態帯で全面再計画を案内します。"))
        threshold_note.setForegroundRole(QPalette.PlaceholderText)
        threshold_note.setWordWrap(True)
        plan_form.addRow(threshold_note)
        layout.addWidget(plan)

        path_label = QLabel(tr("保存先: {path}", path=app_settings.path))
        # 補足情報なので控えめな色にする（ダーク/ライトどちらのパレットにも追従）
        path_label.setForegroundRole(QPalette.PlaceholderText)
        path_label.setWordWrap(True)
        layout.addWidget(path_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("OK")
        buttons.button(QDialogButtonBox.Cancel).setText(tr("キャンセル"))
        self.reset_button = QPushButton(tr("既定値に戻す"))
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
        for name, check in self.bar_label_checks.items():
            check.setChecked(self.app_settings.default(name))
        self._select_data(self.bar_day_count_combo, self.app_settings.default("gantt_bar_day_count"))
        self.replan_threshold_spin.setValue(self.app_settings.default("full_replan_threshold_percent"))

    def accept(self):
        self.app_settings.set("language", self.selected_language())
        self.app_settings.set("undo_memory_limit_mb", self.undo_memory_spin.value())
        self.app_settings.set("gantt_drag_modifier", self.drag_modifier_combo.currentData())
        self.app_settings.set("moved_bar_highlight_seconds", self.highlight_spin.value())
        for name, check in self.bar_label_checks.items():
            self.app_settings.set(name, check.isChecked())
        self.app_settings.set("gantt_bar_day_count", self.bar_day_count_combo.currentData())
        self.app_settings.set("full_replan_threshold_percent", self.replan_threshold_spin.value())
        try:
            self.app_settings.sync()
        except OSError as e:
            # 値はこの起動中は有効なまま。次回起動時に元へ戻ることだけ知らせる。
            QMessageBox.warning(self, tr("オプション"), str(e))
        super().accept()
