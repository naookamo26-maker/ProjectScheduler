"""
全面再計画ダイアログ（docs/roadmap.md §8-6）。

基準日（新しい計画が始まる日）を選ぶ。初期値は今日。結果は変更案として表示する
だけで、確定は「変更を確定」で初めて書き換わる。ダイアログには、基準日を選ぶと
何が起きるかを件数で出す（今の確定のまま残すタスク／置き直すタスク／基準日より
前に手動ピンがあるタスク）。過去の日付も選べるが、警告を出す。
"""

from datetime import date

from PySide6.QtCore import QDate
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)

from gui.plan_confirmation import replan_preview

_WARNING_STYLE = "color: #c62828;"


class ReplanDialog(QDialog):
    def __init__(self, db, today=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("全面再計画")
        self.db = db
        self.today = today or date.today()

        layout = QVBoxLayout(self)
        intro = QLabel(
            "未着手のタスクを、基準日以降に全体として組み直します。"
            "結果は変更案として表示し、「変更を確定」で確定します。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(QDate(self.today.year, self.today.month, self.today.day))
        self.date_edit.dateChanged.connect(self._update_preview)
        form.addRow("新しい計画が始まる日（基準日）", self.date_edit)
        layout.addLayout(form)

        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(_WARNING_STYLE)
        layout.addWidget(self.warning_label)
        note = QLabel("進行中・完了のタスクは動かしません。")
        note.setForegroundRole(QPalette.PlaceholderText)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("全面再計画")
        buttons.button(QDialogButtonBox.Cancel).setText("キャンセル")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(520)
        self._update_preview()

    def base_date(self):
        return self.date_edit.date().toPython()

    def _update_preview(self):
        base = self.base_date()
        preview = replan_preview(self.db, base.isoformat(), self.today.isoformat())
        lines = []
        if preview["kept"]:
            lines.append(
                f"{base:%m/%d} より前に開始予定の未着手タスク {preview['kept']}件は、"
                "今の確定のまま残します。"
            )
        lines.append(f"未着手タスク {preview['replaced']}件を {base:%m/%d} 以降に置き直します。")
        self.preview_label.setText("\n".join(lines))

        warnings = []
        if base < self.today:
            warnings.append("今日より前の日付です。今日より前に置かれるタスクが出ます。")
        if preview["pinned_before"]:
            warnings.append(
                f"{base:%m/%d} より前に開始固定日（手動ピン）がある未着手タスクが "
                f"{preview['pinned_before']}件あります。ピンの日付のまま置かれます。"
            )
        self.warning_label.setText("\n".join(warnings))
        self.warning_label.setVisible(bool(warnings))
