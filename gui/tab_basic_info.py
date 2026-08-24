"""
タブ1「基本情報設定」。

プロジェクト名・開始日、マイルストーン、チーム、休業日を1画面にまとめる。
変更はメモリ上のDBに即時反映されるが、ファイルへの書き込みはCtrl+S等の
明示的な保存を待つ（gui/main.py参照）。

セル編集ウィジェット（QComboBox/QDateEdit）のシグナルは、行インデックスでは
なく DBの整数ID（entity_id）をクロージャで捕まえて紐づける——行の並び替えや
他行の削除で見た目上の行番号がずれても、対象のDB行を取り違えないようにする
ため（QTableWidgetItemのテキスト編集は item.row() が常に最新の行を指すので
そのままで安全）。
"""

from datetime import date, timedelta

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.db import DuplicateNameError, ReferencedEntityError
from gui.widgets_common import (
    CrudSection,
    NoWheelDateEdit,
    NoWheelSpinBox,
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


def _readonly_item(text):
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


class TeamCapacityDialog(QDialog):
    """チームの同時ライン数が期間の途中で変わる場合の変更点（適用開始日・
    ライン数）を追加・削除するダイアログ。開発開始日からの既定値そのものは
    チーム一覧本体の「同時ライン数」列で編集するため、ここでは以降の
    変更点のみを扱う（＋削除・追加のみで、値そのものは表内で直接編集する）。"""

    def __init__(self, db, team, parent=None):
        super().__init__(parent)
        self.db = db
        self.team = team
        self.setWindowTitle(f"「{team['name']}」の同時ライン数の変動")
        self.resize(420, 340)

        layout = QVBoxLayout(self)
        info = QLabel(
            f"開発開始日からの既定値: {team['max_lines']}ライン"
            "（この値自体はチーム一覧の「同時ライン数」列で変更してください）\n"
            "ここでは、途中でライン数が変わる日付とその日以降のライン数を追加できます。"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.section = CrudSection(
            "変更点（適用開始日順）", ["適用開始日", "ライン数"],
            on_add=self._add_change, on_delete=self._delete_change,
        )
        layout.addWidget(self.section, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        # QDialogButtonBox(Close) の役割はRejectRoleのため rejected が発火する。
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)

        self._refresh()

    def _refresh(self):
        table = self.section.table
        table.blockSignals(True)
        table.setRowCount(0)
        for c in self.db.list_team_capacity_changes(self.team["id"]):
            row = table.rowCount()
            table.insertRow(row)
            set_row_id(table, row, c["id"])

            date_edit = NoWheelDateEdit(_to_qdate(c["start_date"]))
            date_edit.setCalendarPopup(True)
            date_edit.setDisplayFormat("yyyy-MM-dd")
            date_edit.dateChanged.connect(
                lambda _qdate, cid=c["id"]: self._on_change_edited(cid)
            )
            table.setCellWidget(row, 0, date_edit)

            lines_spin = NoWheelSpinBox()
            lines_spin.setRange(1, 999)
            lines_spin.setValue(c["lines"])
            lines_spin.valueChanged.connect(
                lambda _val, cid=c["id"]: self._on_change_edited(cid)
            )
            table.setCellWidget(row, 1, lines_spin)
        table.blockSignals(False)
        auto_size_columns(table)

    def _add_change(self):
        # 既定日は、既存の変更点のうち最も遅い日付の翌日（無ければ開発開始日の翌日）を提案する。
        existing = self.db.list_team_capacity_changes(self.team["id"])
        if existing:
            base = _to_qdate(existing[-1]["start_date"]).addDays(1)
        else:
            proj = self.db.get_project()
            base = _to_qdate(proj["start_date"] or date.today().isoformat()).addDays(1)
        existing_dates = {c["start_date"] for c in existing}
        while _to_iso(base) in existing_dates:
            base = base.addDays(1)
        try:
            self.db.add_team_capacity_change(self.team["id"], _to_iso(base), self.team["max_lines"])
        except DuplicateNameError as e:
            QMessageBox.warning(self, "追加できません", str(e))
            return
        self._refresh()

    def _delete_change(self, row):
        change_id = row_id(self.section.table, row)
        self.db.delete_team_capacity_change(change_id)
        self._refresh()

    def _on_change_edited(self, change_id):
        table = self.section.table
        for row in range(table.rowCount()):
            if row_id(table, row) == change_id:
                date_edit = table.cellWidget(row, 0)
                lines_spin = table.cellWidget(row, 1)
                try:
                    self.db.update_team_capacity_change(
                        change_id, _to_iso(date_edit.date()), lines_spin.value()
                    )
                except DuplicateNameError as e:
                    QMessageBox.warning(self, "変更できません", str(e))
                    self._refresh()
                    return
                # 適用開始日を変更すると並び順が変わりうるため、次のイベント
                # ループで並べ直す（このメソッド自体がdate_editのdateChanged
                # シグナル内から呼ばれているため、ウィジェットの再構築を遅延させる）。
                QTimer.singleShot(0, self._refresh)
                return


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
            "チーム", ["チーム名", "同時ライン数（開発開始日からの既定値）", "期間中の変動"],
            on_add=self._add_team, on_delete=self._delete_team,
            on_edit=self._edit_team_capacity,
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

        self.start_date_edit = NoWheelDateEdit()
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
            date_edit = NoWheelDateEdit(_to_qdate(ms["end_date"]))
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
            return
        # 名前は締切日に次ぐ第2ソートキー（db.list_milestones参照）のため、
        # 同じ締切日の別マイルストーンとの前後関係が変わりうる。表示順を
        # 締切日順に合わせ直す（このメソッド自体がitemChangedシグナル内から
        # 呼ばれているため、ウィジェットの再構築は次のイベントループへ遅延させる）。
        QTimer.singleShot(0, self.refresh_milestones)

    def _on_milestone_date_changed(self, milestone_id, qdate):
        table = self.milestones_section.table
        for row in range(table.rowCount()):
            if row_id(table, row) == milestone_id:
                name = table.item(row, 0).text()
                self.db.update_milestone(milestone_id, name, _to_iso(qdate))
                # 締切日を変更したら、締切日順の表示を保つよう並べ直す
                # （dateChangedシグナル内から呼ばれているため、ウィジェットの
                # 再構築は次のイベントループへ遅延させる）。
                QTimer.singleShot(0, self.refresh_milestones)
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
            spin = NoWheelSpinBox()
            spin.setRange(1, 999)
            spin.setValue(team["max_lines"])
            spin.valueChanged.connect(
                lambda value, eid=team["id"]: self._on_team_lines_changed(eid, value)
            )
            table.setCellWidget(row, 1, spin)

            changes = self.db.list_team_capacity_changes(team["id"])
            summary = (
                "、".join(f"{c['start_date']}〜{c['lines']}ライン" for c in changes)
                if changes else "（変動なし。「編集...」から追加）"
            )
            table.setItem(row, 2, _readonly_item(summary))
        table.blockSignals(False)
        auto_size_columns(table)

    def _edit_team_capacity(self, row):
        table = self.teams_section.table
        team_id = row_id(table, row)
        team = next((t for t in self.db.list_teams() if t["id"] == team_id), None)
        if team is None:
            return
        dialog = TeamCapacityDialog(self.db, team, self)
        dialog.exec()
        self.refresh_teams()

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

            date_edit = NoWheelDateEdit(_to_qdate(hol["date"]))
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
