"""
タブ1「基本情報設定」。

プロジェクト名・開始日、マイルストーン、チーム、休業日を1画面にまとめる。
すべて即時DB書き込み（明示的な保存ボタンは持たない）。

セル編集ウィジェット（QComboBox/QDateEdit）のシグナルは、行インデックスでは
なく DBの整数ID（entity_id）をクロージャで捕まえて紐づける——行の並び替えや
他行の削除で見た目上の行番号がずれても、対象のDB行を取り違えないようにする
ため（QTableWidgetItemのテキスト編集は item.row() が常に最新の行を指すので
そのままで安全）。
"""

from datetime import date, timedelta

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QDateEdit,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.db import DuplicateNameError, ReferencedEntityError
from gui.widgets_common import (
    CrudSection,
    auto_size_columns,
    confirm_or_block_delete,
    make_fk_combo,
    row_id,
    set_row_id,
    unique_default_name,
)


def _to_qdate(iso_str):
    d = QDate.fromString(iso_str, "yyyy-MM-dd")
    return d if d.isValid() else QDate.currentDate()


def _to_iso(qdate):
    return qdate.toString("yyyy-MM-dd")


class BasicInfoTab(QWidget):
    def __init__(self, db, on_teams_changed=None, parent=None):
        super().__init__(parent)
        self.db = db
        # チーム一覧が変わったら他タブ（ワークフロー設計等）のコンボも更新したいので
        # 呼び出し元からコールバックを受け取る。
        self.on_teams_changed = on_teams_changed

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)

        layout.addWidget(self._build_project_group())

        self.milestones_section = CrudSection(
            "マイルストーン", ["マイルストーン名", "締切日"],
            on_add=self._add_milestone, on_delete=self._delete_milestone,
        )
        self.milestones_section.table.itemChanged.connect(self._on_milestone_name_changed)
        layout.addWidget(self.milestones_section)

        self.teams_section = CrudSection(
            "チーム", ["チーム名", "同時ライン数"],
            on_add=self._add_team, on_delete=self._delete_team,
        )
        self.teams_section.table.itemChanged.connect(self._on_team_name_changed)
        layout.addWidget(self.teams_section)

        self.holidays_section = CrudSection(
            "休業日", ["日付", "対象チーム（未設定＝全社共通）"],
            on_add=self._add_holiday, on_delete=self._delete_holiday,
        )
        layout.addWidget(self.holidays_section)

        layout.addStretch(1)

        self.refresh_all()

    # -- プロジェクト概要 -----------------------------------------------------

    def _build_project_group(self):
        group = QGroupBox("プロジェクト概要")
        form = QFormLayout(group)

        self.project_name_edit = QLineEdit()
        self.project_name_edit.editingFinished.connect(self._on_project_changed)
        form.addRow("プロジェクト名", self.project_name_edit)

        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.start_date_edit.dateChanged.connect(self._on_project_changed)
        form.addRow("開発開始日", self.start_date_edit)

        return group

    def _on_project_changed(self):
        self.db.set_project(self.project_name_edit.text(), _to_iso(self.start_date_edit.date()))

    def refresh_project(self):
        proj = self.db.get_project()
        self.project_name_edit.blockSignals(True)
        self.start_date_edit.blockSignals(True)
        self.project_name_edit.setText(proj["project_name"])
        self.start_date_edit.setDate(_to_qdate(proj["start_date"] or date.today().isoformat()))
        self.project_name_edit.blockSignals(False)
        self.start_date_edit.blockSignals(False)

    # -- マイルストーン ---------------------------------------------------------

    def refresh_milestones(self):
        table = self.milestones_section.table
        table.blockSignals(True)
        table.setRowCount(0)
        for ms in self.db.list_milestones():
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(ms["name"]))
            set_row_id(table, row, ms["id"])
            date_edit = QDateEdit(_to_qdate(ms["end_date"]))
            date_edit.setCalendarPopup(True)
            date_edit.setDisplayFormat("yyyy-MM-dd")
            date_edit.dateChanged.connect(
                lambda qdate, eid=ms["id"]: self._on_milestone_date_changed(eid, qdate)
            )
            table.setCellWidget(row, 1, date_edit)
        table.blockSignals(False)
        auto_size_columns(table)

    def _add_milestone(self):
        existing = {ms["name"] for ms in self.db.list_milestones()}
        name = unique_default_name(existing, "新しいマイルストーン")
        default_date = date.today().isoformat()
        try:
            self.db.add_milestone(name, default_date)
        except DuplicateNameError as e:
            QMessageBox.warning(self, "追加できません", str(e))
            return
        self.refresh_milestones()

    def _delete_milestone(self, row):
        table = self.milestones_section.table
        ms_id = row_id(table, row)
        count = self.db.milestone_usage_count(ms_id)
        if not confirm_or_block_delete(self, count, "このマイルストーン", hard_block=False):
            return
        self.db.delete_milestone(ms_id)
        self.refresh_milestones()

    def _on_milestone_name_changed(self, item):
        if item.column() != 0:
            return
        table = self.milestones_section.table
        ms_id = row_id(table, item.row())
        if ms_id is None:
            return
        date_edit = table.cellWidget(item.row(), 1)
        try:
            self.db.update_milestone(ms_id, item.text(), _to_iso(date_edit.date()))
        except DuplicateNameError as e:
            QMessageBox.warning(self, "変更できません", str(e))
            self.refresh_milestones()

    def _on_milestone_date_changed(self, milestone_id, qdate):
        table = self.milestones_section.table
        for row in range(table.rowCount()):
            if row_id(table, row) == milestone_id:
                name = table.item(row, 0).text()
                self.db.update_milestone(milestone_id, name, _to_iso(qdate))
                return

    # -- チーム ------------------------------------------------------------------

    def refresh_teams(self):
        table = self.teams_section.table
        table.blockSignals(True)
        table.setRowCount(0)
        for team in self.db.list_teams():
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(team["name"]))
            set_row_id(table, row, team["id"])
            spin = QSpinBox()
            spin.setRange(1, 999)
            spin.setValue(team["max_lines"])
            spin.valueChanged.connect(
                lambda value, eid=team["id"]: self._on_team_lines_changed(eid, value)
            )
            table.setCellWidget(row, 1, spin)
        table.blockSignals(False)
        auto_size_columns(table)

    def _add_team(self):
        existing = {t["name"] for t in self.db.list_teams()}
        name = unique_default_name(existing, "新しいチーム")
        try:
            self.db.add_team(name, 1)
        except DuplicateNameError as e:
            QMessageBox.warning(self, "追加できません", str(e))
            return
        self.refresh_teams()
        self._notify_teams_changed()

    def _delete_team(self, row):
        table = self.teams_section.table
        team_id = row_id(table, row)
        count = self.db.team_usage_count(team_id)
        if not confirm_or_block_delete(self, count, "このチーム", hard_block=True):
            return
        try:
            self.db.delete_team(team_id)
        except ReferencedEntityError as e:
            QMessageBox.warning(self, "削除できません", str(e))
            return
        self.refresh_teams()
        self._notify_teams_changed()

    def _on_team_name_changed(self, item):
        if item.column() != 0:
            return
        table = self.teams_section.table
        team_id = row_id(table, item.row())
        if team_id is None:
            return
        spin = table.cellWidget(item.row(), 1)
        try:
            self.db.update_team(team_id, item.text(), spin.value())
        except DuplicateNameError as e:
            QMessageBox.warning(self, "変更できません", str(e))
            self.refresh_teams()
            return
        self._notify_teams_changed()

    def _on_team_lines_changed(self, team_id, value):
        table = self.teams_section.table
        for row in range(table.rowCount()):
            if row_id(table, row) == team_id:
                name = table.item(row, 0).text()
                self.db.update_team(team_id, name, value)
                return

    def _notify_teams_changed(self):
        if self.on_teams_changed:
            self.on_teams_changed()

    # -- 休業日 -------------------------------------------------------------------

    def refresh_holidays(self):
        table = self.holidays_section.table
        table.blockSignals(True)
        table.setRowCount(0)
        team_options = [(t["id"], t["name"]) for t in self.db.list_teams()]
        for hol in self.db.list_holidays():
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(""))  # 日付列はウィジェット表示のみ、テキストは未使用
            set_row_id(table, row, hol["id"])

            date_edit = QDateEdit(_to_qdate(hol["date"]))
            date_edit.setCalendarPopup(True)
            date_edit.setDisplayFormat("yyyy-MM-dd")
            date_edit.dateChanged.connect(
                lambda qdate, eid=hol["id"]: self._on_holiday_changed(eid)
            )
            table.setCellWidget(row, 0, date_edit)

            combo = make_fk_combo(team_options, hol["team_id"], allow_blank=True, blank_label="（全社共通）")
            combo.currentIndexChanged.connect(
                lambda idx, eid=hol["id"]: self._on_holiday_changed(eid)
            )
            table.setCellWidget(row, 1, combo)
        table.blockSignals(False)
        auto_size_columns(table)

    def _add_holiday(self):
        # 「追加」連打で全社共通・同日の重複エラーが出ないよう、空いている日付を
        # 自動的に探す（既定は全社共通＝チーム未設定）。
        existing = {h["date"] for h in self.db.list_holidays() if h["team_id"] is None}
        d = date.today()
        while d.isoformat() in existing:
            d += timedelta(days=1)
        self.db.add_holiday(d.isoformat(), None)
        self.refresh_holidays()

    def _delete_holiday(self, row):
        table = self.holidays_section.table
        hol_id = row_id(table, row)
        self.db.delete_holiday(hol_id)
        self.refresh_holidays()

    def _on_holiday_changed(self, holiday_id):
        table = self.holidays_section.table
        for row in range(table.rowCount()):
            if row_id(table, row) == holiday_id:
                date_edit = table.cellWidget(row, 0)
                combo = table.cellWidget(row, 1)
                try:
                    self.db.update_holiday(holiday_id, _to_iso(date_edit.date()), combo.currentData())
                except DuplicateNameError as e:
                    QMessageBox.warning(self, "変更できません", str(e))
                    self.refresh_holidays()
                return

    # -- 全体再読み込み ----------------------------------------------------------

    def refresh_all(self):
        self.refresh_project()
        self.refresh_milestones()
        self.refresh_teams()
        self.refresh_holidays()

    def refresh_choices(self):
        """他タブの変更（現状なし）に合わせて表示を更新する共通インターフェース。
        MainWindowのタブ切り替え時フックから呼ばれる。"""
        self.refresh_all()
