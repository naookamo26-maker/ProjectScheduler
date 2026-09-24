"""
ガントチャートタブのタスク編集ウィンドウ（docs/roadmap.md §9）。

ダブルクリック、または右クリック「編集…」で開くフローティングウィンドウ。
常時は表示しない。開いている間はガントの選択に追従し、選択中のタスクを編集する。

- 1件選択: 開始日（固定）・日数・チーム・マイルストーン・状態・有効・タグ
- 複数選択: まとめて変えても不都合のない項目だけ（開始日は「N営業日ずらす」、
  固定は解除のみ、タグは追加・削除のみ、日数は編集不可）。値が揃っていない
  項目は「（複数の値）」と表示し、触らなければ変えない

書き込みは gui/tab_gantt.py の GanttTab.apply_task_fields / shift_tasks を通す
（1回の変更が1つのUndo単位になり、終わると再計算が走る）。1文字ごとに
再計算しないよう、数値・日付は Enter かフォーカスを外した時、コンボは
選んだ時に確定する。
"""

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui.db import normalize_tags, parse_tags
from gui.widgets_common import DefaultAwareSpinBox, OptionalDateEdit
from i18n import N_, tr

_STATUS_OPTIONS = [(None, N_("未着手")), ("in_progress", N_("進行中")), ("done", N_("完了"))]


def _status_options():
    return [(value, tr(label)) for value, label in _STATUS_OPTIONS]

# 複数選択で値が揃っていない項目の表示。選んだままなら何も変えない。
_MIXED = object()
_MIXED_LABEL = N_("（複数の値）")

_GEOMETRY_STATE_KEY = "task_editor_geometry"


class TaskEditWindow(QWidget):
    def __init__(self, tab):
        super().__init__(tab, Qt.Tool)
        self.tab = tab
        self.setWindowTitle(tr("タスクの編集"))
        self._keys = []
        self._rows = []
        self._geometry_restored = False

        layout = QVBoxLayout(self)
        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        font = self.title_label.font()
        font.setBold(True)
        self.title_label.setFont(font)
        layout.addWidget(self.title_label)

        self.pages = QStackedWidget()
        self.empty_page = QLabel(tr("ガントチャートでタスクを選択してください。"))
        self.empty_page.setAlignment(Qt.AlignCenter)
        self.pages.addWidget(self.empty_page)
        self.single_page = self._build_single_page()
        self.pages.addWidget(self.single_page)
        self.multi_page = self._build_multi_page()
        self.pages.addWidget(self.multi_page)
        layout.addWidget(self.pages)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_button = QPushButton(tr("閉じる"))
        close_button.clicked.connect(self.close)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    # -- ページの構築 -------------------------------------------------------------

    def _build_single_page(self):
        page = QWidget()
        form = QFormLayout(page)

        self.schedule_label = QLabel()
        form.addRow(tr("計算上の日程"), self.schedule_label)

        pin_row = QHBoxLayout()
        self.pin_edit = OptionalDateEdit()
        self.pin_edit.editingFinished.connect(self._commit_pin)
        self.pin_edit.calendarWidget().clicked.connect(lambda _d: self._commit_pin())
        pin_row.addWidget(self.pin_edit)
        self.pin_now_button = QPushButton(tr("今の開始日で固定"))
        self.pin_now_button.clicked.connect(self._pin_to_current_start)
        pin_row.addWidget(self.pin_now_button)
        form.addRow(tr("開始固定日"), pin_row)

        self.days_spin = DefaultAwareSpinBox(1)
        self.days_spin.setRange(0, 9999)
        self.days_spin.setSuffix(tr("日"))
        self.days_spin.setKeyboardTracking(False)
        self.days_spin.editingFinished.connect(self._commit_days)
        form.addRow(tr("日数"), self.days_spin)

        self.team_combo = QComboBox()
        self.team_combo.activated.connect(lambda _i: self._commit_combo(self.team_combo, "team_id"))
        form.addRow(tr("チーム"), self.team_combo)

        self.milestone_combo = QComboBox()
        self.milestone_combo.activated.connect(
            lambda _i: self._commit_combo(self.milestone_combo, "milestone_id")
        )
        form.addRow(tr("マイルストーン"), self.milestone_combo)

        self.status_combo = QComboBox()
        self.status_combo.activated.connect(lambda _i: self._commit_combo(self.status_combo, "status"))
        form.addRow(tr("状態"), self.status_combo)

        self.active_check = QCheckBox(tr("このタスクを実行する"))
        self.active_check.clicked.connect(self._commit_active)
        form.addRow(tr("有効"), self.active_check)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText(tr("カンマ区切り"))
        self.tags_edit.editingFinished.connect(self._commit_tags)
        form.addRow(tr("タグ"), self.tags_edit)
        return page

    def _build_multi_page(self):
        page = QWidget()
        form = QFormLayout(page)

        shift_row = QHBoxLayout()
        shift_row.addWidget(QLabel(tr("移動")))
        self.shift_spin = QSpinBox()
        self.shift_spin.setRange(-999, 999)
        self.shift_spin.setSuffix(tr(" 営業日"))
        self.shift_spin.setPrefix("")
        shift_row.addWidget(self.shift_spin)
        self.shift_button = QPushButton(tr("ずらす"))
        self.shift_button.clicked.connect(self._commit_shift)
        shift_row.addWidget(self.shift_button)
        shift_row.addStretch(1)
        form.addRow(tr("開始日"), shift_row)

        self.unpin_button = QPushButton(tr("固定を解除"))
        self.unpin_button.clicked.connect(self._unpin_all)
        form.addRow(tr("開始日の固定"), self.unpin_button)

        days_note = QLabel(tr("複数選択時は編集できません"))
        days_note.setEnabled(False)
        form.addRow(tr("日数"), days_note)

        self.multi_team_combo = QComboBox()
        self.multi_team_combo.activated.connect(
            lambda _i: self._commit_combo(self.multi_team_combo, "team_id")
        )
        form.addRow(tr("チーム"), self.multi_team_combo)

        self.multi_milestone_combo = QComboBox()
        self.multi_milestone_combo.activated.connect(
            lambda _i: self._commit_combo(self.multi_milestone_combo, "milestone_id")
        )
        form.addRow(tr("マイルストーン"), self.multi_milestone_combo)

        self.multi_status_combo = QComboBox()
        self.multi_status_combo.activated.connect(
            lambda _i: self._commit_combo(self.multi_status_combo, "status")
        )
        form.addRow(tr("状態"), self.multi_status_combo)

        self.multi_active_check = QCheckBox(tr("これらのタスクを実行する"))
        self.multi_active_check.clicked.connect(self._commit_multi_active)
        form.addRow(tr("有効"), self.multi_active_check)

        tags_row = QHBoxLayout()
        add_tag = QPushButton(tr("タグを追加…"))
        add_tag.clicked.connect(self._add_tag)
        remove_tag = QPushButton(tr("タグを外す…"))
        remove_tag.clicked.connect(self._remove_tag)
        tags_row.addWidget(add_tag)
        tags_row.addWidget(remove_tag)
        tags_row.addStretch(1)
        form.addRow(tr("タグ"), tags_row)
        return page

    # -- 表示 ---------------------------------------------------------------------

    def set_keys(self, keys):
        """編集対象（(Job_ID, Task_ID) の並び）を差し替えて表示し直す。"""
        self._keys = list(keys)
        self.reload()

    def reload(self):
        """DBと計算結果から表示を作り直す（編集・再計算・Undo/Redoの後に呼ばれる）。"""
        self._rows = self.tab.editor_task_rows(self._keys)
        if not self._rows:
            self.title_label.setText("")
            self.pages.setCurrentWidget(self.empty_page)
            return
        if len(self._rows) == 1:
            self._load_single(self._rows[0])
            self.pages.setCurrentWidget(self.single_page)
        else:
            self._load_multi(self._rows)
            self.pages.setCurrentWidget(self.multi_page)

    def _load_single(self, r):
        self.title_label.setText(f'{r["job_name"]} / {r["task_name"]}')
        if r["start"] is not None:
            self.schedule_label.setText(
                tr("{start:%Y-%m-%d} 〜 {last_day:%Y-%m-%d}（{working_days}営業日）", start=r["start"], last_day=r["last_day"], working_days=r["working_days"])
            )
        else:
            self.schedule_label.setText(tr("（計算中）"))
        self.pin_now_button.setEnabled(r["start"] is not None)
        self.pin_edit.blockSignals(True)
        self.pin_edit.set_value(r["start_pin_date"])
        self.pin_edit.blockSignals(False)

        self.days_spin.blockSignals(True)
        self.days_spin.default_value = r["default_days"]
        self.days_spin.setSpecialValueText(tr("既定（{default_days}日）", default_days=r["default_days"]))
        self.days_spin.setValue(r["override_days"] or 0)
        self.days_spin.blockSignals(False)

        self._fill_combo(self.team_combo, [(None, tr("（既定: {default_team_name}）", default_team_name=r["default_team_name"]))]
                         + self.tab.team_options(), r["override_team_id"])
        self._fill_combo(self.milestone_combo, [(None, tr("（既定: {default_milestone_name}）", default_milestone_name=r["default_milestone_name"]))]
                         + self.tab.milestone_options(), r["override_milestone_id"])
        self._fill_combo(self.status_combo, _status_options(), r["status"])
        self.active_check.setChecked(bool(r["is_active"]))
        self.tags_edit.setText(r["tags"])

    def _load_multi(self, rows):
        self.title_label.setText(tr("{n_rows}件のタスクを編集", n_rows=len(rows)))
        self.unpin_button.setEnabled(any(r["start_pin_date"] for r in rows))
        self._fill_combo(self.multi_team_combo, [(None, tr("（既定を使用）"))] + self.tab.team_options(),
                         self._common(rows, "override_team_id"))
        self._fill_combo(self.multi_milestone_combo,
                         [(None, tr("（既定を使用）"))] + self.tab.milestone_options(),
                         self._common(rows, "override_milestone_id"))
        self._fill_combo(self.multi_status_combo, _status_options(), self._common(rows, "status"))
        active = self._common(rows, "is_active")
        self.multi_active_check.setTristate(active is _MIXED)
        self.multi_active_check.setCheckState(
            Qt.PartiallyChecked if active is _MIXED else (Qt.Checked if active else Qt.Unchecked)
        )

    @staticmethod
    def _common(rows, column):
        values = {r[column] for r in rows}
        return values.pop() if len(values) == 1 else _MIXED

    @staticmethod
    def _fill_combo(combo, options, current):
        combo.blockSignals(True)
        combo.clear()
        if current is _MIXED:
            combo.addItem(tr(_MIXED_LABEL), _MIXED)
        for value, label in options:
            combo.addItem(label, value)
        index = 0 if current is _MIXED else next(
            (i for i in range(combo.count()) if combo.itemData(i) == current), 0
        )
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    # -- 書き込み -----------------------------------------------------------------

    def _apply(self, label, fields):
        if self._keys:
            self.tab.apply_task_fields(self._keys, label, fields)

    def _commit_pin(self):
        if len(self._rows) != 1:
            return
        value = self.pin_edit.value()
        if value != self._rows[0]["start_pin_date"]:
            self._apply(tr("タスクの開始固定日を変更"), {"start_pin_date": value})

    def _pin_to_current_start(self):
        r = self._rows[0] if len(self._rows) == 1 else None
        if r is not None and r["start"] is not None:
            self._apply(tr("タスクの開始日を固定"), {"start_pin_date": r["start"].isoformat()})

    def _commit_days(self):
        if len(self._rows) != 1:
            return
        value = self.days_spin.value() or None
        if value != self._rows[0]["override_days"]:
            self._apply(tr("タスクの日数上書きを変更"), {"override_days": value})

    def _commit_combo(self, combo, column):
        value = combo.currentData()
        if value is _MIXED:
            return
        labels = {"team_id": tr("タスクのチームを変更"), "milestone_id": tr("タスクのマイルストーンを変更"),
                  "status": tr("タスクの状態を変更")}
        self._apply(labels[column], {column: value})

    def _commit_active(self):
        self._apply(tr("タスクの有効／無効を変更"), {"is_active": self.active_check.isChecked()})

    def _commit_multi_active(self):
        # 「（混在）」から一度押したら、以後は通常の2状態にする
        checked = self.multi_active_check.checkState() != Qt.Unchecked
        self.multi_active_check.setTristate(False)
        self.multi_active_check.setChecked(checked)
        self._apply(tr("タスクの有効／無効を変更"), {"is_active": checked})

    def _commit_tags(self):
        if len(self._rows) != 1:
            return
        tags = normalize_tags(self.tags_edit.text())
        if tags != self._rows[0]["tags"]:
            self._apply(tr("タスク タグを変更"), {"tags": tags})
        else:
            self.tags_edit.setText(tags)

    def _commit_shift(self):
        n = self.shift_spin.value()
        if n != 0:
            self.tab.shift_tasks(self._keys, n)

    def _unpin_all(self):
        self._apply(tr("タスクの開始日の固定を解除"), {"start_pin_date": None})

    def _add_tag(self):
        text, ok = QInputDialog.getText(self, tr("タグを追加"), tr("追加するタグ（カンマ区切り）:"))
        new = parse_tags(text) if ok else []
        if new:
            self.tab.apply_task_fields(
                self._keys, tr("タスク タグを追加"),
                lambda r: {"tags": normalize_tags(", ".join(parse_tags(r["tags"]) + new))},
            )

    def _remove_tag(self):
        text, ok = QInputDialog.getText(self, tr("タグを外す"), tr("外すタグ（カンマ区切り）:"))
        drop = set(parse_tags(text)) if ok else set()
        if drop:
            self.tab.apply_task_fields(
                self._keys, tr("タスク タグを外す"),
                lambda r: {"tags": normalize_tags(", ".join(t for t in parse_tags(r["tags"])
                                                           if t not in drop))},
            )

    # -- 位置を覚える ---------------------------------------------------------------

    def showEvent(self, event):
        if not self._geometry_restored:
            self._geometry_restored = True
            geometry = self.tab.app_settings.get_ui_state(_GEOMETRY_STATE_KEY)
            if isinstance(geometry, QByteArray) and not geometry.isEmpty():
                self.restoreGeometry(geometry)
        super().showEvent(event)

    def hideEvent(self, event):
        self.tab.app_settings.set_ui_state(_GEOMETRY_STATE_KEY, self.saveGeometry())
        super().hideEvent(event)
