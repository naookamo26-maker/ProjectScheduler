"""
計画の状態帯（docs/roadmap.md §8-9）。全タブの上に1本だけ置く。

背景色で3つの状態を区別し、帯の色とガントの中身を必ず一致させる
（灰＝未確定、緑＝確定済み＝合意そのもの、橙＝変更案）。文言は事実だけを短く書く。
操作ボタンはガントチャートタブを開いているときだけ出す（結果を見ながら操作する
場所に限定する）。ボタンの有無で帯の高さが変わらないよう、ボタンが無いときも
ボタンぶんの高さを取っておく。

色はライト／ダークの2組を持ち、アプリのテーマに合わせて切り替える（ダークモードで
明るい帯が浮かないように）。ボタンの色も明示する: 親にスタイルシートがあると、
Windows 11 のスタイルはボタンの文字を白で描くことがあり、ダークモードで読めなくなる。
"""

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton

from gui.plan_confirmation import CONFIRMED, DRAFT, UNCONFIRMED
from gui.widgets_common import is_dark_theme
from i18n import N_, tr

# 状態ごとの (背景, 枠, 文字)。ボタンの枠・文字にも同じ枠・文字の色を使う
_COLORS = {
    False: {
        UNCONFIRMED: ("#eef1f5", "#c3cad3", "#3b4652"),
        CONFIRMED: ("#e3f1e6", "#86b893", "#1f4d2b"),
        DRAFT: ("#fff1d6", "#e0a526", "#6b4300"),
    },
    True: {
        UNCONFIRMED: ("#2b3036", "#4d5661", "#d5dbe2"),
        CONFIRMED: ("#1e3226", "#3f7d52", "#bfe5c9"),
        DRAFT: ("#3a2f15", "#a8791c", "#f5d68e"),
    },
}
# ボタンの (背景, マウスが乗ったとき, 押したとき, 無効のときの文字)
_BUTTON_COLORS = {
    False: ("#ffffff", "#f2f4f7", "#e1e5ea", "#9aa3ad"),
    True: ("#3a4048", "#454c55", "#30353c", "#7c848e"),
}
_TITLES = {UNCONFIRMED: N_("未確定"), CONFIRMED: N_("確定済み"), DRAFT: N_("変更案")}


class PlanStatusBand(QFrame):
    confirmRequested = Signal()
    confirmSelectedRequested = Signal()
    discardRequested = Signal()
    clearRequested = Signal()
    fullReplanRequested = Signal()

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

        self.confirm_button = QPushButton(tr("確定する"))
        self.confirm_draft_button = QPushButton(tr("変更を確定"))
        self.confirm_selected_button = QPushButton(tr("選択した変更を確定"))
        self.discard_button = QPushButton(tr("変更を破棄"))
        self.replan_button = QPushButton(tr("全面再計画…"))
        self.clear_button = QPushButton(tr("未確定に戻す"))
        self.confirm_button.clicked.connect(self.confirmRequested)
        self.confirm_draft_button.clicked.connect(self.confirmRequested)
        self.confirm_selected_button.clicked.connect(self.confirmSelectedRequested)
        self.discard_button.clicked.connect(self.discardRequested)
        self.clear_button.clicked.connect(self.clearRequested)
        self.replan_button.clicked.connect(self.fullReplanRequested)
        self._buttons = {
            UNCONFIRMED: [self.confirm_button],
            CONFIRMED: [self.replan_button, self.clear_button],
            DRAFT: [self.confirm_draft_button, self.confirm_selected_button, self.discard_button,
                    self.replan_button, self.clear_button],
        }
        self._all_buttons = (self.confirm_button, self.confirm_draft_button, self.confirm_selected_button,
                             self.discard_button, self.replan_button, self.clear_button)
        for button in self._all_buttons:
            layout.addWidget(button)
        self.status = None
        self._dark = None
        self.set_state(UNCONFIRMED, "", show_buttons=False, buttons_enabled=False)

        # 起動後に OS のテーマ（ライト⇄ダーク）が切り替わったら色を追従させる。スタイル
        # シートを当てたウィジェットにはアプリのパレット変更が届かないため、アプリ側で拾う。
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.ApplicationPaletteChange and watched is QApplication.instance()
                and self.status is not None and is_dark_theme() != self._dark):
            self._apply_colors()
        return False

    def _apply_colors(self):
        self._dark = is_dark_theme()
        background, border, text = _COLORS[self._dark][self.status]
        button, hover, pressed, disabled = _BUTTON_COLORS[self._dark]
        self.setStyleSheet(
            f"#planStatusBand {{ background: {background}; border: 1px solid {border}; "
            f"border-radius: 4px; }} #planStatusBand QLabel {{ color: {text}; }}"
            f" #planStatusBand QPushButton {{ background: {button}; color: {text}; "
            f"border: 1px solid {border}; border-radius: 3px; padding: 3px 10px; }}"
            f" #planStatusBand QPushButton:hover {{ background: {hover}; }}"
            f" #planStatusBand QPushButton:pressed {{ background: {pressed}; }}"
            f" #planStatusBand QPushButton:disabled {{ color: {disabled}; }}"
        )
        # ボタンを出さない状態（ガントチャート以外のタブ）でも帯の高さを変えない
        self.title_label.setMinimumHeight(max(b.sizeHint().height() for b in self._all_buttons))

    def set_state(self, status, detail, show_buttons, buttons_enabled, selected_enabled=True):
        """status: UNCONFIRMED/CONFIRMED/DRAFT。detail: 状態名の右に出す事実（HTML可）。
        selected_enabled: 「選択した変更を確定」を押せるか（ガントで、確定していない
        変更のあるタスクを選んでいるときだけ）。"""
        self.status = status
        self._apply_colors()
        self.title_label.setText(tr(_TITLES[status]))
        self.detail_label.setText(detail)
        visible = set(self._buttons[status]) if show_buttons else set()
        for buttons in self._buttons.values():
            for button in buttons:
                button.setVisible(button in visible)
                button.setEnabled(buttons_enabled)
        self.confirm_selected_button.setEnabled(buttons_enabled and selected_enabled)
