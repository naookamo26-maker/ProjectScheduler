"""
タブ3「ジョブ」。

上段: ジョブ一覧（名前・ワークフロー・既定マイルストーン・優先度）。
下段: 選択中ジョブのタスク上書き表——選択ジョブが使うワークフローのタスク
一覧をそのまま自動的に表示し（手入力不要）、既定から外れる項目（無効化・
日数上書き・マイルストーン上書き・チーム上書き）だけを編集する。
既定値のままの行はDBに保存しない（差分のみ保持、gui/db.py参照）。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QMessageBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.db import DuplicateNameError
from gui.widgets_common import (
    CrudSection,
    confirm_or_block_delete,
    make_fk_combo,
    row_id,
    set_row_id,
    unique_default_name,
)


def _readonly_item(text):
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


class JobsTab(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.current_job_id = None

        layout = QVBoxLayout(self)

        self.jobs_section = CrudSection(
            "ジョブ", ["ジョブ名", "ワークフロー", "既定マイルストーン", "優先度"],
            on_add=self._add_job, on_delete=self._delete_job,
        )
        self.jobs_section.table.itemChanged.connect(self._on_job_name_changed)
        self.jobs_section.table.currentCellChanged.connect(self._on_job_selection_changed)
        layout.addWidget(self.jobs_section, 1)

        override_group = QGroupBox("タスク上書き（選択中のジョブ）")
        override_layout = QVBoxLayout(override_group)
        self.override_table = QTableWidget(0, 7)
        self.override_table.setHorizontalHeaderLabels(
            ["タスク名", "既定チーム", "既定日数", "有効", "日数上書き\n（0＝既定通り）",
             "マイルストーン上書き", "チーム上書き"]
        )
        self.override_table.verticalHeader().setVisible(False)
        self.override_table.setSelectionMode(QTableWidget.NoSelection)
        override_layout.addWidget(self.override_table)
        layout.addWidget(override_group, 1)

        self.refresh_jobs()

    # -- ジョブ一覧 --------------------------------------------------------------

    def refresh_jobs(self, select_id=None):
        table = self.jobs_section.table
        table.blockSignals(True)
        table.setRowCount(0)
        workflow_options = [(w["id"], w["name"]) for w in self.db.list_workflows()]
        milestone_options = [(m["id"], m["name"]) for m in self.db.list_milestones()]
        select_row = -1
        for job in self.db.list_jobs():
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(job["name"]))
            set_row_id(table, row, job["id"])
            if job["id"] == select_id:
                select_row = row

            wf_combo = make_fk_combo(workflow_options, job["workflow_id"])
            wf_combo.currentIndexChanged.connect(
                lambda _idx, jid=job["id"]: self._on_job_field_changed(jid)
            )
            table.setCellWidget(row, 1, wf_combo)

            ms_combo = make_fk_combo(milestone_options, job["default_milestone_id"], allow_blank=True)
            ms_combo.currentIndexChanged.connect(
                lambda _idx, jid=job["id"]: self._on_job_field_changed(jid)
            )
            table.setCellWidget(row, 2, ms_combo)

            priority_spin = QSpinBox()
            priority_spin.setRange(1, 999)
            priority_spin.setValue(job["priority"])
            priority_spin.valueChanged.connect(
                lambda _val, jid=job["id"]: self._on_job_field_changed(jid)
            )
            table.setCellWidget(row, 3, priority_spin)
        table.blockSignals(False)

        if select_row >= 0:
            table.setCurrentCell(select_row, 0)
        else:
            self._on_job_selection_changed(table.currentRow(), 0, -1, -1)

    def _add_job(self):
        if not self.db.list_workflows():
            QMessageBox.information(
                self, "ワークフロー未登録",
                "先に「ワークフロー設計」タブでワークフローを1つ以上作成してください。",
            )
            return
        existing = {j["name"] for j in self.db.list_jobs()}
        name = unique_default_name(existing, "新しいジョブ")
        workflow_id = self.db.list_workflows()[0]["id"]
        try:
            new_id = self.db.add_job(name, workflow_id, None, 100)
        except DuplicateNameError as e:
            QMessageBox.warning(self, "追加できません", str(e))
            return
        self.refresh_jobs(select_id=new_id)

    def _delete_job(self, row):
        job_id = row_id(self.jobs_section.table, row)
        count = self.db.job_usage_count(job_id)
        if not confirm_or_block_delete(self, count, "このジョブ", hard_block=False):
            return
        self.db.delete_job(job_id)
        self.refresh_jobs()

    def _on_job_name_changed(self, item):
        if item.column() != 0:
            return
        job_id = row_id(self.jobs_section.table, item.row())
        if job_id is None:
            return
        self._write_job(job_id, name_override=item.text())

    def _on_job_field_changed(self, job_id):
        self._write_job(job_id)

    def _write_job(self, job_id, name_override=None):
        table = self.jobs_section.table
        for row in range(table.rowCount()):
            if row_id(table, row) == job_id:
                name = name_override if name_override is not None else table.item(row, 0).text()
                workflow_id = table.cellWidget(row, 1).currentData()
                milestone_id = table.cellWidget(row, 2).currentData()
                priority = table.cellWidget(row, 3).value()
                try:
                    self.db.update_job(job_id, name, workflow_id, milestone_id, priority)
                except DuplicateNameError as e:
                    QMessageBox.warning(self, "変更できません", str(e))
                    self.refresh_jobs()
                    return
                if job_id == self.current_job_id:
                    self._refresh_overrides()
                return

    # -- タスク上書き ------------------------------------------------------------

    def _on_job_selection_changed(self, current_row, _current_col, _prev_row, _prev_col):
        if current_row is None or current_row < 0:
            self.current_job_id = None
            self.override_table.setRowCount(0)
            return
        self.current_job_id = row_id(self.jobs_section.table, current_row)
        self._refresh_overrides()

    def _refresh_overrides(self):
        table = self.override_table
        table.blockSignals(True)
        table.setRowCount(0)
        if self.current_job_id is None:
            table.blockSignals(False)
            return

        milestone_options = [(m["id"], m["name"]) for m in self.db.list_milestones()]
        team_options = [(t["id"], t["name"]) for t in self.db.list_teams()]

        for r in self.db.list_job_tasks_with_overrides(self.current_job_id):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, _readonly_item(r["task_name"]))
            table.setItem(row, 1, _readonly_item(r["default_team_name"]))
            table.setItem(row, 2, _readonly_item(f"{r['default_days']}日"))
            set_row_id(table, row, r["workflow_task_id"])

            active_checkbox = QCheckBox()
            active_checkbox.setChecked(True if r["is_active"] is None else bool(r["is_active"]))
            active_checkbox.stateChanged.connect(
                lambda _state, tid=r["workflow_task_id"]: self._on_override_changed(tid)
            )
            table.setCellWidget(row, 3, active_checkbox)

            days_spin = QSpinBox()
            days_spin.setRange(0, 9999)
            days_spin.setSpecialValueText("既定通り")
            days_spin.setValue(r["override_days"] or 0)
            days_spin.valueChanged.connect(
                lambda _val, tid=r["workflow_task_id"]: self._on_override_changed(tid)
            )
            table.setCellWidget(row, 4, days_spin)

            ms_combo = make_fk_combo(
                milestone_options, r["override_milestone_id"], allow_blank=True, blank_label="（既定を使用）"
            )
            ms_combo.currentIndexChanged.connect(
                lambda _idx, tid=r["workflow_task_id"]: self._on_override_changed(tid)
            )
            table.setCellWidget(row, 5, ms_combo)

            team_combo = make_fk_combo(
                team_options, r["override_team_id"], allow_blank=True, blank_label="（既定を使用）"
            )
            team_combo.currentIndexChanged.connect(
                lambda _idx, tid=r["workflow_task_id"]: self._on_override_changed(tid)
            )
            table.setCellWidget(row, 6, team_combo)
        table.blockSignals(False)

    def _on_override_changed(self, workflow_task_id):
        table = self.override_table
        for row in range(table.rowCount()):
            if row_id(table, row) == workflow_task_id:
                is_active = table.cellWidget(row, 3).isChecked()
                days_val = table.cellWidget(row, 4).value()
                override_days = days_val if days_val > 0 else None
                milestone_id = table.cellWidget(row, 5).currentData()
                team_id = table.cellWidget(row, 6).currentData()

                if not is_active or override_days is not None or milestone_id is not None or team_id is not None:
                    self.db.upsert_job_task_override(
                        self.current_job_id, workflow_task_id, is_active=is_active,
                        override_days=override_days, milestone_id=milestone_id, team_id=team_id,
                    )
                else:
                    self.db.clear_job_task_override(self.current_job_id, workflow_task_id)
                return

    # -- 他タブからの通知 ----------------------------------------------------------

    def refresh_choices(self):
        """ワークフロー・マイルストーン・チームの変更を反映する。"""
        self.refresh_jobs(select_id=self.current_job_id)
