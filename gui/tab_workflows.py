"""
タブ2「ワークフロー設計」。

左側にワークフロー一覧（追加・名前変更・削除）、右側に選択中のワークフローの
ノードグラフキャンバス（gui/node_canvas.py）を表示する。
"""

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.db import DuplicateNameError, ProjectDatabaseError, ReferencedEntityError
from gui.node_canvas import WorkflowGraphScene, WorkflowGraphView
from gui.widgets_common import (
    CrudSection,
    auto_size_columns,
    confirm_or_block_delete,
    keep_selection_visible,
    row_id,
    set_row_id,
)

_HELP_TEXT = (
    "左のリストからワークフローを選択するか、「＋追加」で新規作成してください。\n\n"
    "キャンバス上で右クリックすると、その位置にタスクを追加できます。\n"
    "タスク右端の丸（出力）から別タスク左端の丸（入力）へドラッグすると、\n"
    "依存関係（先に完了すべきタスク → 後続タスク）を作成できます。\n"
    "循環する依存関係は自動的に拒否されます。"
)


class DependencyTemplateDialog(QDialog):
    """依存テンプレート（ワークフローペア単位の既定タスク対応）の追加・編集ダイアログ。
    「このワークフローのタスク」は現在選択中のワークフロー内のタスクに固定し、
    依存先ワークフロー→依存先タスクをカスケードのドロップダウンで選ばせる
    （追加・編集のいずれも同じ3つのドロップダウンから後から選び直せる）。"""

    def __init__(self, db, workflow_id, parent=None, initial=None):
        super().__init__(parent)
        self.db = db
        self.workflow_id = workflow_id
        self.setWindowTitle("依存テンプレートを編集" if initial else "依存テンプレートを追加")

        form = QFormLayout(self)

        self.task_combo = QComboBox()
        for t in db.list_workflow_tasks(workflow_id):
            self.task_combo.addItem(t["name"], t["id"])
        form.addRow("このワークフローのタスク", self.task_combo)

        self.target_workflow_combo = QComboBox()
        for wf in db.list_workflows():
            if wf["id"] != workflow_id:
                self.target_workflow_combo.addItem(wf["name"], wf["id"])
        self.target_workflow_combo.currentIndexChanged.connect(self._reload_target_tasks)

        self.target_task_combo = QComboBox()

        form.addRow("依存先ワークフロー", self.target_workflow_combo)
        form.addRow("依存先タスク", self.target_task_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        if initial is not None:
            task_id, target_workflow_id, target_task_id = initial
            idx = self.task_combo.findData(task_id)
            if idx >= 0:
                self.task_combo.setCurrentIndex(idx)
            idx = self.target_workflow_combo.findData(target_workflow_id)
            if idx >= 0:
                self.target_workflow_combo.setCurrentIndex(idx)
        self._reload_target_tasks()
        if initial is not None:
            idx = self.target_task_combo.findData(initial[2])
            if idx >= 0:
                self.target_task_combo.setCurrentIndex(idx)

    def _reload_target_tasks(self):
        self.target_task_combo.clear()
        target_workflow_id = self.target_workflow_combo.currentData()
        if target_workflow_id is None:
            return
        for t in self.db.list_workflow_tasks(target_workflow_id):
            self.target_task_combo.addItem(t["name"], t["id"])

    def values(self):
        return (
            self.task_combo.currentData(),
            self.target_workflow_combo.currentData(),
            self.target_task_combo.currentData(),
        )


class WorkflowsTab(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.current_scene = None
        self.current_workflow_id = None

        layout = QHBoxLayout(self)

        left = QWidget()
        left.setMaximumWidth(240)
        left_layout = QVBoxLayout(left)

        toolbar = QHBoxLayout()
        add_btn = QPushButton("＋追加")
        add_btn.clicked.connect(self._add_workflow)
        rename_btn = QPushButton("名前変更")
        rename_btn.clicked.connect(self._rename_workflow)
        delete_btn = QPushButton("－削除")
        delete_btn.clicked.connect(self._delete_workflow)
        toolbar.addWidget(add_btn)
        toolbar.addWidget(rename_btn)
        toolbar.addWidget(delete_btn)
        left_layout.addLayout(toolbar)

        self.workflow_list = QListWidget()
        self.workflow_list.currentItemChanged.connect(self._on_selection_changed)
        # ドラッグで上下の表示順を入れ替えられるようにする（並び順はDBの
        # workflows.sort_orderに保存し、次回開いた時も同じ順序で表示する）。
        self.workflow_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.workflow_list.model().rowsMoved.connect(self._on_workflow_reordered)
        keep_selection_visible(self.workflow_list)
        left_layout.addWidget(self.workflow_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.view = WorkflowGraphView()

        self.empty_label = QLabel(_HELP_TEXT)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignCenter)

        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self.empty_label)
        self.right_stack.addWidget(self.view)
        right_layout.addWidget(self.right_stack, 3)

        self.template_section = CrudSection(
            "依存テンプレート（このワークフローが他のワークフローに依存する場合の既定タスク対応）",
            ["このワークフローのタスク", "依存先ワークフロー", "依存先タスク"],
            on_add=self._add_template, on_delete=self._delete_template,
            on_edit=self._edit_template,
        )
        right_layout.addWidget(self.template_section, 1)

        layout.addWidget(left)
        layout.addWidget(right, 1)

        self.refresh_workflows()

    def refresh_workflows(self, select_id=None):
        self.workflow_list.blockSignals(True)
        self.workflow_list.clear()
        selected_item = None
        for wf in self.db.list_workflows():
            item = QListWidgetItem(wf["name"])
            item.setData(Qt.UserRole, wf["id"])
            self.workflow_list.addItem(item)
            if select_id == wf["id"]:
                selected_item = item
        self.workflow_list.blockSignals(False)

        if selected_item is not None:
            self.workflow_list.setCurrentItem(selected_item)
        elif self.workflow_list.count() > 0:
            self.workflow_list.setCurrentRow(0)
        else:
            self.workflow_list.setCurrentRow(-1)
        self._on_selection_changed(self.workflow_list.currentItem(), None)

    def _on_workflow_reordered(self, *_args):
        """ワークフロー一覧をドラッグで並び替えた直後（QListWidgetModelの
        rowsMoved）に呼ばれ、新しい表示順をDBに保存する。"""
        ordered_ids = [
            self.workflow_list.item(i).data(Qt.UserRole)
            for i in range(self.workflow_list.count())
        ]
        self.db.reorder_workflows(ordered_ids)

    def _on_selection_changed(self, current, _previous):
        if current is None:
            self.right_stack.setCurrentWidget(self.empty_label)
            self.current_scene = None
            self.current_workflow_id = None
            self.template_section.setEnabled(False)
            self.template_section.table.setRowCount(0)
            return
        workflow_id = current.data(Qt.UserRole)
        self.current_workflow_id = workflow_id
        self.current_scene = WorkflowGraphScene(self.db, workflow_id, self)
        self.view.setScene(self.current_scene)
        self.right_stack.setCurrentWidget(self.view)
        # シーン切替直後はビューポートサイズが未確定な場合があるため、
        # レイアウト確定後（次のイベントループ）にフィットさせる。
        QTimer.singleShot(0, self.view.fit_all)
        self.template_section.setEnabled(True)
        self.refresh_templates()

    # -- 依存テンプレート ----------------------------------------------------------

    def refresh_templates(self):
        table = self.template_section.table
        table.setRowCount(0)
        if self.current_workflow_id is None:
            return
        for tpl in self.db.list_dependency_templates(self.current_workflow_id):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(tpl["task_name"]))
            table.setItem(row, 1, QTableWidgetItem(tpl["depends_on_workflow_name"]))
            table.setItem(row, 2, QTableWidgetItem(tpl["depends_on_task_name"]))
            for col in range(3):
                table.item(row, col).setFlags(table.item(row, col).flags() & ~Qt.ItemIsEditable)
            set_row_id(table, row, tpl["id"])
        auto_size_columns(table)

    def _add_template(self):
        if self.current_workflow_id is None:
            return
        other_workflows = [w for w in self.db.list_workflows() if w["id"] != self.current_workflow_id]
        if not self.db.list_workflow_tasks(self.current_workflow_id):
            QMessageBox.information(self, "タスク未登録", "先にこのワークフローにタスクを1つ以上追加してください。")
            return
        if not other_workflows:
            QMessageBox.information(self, "依存先ワークフローがありません", "他のワークフローを先に作成してください。")
            return
        dialog = DependencyTemplateDialog(self.db, self.current_workflow_id, self)
        if dialog.exec() != QDialog.Accepted:
            return
        task_id, target_workflow_id, target_task_id = dialog.values()
        if None in (task_id, target_workflow_id, target_task_id):
            QMessageBox.warning(self, "入力エラー", "すべての項目を選択してください。")
            return
        try:
            self.db.add_dependency_template(
                self.current_workflow_id, task_id, target_workflow_id, target_task_id
            )
        except ProjectDatabaseError as e:
            QMessageBox.warning(self, "追加できません", str(e))
            return
        self.refresh_templates()

    def _edit_template(self, row):
        template_id = row_id(self.template_section.table, row)
        current = next(
            t for t in self.db.list_dependency_templates(self.current_workflow_id)
            if t["id"] == template_id
        )
        dialog = DependencyTemplateDialog(
            self.db, self.current_workflow_id, self,
            initial=(
                current["workflow_task_id"],
                current["depends_on_workflow_id"],
                current["depends_on_workflow_task_id"],
            ),
        )
        if dialog.exec() != QDialog.Accepted:
            return
        task_id, target_workflow_id, target_task_id = dialog.values()
        if None in (task_id, target_workflow_id, target_task_id):
            QMessageBox.warning(self, "入力エラー", "すべての項目を選択してください。")
            return
        try:
            self.db.update_dependency_template(template_id, task_id, target_workflow_id, target_task_id)
        except ProjectDatabaseError as e:
            QMessageBox.warning(self, "変更できません", str(e))
            return
        self.refresh_templates()

    def _delete_template(self, row):
        template_id = row_id(self.template_section.table, row)
        self.db.delete_dependency_template(template_id)
        self.refresh_templates()

    def _add_workflow(self):
        name, ok = QInputDialog.getText(self, "ワークフローを追加", "ワークフロー名:")
        if not ok or not name.strip():
            return
        try:
            new_id = self.db.add_workflow(name.strip())
        except DuplicateNameError as e:
            QMessageBox.warning(self, "追加できません", str(e))
            return
        self.refresh_workflows(select_id=new_id)

    def _rename_workflow(self):
        item = self.workflow_list.currentItem()
        if item is None:
            return
        wf_id = item.data(Qt.UserRole)
        name, ok = QInputDialog.getText(self, "ワークフロー名を変更", "ワークフロー名:", text=item.text())
        if not ok or not name.strip():
            return
        try:
            self.db.rename_workflow(wf_id, name.strip())
        except DuplicateNameError as e:
            QMessageBox.warning(self, "変更できません", str(e))
            return
        self.refresh_workflows(select_id=wf_id)

    def _delete_workflow(self):
        item = self.workflow_list.currentItem()
        if item is None:
            return
        wf_id = item.data(Qt.UserRole)
        count = self.db.workflow_usage_count(wf_id)
        if not confirm_or_block_delete(self, count, "このワークフロー", hard_block=True):
            return
        try:
            self.db.delete_workflow(wf_id)
        except ReferencedEntityError as e:
            QMessageBox.warning(self, "削除できません", str(e))
            return
        self.refresh_workflows()

    def refresh_team_choices(self):
        """チームマスタ変更時、表示中キャンバスの色・ラベルを再計算する。"""
        if self.current_scene is not None:
            self.current_scene.refresh_colors()

    def refresh_choices(self):
        """MainWindowのタブ切り替え時フックから呼ばれる共通インターフェース。
        選択中のワークフローを維持したまま一覧・キャンバスを最新化する。"""
        current = self.workflow_list.currentItem()
        select_id = current.data(Qt.UserRole) if current else None
        self.refresh_workflows(select_id=select_id)
