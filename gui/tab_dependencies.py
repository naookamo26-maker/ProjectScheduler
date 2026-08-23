"""
タブ4「依存関係」（External_Dependencies相当）。

ジョブをまたぐタスク依存を一覧・追加・削除する。行の直接編集は行わず、
「＋追加」で開くダイアログ（ジョブ→タスクのカスケードコンボ×2）で
新しい依存を作成し、既存行は名前表示のみ（削除して作り直す形にすることで、
カスケードコンボを常時ライブ編集可能にする複雑さを避けている）。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QMessageBox,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.db import ProjectDatabaseError
from gui.widgets_common import CrudSection, row_id, set_row_id


class ExternalDependencyDialog(QDialog):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("外部依存を追加")

        form = QFormLayout(self)

        self.job_combo = QComboBox()
        self.task_combo = QComboBox()
        self.dep_job_combo = QComboBox()
        self.dep_task_combo = QComboBox()

        for j in self.db.list_jobs():
            self.job_combo.addItem(j["name"], j["id"])
            self.dep_job_combo.addItem(j["name"], j["id"])

        self.job_combo.currentIndexChanged.connect(
            lambda: self._reload_tasks(self.job_combo, self.task_combo)
        )
        self.dep_job_combo.currentIndexChanged.connect(
            lambda: self._reload_tasks(self.dep_job_combo, self.dep_task_combo)
        )

        form.addRow("依存するジョブ", self.job_combo)
        form.addRow("依存するタスク", self.task_combo)
        form.addRow("依存先ジョブ（先に終わっている必要がある）", self.dep_job_combo)
        form.addRow("依存先タスク", self.dep_task_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._reload_tasks(self.job_combo, self.task_combo)
        self._reload_tasks(self.dep_job_combo, self.dep_task_combo)

    def _reload_tasks(self, job_combo, task_combo):
        task_combo.clear()
        job_id = job_combo.currentData()
        if job_id is None:
            return
        job = next((j for j in self.db.list_jobs() if j["id"] == job_id), None)
        if job is None:
            return
        for t in self.db.list_workflow_tasks(job["workflow_id"]):
            task_combo.addItem(t["name"], t["id"])

    def values(self):
        return (
            self.job_combo.currentData(), self.task_combo.currentData(),
            self.dep_job_combo.currentData(), self.dep_task_combo.currentData(),
        )


class DependenciesTab(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        layout = QVBoxLayout(self)
        self.section = CrudSection(
            "依存関係（ジョブをまたぐタスクの依存）",
            ["依存するジョブ", "タスク", "依存先ジョブ", "依存先タスク"],
            on_add=self._add, on_delete=self._delete,
        )
        layout.addWidget(self.section)

        self.refresh()

    def refresh(self):
        table = self.section.table
        table.setRowCount(0)
        for d in self.db.list_external_dependencies():
            row = table.rowCount()
            table.insertRow(row)
            for col, key in enumerate(
                ["job_name", "task_name", "depends_on_job_name", "depends_on_task_name"]
            ):
                item = QTableWidgetItem(str(d[key]))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                table.setItem(row, col, item)
            set_row_id(table, row, d["id"])

    def _add(self):
        if not self.db.list_jobs():
            QMessageBox.information(self, "ジョブ未登録", "先に「ジョブ」タブでジョブを作成してください。")
            return
        dialog = ExternalDependencyDialog(self.db, self)
        if dialog.exec() != QDialog.Accepted:
            return
        job_id, task_id, dep_job_id, dep_task_id = dialog.values()
        if None in (job_id, task_id, dep_job_id, dep_task_id):
            QMessageBox.warning(self, "入力エラー", "すべての項目を選択してください。")
            return
        try:
            self.db.add_external_dependency(job_id, task_id, dep_job_id, dep_task_id)
        except ProjectDatabaseError as e:
            QMessageBox.warning(self, "追加できません", str(e))
            return
        self.refresh()

    def _delete(self, row):
        dep_id = row_id(self.section.table, row)
        self.db.delete_external_dependency(dep_id)
        self.refresh()

    def refresh_choices(self):
        self.refresh()
