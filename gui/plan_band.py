"""
計画の状態帯（docs/roadmap.md §8-9）。全タブの上に1本だけ置く。

背景色で3つの状態を区別し、帯の色とガントの中身を必ず一致させる
（灰＝未確定、緑＝確定済み＝合意そのもの、橙＝変更案）。文言は事実だけを短く書く。
操作ボタンはガントチャートタブを開いているときだけ出す（結果を見ながら操作する
場所に限定する）。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from gui.plan_confirmation import CONFIRMED, DRAFT, UNCONFIRMED

# (背景, 枠, 文字)
_COLORS = {
    UNCONFIRMED: ("#eef1f5", "#c3cad3", "#3b4652"),
    CONFIRMED: ("#e3f1e6", "#86b893", "#1f4d2b"),
    DRAFT: ("#fff1d6", "#e0a526", "#6b4300"),
}
_TITLES = {UNCONFIRMED: "未確定", CONFIRMED: "確定済み", DRAFT: "変更案"}


class PlanStatusBand(QFrame):
    confirmRequested = Signal()
    confirmSelectedRequested = Signal()
    discardRequested = Signal()
    clearRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("planStatusBand")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        self.title_label = QLabel()
        font = self.title_label.font()
        font.setBold(True)
        self.title_label.setFont(font)
        layout.addWidget(self.title_label)
        self.detail_label = QLabel()
        self.detail_label.setTextFormat(Qt.RichText)
        layout.addWidget(self.detail_label, 1)

        self.confirm_button = QPushButton("確定する")
        self.confirm_draft_button = QPushButton("変更を確定")
        self.confirm_selected_button = QPushButton("選択した変更を確定")
        self.discard_button = QPushButton("変更を破棄")
        self.clear_button = QPushButton("確定を解除")
        self.confirm_button.clicked.connect(self.confirmRequested)
        self.confirm_draft_button.clicked.connect(self.confirmRequested)
        self.confirm_selected_button.clicked.connect(self.confirmSelectedRequested)
        self.discard_button.clicked.connect(self.discardRequested)
        self.clear_button.clicked.connect(self.clearRequested)
        self._buttons = {
            UNCONFIRMED: [self.confirm_button],
            CONFIRMED: [self.clear_button],
            DRAFT: [self.confirm_draft_button, self.confirm_selected_button, self.discard_button,
                    self.clear_button],
        }
        for button in (self.confirm_button, self.confirm_draft_button, self.confirm_selected_button,
                       self.discard_button, self.clear_button):
            layout.addWidget(button)
        self.status = None
        self.set_state(UNCONFIRMED, "", show_buttons=False, buttons_enabled=False)

    def set_state(self, status, detail, show_buttons, buttons_enabled, selected_enabled=True):
        """status: UNCONFIRMED/CONFIRMED/DRAFT。detail: 状態名の右に出す事実（HTML可）。
        selected_enabled: 「選択した変更を確定」を押せるか（ガントで、確定していない
        変更のあるタスクを選んでいるときだけ）。"""
        self.status = status
        background, border, text = _COLORS[status]
        self.setStyleSheet(
            f"#planStatusBand {{ background: {background}; border: 1px solid {border}; "
            f"border-radius: 4px; }} #planStatusBand QLabel {{ color: {text}; }}"
        )
        self.title_label.setText(_TITLES[status])
        self.detail_label.setText(detail)
        visible = set(self._buttons[status]) if show_buttons else set()
        for buttons in self._buttons.values():
            for button in buttons:
                button.setVisible(button in visible)
                button.setEnabled(buttons_enabled)
        self.confirm_selected_button.setEnabled(buttons_enabled and selected_enabled)
